"""IMAP operations used by the worker.

Safety properties this module guarantees:
- Reads use EXAMINE (read-only select) plus BODY.PEEK, so no flag ever changes on fetch.
- Moves use UID MOVE, which keeps flags (read state, star) and the internal date.
- Without MOVE, the fallback expunges only the moved UID (UID EXPUNGE); it never runs a plain
  EXPUNGE that could remove other mail flagged \\Deleted by the user.
- Every move is verified by Message-ID in both the source and the destination folder.
"""
from __future__ import annotations

import imaplib
import logging
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from imap_tools import MailBox, MailBoxStartTls, MailBoxUnencrypted
from imap_tools.utils import encode_folder

from mailcat.imap.providers import fallback_names

log = logging.getLogger(__name__)

SPECIAL_USE = {"sent": "\\Sent", "spam": "\\Junk", "trash": "\\Trash", "all": "\\All"}

_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_DATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_MSGID_RE = re.compile(rb"<[^<>\s]+>")


class ImapError(RuntimeError):
    pass


class WriteRefused(ImapError):
    """A write was blocked by MAILCAT_READ_ONLY or by a sandbox session's scope."""


@dataclass
class FetchRecord:
    uid: int
    flags: tuple[str, ...]
    internal_date: datetime | None
    size: int
    raw: bytes | None = None
    message_id: str | None = None

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags

    @property
    def flagged(self) -> bool:
        return "\\Flagged" in self.flags


@dataclass
class FolderInfo:
    name: str
    delim: str
    flags: tuple[str, ...]


def parse_internaldate(value: bytes | str) -> datetime | None:
    text = value.decode() if isinstance(value, bytes) else value
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _uid_set(uids: list[int]) -> str:
    return ",".join(str(u) for u in uids)


def _check(result: tuple, what: str) -> list:
    typ, data = result
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        raise ImapError(f"{what} failed: {typ} {detail}")
    return data


def parse_fetch(data: list) -> list[FetchRecord]:
    """Parse an imaplib UID FETCH response. Items may be (meta, literal) tuples followed by a
    bytes tail such as b' FLAGS (\\Seen))', or plain bytes when no literal was requested."""
    records: list[FetchRecord] = []
    current_meta: bytes | None = None
    current_literal: bytes | None = None

    def flush() -> None:
        if current_meta is None:
            return
        uid_m = _UID_RE.search(current_meta)
        if not uid_m:
            return
        flags_m = _FLAGS_RE.search(current_meta)
        date_m = _DATE_RE.search(current_meta)
        size_m = _SIZE_RE.search(current_meta)
        flags = tuple(f.decode(errors="replace") for f in flags_m.group(1).split()) if flags_m else ()
        record = FetchRecord(
            uid=int(uid_m.group(1)), flags=flags,
            internal_date=parse_internaldate(date_m.group(1)) if date_m else None,
            size=int(size_m.group(1)) if size_m else 0, raw=current_literal,
        )
        records.append(record)

    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            flush()
            current_meta, current_literal = item[0], item[1]
        elif isinstance(item, bytes):
            if current_meta is not None and current_literal is not None and not re.match(rb"^\d+ \(", item):
                current_meta = current_meta + item  # trailing part of the previous message
            else:
                flush()
                current_meta, current_literal = item, None
    flush()
    return records


class ImapSession:
    def __init__(self, host: str, port: int, provider: str, timeout: int = 60,
                 folder_overrides: dict[str, str] | None = None, security: str = "ssl",
                 read_only: bool = False, write_scope: str | None = None) -> None:
        self.host = host
        self.port = port
        self.security = security
        self.provider = provider
        self.timeout = timeout
        self.folder_overrides = folder_overrides or {}
        self.mailbox: MailBox | None = None
        self._folders: list[FolderInfo] | None = None
        self._selected: tuple[str, bool] | None = None
        # read_only refuses every command that changes the mailbox. write_scope (a sandbox root
        # folder) limits writes to that folder tree, plus moving its own mail to and from Trash.
        self.read_only = read_only
        self.write_scope = write_scope

    # -- connection -------------------------------------------------------------------------

    def _mailbox(self) -> MailBox:
        if self.security == "plain":
            return MailBoxUnencrypted(self.host, self.port, timeout=self.timeout)
        if self.security == "starttls":
            return MailBoxStartTls(self.host, self.port, timeout=self.timeout,
                                   ssl_context=ssl.create_default_context())
        return MailBox(self.host, self.port, timeout=self.timeout, ssl_context=ssl.create_default_context())

    def login_password(self, username: str, password: str) -> None:
        self.mailbox = self._mailbox()
        try:
            self.mailbox.login(username, password, initial_folder=None)
        except Exception as exc:
            raise ImapError(f"login failed for {username}@{self.host}: {exc}") from exc

    def login_xoauth2(self, username: str, token: str) -> None:
        self.mailbox = self._mailbox()
        try:
            self.mailbox.xoauth2(username, token, initial_folder=None)
        except Exception as exc:
            raise ImapError(f"OAuth2 login failed for {username}@{self.host}: {exc}") from exc

    def close(self) -> None:
        if self.mailbox is None:
            return
        try:
            self.mailbox.logout()
        except Exception:  # noqa: BLE001 - best effort on a dying connection
            pass
        self.mailbox = None

    def __enter__(self) -> ImapSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def client(self) -> imaplib.IMAP4:
        if self.mailbox is None:
            raise ImapError("not connected")
        return self.mailbox.client

    @property
    def capabilities(self) -> set[str]:
        return {c.upper() for c in self.client.capabilities}

    @property
    def is_gmail(self) -> bool:
        return self.provider == "gmail" or "X-GM-EXT-1" in self.capabilities

    # -- folders ----------------------------------------------------------------------------

    def folders(self, refresh: bool = False) -> list[FolderInfo]:
        if self._folders is None or refresh:
            assert self.mailbox is not None
            self._folders = [FolderInfo(f.name, f.delim or "/", tuple(f.flags))
                             for f in self.mailbox.folder.list()]
        return self._folders

    @property
    def delimiter(self) -> str:
        for folder in self.folders():
            if folder.delim:
                return folder.delim
        return "/"

    def folder_exists(self, name: str) -> bool:
        return any(f.name == name for f in self.folders())

    def resolve_role(self, role: str) -> str | None:
        if role == "inbox":
            return "INBOX"
        override = self.folder_overrides.get(role)
        if override:
            return override if self.folder_exists(override) else None
        flag = SPECIAL_USE.get(role)
        if flag:
            for folder in self.folders():
                if any(f.lower() == flag.lower() for f in folder.flags):
                    return folder.name
        names = {f.name.lower(): f.name for f in self.folders()}
        for candidate in fallback_names(self.provider, role):
            if candidate.lower() in names:
                return names[candidate.lower()]
        return None

    def category_folder(self, parent: str, category_name: str) -> str:
        delim = self.delimiter
        safe = category_name.replace(delim, "-").strip() or "Unnamed"
        return f"{parent}{delim}{safe}" if parent else safe

    # -- write guards -----------------------------------------------------------------------

    def in_scope(self, folder: str) -> bool:
        if self.write_scope is None:
            return True
        return folder == self.write_scope or folder.startswith(self.write_scope + self.delimiter)

    def _guard_write(self, *folders: str, trash_ok: bool = False) -> None:
        if self.read_only:
            raise WriteRefused("MAILCAT_READ_ONLY is on; the mailbox is never changed")
        if self.write_scope is None:
            return
        trash = self.resolve_role("trash") if trash_ok else None
        for folder in folders:
            if not self.in_scope(folder) and not (trash and folder == trash):
                raise WriteRefused(f"sandbox session may only write inside {self.write_scope}; refused {folder}")

    def ensure_folder(self, name: str) -> bool:
        """Create the folder (and its parent) if missing. Returns True when something was created."""
        if self.folder_exists(name):
            return False
        self._guard_write(name)
        assert self.mailbox is not None
        delim = self.delimiter
        created = False
        parts = name.split(delim)
        for depth in range(1, len(parts) + 1):
            path = delim.join(parts[:depth])
            if not self.folder_exists(path):
                try:
                    self.mailbox.folder.create(path)
                    created = True
                except Exception as exc:  # noqa: BLE001
                    # A server that auto-creates parents may report the parent as existing.
                    if depth == len(parts):
                        raise ImapError(f"cannot create folder {path}: {exc}") from exc
                self.folders(refresh=True)
        return created

    def rename_folder(self, old: str, new: str) -> None:
        self._guard_write(old, new)
        assert self.mailbox is not None
        try:
            self.mailbox.folder.rename(old, new)
        except Exception as exc:  # noqa: BLE001
            raise ImapError(f"cannot rename folder {old} to {new}: {exc}") from exc
        self.folders(refresh=True)

    def select(self, folder: str, readonly: bool = True) -> tuple[int, int]:
        """Select (or EXAMINE when readonly). Returns (uidvalidity, message_count)."""
        if not readonly:
            self._guard_write(folder, trash_ok=True)
        typ, data = self.client.select(encode_folder(folder), readonly)
        if typ != "OK":
            raise ImapError(f"cannot select {folder}: {data}")
        self._selected = (folder, readonly)
        count = int(data[0]) if data and data[0] and data[0].isdigit() else 0
        uv = self.client.untagged_responses.get("UIDVALIDITY", [b"0"])
        uidvalidity = int(uv[-1]) if uv and uv[-1] else 0
        return uidvalidity, count

    def uidvalidity(self, folder: str) -> int | None:
        assert self.mailbox is not None
        try:
            return self.mailbox.folder.status(folder, ["UIDVALIDITY"]).get("UIDVALIDITY")
        except Exception:  # noqa: BLE001
            return None

    def _ensure_selected(self, folder: str, readonly: bool) -> None:
        if self._selected is None or self._selected[0] != folder or (self._selected[1] and not readonly):
            self.select(folder, readonly)

    # -- reads ------------------------------------------------------------------------------

    def uids(self, folder: str, criteria: str = "ALL") -> list[int]:
        self._ensure_selected(folder, readonly=True)
        data = _check(self.client.uid("SEARCH", None, criteria), f"UID SEARCH {criteria}")
        return sorted(int(x) for x in (data[0] or b"").split())

    def uids_since(self, folder: str, since: datetime) -> list[int]:
        return self.uids(folder, f"SINCE {since.strftime('%d-%b-%Y')}")

    def fetch_flags(self, folder: str) -> dict[int, tuple[str, ...]]:
        self._ensure_selected(folder, readonly=True)
        typ, data = self.client.uid("FETCH", "1:*", "(UID FLAGS)")
        if typ != "OK":
            raise ImapError(f"flag fetch failed in {folder}")
        return {r.uid: r.flags for r in parse_fetch(data or [])}

    def fetch_meta(self, folder: str, uids: list[int], chunk: int = 200) -> list[FetchRecord]:
        """UID, flags, internal date, size and Message-ID, without the body."""
        self._ensure_selected(folder, readonly=True)
        records: list[FetchRecord] = []
        for start in range(0, len(uids), chunk):
            part = uids[start:start + chunk]
            data = _check(self.client.uid(
                "FETCH", _uid_set(part),
                "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            ), "meta fetch")
            for record in parse_fetch(data):
                ids = _MSGID_RE.findall(record.raw or b"")
                record.message_id = ids[0].decode(errors="replace") if ids else None
                record.raw = None
                records.append(record)
        return records

    def fetch_full(self, folder: str, uids: list[int], chunk: int = 20) -> Iterator[FetchRecord]:
        self._ensure_selected(folder, readonly=True)
        for start in range(0, len(uids), chunk):
            part = uids[start:start + chunk]
            data = _check(self.client.uid(
                "FETCH", _uid_set(part), "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
            ), "full fetch")
            yield from parse_fetch(data)

    def search_message_id(self, folder: str, message_id: str) -> list[int]:
        self._ensure_selected(folder, readonly=True)
        found: set[int] = set()
        typ, data = self.client.uid("SEARCH", None, "HEADER", "Message-ID", _quote(message_id))
        if typ == "OK" and data and data[0]:
            found.update(int(x) for x in data[0].split())
        if not found and self.is_gmail:
            raw_id = message_id.strip("<>")
            typ, data = self.client.uid("SEARCH", None, "X-GM-RAW", _quote(f"rfc822msgid:{raw_id}"))
            if typ == "OK" and data and data[0]:
                found.update(int(x) for x in data[0].split())
        return sorted(found)

    # -- writes -----------------------------------------------------------------------------

    def move(self, source: str, uid: int, destination: str) -> int | None:
        """Move one message. Returns its UID in the destination when the server reports it."""
        self._guard_write(source, destination, trash_ok=True)
        if self.write_scope is not None and not (self.in_scope(source) or self.in_scope(destination)):
            raise WriteRefused(f"sandbox session refused a move outside {self.write_scope}")
        self._ensure_selected(source, readonly=False)
        caps = self.capabilities
        if "MOVE" in caps:
            typ, data = self.client.uid("MOVE", str(uid), encode_folder(destination))
            if typ != "OK":
                raise ImapError(f"MOVE {uid} {source} -> {destination} failed: {data}")
            return self._copyuid(data)
        typ, data = self.client.uid("COPY", str(uid), encode_folder(destination))
        if typ != "OK":
            raise ImapError(f"COPY {uid} {source} -> {destination} failed: {data}")
        new_uid = self._copyuid(data)
        _check(self.client.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)"), "STORE \\Deleted")
        if "UIDPLUS" in caps:
            _check(self.client.uid("EXPUNGE", str(uid)), "UID EXPUNGE")
        else:
            # A plain EXPUNGE would also remove mail the user flagged \Deleted. Only expunge when
            # ours is the only such message; otherwise undo the flag and report the copy.
            deleted = self._deleted_uids()
            if deleted == [uid]:
                _check(self.client.expunge(), "EXPUNGE")
            else:
                self.client.uid("STORE", str(uid), "-FLAGS.SILENT", "(\\Deleted)")
                raise ImapError("server lacks MOVE and UIDPLUS and other mail is flagged \\Deleted; "
                                "the message was copied but not removed from the source")
        return new_uid

    def _deleted_uids(self) -> list[int]:
        data = _check(self.client.uid("SEARCH", None, "DELETED"), "UID SEARCH DELETED")
        return sorted(int(x) for x in (data[0] or b"").split())

    def _copyuid(self, data: list) -> int | None:
        """UIDPLUS servers report the new UID in an untagged `OK [COPYUID uv src dst]`; imaplib
        files it under "COPYUID" as b"uv src dst". Callers verify by Message-ID regardless."""
        for blob in self.client.untagged_responses.pop("COPYUID", []) or []:
            if not isinstance(blob, bytes):
                continue
            parts = blob.split()
            if len(parts) >= 3 and parts[2].isdigit():
                return int(parts[2])
        return None

    def append(self, folder: str, raw: bytes, flags: str = "", date: datetime | None = None) -> None:
        """Used by the provider acceptance test to seed a throwaway mailbox."""
        self._guard_write(folder)
        when = imaplib.Time2Internaldate(date.timestamp()) if date else None
        _check(self.client.append(encode_folder(folder), flags or None, when, raw), "APPEND")
        self._selected = None

    def set_flag(self, folder: str, uid: int, flag: str, value: bool) -> None:
        """Only used by tests to prepare mailboxes; the worker never changes flags."""
        self._guard_write(folder)
        self._ensure_selected(folder, readonly=False)
        _check(self.client.uid("STORE", str(uid), ("+" if value else "-") + "FLAGS.SILENT", f"({flag})"),
               "STORE")

    def delete_folder(self, name: str) -> None:
        """Used only to remove the provider test's own sandbox folders."""
        self._guard_write(name)
        assert self.mailbox is not None
        if self._selected and self._selected[0] == name:
            # Some servers misbehave when the selected folder is deleted; look at INBOX instead.
            self.select("INBOX", readonly=True)
        try:
            self.mailbox.folder.delete(name)
        except Exception as exc:  # noqa: BLE001
            raise ImapError(f"cannot delete folder {name}: {exc}") from exc
        self.folders(refresh=True)

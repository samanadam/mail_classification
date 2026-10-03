"""Inert attachment storage and risky-type detection.

Files are written under random names with no extension, mode 0600, on a volume mounted `noexec`.
Nothing here ever opens, unpacks or renders an attachment.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path, PurePosixPath

from mailcat.config import get_config

RISKY_EXTENSIONS = {
    # executables and scripts
    "exe", "com", "scr", "pif", "bat", "cmd", "msi", "msp", "dll", "cpl", "jar", "js", "jse", "vbs",
    "vbe", "wsf", "wsh", "ps1", "psm1", "sh", "bash", "py", "app", "apk", "deb", "rpm", "lnk", "reg",
    "hta", "gadget", "msc", "iso", "img", "vhd", "vhdx", "dmg", "pkg",
    # active documents
    "html", "htm", "xhtml", "svg", "shtml", "mht", "mhtml", "url", "website", "chm",
    # macro-enabled office
    "docm", "dotm", "xlsm", "xltm", "xlam", "pptm", "potm", "ppam", "ppsm", "sldm", "one", "xll",
    # archives (may hide any of the above)
    "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "cab", "arj", "lzh", "ace", "z",
}
RISKY_CONTENT_TYPES = (
    "application/x-msdownload", "application/x-dosexec", "application/x-executable",
    "application/javascript", "text/javascript", "text/html", "application/x-sh",
    "application/vnd.ms-excel.sheet.macroenabled", "application/vnd.ms-word.document.macroenabled",
    "application/zip", "application/x-rar", "application/x-7z-compressed", "application/java-archive",
    "image/svg+xml", "application/hta",
)
_MAGIC = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf", b"#!")


def extensions(filename: str) -> list[str]:
    name = PurePosixPath(filename.replace("\\", "/")).name.lower()
    return [part for part in name.split(".")[1:] if part]


def is_risky(filename: str, content_type: str, payload: bytes) -> bool:
    exts = extensions(filename)
    if any(ext in RISKY_EXTENSIONS for ext in exts):
        return True
    ctype = (content_type or "").lower()
    if any(ctype.startswith(t) for t in RISKY_CONTENT_TYPES):
        return True
    return payload[:8].startswith(_MAGIC)


def attachment_dir() -> Path:
    path = get_config().attachments_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_bytes(payload: bytes) -> tuple[str, str]:
    """Write payload under a fresh random name. Returns (stored_name, sha256)."""
    stored_name = secrets.token_hex(24)
    path = attachment_dir() / stored_name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return stored_name, hashlib.sha256(payload).hexdigest()


def path_for(stored_name: str) -> Path:
    if not stored_name or "/" in stored_name or "\\" in stored_name or stored_name.startswith("."):
        raise ValueError("invalid stored attachment name")
    return attachment_dir() / stored_name


def remove(stored_name: str) -> None:
    try:
        path_for(stored_name).unlink()
    except FileNotFoundError:
        pass

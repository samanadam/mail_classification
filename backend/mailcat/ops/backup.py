"""Encrypted nightly backups of Postgres and the attachment store, kept `backup_keep_days`.

Layout: <backup_dir>/<UTC timestamp>/{db.dump.gpg, attachments.tar.gpg, manifest.json}
Encryption: GnuPG symmetric AES-256, passphrase from MAILCAT_BACKUP_PASSPHRASE.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailcat.config import get_config

log = logging.getLogger(__name__)

STAMP = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    pass


def _libpq_url() -> str:
    url = get_config().database_url
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def _passphrase() -> str:
    phrase = get_config().backup_passphrase
    if len(phrase) < 16:
        raise BackupError("MAILCAT_BACKUP_PASSPHRASE must be set (at least 16 characters); "
                          "backups are never written unencrypted")
    return phrase


def _pass_fd() -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (_passphrase() + "\n").encode())
    os.close(write_fd)
    return read_fd


_GPG_HOME: str | None = None


def _gpg_home() -> str:
    """Symmetric encryption needs no keyring, but gpg still wants a home directory. The containers'
    root filesystem is read-only, so use a private one under /tmp."""
    global _GPG_HOME
    if _GPG_HOME is None or not os.path.isdir(_GPG_HOME):
        _GPG_HOME = tempfile.mkdtemp(prefix="mailcat-gnupg-")
        os.chmod(_GPG_HOME, 0o700)
    return _GPG_HOME


def _gpg(args: list[str], pass_fd: int, **kwargs) -> subprocess.Popen:
    cmd = ["gpg", "--homedir", _gpg_home(), "--batch", "--yes", "--quiet", "--no-tty", "--pinentry-mode",
           "loopback", "--passphrase-fd", str(pass_fd), *args]
    return subprocess.Popen(cmd, pass_fds=(pass_fd,), **kwargs)  # noqa: S603


def _encrypt_cmd(out: Path) -> list[str]:
    return ["--symmetric", "--cipher-algo", "AES256", "--compress-algo", "zlib", "-o", str(out)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _wait(proc: subprocess.Popen, what: str) -> None:
    code = proc.wait()
    if code != 0:
        stderr = proc.stderr.read().decode(errors="replace")[-500:] if proc.stderr else ""
        raise BackupError(f"{what} failed with exit code {code}: {stderr}")


def create_backup() -> Path:
    cfg = get_config()
    _passphrase()
    target = cfg.backup_dir / datetime.now(UTC).strftime(STAMP)
    tmp = target.with_name(target.name + ".partial")
    tmp.mkdir(parents=True, exist_ok=False)
    os.chmod(tmp, 0o700)
    try:
        db_out = tmp / "db.dump.gpg"
        pass_fd = _pass_fd()
        try:
            dump = subprocess.Popen(  # noqa: S603
                ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--dbname={_libpq_url()}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            gpg = _gpg(_encrypt_cmd(db_out), pass_fd, stdin=dump.stdout, stderr=subprocess.PIPE)
            assert dump.stdout is not None
            dump.stdout.close()
            _wait(gpg, "gpg (database)")
            _wait(dump, "pg_dump")
        finally:
            os.close(pass_fd)

        att_out = tmp / "attachments.tar.gpg"
        pass_fd = _pass_fd()
        try:
            gpg = _gpg(_encrypt_cmd(att_out), pass_fd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            assert gpg.stdin is not None
            with tarfile.open(fileobj=gpg.stdin, mode="w|") as tar:
                att_dir = cfg.attachments_path
                if att_dir.exists():
                    tar.add(att_dir, arcname="attachments")
            gpg.stdin.close()
            _wait(gpg, "gpg (attachments)")
        finally:
            os.close(pass_fd)

        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "files": {p.name: {"size": p.stat().st_size, "sha256": _sha256(p)} for p in (db_out, att_out)},
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for item in tmp.iterdir():
            os.chmod(item, 0o600)
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("backup written to %s", target)
    return target


def list_backups() -> list[Path]:
    root = get_config().backup_dir
    if not root.exists():
        return []
    found = []
    for path in root.iterdir():
        if path.is_dir() and (path / "manifest.json").exists():
            try:
                datetime.strptime(path.name, STAMP)
            except ValueError:
                continue
            found.append(path)
    return sorted(found)


def last_backup_time() -> datetime | None:
    backups = list_backups()
    if not backups:
        return None
    return datetime.strptime(backups[-1].name, STAMP).replace(tzinfo=UTC)


def prune(keep_days: int) -> list[Path]:
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = []
    backups = list_backups()
    for path in backups[:-1]:  # never remove the newest backup
        if datetime.strptime(path.name, STAMP).replace(tzinfo=UTC) < cutoff:
            shutil.rmtree(path)
            removed.append(path)
    root = get_config().backup_dir
    if root.exists():
        for partial in root.glob("*.partial"):
            shutil.rmtree(partial, ignore_errors=True)
    return removed


def _decrypt(src: Path, **kwargs) -> tuple[subprocess.Popen, int]:
    pass_fd = _pass_fd()
    return _gpg(["--decrypt", str(src)], pass_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs), pass_fd


def verify_backup(path: Path) -> dict:
    if not (path / "manifest.json").is_file():
        raise BackupError(f"{path} is not a backup directory (no manifest.json)")
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        if _sha256(path / name) != meta["sha256"]:
            raise BackupError(f"{name}: checksum mismatch")
    gpg, fd = _decrypt(path / "db.dump.gpg")
    try:
        listing = subprocess.run(["pg_restore", "--list"], stdin=gpg.stdout,  # noqa: S603
                                 capture_output=True, check=False)
        assert gpg.stdout is not None
        # pg_restore --list stops after the table of contents; drain the rest so gpg
        # can finish (and check the integrity of the whole file) instead of hitting EPIPE.
        while gpg.stdout.read(1 << 20):
            pass
        gpg.stdout.close()
        _wait(gpg, "gpg decrypt (database)")
    finally:
        os.close(fd)
    if listing.returncode != 0:
        raise BackupError(f"pg_restore cannot read the dump: {listing.stderr.decode(errors='replace')[-300:]}")
    gpg, fd = _decrypt(path / "attachments.tar.gpg")
    try:
        with tarfile.open(fileobj=gpg.stdout, mode="r|") as tar:
            count = sum(1 for m in tar if m.isfile())
        _wait(gpg, "gpg decrypt (attachments)")
    finally:
        os.close(fd)
    return {"ok": True, "tables_listed": listing.stdout.count(b"TABLE DATA"), "attachments": count}


def restore_backup(path: Path) -> None:
    """Restores the database (dropping current objects) and the attachment files."""
    verify_backup(path)
    gpg, fd = _decrypt(path / "db.dump.gpg")
    try:
        restore = subprocess.run(  # noqa: S603
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--single-transaction",
             f"--dbname={_libpq_url()}"], stdin=gpg.stdout, capture_output=True, check=False)
        assert gpg.stdout is not None
        gpg.stdout.close()
        _wait(gpg, "gpg decrypt (database)")
    finally:
        os.close(fd)
    if restore.returncode != 0:
        raise BackupError(f"pg_restore failed: {restore.stderr.decode(errors='replace')[-500:]}")
    att_dir = get_config().attachments_path
    gpg, fd = _decrypt(path / "attachments.tar.gpg")
    try:
        with tarfile.open(fileobj=gpg.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if member.name != f"attachments/{name}" or name.startswith("."):
                    continue  # only flat files from our own layout
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                dest = att_dir / name
                with dest.open("wb") as out:
                    shutil.copyfileobj(handle, out)
                os.chmod(dest, 0o600)
        _wait(gpg, "gpg decrypt (attachments)")
    finally:
        os.close(fd)

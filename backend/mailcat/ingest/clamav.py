"""ClamAV scanning over the clamd INSTREAM protocol.

In "managed" mode the worker starts clamd only for the duration of a run (it needs ~1 GB RAM
while loaded) and stops it afterwards, matching the plan's "runs during the run".
"""
from __future__ import annotations

import logging
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from mailcat.config import get_config

log = logging.getLogger(__name__)

CHUNK = 64 * 1024


@dataclass
class ScanResult:
    status: str  # clean | infected | error | skipped
    detail: str | None = None


class ClamdClient:
    def __init__(self, host: str, port: int, timeout: float = 120.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        return socket.create_connection((self.host, self.port), timeout=self.timeout)

    def ping(self) -> bool:
        try:
            with self._connect() as sock:
                sock.sendall(b"zPING\0")
                return sock.recv(64).strip(b"\0\n ") == b"PONG"
        except OSError:
            return False

    def scan_bytes(self, payload: bytes) -> ScanResult:
        try:
            with self._connect() as sock:
                sock.sendall(b"zINSTREAM\0")
                for start in range(0, len(payload), CHUNK):
                    chunk = payload[start:start + CHUNK]
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
                sock.sendall(struct.pack("!L", 0))
                reply = b""
                while not reply.endswith(b"\0"):
                    data = sock.recv(4096)
                    if not data:
                        break
                    reply += data
        except OSError as exc:
            return ScanResult("error", f"clamd unreachable: {exc}")
        text = reply.strip(b"\0\n ").decode(errors="replace")
        if text.endswith("OK"):
            return ScanResult("clean")
        if text.endswith("FOUND"):
            signature = text.split(":", 1)[-1].rsplit(" ", 1)[0].strip()
            return ScanResult("infected", signature)
        return ScanResult("error", text or "empty reply from clamd")

    def scan_file(self, path: Path) -> ScanResult:
        return self.scan_bytes(path.read_bytes())


class Scanner:
    """Context manager owning clamd for the run (managed mode) or wrapping an external one."""

    def __init__(self) -> None:
        cfg = get_config()
        self.mode = cfg.clamav_mode
        self.client = ClamdClient(cfg.clamd_host, cfg.clamd_port)
        self._proc: subprocess.Popen[bytes] | None = None
        self.available = False

    def __enter__(self) -> Scanner:
        if self.mode == "off":
            return self
        if self.mode == "managed" and not self.client.ping():
            cfg = get_config()
            try:
                self._proc = subprocess.Popen(  # noqa: S603 - fixed binary and arguments
                    ["clamd", "--foreground=yes", f"--config-file={cfg.clamd_config}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                log.error("clamd binary not found; attachments will be marked as scan errors")
                return self
        # Loading signatures takes 20-90 s on a laptop CPU.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if self.client.ping():
                self.available = True
                break
            if self._proc and self._proc.poll() is not None:
                log.error("clamd exited during start-up with code %s", self._proc.returncode)
                break
            time.sleep(2)
        if not self.available:
            log.error("clamd is not reachable; attachments will be marked as scan errors")
        return self

    def scan(self, payload: bytes) -> ScanResult:
        if self.mode == "off":
            return ScanResult("skipped", "scanning disabled")
        if not self.available:
            return ScanResult("error", "clamd not available")
        return self.client.scan_bytes(payload)

    def __exit__(self, *exc: object) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def update_signatures() -> bool:
    """Refresh ClamAV signatures. Returns False if freshclam failed (old signatures stay)."""
    if get_config().clamav_mode != "managed":
        return True
    (get_config().data_dir / "clamav").mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603
            ["freshclam", "--quiet", "--stdout"], capture_output=True, timeout=600, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("freshclam failed: %s", exc)
        return False
    if result.returncode not in (0, 1):  # 1 = already up to date on some versions
        log.warning("freshclam exited %s: %s", result.returncode, result.stdout[-500:])
        return False
    return True

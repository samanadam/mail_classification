"""Process configuration from the environment.

Only values that must exist before the database is reachable, or that are secrets, live here.
Everything a user tunes (timers, thresholds, categories, run time) lives in the database and is
edited in the dashboard; see `mailcat.settings_store`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAILCAT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mailcat:mailcat@localhost:5432/mailcat"

    # Fernet key (urlsafe base64, 32 bytes) used to encrypt mailbox credentials at rest.
    # Generate with: mailcat gen-key
    secret_key: str = Field(default="", repr=False)

    data_dir: Path = Path("/data")
    attachment_dir: Path | None = None
    blocklist_dir: Path | None = None
    model_cache_dir: Path | None = None
    backup_dir: Path = Path("/backup")
    backup_passphrase: str = Field(default="", repr=False)

    discord_webhook_url: str = Field(default="", repr=False)

    # Classifier backend: laya | laya_onnx | ollama | rules_only
    classifier: str = "laya"
    laya_model: str = "convaiinnovations/laya"
    laya_subfolder: str = "multilingual"
    laya_revision: str | None = None
    laya_onnx_path: Path = Path("/data/models/laya.onnx")
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:3b"

    # ClamAV: "managed" starts clamd for the duration of a run, "tcp" uses an existing daemon,
    # "off" skips scanning (attachments are then marked `skipped`, never `clean`).
    clamav_mode: str = "managed"
    clamd_host: str = "127.0.0.1"
    clamd_port: int = 3310
    clamd_config: Path = Path("/etc/clamav/clamd.conf")

    # Dashboard session cookie. Secure must stay on unless the dashboard is served over plain
    # HTTP inside the tailnet (Tailscale already encrypts the transport).
    cookie_secure: bool = False
    session_hours: int = 24 * 7
    static_dir: Path | None = None

    log_level: str = "INFO"
    imap_timeout: int = 60
    # Hard switch: refuse every IMAP command that changes a mailbox, whatever the dashboard says.
    read_only: bool = False

    @property
    def attachments_path(self) -> Path:
        return self.attachment_dir or self.data_dir / "attachments"

    @property
    def blocklists_path(self) -> Path:
        return self.blocklist_dir or self.data_dir / "blocklists"

    @property
    def models_path(self) -> Path:
        return self.model_cache_dir or self.data_dir / "models"


@lru_cache
def get_config() -> Config:
    return Config()

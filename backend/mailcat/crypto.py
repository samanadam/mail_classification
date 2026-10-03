"""Encryption of mailbox credentials at rest (Fernet: AES-128-CBC + HMAC-SHA256)."""
from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from mailcat.config import get_config


class CredentialError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = get_config().secret_key
    if not key:
        raise CredentialError("MAILCAT_SECRET_KEY is not set; generate one with `mailcat gen-key`")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CredentialError("MAILCAT_SECRET_KEY is not a valid Fernet key") from exc


def encrypt_credentials(data: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode())


def decrypt_credentials(blob: bytes) -> dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(bytes(blob)))
    except InvalidToken as exc:
        raise CredentialError("credentials cannot be decrypted with the current secret key") from exc

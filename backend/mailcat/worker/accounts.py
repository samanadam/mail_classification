"""Open an IMAP session for an account, refreshing OAuth tokens when needed."""
from __future__ import annotations

from sqlalchemy.orm import Session

from mailcat.config import get_config
from mailcat.crypto import decrypt_credentials, encrypt_credentials
from mailcat.imap import oauth
from mailcat.imap.client import ImapSession
from mailcat.models import Account


def open_session(session: Session, account: Account) -> ImapSession:
    creds = decrypt_credentials(account.credentials_enc)
    imap = ImapSession(account.host, account.port, account.provider, timeout=get_config().imap_timeout,
                       folder_overrides=account.folder_overrides or {}, security=account.security)
    if account.auth_method == "oauth2":
        token, updated = oauth.access_token(creds)
        if updated != creds:
            account.credentials_enc = encrypt_credentials(updated)
            session.commit()
        imap.login_xoauth2(account.username, token)
    else:
        password = creds.get("password")
        if not password:
            raise ValueError(f"account {account.name} has no password stored")
        imap.login_password(account.username, password)
    return imap

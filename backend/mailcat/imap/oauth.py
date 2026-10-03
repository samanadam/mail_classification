"""OAuth2 for Outlook / Microsoft 365 IMAP (XOAUTH2).

Setup is a one-time device-code login (`mailcat account oauth <name>`), which stores a refresh
token encrypted. Each run exchanges it for an access token and stores the rotated refresh token.
"""
from __future__ import annotations

from typing import Any

import msal

SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]


class OAuthError(RuntimeError):
    pass


def _app(creds: dict[str, Any]) -> msal.PublicClientApplication:
    client_id = creds.get("client_id")
    if not client_id:
        raise OAuthError("OAuth account has no client_id")
    tenant = creds.get("tenant") or "common"
    return msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}")


def device_code_login(creds: dict[str, Any], prompt: Any = print) -> dict[str, Any]:
    app = _app(creds)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OAuthError(f"device flow failed: {flow.get('error_description', flow)}")
    prompt(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "refresh_token" not in result:
        raise OAuthError(f"login failed: {result.get('error_description', result.get('error'))}")
    return {**creds, "refresh_token": result["refresh_token"]}


def access_token(creds: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Returns (access_token, updated_credentials). Persist the updated credentials: Microsoft
    rotates refresh tokens."""
    refresh = creds.get("refresh_token")
    if not refresh:
        raise OAuthError("no refresh token stored; run `mailcat account oauth <name>`")
    result = _app(creds).acquire_token_by_refresh_token(refresh, scopes=SCOPES)
    if "access_token" not in result:
        raise OAuthError(f"token refresh failed: {result.get('error_description', result.get('error'))}")
    updated = dict(creds)
    if result.get("refresh_token"):
        updated["refresh_token"] = result["refresh_token"]
    return result["access_token"], updated

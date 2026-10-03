"""Provider defaults and folder-name fallbacks for servers without SPECIAL-USE."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    host: str
    port: int = 993
    auth_method: str = "password"
    notes: str = ""
    fallback_names: dict[str, tuple[str, ...]] = field(default_factory=dict)


_COMMON = {
    "sent": ("Sent", "Sent Items", "Sent Messages", "INBOX.Sent", "INBOX/Sent", "Gönderilmiş Öğeler",
             "Gönderilenler", "Отправленные"),
    "spam": ("Junk", "Spam", "Junk E-mail", "Junk Email", "INBOX.Junk", "INBOX.spam", "Bulk Mail",
             "Önemsiz E-posta", "Istenmeyen", "Спам"),
    "trash": ("Trash", "Deleted Items", "Deleted Messages", "Deleted", "INBOX.Trash", "Bin",
              "Silinmiş Öğeler", "Çöp", "Çöp Kutusu", "Удаленные"),
    "all": (),
}

PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        "gmail", "Gmail", "imap.gmail.com",
        notes="Needs 2-step verification and an app password. Labels act as folders.",
        fallback_names={
            "sent": ("[Gmail]/Sent Mail", "[Gmail]/Gönderilmiş Postalar"),
            "spam": ("[Gmail]/Spam",),
            "trash": ("[Gmail]/Trash", "[Gmail]/Bin", "[Gmail]/Çöp kutusu", "[Gmail]/Çöp Kutusu"),
            "all": ("[Gmail]/All Mail", "[Gmail]/Tüm Postalar"),
        },
    ),
    "icloud": Provider("icloud", "iCloud", "imap.mail.me.com",
                       notes="Use an app-specific password from appleid.apple.com.",
                       fallback_names={"sent": ("Sent Messages",), "spam": ("Junk",),
                                       "trash": ("Deleted Messages",)}),
    "yandex": Provider("yandex", "Yandex", "imap.yandex.com",
                       notes="Enable IMAP in Yandex Mail settings and create an app password."),
    "outlook": Provider("outlook", "Outlook / Microsoft 365", "outlook.office365.com", auth_method="oauth2",
                        notes="OAuth2 through an Azure app registration; run `mailcat account oauth`.",
                        fallback_names={"sent": ("Sent Items",), "spam": ("Junk Email",),
                                        "trash": ("Deleted Items",)}),
    "custom": Provider("custom", "Custom domain (IMAP)", "",
                       notes="Normal IMAP login with the host's settings."),
}


def fallback_names(provider: str, role: str) -> tuple[str, ...]:
    specific = PROVIDERS.get(provider, PROVIDERS["custom"]).fallback_names.get(role, ())
    return tuple(specific) + _COMMON.get(role, ())

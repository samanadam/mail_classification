"""Safe HTML view of a mail.

The output contains no scripts, no forms, no remote resources (images, fonts, CSS) and no
clickable links: every link becomes inert text that shows its real destination. The dashboard
additionally puts it in a sandboxed iframe with a restrictive CSP.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import nh3
from bs4 import BeautifulSoup

ALLOWED_TAGS = {
    "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "dd", "del",
    "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins", "li", "mark",
    "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
ALLOWED_ATTRIBUTES = {
    "*": {"align", "valign", "dir", "lang", "title", "class"},
    "td": {"colspan", "rowspan", "width"},
    "th": {"colspan", "rowspan", "width"},
    "table": {"width", "cellpadding", "cellspacing", "border"},
    "font": {"color", "size"},
    "span": {"data-href", "data-host"},
}
_DROP = ("script", "style", "head", "title", "meta", "link", "base", "iframe", "frame", "frameset",
         "object", "embed", "applet", "form", "input", "button", "select", "textarea", "svg", "math",
         "video", "audio", "source", "track", "picture", "noscript", "template")


def safe_html(html: str) -> tuple[str, int]:
    """Returns (sanitized html, number of blocked remote images)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_DROP)):
        tag.decompose()
    blocked = 0
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        alt = str(img.get("alt") or "").strip()
        if src.lower().startswith(("http:", "https:", "//")):
            blocked += 1
        placeholder = soup.new_tag("span")
        placeholder["class"] = "mc-img-blocked"
        placeholder.string = f"[image{': ' + alt[:80] if alt else ''}]"
        img.replace_with(placeholder)
    for a in soup.find_all("a"):
        href = str(a.get("href") or "").strip()
        span = soup.new_tag("span")
        span["class"] = "mc-link"
        if href and not href.lower().startswith(("mailto:", "#", "cid:")):
            host = (urlsplit(href if "://" in href else "http://" + href).hostname or "") \
                if not href.lower().startswith(("javascript:", "data:")) else "blocked"
            span["data-href"] = href[:2000]
            span["data-host"] = host
            span["title"] = f"Goes to: {href[:500]}"
        span.extend(list(a.contents))
        if href:
            marker = soup.new_tag("span")
            marker["class"] = "mc-link-dest"
            marker.string = f" [{(span.get('data-host') or href)[:80]}]"
            span.append(marker)
        a.replace_with(span)
    cleaned = nh3.clean(
        str(soup), tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, url_schemes=set(),
        strip_comments=True, link_rel=None,
    )
    return cleaned, blocked

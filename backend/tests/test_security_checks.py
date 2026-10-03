from mailcat.api.render import safe_html
from mailcat.security.blocklists import Blocklist, normalize_url
from mailcat.security.headers import check_headers, parse_auth_results
from mailcat.security.links import check_link, lookalike_of


def test_lookalike_domains():
    assert lookalike_of("paypa1.com") == "paypal.com"
    assert lookalike_of("g00gle.com") == "google.com"
    assert lookalike_of("xn--pypal-4ve.com") == "paypal.com"  # Cyrillic а
    assert lookalike_of("paypal.com.account-verify.xyz") == "paypal.com"
    assert lookalike_of("amazon.de") is None  # same brand label, country site
    assert lookalike_of("google.com.tr") is None
    assert lookalike_of("accounts.google.com") is None
    assert lookalike_of("example.org") is None


def test_link_heuristics():
    _, v = check_link("http://192.168.1.10/login", "", None)
    assert v.verdict == "suspicious" and "raw IP address" in v.reasons
    _, v = check_link("https://bit.ly/abc", "", None)
    assert "URL shortener hides the destination" in v.reasons
    host, v = check_link("https://evil.example.com/x", "https://bank.com.tr", None)
    assert host == "evil.example.com" and any("link text shows" in r for r in v.reasons)
    _, v = check_link("https://www.github.com/a", "github.com", None)
    assert v.verdict == "ok"
    _, v = check_link("javascript:alert(1)", "click", None)
    assert v.verdict == "malicious"
    _, v = check_link("https://xn--80ak6aa92e.com/", "", None)
    assert "punycode domain" in v.reasons


def test_blocklist_matching():
    bl = Blocklist()
    bl.add("urlhaus", "http://bad.example.com/payload.exe")
    bl.add("openphish", "https://phish.example.net/login/")
    assert normalize_url("HTTP://Bad.Example.com/payload.exe#frag") == "http://bad.example.com/payload.exe"
    _, v = check_link("http://bad.example.com/payload.exe", "", bl)
    assert v.verdict == "malicious"
    _, v = check_link("https://phish.example.net/login?session=1", "", bl)
    assert v.verdict == "malicious"  # same path, tracking query ignored
    _, v = check_link("https://phish.example.net/other", "", bl)
    assert v.verdict == "suspicious"  # phishing host, different path
    _, v = check_link("http://bad.example.com/other", "", bl)
    assert v.verdict == "ok"  # malware hosts are often compromised legit sites


def test_auth_results_only_topmost_header_counts():
    assert parse_auth_results("mx; dkim=fail header.d=a; dkim=pass header.d=b; spf=softfail") == {
        "dkim": "pass", "spf": "softfail"}
    verdict = check_headers({"authentication-results": [
        "mx.google.com; spf=fail smtp.mailfrom=x; dkim=none",
        "forged; spf=pass; dkim=pass",
    ]}, "Bank", "info@bank.com.tr")
    assert "SPF fail" in verdict.hard
    assert check_headers({"authentication-results": ["mx; dkim=pass; spf=pass; dmarc=pass"]},
                         "GitHub", "noreply@github.com").hard == []


def test_display_name_spoofing():
    verdict = check_headers({}, "support@paypal.com", "random@mailer.xyz")
    assert any("display name shows" in r for r in verdict.hard)
    verdict = check_headers({}, "PayPal Service", "alerts@paypa1.com")
    assert any("imitates paypal.com" in r for r in verdict.hard)
    verdict = check_headers({}, "Netflix", "billing@netfix-billing.com")
    assert verdict.soft


def test_safe_html_render():
    html, blocked = safe_html(
        '<div onclick="x()"><script>alert(1)</script><img src="https://track.example/p.gif" alt="logo">'
        '<a href="https://evil.example.com/x">Click <b>here</b></a><form action="/x"><input></form>'
        '<iframe src="https://x"></iframe><p style="color:red">text</p></div>')
    assert blocked == 1
    assert "<script" not in html and "onclick" not in html and "<img" not in html
    assert "<form" not in html and "<iframe" not in html and "<a " not in html and " href=" not in html
    assert "[evil.example.com]" in html and "Click" in html and "[image: logo]" in html

"""
Who built this site, read from the footer credit ("Website by XYZ").
Idea and wording patterns borrowed from the Harvey project's profile collector
(MIT). Free: runs over HTML already fetched, no request, no model.

A wrong name is worse than none, so only a clear credit is returned
(`RECORD_FLOOR`); a bare external link in the footer is not one. For operator
prioritisation only: nothing here is written into an email.
"""
import re
from urllib.parse import urlparse

# Footer links that are never the site's agency (matched against href and host).
_BENIGN_HOSTS = (
    "facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "youtube.",
    "tiktok.", "pinterest.", "yelp.", "threads.", "wa.me", "whatsapp.",
    "google.com/maps", "g.page", "maps.app.goo.gl", "goo.gl",
    "paypal", "stripe.com", "razorpay", "trustpilot", "justdial", "sulekha",
    "accessibe", "userway", "termly", "iubenda", "onetrust",
    "cloudflare", "godaddy", "wordpress.org", "wordpress.com", "wix.com",
    "squarespace.com", "shopify.com", "webflow.com", "gstatic", "googleapis",
    "w3.org", "schema.org",
)

_STRONG_CREDIT = re.compile(
    r"\b(powered|website|site|web\s*design|designed|developed|built|created|marketing)"
    r"\s+(by|&)\b", re.I)
_DESCRIPTIVE_CREDIT = re.compile(
    r"\b(web\s*design|website\s*design|digital\s*marketing|seo|marketing\s*agency|"
    r"web\s*development)\b", re.I)
RECORD_FLOOR = 0.75


def _strip_code(html: str) -> str:
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    return re.sub(r"(?s)<!--.*?-->", " ", html)


def detect_agency(html: str, domain: str) -> dict | None:
    """The agency credited in the footer: {name, confidence, url}, or None."""
    if not html or not domain:
        return None
    domain = domain.lower().removeprefix("www.")
    cleaned = _strip_code(html)
    m = re.search(r"(?is)<footer\b.*?</footer>", cleaned)
    region = m.group(0) if m else cleaned[-12000:]

    best = None
    for link in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', region):
        href = link.group(1)
        anchor = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", link.group(2))).strip()
        if not href.startswith("http"):
            continue
        host = (urlparse(href).netloc or "").lower().removeprefix("www.")
        if not host or host.endswith(domain) or domain.endswith(host):
            continue
        if any(b in href.lower() or b in host for b in _BENIGN_HOSTS):
            continue
        if not anchor or len(anchor) > 80:
            continue
        # The ~120 characters before the link is where "Website by" lives.
        before = re.sub(r"(?s)<[^>]+>", " ", region[max(0, link.start() - 120):link.start()])
        if _STRONG_CREDIT.search(f"{before} {anchor}"):
            conf = 0.9
        elif _DESCRIPTIVE_CREDIT.search(anchor):
            conf = 0.75
        else:
            continue
        if best is None or conf > best["confidence"]:
            best = {"name": anchor[:80], "confidence": conf, "url": href}
    return best if best and best["confidence"] >= RECORD_FLOOR else None

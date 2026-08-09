"""
NAP consistency — does the website agree with the Google Business Profile?

NAP is Name / Address / Phone. When the details on a business's own site
disagree with its Google listing, Google has conflicting information about
who and where the business is, and local ranking suffers: citation signals
(NAP consistency chief among them) are consistently measured as a
double-digit share of the local pack ranking algorithm. It also costs the
business directly and obviously — a wrong or outdated phone number on the
site is a call that never happens.

This check costs nothing new. Both sides are already in hand:

  - the Google Business Profile side comes from the Places API response the
    lead was scraped from (scrapers/google_maps.py already requests
    nationalPhoneNumber and formattedAddress in its field mask)
  - the website side is parsed out of HTML this pipeline has already fetched

so there is no extra API call, no extra page load, and no new dependency.

Deliberately conservative, in both directions:

  - Only a phone MISMATCH is reported, never a missing one. "No phone found
    in the HTML" is far more likely to mean the number is in an image, a
    JS-rendered widget, or a contact page we didn't crawl than that the
    business has no phone. Claiming otherwise is the same confident-wrong-
    claim-from-a-missing-signal bug this codebase keeps having to fix.
  - Address comparison is a loose token overlap, not string equality.
    Google's formattedAddress is heavily normalised ("Shop 4, MG Road,
    Bengaluru, Karnataka 560001, India") and almost never matches how a
    site writes the same address. Only a genuinely low overlap counts as a
    mismatch, so a formatting difference doesn't get reported as a factual
    one.
"""

import re

# Enough digits that a match is meaningful, few enough that country-code and
# trunk-prefix differences (+91 98765 43210 vs 098765 43210) don't cause a
# false mismatch on what is really the same number. Indian mobiles are 10
# digits, so the last 8 is a strong signal without being brittle.
_PHONE_SIGNIFICANT_DIGITS = 8

# Below this share of the Google address's meaningful tokens appearing on
# the site, treat it as a real discrepancy rather than a formatting quirk.
_ADDRESS_OVERLAP_THRESHOLD = 0.34

# Tokens that carry no identifying information and would inflate the overlap
# score on any Indian address.
_ADDRESS_STOPWORDS = {
    "shop", "no", "floor", "near", "opp", "opposite", "road", "rd", "street",
    "st", "lane", "cross", "main", "block", "sector", "phase", "nagar",
    "india", "ltd", "pvt", "and", "the",
}


def normalise_phone(raw: str) -> str:
    """Reduce a phone number to its comparable digits."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-_PHONE_SIGNIFICANT_DIGITS:] if len(digits) >= _PHONE_SIGNIFICANT_DIGITS else ""


def _address_tokens(raw: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", (raw or "").lower())
    return {t for t in tokens if len(t) > 2 and t not in _ADDRESS_STOPWORDS}


def phone_mismatch(gbp_phone: str, site_phones: list[str]) -> str | None:
    """
    Return the Google-listed phone if the site shows numbers but none match.

    Returns None when there's no GBP phone to compare against, when the site
    yielded no numbers at all (absence is not evidence — see module docstring),
    or when any number on the site matches.
    """
    target = normalise_phone(gbp_phone)
    if not target:
        return None

    found = {normalise_phone(p) for p in site_phones}
    found.discard("")
    if not found:
        return None

    return None if target in found else gbp_phone


def address_mismatch(gbp_address: str, page_text: str) -> str | None:
    """
    Return the Google-listed address if the page text doesn't corroborate it.

    Loose token overlap rather than equality — see the module docstring for
    why exact matching would be almost all false positives.
    """
    expected = _address_tokens(gbp_address)
    if len(expected) < 3:
        # Too little to judge on (a bare city name, say) — no signal.
        return None

    present = _address_tokens(page_text)
    if not present:
        return None

    overlap = len(expected & present) / len(expected)
    return gbp_address if overlap < _ADDRESS_OVERLAP_THRESHOLD else None

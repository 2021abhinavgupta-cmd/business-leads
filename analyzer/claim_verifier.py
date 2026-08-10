"""
Independent re-verification of the factual claims in a drafted email.

Every other accuracy check in this pipeline reads the SAME data the drafting
model read. _verify_grounding is the clearest case: it is handed the prompt
text and asked whether the draft follows from it, so when the prompt itself
carries a wrong number, the judge confirms the draft is faithful to it and
the email goes out. Two models agreeing about bad input is not verification.

This module is the second, independent measurement path. It ignores the audit
entirely and goes back to the live site, which is the only way to catch a
claim that was wrong before the AI ever saw it. Three real examples from
yogahouse.in, all of which shipped in one draft:

  - "your phone number doesn't match your Google listing" — produced by a
    regex that truncated "+91 86559 30022" by one digit
  - "9 broken links ... like the anchor link to your content section" — a
    same-page fragment and a Facebook link that refuses bots
  - "about 5 seconds" attributed to mobile visitors — a desktop, unthrottled
    measurement; the real throttled-mobile figure was 15.9s

None of the three were AI errors, so no amount of prompt work or LLM judging
could have caught them. Re-measuring catches all three.

Deliberately deterministic. No model is called here: the checks are regex
claim-extraction plus HTTP probes plus the PageSpeed API, so a warning is
reproducible and costs no tokens. It is also deliberately QUIET — every check
returns None when it cannot get a clean answer, because a verifier that
cannot reach the site must not imply the draft is wrong. The failure mode
this codebase keeps having to fix is a missing signal being reported as a
finding, and a verifier is the worst possible place to repeat it.

Warnings surface through the same review_warnings path as every other check:
red banner in the Drafts UI, and /api/send returns 409 until acknowledged.
"""

import re

import httpx

import config
from analyzer.nap_check import normalise_phone

# Browser UA. Some sites and most WAFs serve different markup — or nothing at
# all — to an obvious bot, and a verifier that sees a different page than a
# visitor does would manufacture disagreements.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_FETCH_TIMEOUT = 25
_PROBE_TIMEOUT = 15
_PAGESPEED_TIMEOUT = 120
_PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Cap on links re-probed. This runs once per drafted lead, not per audit, but
# it is still N requests against someone else's server.
_MAX_LINKS_TO_REPROBE = 25

# A quoted duration is allowed to differ from the re-measured one by this much
# before it counts as a disagreement. Page speed genuinely varies run to run,
# and the check is meant to catch "5s claimed, 15.9s measured", not rounding.
_SPEED_TOLERANCE_S = 2.5


# ---------------------------------------------------------------------------
# Tools — the three things the verifier can actually do
# ---------------------------------------------------------------------------

def fetch_page_text(url: str) -> str | None:
    """Visible text of the live page, or None if it could not be fetched."""
    try:
        from bs4 import BeautifulSoup

        response = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=_FETCH_TIMEOUT)
        if response.status_code >= 400:
            return None
        return " ".join(BeautifulSoup(response.text, "html.parser").get_text(" ").split())
    except Exception as exc:
        print(f"[Verify] Could not fetch {url}: {exc}")
        return None


def probe_url(url: str) -> bool | None:
    """
    True if *url* responds, False if it is genuinely dead, None if unknown.

    Tri-state on purpose. A transport error from our end says nothing about
    whether the link works for a visitor, and collapsing that into False is
    how "this link is broken" gets asserted about a link that is fine.
    """
    try:
        with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=_PROBE_TIMEOUT) as client:
            try:
                status = client.head(url).status_code
            except Exception:
                status = None
            if status is not None and status < 400:
                return True
            # Some servers reject HEAD from automated clients but serve GET
            # normally, so a HEAD failure is retried before being believed.
            status = client.get(url).status_code
            return status < 400
    except Exception:
        return None


def pagespeed_lcp_seconds(url: str, strategy: str = "mobile") -> float | None:
    """Largest Contentful Paint in seconds from the PageSpeed API, or None."""
    if not config.PAGESPEED_KEY:
        return None
    try:
        response = httpx.get(
            _PAGESPEED_URL,
            params={"url": url, "strategy": strategy, "category": "PERFORMANCE", "key": config.PAGESPEED_KEY},
            timeout=_PAGESPEED_TIMEOUT,
        )
        if response.status_code >= 400:
            return None
        audits = response.json().get("lighthouseResult", {}).get("audits", {})
        value = audits.get("largest-contentful-paint", {}).get("numericValue")
        return None if value is None else value / 1000
    except Exception as exc:
        print(f"[Verify] PageSpeed lookup failed for {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def _draft_text(parsed: dict) -> str:
    """Everything the recipient will actually read, as one string."""
    parts = [parsed.get("email_subject", ""), parsed.get("opening_line", "")]
    for flaw in parsed.get("flaws") or []:
        parts.append(flaw.get("paragraph", "") if isinstance(flaw, dict) else str(flaw))
    return " ".join(p for p in parts if p)


# Matches phone-shaped runs the same way scrapers.website does, so the
# verifier and the scraper agree on what counts as a number.
_PHONE_IN_COPY = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s\-.]?)?(?:\(\d{2,5}\)|\d{2,5})(?:[\s\-.]?\d{2,6}){1,3}(?!\d)"
)
_BROKEN_LINK_CLAIM = re.compile(r"\b(\d{1,3})\s+broken\s+links?\b", re.I)
_SECONDS_CLAIM = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:seconds?|secs?|s)\b(?!\w)", re.I)


def _phones_in(text: str) -> set[str]:
    found = set()
    for match in _PHONE_IN_COPY.finditer(text or ""):
        candidate = match.group(0)
        if 8 <= len(re.sub(r"\D", "", candidate)) <= 15:
            normalised = normalise_phone(candidate)
            if normalised:
                found.add(normalised)
    return found


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _check_phone_claims(parsed: dict, url: str, gbp_phone: str) -> str | None:
    """
    Warn when the draft quotes a number that is neither on the live page nor
    the Google listing.

    This is the check that would have caught the truncation bug: the draft
    asserted a mismatch against a number the page does in fact print.
    """
    quoted = _phones_in(_draft_text(parsed))
    if not quoted:
        return None

    page_text = fetch_page_text(url)
    if page_text is None:
        return None

    known = _phones_in(page_text)
    gbp = normalise_phone(gbp_phone)
    if gbp:
        known.add(gbp)
    if not known:
        # Nothing to compare against — the number may well be in an image or
        # a JS widget. Silence, not a finding.
        return None

    unknown = quoted - known
    if not unknown:
        return None

    warning = (
        f"Phone claim unverified: the draft quotes a number that appears neither on "
        f"{url} nor on the Google listing. Numbers on the live page: "
        f"{', '.join(sorted(known)) or 'none found'}."
    )
    print(f"[Verify] {warning}")
    return warning


def _check_broken_link_claim(parsed: dict, url: str) -> str | None:
    """
    Re-probe the page's links when the draft claims a broken-link count.

    Only fires when the live probe finds NONE, which is the unambiguous case:
    a count that differs by one or two is ordinary flakiness, but "9 broken
    links" against a page where every link resolves is a claim to retract.
    """
    match = _BROKEN_LINK_CLAIM.search(_draft_text(parsed))
    if not match:
        return None
    claimed = int(match.group(1))
    if claimed <= 0:
        return None

    from urllib.parse import urljoin, urlparse

    from analyzer.visuals import _is_bot_hostile

    try:
        response = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=_FETCH_TIMEOUT)
        if response.status_code >= 400:
            return None
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.text, "html.parser")
        final_url = str(response.url)
    except Exception as exc:
        print(f"[Verify] Could not re-probe links on {url}: {exc}")
        return None

    def _same_page_fragment(raw: str, absolute: str) -> bool:
        if raw.strip().startswith("#"):
            return True
        a, b = urlparse(absolute), urlparse(final_url)
        return bool(a.fragment) and (a.netloc, a.path, a.query) == (b.netloc, b.path, b.query)

    candidates = []
    for anchor in soup.select("a[href]"):
        raw = anchor["href"]
        absolute = urljoin(final_url, raw)
        if not absolute.startswith("http"):
            continue
        if _same_page_fragment(raw, absolute):
            continue
        if _is_bot_hostile(absolute):
            continue
        if absolute not in candidates:
            candidates.append(absolute)

    candidates = candidates[:_MAX_LINKS_TO_REPROBE]
    if not candidates:
        return None

    dead = 0
    inconclusive = 0
    for link in candidates:
        result = probe_url(link)
        if result is None:
            inconclusive += 1
        elif result is False:
            dead += 1

    # If most probes were inconclusive we learned nothing; staying silent is
    # the only honest option.
    if inconclusive > len(candidates) // 2:
        return None
    if dead > 0:
        return None

    warning = (
        f"Broken-link claim unverified: the draft says {claimed} broken links, but re-probing "
        f"{len(candidates)} links on {url} found none dead. Excluded same-page anchors and "
        f"bot-blocking hosts, which are the usual sources of a false count."
    )
    print(f"[Verify] {warning}")
    return warning


def _check_speed_claim(parsed: dict, url: str) -> str | None:
    """
    Warn when a duration in the copy matches neither the desktop nor the
    mobile measurement.

    Both strategies are allowed because either can be the honest number
    depending on which source the audit picked; the claim only fails when it
    sits outside both. This is what catches a desktop figure being sold to
    the recipient as their mobile visitors' experience.
    """
    seconds = [float(m.group(1)) for m in _SECONDS_CLAIM.finditer(_draft_text(parsed))]
    seconds = [s for s in seconds if 0.5 <= s <= 60]
    if not seconds:
        return None

    measured = [
        value
        for value in (pagespeed_lcp_seconds(url, "mobile"), pagespeed_lcp_seconds(url, "desktop"))
        if value is not None
    ]
    if not measured:
        return None

    off = [s for s in seconds if all(abs(s - m) > _SPEED_TOLERANCE_S for m in measured)]
    if not off:
        return None

    warning = (
        f"Speed claim unverified: the draft cites {', '.join(f'{s:g}s' for s in off)}, but "
        f"re-measured LCP is {' / '.join(f'{m:.1f}s' for m in measured)} "
        f"(mobile / desktop as available). Check which device the claim describes."
    )
    print(f"[Verify] {warning}")
    return warning


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def verify_claims(parsed: dict, url: str, *, gbp_phone: str = "") -> list[str]:
    """
    Re-check the draft's factual claims against the live site.

    Returns a list of warning strings, empty when everything checked out or
    when nothing could be checked. Never raises: a verifier that can break
    the audit is worse than no verifier, since the pipeline would lose leads
    over a check that exists purely to add confidence.
    """
    if not url or not config.VERIFY_CLAIMS_LIVE:
        return []

    warnings = []
    for check in (_check_phone_claims, _check_broken_link_claim, _check_speed_claim):
        try:
            if check is _check_phone_claims:
                result = check(parsed, url, gbp_phone)
            else:
                result = check(parsed, url)
            if result:
                warnings.append(result)
        except Exception as exc:
            print(f"[Verify] {check.__name__} errored (non-fatal): {exc}")
    return warnings

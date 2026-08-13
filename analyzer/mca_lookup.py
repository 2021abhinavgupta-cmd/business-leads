"""
India MCA (Ministry of Corporate Affairs) Company Master Data lookup, via
data.gov.in's free Open Government Data API (added 2026-08-10, feeding
analyzer/budget_signal.py).

Real financial data — paid-up and authorized capital — for companies
registered with the Registrar of Companies. This is the strongest signal
budget_signal.py can get when it applies, and stronger than any of that
module's other proxies (review count, detected tooling, follower count all
infer scale; this states a number a company itself filed with a regulator).

It usually does NOT apply. A sole proprietorship — a very common structure
for a single-location small business, exactly the kind this pipeline
targets — has no MCA record at all, since only registered company
structures (Pvt Ltd, LLP, etc.) file with the Registrar. Silence here means
"not registered this way", never "small" or "can't afford this" — same
absence-is-not-evidence principle as nap_check.py and everywhere else in
this codebase that infers from something being missing.

Two things make a wrong answer here worse than a wrong answer almost
anywhere else in this codebase: it is a specific, checkable financial claim
about a specific legal entity, and the free API's own name filter is a
substring/prefix match, NOT an exact one, so a query for "Acme" will surface
every "Acme Something Pvt Ltd" and "Something Acme LLP" registered anywhere
in India. This module is deliberately conservative to compensate:

  - Matching is EXACT on a normalised name, never fuzzy/scored. No
    "closest match" logic exists anywhere here.
  - More than one exact match (a real possibility — common business names
    repeat across India, and the free dataset carries no phone/address to
    disambiguate) is treated exactly the same as zero matches: no match,
    don't guess.
  - A confirmed miss is cached distinctly from "never looked up" so a
    genuinely absent company isn't re-queried against the free tier's daily
    rate limit on every re-audit — but a network/parse FAILURE is never
    cached, so a transient outage doesn't get permanently remembered as
    "this company doesn't exist."

Entirely inert — no import-time or call-time side effect — until both
DATA_GOV_IN_API_KEY and MCA_COMPANY_MASTER_RESOURCE_ID are set. See
config.py for how to obtain each.
"""

import re

import httpx

import config
from storage import db

_API_BASE = "https://api.data.gov.in/resource"
_REQUEST_TIMEOUT = 15
# The free filter is substring-based, so pulling a generous batch and doing
# the real (exact, normalised) match ourselves costs one request either way.
_CANDIDATE_LIMIT = 25

# Company-suffix words stripped before comparison, so "Acme Pvt Ltd" and
# "ACME PRIVATE LIMITED" normalise to the same string. Order matters only in
# that longer phrases are removed before their shorter substrings would be.
_SUFFIX_WORDS = (
    "private limited", "pvt ltd", "pvt. ltd.", "pvt.ltd", "pvt ltd.",
    "limited liability partnership", "llp", "limited", "ltd", "ltd.",
    "incorporated", "inc.", "inc", "private", "pvt",
)

# Below this many characters after normalisation, a name is too short/generic
# to search safely — "Yoga" or "Studio" alone would surface far too many
# unrelated companies for the exact-match filter to meaningfully narrow.
_MIN_NORMALISED_LENGTH = 4


def _normalise_company_name(name: str) -> str:
    """Lowercase, suffix-stripped, whitespace-collapsed form used for BOTH
    the cache key and the exact-match comparison — the cache and a live
    lookup must agree on what counts as the same company."""
    text = (name or "").lower()
    text = re.sub(r"[.,&\-]", " ", text)
    for suffix in _SUFFIX_WORDS:
        text = re.sub(rf"\b{re.escape(suffix)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_candidate(record: dict) -> dict:
    """Pull out the fields budget_signal.py actually uses, tolerating the
    field-name variants seen across different data.gov.in dataset revisions."""
    def _get(*keys):
        for key in keys:
            if key in record and record[key] not in (None, ""):
                return record[key]
        return None

    return {
        "cin": _get("CIN", "cin"),
        "company_name": _get("COMPANY_NAME", "company_name"),
        "status": _get("COMPANY_STATUS", "company_status", "status"),
        "paidup_capital": _get("PAIDUP_CAPITAL", "paidup_capital"),
        "authorized_capital": _get("AUTHORIZED_CAP", "authorized_capital", "AUTHORIZED_CAPITAL"),
        "registered_state": _get("REGISTERED_STATE", "registered_state", "state"),
    }


def lookup_company(company_name: str) -> dict | None:
    """
    A confident MCA Company Master Data match for *company_name*, or None.

    None means either "not configured", "name too generic to search
    safely", "no exact match", "more than one exact match (ambiguous)", or
    "the lookup failed" — deliberately collapsed into one return value,
    because every one of those cases means the same thing to the caller:
    don't make a financial claim from this. Never raises.
    """
    if not config.DATA_GOV_IN_API_KEY or not config.MCA_COMPANY_MASTER_RESOURCE_ID:
        return None

    normalised = _normalise_company_name(company_name)
    if len(normalised) < _MIN_NORMALISED_LENGTH:
        return None

    found, cached = db.get_mca_cache(normalised)
    if found:
        return cached

    try:
        response = httpx.get(
            f"{_API_BASE}/{config.MCA_COMPANY_MASTER_RESOURCE_ID}",
            params={
                "api-key": config.DATA_GOV_IN_API_KEY,
                "format": "json",
                "limit": _CANDIDATE_LIMIT,
                "filters[company_name]": company_name,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            print(f"[MCA] Lookup for '{company_name}' failed: HTTP {response.status_code} — not caching, will retry next audit.")
            return None
        records = response.json().get("records", []) or []
    except Exception as e:
        print(f"[MCA] Lookup for '{company_name}' failed (non-fatal, not cached): {e}")
        return None

    # Records come from a free, loosely-typed government dataset — a
    # malformed entry (not even a dict, missing the expected key) must drop
    # out of consideration, not crash a lookup over one bad row.
    exact_matches = []
    for record in records:
        try:
            candidate_name = record.get("company_name") or record.get("COMPANY_NAME") or ""
        except AttributeError:
            continue
        if _normalise_company_name(candidate_name) == normalised:
            exact_matches.append(_extract_candidate(record))

    if len(exact_matches) != 1:
        if len(exact_matches) > 1:
            print(f"[MCA] '{company_name}' matched {len(exact_matches)} registered companies exactly — ambiguous, treating as no match rather than guessing.")
        db.set_mca_cache(normalised, None)
        return None

    match = exact_matches[0]
    db.set_mca_cache(normalised, match)
    return match

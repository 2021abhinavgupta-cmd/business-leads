"""
Tests for analyzer/mca_lookup.py (added 2026-08-10) — India MCA Company
Master Data lookup via data.gov.in, feeding analyzer/budget_signal.py.

Built deliberately conservative, because a wrong answer here is worse than
almost anywhere else in this codebase: it's a specific, checkable financial
claim about a specific legal entity, backed by a free API whose own name
filter is substring/prefix, not exact. Most of these tests are about the
boundary between "confident enough to use" and "not confident, stay quiet."

Unit tests only — every HTTP call and DB call is stubbed. No real API key,
no real data.gov.in traffic.
"""

import pytest

import config
from analyzer import mca_lookup


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config, "DATA_GOV_IN_API_KEY", "test-key")
    monkeypatch.setattr(config, "MCA_COMPANY_MASTER_RESOURCE_ID", "test-resource-id")


# ---------------------------------------------------------------------------
# Name normalisation — the safety mechanism everything else relies on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Acme Private Limited", "acme"),
    ("ACME PVT LTD", "acme"),
    ("Acme Pvt. Ltd.", "acme"),
    ("Acme LLP", "acme"),
    ("Acme  Limited", "acme"),
    ("Acme & Co.", "acme co"),
])
def test_common_suffixes_normalise_to_the_same_string(raw, expected):
    assert mca_lookup._normalise_company_name(raw) == expected


def test_two_different_companies_do_not_collide():
    assert mca_lookup._normalise_company_name("Acme Pvt Ltd") != mca_lookup._normalise_company_name("Acme Global Pvt Ltd")


# ---------------------------------------------------------------------------
# Gating — inert until fully configured
# ---------------------------------------------------------------------------

def test_missing_api_key_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(config, "DATA_GOV_IN_API_KEY", "")

    def _explode(*a, **k):
        raise AssertionError("should not touch the network without an API key")

    monkeypatch.setattr(mca_lookup.httpx, "get", _explode)
    assert mca_lookup.lookup_company("Acme Pvt Ltd") is None


def test_missing_resource_id_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(config, "MCA_COMPANY_MASTER_RESOURCE_ID", "")

    def _explode(*a, **k):
        raise AssertionError("should not touch the network without a resource id")

    monkeypatch.setattr(mca_lookup.httpx, "get", _explode)
    assert mca_lookup.lookup_company("Acme Pvt Ltd") is None


def test_a_too_short_name_is_not_searched(monkeypatch):
    """"Yoga" or "Studio" alone would surface far too many unrelated hits."""
    def _explode(*a, **k):
        raise AssertionError("should not search on a dangerously generic name")

    monkeypatch.setattr(mca_lookup.httpx, "get", _explode)
    assert mca_lookup.lookup_company("Go") is None


# ---------------------------------------------------------------------------
# Matching — exact only, never fuzzy
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, records, status_code=200):
        self._records = records
        self.status_code = status_code

    def json(self):
        return {"records": self._records}


def _no_cache(monkeypatch):
    monkeypatch.setattr(mca_lookup.db, "get_mca_cache", lambda name: (False, None))
    calls = []
    monkeypatch.setattr(mca_lookup.db, "set_mca_cache", lambda name, data: calls.append((name, data)))
    return calls


def test_a_single_exact_match_is_returned(monkeypatch):
    cache_calls = _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([
        {"company_name": "Yoga House Private Limited", "cin": "U12345MH2015PTC000001",
         "company_status": "Active", "paidup_capital": "1000000"},
    ]))

    result = mca_lookup.lookup_company("Yoga House")
    assert result is not None
    assert result["cin"] == "U12345MH2015PTC000001"
    assert cache_calls == [("yoga house", result)]


def test_multiple_exact_matches_are_treated_as_no_match(monkeypatch):
    """
    A real possibility: common business names repeat across India, and the
    free dataset carries no phone/address to disambiguate. Don't guess.
    """
    _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([
        {"company_name": "Yoga House Private Limited", "cin": "U1"},
        {"company_name": "Yoga House LLP", "cin": "U2"},
    ]))

    assert mca_lookup.lookup_company("Yoga House") is None


def test_a_substring_match_that_is_not_exact_is_rejected(monkeypatch):
    """
    The API's own filter is substring/prefix — "Yoga House Wellness Center
    Private Limited" containing "Yoga House" must NOT count as a match for
    a search on "Yoga House" alone.
    """
    _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([
        {"company_name": "Yoga House Wellness Center Private Limited", "cin": "U1"},
    ]))

    assert mca_lookup.lookup_company("Yoga House") is None


def test_no_candidates_returned_is_no_match(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([]))
    assert mca_lookup.lookup_company("Yoga House") is None


# ---------------------------------------------------------------------------
# Caching — a confirmed miss is distinct from "never checked"
# ---------------------------------------------------------------------------

def test_a_cache_hit_skips_the_network_entirely(monkeypatch):
    monkeypatch.setattr(mca_lookup.db, "get_mca_cache", lambda name: (True, {"cin": "cached"}))

    def _explode(*a, **k):
        raise AssertionError("should not query the API on a cache hit")

    monkeypatch.setattr(mca_lookup.httpx, "get", _explode)
    assert mca_lookup.lookup_company("Yoga House") == {"cin": "cached"}


def test_a_confirmed_miss_is_cached_as_none(monkeypatch):
    cache_calls = _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([]))

    mca_lookup.lookup_company("Yoga House")
    assert cache_calls == [("yoga house", None)]


def test_a_network_failure_is_not_cached(monkeypatch):
    """
    A transient outage must be retried next time, not remembered forever as
    "this company doesn't exist."
    """
    monkeypatch.setattr(mca_lookup.db, "get_mca_cache", lambda name: (False, None))
    cache_writes = []
    monkeypatch.setattr(mca_lookup.db, "set_mca_cache", lambda *a: cache_writes.append(a))

    def _explode(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(mca_lookup.httpx, "get", _explode)
    assert mca_lookup.lookup_company("Yoga House") is None
    assert cache_writes == []


def test_an_http_error_status_is_not_cached(monkeypatch):
    monkeypatch.setattr(mca_lookup.db, "get_mca_cache", lambda name: (False, None))
    cache_writes = []
    monkeypatch.setattr(mca_lookup.db, "set_mca_cache", lambda *a: cache_writes.append(a))
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([], status_code=503))

    assert mca_lookup.lookup_company("Yoga House") is None
    assert cache_writes == []


def test_lookup_never_raises_on_malformed_records(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(mca_lookup.httpx, "get", lambda *a, **k: _Resp([
        {"company_name": None}, {}, "not even a dict",
    ]))
    # Must not raise — malformed upstream data is exactly what a free,
    # loosely-typed government dataset can serve up.
    assert mca_lookup.lookup_company("Yoga House") is None

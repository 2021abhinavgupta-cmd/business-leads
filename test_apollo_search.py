"""
Tests for /api/search-apollo (added 2026-09-16).

ApolloFreeScraper (scrapers/apollo_free.py) already existed and was wired
into scheduler.py's automated LEAD_SOURCE=b2b job, but had no way to be
triggered on demand from the UI — this endpoint is that missing wiring.
Unit tests only; the real Apollo API is never called.
"""

from fastapi.testclient import TestClient
import app as app_module


def _test_client(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    # /api/search-apollo geocodes any place it is given, to tell a city
    # (which Apollo can filter on) from a district (which it cannot). Left
    # unstubbed that is a real network call to Google on every test that
    # passes a city, so it is stubbed to a plain city by default; the
    # district tests below override it.
    monkeypatch.setattr(
        app_module.maps_scraper, "resolve_area",
        lambda name: {"name": name, "city": name, "is_city": True, "rectangle": {"low": {}, "high": {}}},
    )
    return app_module, TestClient(app_module.app, raise_server_exceptions=False)


def test_search_apollo_400s_when_the_api_key_is_not_set(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "")

    res = client.post("/api/search-apollo", json={"niche": "Dentist"})
    assert res.status_code == 400
    assert "APOLLO_API_KEY" in res.json()["detail"]


def test_search_apollo_returns_leads_and_uses_the_given_niche_and_limit(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    captured = {}

    def _fake_scrape(niche, limit, city="", domains=None):
        captured["niche"] = niche
        captured["limit"] = limit
        return [{"Company": "Acme", "Website": "acme.com", "Email": "ceo@acme.com", "Decision Maker Name": "Jane Doe"}]

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _fake_scrape)
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    res = client.post("/api/search-apollo", json={"niche": "Dentist", "limit": 40})
    assert res.status_code == 200
    data = res.json()
    assert data["leads"] == [{"Company": "Acme", "Website": "acme.com", "Email": "ceo@acme.com", "Decision Maker Name": "Jane Doe"}]
    assert captured == {"niche": "Dentist", "limit": 40}


def test_search_apollo_defaults_limit_to_25(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    captured = {}
    monkeypatch.setattr(app_module.apollo_scraper, "scrape", lambda niche, limit, city="", domains=None: captured.update(limit=limit) or [])
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    res = client.post("/api/search-apollo", json={"niche": "Dentist"})
    assert res.status_code == 200
    assert captured["limit"] == 25


def test_search_apollo_rejects_a_limit_over_100(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    res = client.post("/api/search-apollo", json={"niche": "Dentist", "limit": 500})
    assert res.status_code == 422


def test_search_apollo_500s_when_the_scraper_raises(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    def _boom(niche, limit, city="", domains=None):
        raise RuntimeError("Apollo API HTTP error")

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _boom)

    res = client.post("/api/search-apollo", json={"niche": "Dentist"})
    assert res.status_code == 500


# ---------------------------------------------------------------
# City narrowing (added 2026-09-25)
#
# Until this point every Apollo search sent organization_locations:
# ["india"], so a search for one city's businesses swept the whole country.
# Apollo cannot be narrowed below city level at all (its docs list cities,
# US states and countries as the accepted values), which is why an area
# like BKC goes through /api/search-area instead.
# ---------------------------------------------------------------

def test_search_apollo_passes_the_city_through_to_the_scraper(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    captured = {}

    def _fake_scrape(niche, limit, city="", domains=None):
        captured["city"] = city
        return []

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _fake_scrape)
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    # Own X-API-Key so this test gets its own rate-limit bucket — the
    # 5/60 limiter on this endpoint is keyed by API key and is otherwise
    # shared with every other test in this file.
    res = client.post(
        "/api/search-apollo",
        json={"niche": "Dentist", "city": "Mumbai"},
        headers={"X-API-Key": "bucket-city-passthrough"},
    )

    assert res.status_code == 200
    assert captured["city"] == "Mumbai"


def test_search_apollo_city_defaults_to_blank(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")

    captured = {}

    def _fake_scrape(niche, limit, city="", domains=None):
        captured["city"] = city
        return []

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _fake_scrape)
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    res = client.post(
        "/api/search-apollo",
        json={"niche": "Dentist"},
        headers={"X-API-Key": "bucket-city-default"},
    )
    assert res.status_code == 200
    assert captured["city"] == ""


# ---------------------------------------------------------------------------
# District targeting + error surfacing (added 2026-09-27)
#
# Request: "i just want that apollo searches gives me leads if i type bkc".
# Apollo's location filter stops at city level, so a district is served by
# resolving it on the map, sweeping the real companies inside its boundary,
# and asking Apollo for decision makers at those exact domains.
# ---------------------------------------------------------------------------

from scrapers.apollo_free import ApolloError


_MUMBAI = {"name": "Mumbai, Maharashtra, India", "city": "Mumbai", "is_city": True,
           "rectangle": {"low": {}, "high": {}}}
_BKC = {"name": "Bandra Kurla Complex, Mumbai", "city": "Mumbai", "is_city": False,
        "rectangle": {"low": {}, "high": {}}}


def _apollo_ready(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "APOLLO_API_KEY", "fake-key")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "fake-maps-key")
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)
    return app_module, client


def _area_returning(monkeypatch, app_module, area, area_leads):
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: area)

    async def _fake_scrape_area(a, limit):
        return area_leads

    monkeypatch.setattr(app_module.maps_scraper, "scrape_area", _fake_scrape_area)


def test_a_district_sends_the_mapped_domains_to_apollo(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a-corp.com"},
        {"Company": "B", "Website": "https://www.b-corp.in"},
    ])

    captured = {}

    def _fake_scrape(niche, limit, city="", domains=None):
        captured.update(niche=niche, city=city, domains=domains)
        return [{"Company": "A", "Website": "https://a-corp.com", "Email": "o@a-corp.com"}]

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _fake_scrape)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-district"})

    assert res.status_code == 200, res.text
    assert captured["domains"] == ["a-corp.com", "b-corp.in"]
    # The map already decided the geography; layering Apollo's own guess on
    # top could only remove correct answers.
    assert captured["city"] == ""


def test_a_district_reports_how_many_companies_apollo_actually_knew(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a-corp.com"},
        {"Company": "B", "Website": "https://b-corp.in"},
        {"Company": "C", "Website": "https://c-corp.com"},
    ])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [
            {"Company": "A", "Website": "https://a-corp.com"},
        ],
    )

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-district-count"})

    data = res.json()
    assert res.status_code == 200, res.text
    # A partial match is the NORMAL outcome, so it is reported rather than
    # left to look like the district being empty.
    assert data["companies_found"] == 3
    assert data["companies_matched"] == 1
    assert data["area"]["name"] == "Bandra Kurla Complex, Mumbai"


def test_a_whole_city_skips_the_map_sweep_entirely(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: _MUMBAI)

    async def _must_not_run(a, limit):
        raise AssertionError("a city must not trigger the Places sweep")

    monkeypatch.setattr(app_module.maps_scraper, "scrape_area", _must_not_run)

    captured = {}

    def _fake_scrape(niche, limit, city="", domains=None):
        captured.update(city=city, domains=domains)
        return []

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _fake_scrape)

    res = client.post("/api/search-apollo", json={"niche": "Dentist", "city": "Mumbai"},
                      headers={"X-API-Key": "bucket-city-path"})

    assert res.status_code == 200, res.text
    assert captured["city"] == "Mumbai"
    assert not captured["domains"]
    assert res.json()["area"] is None


def test_an_unresolvable_place_falls_back_to_the_plain_city_search(monkeypatch):
    """Geocoding being down must not block an ordinary Apollo search."""
    app_module, client = _apollo_ready(monkeypatch)
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: None)

    captured = {}
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: captured.update(city=city) or [],
    )

    res = client.post("/api/search-apollo", json={"niche": "Dentist", "city": "Nowhereville"},
                      headers={"X-API-Key": "bucket-unresolvable"})

    assert res.status_code == 200, res.text
    assert captured["city"] == "Nowhereville"


def test_a_district_with_no_websites_says_so_instead_of_calling_apollo(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [])

    def _must_not_run(*a, **k):
        raise AssertionError("Apollo must not be called with an empty domain list")

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _must_not_run)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-empty-district"})

    data = res.json()
    assert res.status_code == 200, res.text
    assert data["leads"] == []
    assert data["companies_found"] == 0
    assert "nothing to ask Apollo about" in data["note"]


def test_a_place_alone_is_enough_no_niche_required(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [{"Company": "A", "Website": "https://a.com"}])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [],
    )

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-place-only"})

    assert res.status_code == 200, res.text


def test_neither_a_niche_nor_a_place_is_rejected_with_an_explanation(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)

    res = client.post("/api/search-apollo", json={},
                      headers={"X-API-Key": "bucket-nothing"})

    assert res.status_code == 400
    assert "niche" in res.json()["detail"].lower()


def test_apollo_refusing_the_request_surfaces_the_real_reason(monkeypatch):
    """
    The bug this exists for: an invalid key, a blocked plan and an empty
    search all rendered as "Added 0 leads from Apollo".
    """
    app_module, client = _apollo_ready(monkeypatch)
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: _MUMBAI)

    def _refuse(niche, limit, city="", domains=None):
        raise ApolloError("Apollo rejected the API key. Generate a new one...")

    monkeypatch.setattr(app_module.apollo_scraper, "scrape", _refuse)

    res = client.post("/api/search-apollo", json={"niche": "Dentist", "city": "Mumbai"},
                      headers={"X-API-Key": "bucket-refused"})

    assert res.status_code == 502
    assert "rejected the API key" in res.json()["detail"]


def test_a_district_search_without_a_maps_key_explains_why_it_cannot_run(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: _BKC)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-no-maps-key"})

    assert res.status_code == 400
    assert "GOOGLE_MAPS_API_KEY" in res.json()["detail"]


# ---------------------------------------------------------------------------
# The "why zero" diagnostic (added 2026-09-27)
#
# Live report: "Found 60 companies on the map there, Apollo had decision
# makers for 0." Two completely different causes look identical from the
# outside, so one unfiltered probe settles which, and only when the search
# already came back empty.
# ---------------------------------------------------------------------------

def test_no_matches_and_no_coverage_blames_apollos_data(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a.com"},
    ])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [],
    )
    monkeypatch.setattr(app_module.apollo_scraper, "count_people_at_domains", lambda domains: 0)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-diag-nocoverage"})

    detail = res.json()["diagnostic"]
    assert "no contacts" in detail
    # Points at the thing that DOES work for this district rather than
    # leaving the user at a dead end.
    assert "Search an Area" in detail


def test_no_matches_but_real_coverage_says_the_contacts_are_too_junior(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a.com"},
    ])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [],
    )
    monkeypatch.setattr(app_module.apollo_scraper, "count_people_at_domains", lambda domains: 23)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-diag-junior"})

    detail = res.json()["diagnostic"]
    assert "23 contact" in detail
    assert "too junior" in detail


def test_a_probe_that_cannot_answer_says_so_rather_than_inventing_a_reason(monkeypatch):
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a.com"},
    ])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [],
    )
    monkeypatch.setattr(app_module.apollo_scraper, "count_people_at_domains", lambda domains: None)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-diag-unknown"})

    assert res.status_code == 200
    assert "could not be asked" in res.json()["diagnostic"]


def test_a_successful_district_search_never_runs_the_probe(monkeypatch):
    """It costs an extra Apollo call, so it only fires on an empty result."""
    app_module, client = _apollo_ready(monkeypatch)
    _area_returning(monkeypatch, app_module, _BKC, [
        {"Company": "A", "Website": "https://a.com"},
    ])
    monkeypatch.setattr(
        app_module.apollo_scraper, "scrape",
        lambda niche, limit, city="", domains=None: [
            {"Company": "A", "Website": "https://a.com"},
        ],
    )

    def _must_not_run(domains):
        raise AssertionError("the probe must not run when leads were found")

    monkeypatch.setattr(app_module.apollo_scraper, "count_people_at_domains", _must_not_run)

    res = client.post("/api/search-apollo", json={"city": "BKC, Mumbai"},
                      headers={"X-API-Key": "bucket-diag-skipped"})

    assert res.status_code == 200, res.text
    assert res.json()["diagnostic"] is None

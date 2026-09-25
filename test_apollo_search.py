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

    def _fake_scrape(niche, limit, city=""):
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
    monkeypatch.setattr(app_module.apollo_scraper, "scrape", lambda niche, limit, city="": captured.update(limit=limit) or [])
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

    def _boom(niche, limit, city=""):
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

    def _fake_scrape(niche, limit, city=""):
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

    def _fake_scrape(niche, limit, city=""):
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

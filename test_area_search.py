"""
Tests for area-based lead search (added 2026-09-25).

The problem this solves: a business district like BKC or Koramangala is
known to be full of businesses, but not WHICH businesses — so there is no
niche to type into the existing /api/search. /api/search-nearby could
already search without a niche, but only around the caller's own device
GPS, which means you can only prospect where you physically are.

/api/search-area closes that gap: a typed place name is resolved to a real
search box via Google's Geocoding API, then searched one business type at
a time inside that box. The leads come back carrying a Category, which is
what turns "I don't know the niche" into a list of the niches actually
present.

No real Google API is ever called — httpx is monkeypatched throughout.
"""

import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import config
from scrapers.google_maps import GoogleMapsScraper


# ---------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------

def _json_response(json_data: dict, status_code: int = 200, method: str = "POST"):
    request = httpx.Request(method, "https://example.invalid/x")
    return httpx.Response(status_code, json=json_data, request=request)


def _geocode_payload(
    lat=19.0656,
    lng=72.8679,
    ne=(19.0720, 72.8750),
    sw=(19.0590, 72.8600),
    city="Mumbai",
    formatted="Bandra Kurla Complex, Mumbai, Maharashtra, India",
):
    return {
        "status": "OK",
        "results": [
            {
                "formatted_address": formatted,
                "address_components": [
                    {"long_name": "Bandra Kurla Complex", "types": ["sublocality", "political"]},
                    {"long_name": city, "types": ["locality", "political"]},
                    {"long_name": "Maharashtra", "types": ["administrative_area_level_1"]},
                ],
                "geometry": {
                    "location": {"lat": lat, "lng": lng},
                    "viewport": {
                        "northeast": {"lat": ne[0], "lng": ne[1]},
                        "southwest": {"lat": sw[0], "lng": sw[1]},
                    },
                },
            }
        ],
    }


def _place(name, website, types=None, category="Dentist", reviews=50):
    return {
        "displayName": {"text": name},
        "websiteUri": website,
        "nationalPhoneNumber": "+91 22 1234 5678",
        "formattedAddress": "Somewhere, Mumbai",
        "rating": 4.5,
        "userRatingCount": reviews,
        "primaryTypeDisplayName": {"text": category},
        "types": types or ["dentist"],
    }


class _FakeHttpClient:
    """Stands in for the scraper's long-lived httpx.Client."""

    def __init__(self, get_fn=None, post_fn=None):
        self._get_fn = get_fn
        self._post_fn = post_fn
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, **kwargs):
        self.get_calls.append({"url": url, "params": params})
        return self._get_fn(url, params)

    def post(self, url, headers=None, json=None, **kwargs):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return self._post_fn(url, headers, json)


def _scraper(monkeypatch, get_fn=None, post_fn=None, api_key="fake-key"):
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", api_key)
    # No pacing sleeps in tests — the type rotation would otherwise take
    # ~30 real seconds per scrape_area call.
    monkeypatch.setattr("scrapers.google_maps.asyncio.sleep", _no_sleep)
    scraper = GoogleMapsScraper()
    scraper.api_key = api_key
    scraper.client = _FakeHttpClient(get_fn=get_fn, post_fn=post_fn)
    return scraper


async def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------
# resolve_area
# ---------------------------------------------------------------

def test_resolve_area_returns_none_without_an_api_key(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("Geocoding must not be called without an API key")

    scraper = _scraper(monkeypatch, get_fn=_explode, api_key="")
    assert scraper.resolve_area("BKC") is None


def test_resolve_area_returns_none_for_a_blank_name(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("Geocoding must not be called for a blank area")

    scraper = _scraper(monkeypatch, get_fn=_explode)
    assert scraper.resolve_area("   ") is None


def test_resolve_area_maps_the_geocoding_viewport_onto_a_places_rectangle(monkeypatch):
    scraper = _scraper(monkeypatch, get_fn=lambda url, params: _json_response(_geocode_payload(), method="GET"))

    area = scraper.resolve_area("BKC, Mumbai")

    assert area is not None
    assert area["latitude"] == 19.0656
    assert area["longitude"] == 72.8679
    assert area["city"] == "Mumbai"
    assert area["query"] == "BKC, Mumbai"
    # Google's Geocoding names the corners northeast/southwest; the Places
    # API (New) names them high/low. This mapping is the whole point of the
    # method, so it is asserted exactly.
    assert area["rectangle"] == {
        "low": {"latitude": 19.0590, "longitude": 72.8600},
        "high": {"latitude": 19.0720, "longitude": 72.8750},
    }


def test_resolve_area_sends_the_typed_name_and_key_to_geocoding(monkeypatch):
    scraper = _scraper(monkeypatch, get_fn=lambda url, params: _json_response(_geocode_payload(), method="GET"))

    scraper.resolve_area("  Koramangala  ")

    call = scraper.client.get_calls[0]
    assert "maps/api/geocode/json" in call["url"]
    assert call["params"] == {"address": "Koramangala", "key": "fake-key"}


def test_resolve_area_returns_none_when_google_finds_nothing(monkeypatch):
    scraper = _scraper(
        monkeypatch,
        get_fn=lambda url, params: _json_response({"status": "ZERO_RESULTS", "results": []}, method="GET"),
    )
    assert scraper.resolve_area("asdkjhaskdjh") is None


def test_resolve_area_returns_none_when_the_geocoding_api_is_not_enabled(monkeypatch):
    # REQUEST_DENIED is what a Places-only key gets back, since Geocoding is
    # a separate API that has to be switched on in the Cloud console. It has
    # to degrade to "area not found", not crash the endpoint.
    scraper = _scraper(
        monkeypatch,
        get_fn=lambda url, params: _json_response(
            {"status": "REQUEST_DENIED", "error_message": "API not enabled"}, method="GET"
        ),
    )
    assert scraper.resolve_area("BKC") is None


def test_resolve_area_returns_none_on_a_network_error(monkeypatch):
    def _boom(url, params):
        raise httpx.ConnectError("no network")

    scraper = _scraper(monkeypatch, get_fn=_boom)
    assert scraper.resolve_area("BKC") is None


def test_resolve_area_falls_back_to_a_default_box_when_there_is_no_viewport(monkeypatch):
    payload = _geocode_payload()
    del payload["results"][0]["geometry"]["viewport"]
    scraper = _scraper(monkeypatch, get_fn=lambda url, params: _json_response(payload, method="GET"))

    area = scraper.resolve_area("BKC")

    assert area is not None
    assert area["rectangle"]["low"]["latitude"] == pytest.approx(19.0656 - 0.02)
    assert area["rectangle"]["high"]["latitude"] == pytest.approx(19.0656 + 0.02)


def test_resolve_area_falls_back_through_the_administrative_levels_for_a_city(monkeypatch):
    payload = _geocode_payload()
    # An area whose result carries no `locality` at all — the city name has
    # to come from the next level down rather than ending up blank, because
    # a blank city is what gets handed to Apollo.
    payload["results"][0]["address_components"] = [
        {"long_name": "Some Estate", "types": ["sublocality"]},
        {"long_name": "Pune District", "types": ["administrative_area_level_2"]},
    ]
    scraper = _scraper(monkeypatch, get_fn=lambda url, params: _json_response(payload, method="GET"))

    assert scraper.resolve_area("Some Estate")["city"] == "Pune District"


# ---------------------------------------------------------------
# scrape_area
# ---------------------------------------------------------------

def test_scrape_area_returns_nothing_for_an_area_with_no_box(monkeypatch):
    scraper = _scraper(monkeypatch)
    assert asyncio.run(scraper.scrape_area({}, limit=5)) == []
    assert asyncio.run(scraper.scrape_area({"name": "x"}, limit=5)) == []


def test_scrape_area_restricts_every_search_to_the_areas_rectangle(monkeypatch):
    rectangle = {
        "low": {"latitude": 19.0590, "longitude": 72.8600},
        "high": {"latitude": 19.0720, "longitude": 72.8750},
    }
    scraper = _scraper(
        monkeypatch,
        post_fn=lambda url, headers, json: _json_response({"places": [_place("A Clinic", "https://a.com")]}),
    )

    asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=1))

    body = scraper.client.post_calls[0]["json"]
    assert body["locationRestriction"] == {"rectangle": rectangle}
    # searchText, not searchNearby — a rectangle is only available on the
    # text endpoint, and it is a hard restriction rather than a ranking hint.
    assert scraper.client.post_calls[0]["url"].endswith(":searchText")


def test_scrape_area_queries_business_types_as_readable_text(monkeypatch):
    scraper = _scraper(
        monkeypatch,
        post_fn=lambda url, headers, json: _json_response({"places": []}),
    )
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=5))

    queries = [call["json"]["textQuery"] for call in scraper.client.post_calls]
    # The rotation holds API enum tokens ("beauty_salon"); searchText takes
    # free text, so underscores have to become spaces or the query is junk.
    assert "dentist" in queries
    assert "beauty salon" in queries
    assert not any("_" in q for q in queries)


def test_scrape_area_returns_the_category_so_niches_can_be_counted(monkeypatch):
    scraper = _scraper(
        monkeypatch,
        post_fn=lambda url, headers, json: _json_response(
            {"places": [_place("Smile Co", "https://smile.com", category="Dental clinic")]}
        ),
    )
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    leads = asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=1))

    assert len(leads) == 1
    assert leads[0]["Category"] == "Dental clinic"
    assert leads[0]["Source"] == "Google Maps (Area)"


def test_scrape_area_skips_non_business_places_and_websiteless_listings(monkeypatch):
    places = [
        _place("Bandra Station", "https://railways.gov.in", types=["train_station"]),
        _place("No Site Cafe", "", types=["cafe"]),
        _place("Real Salon", "https://realsalon.com", types=["beauty_salon"]),
    ]
    scraper = _scraper(monkeypatch, post_fn=lambda url, headers, json: _json_response({"places": places}))
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    leads = asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=10))

    assert [lead["Company"] for lead in leads] == ["Real Salon"]


def test_scrape_area_counts_a_business_once_even_if_several_types_match_it(monkeypatch):
    # The same domain coming back under "spa" and again under "beauty salon"
    # is one lead, not two — otherwise a limit of 25 fills with duplicates.
    scraper = _scraper(
        monkeypatch,
        post_fn=lambda url, headers, json: _json_response(
            {"places": [_place("Glow", "https://glow.com", types=["spa"])]}
        ),
    )
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    leads = asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=10))

    assert len(leads) == 1


def test_scrape_area_stops_once_the_limit_is_reached(monkeypatch):
    counter = {"n": 0}

    def _post(url, headers, json):
        counter["n"] += 1
        return _json_response({"places": [_place(f"Biz {counter['n']}", f"https://biz{counter['n']}.com")]})

    scraper = _scraper(monkeypatch, post_fn=_post)
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    leads = asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=3))

    assert len(leads) == 3
    # Each Places call is billable, so the rotation must stop rather than
    # run all ~38 types after the caller already has what they asked for.
    assert counter["n"] == 3


def test_scrape_area_survives_one_failing_search(monkeypatch):
    calls = {"n": 0}

    def _post(url, headers, json):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return _json_response({"places": [_place("Later Biz", "https://later.com")]})

    scraper = _scraper(monkeypatch, post_fn=_post)
    rectangle = {"low": {"latitude": 1.0, "longitude": 2.0}, "high": {"latitude": 3.0, "longitude": 4.0}}

    leads = asyncio.run(scraper.scrape_area({"name": "BKC", "rectangle": rectangle}, limit=1))

    assert [lead["Company"] for lead in leads] == ["Later Biz"]


# ---------------------------------------------------------------
# /api/search-area
# ---------------------------------------------------------------

def _client(monkeypatch, bucket):
    """
    A client whose requests carry their own X-API-Key.

    Auth is off (API_KEY is None), but /api/search-area's 5/60 rate
    limiter keys its buckets by that header — so without a distinct value
    per test, the sixth endpoint test in this file would 429 on a limiter
    the test is not trying to exercise.
    """
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return TestClient(app_module.app, raise_server_exceptions=False, headers={"X-API-Key": bucket})


def test_search_area_400s_when_the_maps_key_is_not_set(monkeypatch):
    client = _client(monkeypatch, "area-no-key")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "")

    res = client.post("/api/search-area", json={"area": "BKC"})

    assert res.status_code == 400
    assert "GOOGLE_MAPS_API_KEY" in res.json()["detail"]


def test_search_area_404s_when_the_area_cannot_be_placed(monkeypatch):
    client = _client(monkeypatch, "area-404")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "fake-key")
    monkeypatch.setattr(app_module.maps_scraper, "resolve_area", lambda name: None)

    res = client.post("/api/search-area", json={"area": "zzzzz"})

    assert res.status_code == 404
    assert "zzzzz" in res.json()["detail"]


def test_search_area_returns_leads_and_the_city_for_apollo(monkeypatch):
    client = _client(monkeypatch, "area-leads")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "fake-key")
    monkeypatch.setattr(
        app_module.maps_scraper,
        "resolve_area",
        lambda name: {
            "name": "Bandra Kurla Complex, Mumbai",
            "city": "Mumbai",
            "rectangle": {"low": {}, "high": {}},
        },
    )

    captured = {}

    async def _fake_scrape_area(area, limit):
        captured["area"] = area
        captured["limit"] = limit
        return [{"Company": "Acme", "Website": "https://acme.com", "Category": "Lawyer"}]

    monkeypatch.setattr(app_module.maps_scraper, "scrape_area", _fake_scrape_area)
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    res = client.post("/api/search-area", json={"area": "BKC", "limit": 40})

    assert res.status_code == 200
    data = res.json()
    assert data["leads"][0]["Category"] == "Lawyer"
    # Apollo can only be aimed at a city, never at the area itself, so the
    # resolved city has to travel back with the results.
    assert data["area"] == {"name": "Bandra Kurla Complex, Mumbai", "city": "Mumbai"}
    assert captured["limit"] == 40


def test_search_area_defaults_the_limit_to_25(monkeypatch):
    client = _client(monkeypatch, "area-default-limit")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "fake-key")
    monkeypatch.setattr(
        app_module.maps_scraper, "resolve_area",
        lambda name: {"name": "X", "city": "Y", "rectangle": {"low": {}, "high": {}}},
    )

    captured = {}

    async def _fake_scrape_area(area, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(app_module.maps_scraper, "scrape_area", _fake_scrape_area)
    monkeypatch.setattr(app_module, "save_leads_to_sheets_bg", lambda leads: None)

    assert client.post("/api/search-area", json={"area": "BKC"}).status_code == 200
    assert captured["limit"] == 25


def test_search_area_rejects_an_out_of_range_limit(monkeypatch):
    client = _client(monkeypatch, "area-bad-limit")
    monkeypatch.setattr(app_module.config, "GOOGLE_MAPS_API_KEY", "fake-key")

    assert client.post("/api/search-area", json={"area": "BKC", "limit": 500}).status_code == 422

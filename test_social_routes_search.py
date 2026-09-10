from fastapi.testclient import TestClient
import app as app_module


def _client(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_sync_search_attaches_handles(monkeypatch):
    async def fake_scrape(niche, city, limit=10, on_stage=None):
        return [{"Company": "Acme Candles", "Website": "https://acme.com", "Phone": "+91 98 000"}]

    async def fake_handles(url, client=None):
        return {"instagram": "https://instagram.com/acme", "youtube": "", "facebook": "", "linkedin": ""}

    monkeypatch.setattr(app_module.maps_scraper, "scrape_google_maps", fake_scrape)
    monkeypatch.setattr(app_module, "find_social_handles", fake_handles)
    client = _client(monkeypatch)

    r = client.post("/api/social/search", json={"niche": "candle shop", "city": "Mumbai", "limit": 1})
    assert r.status_code == 200, r.text
    biz = r.json()["businesses"]
    assert biz[0]["company"] == "Acme Candles"
    assert biz[0]["handles"]["instagram"] == "https://instagram.com/acme"


def test_async_search_start_handshake(monkeypatch):
    async def fake_scrape(niche, city, limit=10, on_stage=None):
        return [{"Company": "Acme", "Website": "https://acme.com", "Phone": ""}]

    async def fake_handles(url, client=None):
        return {"instagram": "", "youtube": "", "facebook": "", "linkedin": ""}

    monkeypatch.setattr(app_module.maps_scraper, "scrape_google_maps", fake_scrape)
    monkeypatch.setattr(app_module, "find_social_handles", fake_handles)

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app_module.config, "API_KEY", None)
        start = client.post("/api/social/search", json={"niche": "x", "city": "y", "async_mode": True})
        assert start.status_code == 200, start.text
        assert start.json()["started"] is True
        key = start.json()["key"]
        assert key
        import time
        result = None
        for _ in range(50):
            r = client.get("/api/social/search/result", params={"key": key}).json()
            if r.get("ready"):
                result = r
                break
            time.sleep(0.05)
        assert result is not None
        assert result["businesses"][0]["company"] == "Acme"


def test_routes_gated():
    import inspect
    for fn in (app_module.social_search, app_module.social_search_progress, app_module.social_search_result):
        src = inspect.getsource(fn)
        assert "require_api_key" in src and "rate_limit" in src

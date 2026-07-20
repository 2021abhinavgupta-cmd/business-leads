"""
Integration smoke test for the Lighthouse CLI runner. Hits a real website and
shells out to a real Lighthouse binary, so it's marked `integration` and
skips itself gracefully if no Lighthouse binary is reachable.
"""

import pytest

import analyzer.lighthouse as lighthouse_module
from analyzer.lighthouse import run_lighthouse

TEST_URL = "https://www.hitchki.co/"


@pytest.mark.integration
async def test_lighthouse_returns_scores():
    scores = await run_lighthouse(TEST_URL)
    if not scores:
        pytest.skip("Lighthouse CLI not available in this environment")

    for key in ("performance", "seo", "accessibility", "best_practices"):
        assert key in scores
        assert 0 <= scores[key] <= 100


async def test_run_lighthouse_averages_two_runs(monkeypatch):
    """
    run_lighthouse() now calls _run_lighthouse_once() twice and averages —
    same run-to-run-variance smoothing as WebsiteScraper._pagespeed. Mocked
    here so it doesn't need a real Lighthouse binary to lock in the
    averaging math itself.
    """
    calls = iter([
        {"performance": 40, "seo": 80, "accessibility": 90, "best_practices": 70, "lcp_ms": 3000, "cls": 0.20, "tbt_ms": 300},
        {"performance": 60, "seo": 80, "accessibility": 90, "best_practices": 70, "lcp_ms": 5000, "cls": 0.10, "tbt_ms": 500},
    ])

    async def _fake_once(url):
        return next(calls)

    monkeypatch.setattr(lighthouse_module, "_run_lighthouse_once", _fake_once)
    result = await run_lighthouse(TEST_URL)

    assert result["performance"] == 50  # (40 + 60) / 2
    assert result["lcp_ms"] == 4000      # (3000 + 5000) / 2
    assert result["cls"] == 0.15         # (0.20 + 0.10) / 2


async def test_run_lighthouse_falls_back_to_single_run_if_one_fails(monkeypatch):
    calls = iter([
        {"performance": 55, "seo": 80, "accessibility": 90, "best_practices": 70},
        {},
    ])

    async def _fake_once(url):
        return next(calls)

    monkeypatch.setattr(lighthouse_module, "_run_lighthouse_once", _fake_once)
    result = await run_lighthouse(TEST_URL)

    assert result["performance"] == 55

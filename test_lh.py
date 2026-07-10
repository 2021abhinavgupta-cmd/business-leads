"""
Integration smoke test for the Lighthouse CLI runner. Hits a real website and
shells out to a real Lighthouse binary, so it's marked `integration` and
skips itself gracefully if no Lighthouse binary is reachable.
"""

import pytest

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

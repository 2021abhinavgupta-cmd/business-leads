"""
Unit test for analyzer/visuals.py's _get_real_web_vitals — no browser, no
network. Fakes the Playwright `page.evaluate()` call it wraps.
"""

from analyzer.visuals import _get_real_web_vitals


class _FakePage:
    def __init__(self, return_value):
        self._return_value = return_value

    async def evaluate(self, script):
        return self._return_value


async def test_real_web_vitals_rounds_values():
    page = _FakePage({"lcp": 2456.789, "clsSum": 0.12345, "tbtMs": 199.4})
    result = await _get_real_web_vitals(page)
    assert result == {"lcp_ms": 2457, "cls": 0.123, "tbt_ms": 199}


async def test_real_web_vitals_missing_observers_returns_empty_dict():
    page = _FakePage(None)
    assert await _get_real_web_vitals(page) == {}


async def test_real_web_vitals_partial_data():
    # LCP observer never fired (e.g. page had no image/text block) but CLS did.
    page = _FakePage({"lcp": None, "clsSum": 0.05, "tbtMs": 0})
    result = await _get_real_web_vitals(page)
    assert result["lcp_ms"] is None
    assert result["cls"] == 0.05
    assert result["tbt_ms"] == 0


async def test_real_web_vitals_evaluate_exception_degrades_gracefully():
    class _BrokenPage:
        async def evaluate(self, script):
            raise RuntimeError("page closed")

    assert await _get_real_web_vitals(_BrokenPage()) == {}

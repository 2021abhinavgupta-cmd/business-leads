"""
Tests for three signal-coverage bugs found by tracing a "Partial coverage"
banner back to its causes (added 2026-08-10).

The banner on a real lead named five signals at once. They turned out to be
three unrelated problems wearing one label, which is exactly why the banner
was hard to act on:

  - lighthouse / html_validate / pa11y are Node CLIs that cannot be exec'd on
    native Windows. Expected locally, fine in Docker. But only Lighthouse
    LOGGED it — the other two returned an empty result in total silence, and
    an empty result is what a clean page produces too.
  - pyseoanalyzer was getting HTTP 406 from the site's mod_security, because
    it ships a PoolManager built with the truncated User-Agent "Mozilla/5.0"
    and nothing else. Environment-independent: it fails on Railway too.
    Verified on yogawithrajani.com — bare UA returns 406 and 226 bytes, a
    full Chrome UA returns 200 and 39,419 bytes from the same URL.
  - crux_field_data can never succeed for these leads. Google publishes CrUX
    only for origins with enough real Chrome traffic; a neighbourhood yoga
    studio has nowhere near it. Recording that as a failed signal made
    partial_coverage non-empty on virtually every lead, which silently
    disabled should_contact()'s skip-if-healthy rule and made the banner
    permanent.

Unit tests only — no network, no Node, no browser.
"""

import inspect

import pytest

from analyzer.ai_audit import AIAuditor
from scrapers.website import WebsiteScraper


# ---------------------------------------------------------------------------
# CrUX: absent is not failed
# ---------------------------------------------------------------------------

def test_absent_crux_does_not_count_as_degraded_coverage():
    """
    The distinction that matters: "the source has no data for this site and
    never will" vs "we tried to measure and could not".
    """
    signal_status = {"lighthouse": "ok", "crux_field_data": "unavailable"}
    degraded = [n for n, s in signal_status.items() if s not in ("ok", "unavailable")]
    assert degraded == []


def test_the_degraded_filter_excludes_unavailable():
    source = inspect.getsource(AIAuditor.analyze_lead)
    assert 'state not in ("ok", "unavailable")' in source, (
        "partial_coverage must not count sources that have no data to give"
    )


def test_crux_is_recorded_as_unavailable_not_no_data():
    source = inspect.getsource(WebsiteScraper.audit_website)
    assert 'signal_status["crux_field_data"] = "ok" if crux else "unavailable"' in source


def test_a_genuinely_failed_signal_still_counts():
    """The fix must not blunt real coverage loss."""
    signal_status = {"lighthouse": "no_data", "crux_field_data": "unavailable"}
    degraded = [n for n, s in signal_status.items() if s not in ("ok", "unavailable")]
    assert degraded == ["lighthouse"]


def test_a_healthy_fully_covered_lead_is_still_skipped():
    """
    This is what the bug broke. With crux permanently counted as degraded,
    partial_coverage was never empty, so this branch was unreachable and
    every healthy lead got contacted regardless of score.
    """
    auditor = AIAuditor.__new__(AIAuditor)
    assert auditor.should_contact({"overall_score": 88, "partial_coverage": []}) is False


def test_a_genuinely_partial_audit_still_bypasses_the_skip():
    auditor = AIAuditor.__new__(AIAuditor)
    assert auditor.should_contact({"overall_score": 88, "partial_coverage": ["lighthouse"]}) is True


# ---------------------------------------------------------------------------
# pyseoanalyzer: the bot block
# ---------------------------------------------------------------------------

def test_pyseoanalyzer_gets_a_full_browser_user_agent():
    """
    The shipped default is the truncated "Mozilla/5.0", which mod_security —
    which fronts a large share of the shared hosting these leads sit on —
    answers with 406 Not Acceptable.
    """
    source = inspect.getsource(WebsiteScraper._patch_pyseoanalyzer_headers)
    assert "Chrome/120" in source
    assert "Accept-Language" in source


def test_the_patch_is_applied_before_every_analyze_call():
    source = inspect.getsource(WebsiteScraper._run_pyseoanalyzer_sync)
    assert source.index("_patch_pyseoanalyzer_headers") < source.index("seo_analyze(")


def test_the_patch_actually_replaces_the_pool_manager(monkeypatch):
    from pyseoanalyzer import http as seo_http

    original = seo_http.http.http
    try:
        WebsiteScraper._patch_pyseoanalyzer_headers()
        headers = seo_http.http.http.headers
        assert "Chrome/120" in headers["User-Agent"]
        assert headers["User-Agent"] != "Mozilla/5.0"
    finally:
        seo_http.http.http = original


def test_a_patch_failure_does_not_break_the_audit(monkeypatch, capsys):
    """Losing the patch means the old behaviour, not a dead pipeline."""
    import builtins

    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name == "pyseoanalyzer.http" or name == "pyseoanalyzer":
            raise ImportError("restructured upstream")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _explode)
    WebsiteScraper._patch_pyseoanalyzer_headers()  # must not raise
    assert "Could not patch request headers" in capsys.readouterr().out


def test_zero_pages_is_logged_rather_than_returned_silently():
    """
    pyseoanalyzer reports a bot-block as zero pages with an EMPTY errors
    list, so the caller's `except` never fires. Without a log line the audit
    just quietly loses its SEO crawler — and since compute_score subtracts
    per detected flaw, losing a source RAISES the reported SEO score.
    """
    source = inspect.getsource(WebsiteScraper._run_pyseoanalyzer_sync)
    assert "Returned no pages" in source
    assert "if not pages:" in source


# ---------------------------------------------------------------------------
# Node CLI failures: logged, not swallowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name, function_name", [
    ("analyzer.html_validate", "_run_sync"),
    ("analyzer.pa11y_check", "_run_sync"),
])
def test_the_npx_fallback_failure_is_logged(module_name, function_name):
    """
    Both returned their empty value from the npx fallback in total silence.
    An empty result is exactly what a clean page produces, so a silent return
    is indistinguishable from "nothing wrong here" — and it is the reason a
    Partial coverage banner could not be acted on. Lighthouse always logged
    its equivalent failure; these two did not.
    """
    module = __import__(module_name, fromlist=[function_name])
    source = inspect.getsource(getattr(module, function_name))

    fallback = source[source.index("npx"):]
    assert "print(" in fallback, f"{module_name} still fails silently"


@pytest.mark.parametrize("module_name", ["analyzer.html_validate", "analyzer.pa11y_check"])
def test_the_local_cli_failure_is_also_logged(module_name):
    """
    On Windows the local .bin shim raises WinError 193 before npx is tried;
    logging both steps is what makes the two-stage fallback legible.
    """
    module = __import__(module_name, fromlist=["_run_sync"])
    source = inspect.getsource(module._run_sync)
    assert "Local CLI unavailable" in source


def test_the_windows_shim_failure_is_still_caught_as_oserror():
    """
    WinError 193 arrives as OSError, not FileNotFoundError — a narrower
    except would skip the npx fallback entirely on Windows.
    """
    for module_name in ("analyzer.html_validate", "analyzer.pa11y_check"):
        module = __import__(module_name, fromlist=["_run_sync"])
        source = inspect.getsource(module._run_sync)
        assert "except (OSError, subprocess.TimeoutExpired)" in source

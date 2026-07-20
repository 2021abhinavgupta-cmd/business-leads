"""
Pa11y cross-check — a second, independent accessibility engine
(https://github.com/pa11y/pa11y, HTML_CodeSniffer under the hood) run
alongside axe-core (analyzer/visuals.py). Different rule engine catches some
things axe-core's ruleset doesn't and vice versa, so this is reported as a
supplementary signal rather than claimed as "confirmed by two engines" — the
two tools use different rule-code taxonomies with no reliable 1:1 mapping,
so honestly reporting them as separate findings beats a false-confidence
dedup that might match the wrong things.

Runs sequentially, not concurrently, with Playwright's own browser —
audit_website() always runs after generate_audit_screenshot() has already
closed its Chromium instance (see app.py), so there's never two headless
browsers alive at once on Railway's 500MB instances. Homepage only (Pa11y
launches its own browser per call, so keeping it to one page bounds cost).
"""

import asyncio
import json
import os
import subprocess

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def run_pa11y(url: str) -> list[dict]:
    """
    Run Pa11y against *url* and return up to 10 error-level issues as
    {"code", "message", "selector"} dicts. Returns [] on any failure/
    timeout/missing install — matches every other optional check in this
    codebase (Wappalyzer, Crawl4AI, pyseoanalyzer).
    """
    return await asyncio.to_thread(_run_sync, url)


def _run_sync(url: str) -> list[dict]:
    binary = os.path.join(_BASE_DIR, "node_modules", ".bin", "pa11y")
    try:
        result = subprocess.run(
            [binary, "--reporter", "json", "--timeout", "20000", url],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # See analyzer/html_validate.py's matching comment — OSError (not
        # just FileNotFoundError) so the Windows "WinError 193" shim-exec
        # failure falls through to npx instead of being silently swallowed.
        try:
            result = subprocess.run(
                ["npx", "--yes", "pa11y", "--reporter", "json", "--timeout", "20000", url],
                capture_output=True, text=True, timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        except Exception as e:
            print(f"[Pa11y] npx fallback failed: {e}")
            return []
    except Exception as e:
        print(f"[Pa11y] Failed: {e}")
        return []

    if not result.stdout.strip():
        return []

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    return [
        {
            "code": issue.get("code", ""),
            "message": issue.get("message", ""),
            "selector": issue.get("selector", ""),
        }
        for issue in issues
        if issue.get("type") == "error"
    ][:10]

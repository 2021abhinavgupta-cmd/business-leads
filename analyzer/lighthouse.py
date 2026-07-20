"""
Lighthouse Runner — runs Google Lighthouse via Node.js CLI for accurate
performance, SEO, accessibility, and best practices scores.

Falls back to PageSpeed Insights API if Lighthouse CLI is not available.
"""

import asyncio
import json
import subprocess
import tempfile
import os


async def run_lighthouse(url: str) -> dict:
    """
    Run Lighthouse CLI on *url* TWICE and average the numeric results, then
    return structured scores.

    Lighthouse has real run-to-run variance from the same lab-simulation
    noise as PageSpeed Insights (see WebsiteScraper._pagespeed's docstring
    for a live-observed example on that API) — a single run can catch a
    fluke and an email still quotes it as a precise, checkable fact. Runs
    SEQUENTIALLY, not concurrently — each run launches its own headless
    Chrome via chrome-launcher, and Railway's 500MB instances already run
    Playwright under a hard concurrency-1 semaphore (see analyzer/visuals.py)
    specifically to avoid OOM from two Chrome processes alive at once; this
    trades extra wall-clock time for the same memory safety instead.

    Returns:
        Dict with keys: performance, seo, accessibility, best_practices (each 0-100)
        Returns empty dict on failure.
    """
    first = await _run_lighthouse_once(url)
    second = await _run_lighthouse_once(url)

    if not first:
        return second
    if not second:
        return first

    averaged: dict = {}
    for key in set(first) | set(second):
        v1, v2 = first.get(key), second.get(key)
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            averaged[key] = (v1 + v2) / 2
        else:
            averaged[key] = v1 if v1 is not None else v2

    for key in ("performance", "seo", "accessibility", "best_practices", "lcp_ms", "tbt_ms"):
        if averaged.get(key) is not None:
            averaged[key] = round(averaged[key])
    if averaged.get("cls") is not None:
        averaged["cls"] = round(averaged["cls"], 3)

    print(f"[Lighthouse] Averaged scores from 2 runs: {averaged}")
    return averaged


async def _run_lighthouse_once(url: str) -> dict:
    """One Lighthouse pass: local CLI (installed via package.json) first, npx as fallback."""
    # Both helpers shell out via blocking subprocess.run, so run them in a
    # worker thread — otherwise a 120s Lighthouse run blocks the event loop.
    scores = await asyncio.to_thread(_run_lighthouse_cli, url)
    if scores:
        return scores

    scores = await asyncio.to_thread(_run_lighthouse_npx, url)
    if scores:
        return scores

    print("[Lighthouse] CLI not available, skipping.")
    return {}


def _extract_core_web_vitals(audits: dict) -> dict:
    """
    Pull the raw Core Web Vitals numbers (not just the 0-100 category score)
    out of Lighthouse's audits object, so flaw text can quote real units
    ("LCP takes 4.2s") instead of just an opaque score. Keys map 1:1 to what
    scrapers/website.py._build_flaws expects; any missing/malformed audit
    just yields None for that key rather than failing the whole parse.
    """
    def _numeric(audit_id):
        value = audits.get(audit_id, {}).get("numericValue")
        return value if isinstance(value, (int, float)) else None

    lcp = _numeric("largest-contentful-paint")
    cls = _numeric("cumulative-layout-shift")
    tbt = _numeric("total-blocking-time")

    return {
        "lcp_ms": round(lcp) if lcp is not None else None,
        "cls": round(cls, 3) if cls is not None else None,
        "tbt_ms": round(tbt) if tbt is not None else None,
    }


def _run_lighthouse_cli(url: str) -> dict:
    """Run lighthouse from node_modules."""
    try:
        # Check common paths for the lighthouse binary
        possible_paths = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules", ".bin", "lighthouse"),
            "lighthouse",  # Global install
        ]
        
        lighthouse_bin = None
        for path in possible_paths:
            try:
                result = subprocess.run(
                    [path, "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    lighthouse_bin = path
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        
        if not lighthouse_bin:
            return {}
        
        return _execute_lighthouse(lighthouse_bin, url)
        
    except Exception as e:
        print(f"[Lighthouse] CLI error: {e}")
        return {}


def _run_lighthouse_npx(url: str) -> dict:
    """Run lighthouse via npx as fallback."""
    try:
        result = subprocess.run(
            ["npx", "--yes", "lighthouse", "--version"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return _execute_lighthouse("npx", url, use_npx=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {}


def _execute_lighthouse(binary: str, url: str, use_npx: bool = False) -> dict:
    """Execute lighthouse and parse the JSON output."""
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            output_path = tmp.name

        cmd = []
        if use_npx:
            cmd = ["npx", "--yes", "lighthouse"]
        else:
            cmd = [binary]
        
        cmd.extend([
            url,
            "--output=json",
            f"--output-path={output_path}",
            "--chrome-flags=--headless --no-sandbox --disable-gpu --disable-dev-shm-usage",
            "--only-categories=performance,seo,accessibility,best-practices",
            "--quiet",
        ])
        
        print(f"[Lighthouse] Running audit for {url}...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120  # 120 second timeout for 100% accuracy on slow sites
        )
        
        if os.path.exists(output_path):
            with open(output_path, "r") as f:
                data = json.load(f)

            os.unlink(output_path)  # Clean up

            result = data.get("lighthouseResult", data)
            categories = result.get("categories", {})

            scores = {
                "performance": int((categories.get("performance", {}).get("score") or 0) * 100),
                "seo": int((categories.get("seo", {}).get("score") or 0) * 100),
                "accessibility": int((categories.get("accessibility", {}).get("score") or 0) * 100),
                "best_practices": int((categories.get("best-practices", {}).get("score") or 0) * 100),
                **_extract_core_web_vitals(result.get("audits", {})),
            }

            print(f"[Lighthouse] Scores: {scores}")
            return scores
        
        print(f"[Lighthouse] No output file generated. stderr: {result.stderr[:200]}")
        return {}
        
    except subprocess.TimeoutExpired:
        print("[Lighthouse] Timed out after 60 seconds.")
        return {}
    except Exception as e:
        print(f"[Lighthouse] Execution error: {e}")
        return {}
    finally:
        # Clean up temp file if it exists
        try:
            if output_path and os.path.exists(output_path):
                os.unlink(output_path)
        except OSError:
            pass

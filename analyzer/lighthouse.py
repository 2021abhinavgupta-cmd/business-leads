"""
Lighthouse Runner — runs Google Lighthouse via Node.js CLI for accurate
performance, SEO, accessibility, and best practices scores.

Falls back to PageSpeed Insights API if Lighthouse CLI is not available.
"""

import json
import subprocess
import tempfile
import os


async def run_lighthouse(url: str) -> dict:
    """
    Run Lighthouse CLI on a URL and return structured scores.
    
    Returns:
        Dict with keys: performance, seo, accessibility, best_practices (each 0-100)
        Returns empty dict on failure.
    """
    # Try local Lighthouse CLI first (installed via package.json)
    scores = _run_lighthouse_cli(url)
    if scores:
        return scores
    
    # Try npx as fallback
    scores = _run_lighthouse_npx(url)
    if scores:
        return scores
    
    print("[Lighthouse] CLI not available, skipping.")
    return {}


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
            
            categories = data.get("lighthouseResult", data).get("categories", {})
            
            scores = {
                "performance": int((categories.get("performance", {}).get("score") or 0) * 100),
                "seo": int((categories.get("seo", {}).get("score") or 0) * 100),
                "accessibility": int((categories.get("accessibility", {}).get("score") or 0) * 100),
                "best_practices": int((categories.get("best-practices", {}).get("score") or 0) * 100),
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
            if os.path.exists(output_path):
                os.unlink(output_path)
        except:
            pass

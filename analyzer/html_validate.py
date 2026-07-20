"""
HTML Validator — runs html-validate (https://html-validate.org/) via Node.js
CLI against already-fetched HTML, giving a concrete, checkable "your HTML is
invalid" signal that wasn't previously checked at all. Same subprocess-via-
Node pattern as analyzer/lighthouse.py: local node_modules/.bin first, npx as
a fallback, degrades to an empty result on any failure so a missing/broken
Node install never sinks the whole audit.
"""

import asyncio
import json
import os
import subprocess
import tempfile

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def run_html_validate(html: str) -> dict:
    """
    Validate *html* and return {"error_count": int, "messages": [str, ...]}
    (up to 5 distinct rule violations, deduplicated by rule id). Returns {}
    on any failure — timeout, Node/CLI unavailable, malformed output — so a
    missing local install just means this one signal is skipped, same as
    every other optional check in this codebase.
    """
    return await asyncio.to_thread(_run_sync, html)


def _run_sync(html: str) -> dict:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_path = f.name

        binary = os.path.join(_BASE_DIR, "node_modules", ".bin", "html-validate")
        try:
            result = subprocess.run(
                [binary, "--formatter", "json", tmp_path],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            # OSError (not just FileNotFoundError) — on Windows, executing
            # the node_modules/.bin shim directly raises "WinError 193: %1
            # is not a valid Win32 application" rather than a not-found
            # error, since it's a Unix shebang script; a narrower except
            # here would silently skip this signal entirely on Windows dev
            # machines instead of falling through to npx. Same underlying
            # Windows/Linux parity gotcha already documented for Lighthouse
            # in CLAUDE.md — works fine in the Docker/Linux deploy target.
            try:
                result = subprocess.run(
                    ["npx", "--yes", "html-validate", "--formatter", "json", tmp_path],
                    capture_output=True, text=True, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {}

        # html-validate exits non-zero when it finds errors — expected,
        # not itself a failure. Only stdout parsing matters here.
        if not result.stdout.strip():
            return {}

        data = json.loads(result.stdout)
        messages = []
        seen_rules = set()
        error_count = 0
        for file_result in data:
            for msg in file_result.get("messages", []):
                if msg.get("severity") != 2:  # 2 = error, 1 = warning
                    continue
                error_count += 1
                rule_id = msg.get("ruleId", "")
                if rule_id and rule_id not in seen_rules:
                    seen_rules.add(rule_id)
                    messages.append(f"{msg.get('message', '')} ({rule_id})")

        return {"error_count": error_count, "messages": messages[:5]}
    except Exception as e:
        print(f"[HTMLValidate] Failed: {e}")
        return {}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

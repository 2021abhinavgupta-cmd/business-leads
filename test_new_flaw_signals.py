"""
Unit tests for analyzer/html_validate.py and analyzer/pa11y_check.py — no
network, no Node/CLI install required. subprocess.run is monkeypatched with
canned real-shaped output (captured live from both CLIs against real HTML/
URLs) so the JSON-parsing logic is locked in independent of whether Node
tooling is actually installed/executable in whatever environment runs the
suite (see CLAUDE.md §8 for the Windows/Docker parity gotcha this sidesteps
— same one Lighthouse already has).
"""

import subprocess
from types import SimpleNamespace

from analyzer.html_validate import _run_sync as html_validate_run_sync
from analyzer.pa11y_check import _run_sync as pa11y_run_sync

# Real html-validate --formatter json output, captured live against
# <!DOCTYPE html><html><body><img src="x.jpg"><div><span></div></body></html>
_HTML_VALIDATE_OUTPUT = """[{"filePath":"x.html","messages":[
{"ruleId":"element-required-attributes","severity":2,"message":"<html> is missing required \\"lang\\" attribute"},
{"ruleId":"wcag/h37","severity":2,"message":"<img> is missing required \\"alt\\" attribute"},
{"ruleId":"close-order","severity":1,"message":"Just a warning, not an error"}
],"errorCount":2,"warningCount":1}]"""

# Real pa11y --reporter json output shape, captured live against a real site.
_PA11Y_OUTPUT = """[
{"code":"WCAG2AA.Principle1.Guideline1_3.1_3_1.F92,ARIA4","type":"error","message":"This element's role is presentation but contains child elements.","selector":"svg.icon"},
{"code":"WCAG2AA.Something","type":"notice","message":"Just a notice, not an error","selector":"div"}
]"""


def _fake_run(stdout, returncode=1):
    def _run(cmd, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return _run


def test_html_validate_parses_error_count_and_dedupes_by_rule(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(_HTML_VALIDATE_OUTPUT))
    result = html_validate_run_sync("<html></html>")
    assert result["error_count"] == 2
    assert len(result["messages"]) == 2
    assert any("lang" in m for m in result["messages"])
    assert any("alt" in m for m in result["messages"])
    # The severity-1 warning must not be counted as an error.
    assert not any("Just a warning" in m for m in result["messages"])


def test_html_validate_empty_stdout_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    assert html_validate_run_sync("<html></html>") == {}


def test_html_validate_falls_back_to_npx_on_oserror(monkeypatch):
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] != "npx":
            raise OSError("WinError 193: %1 is not a valid Win32 application")
        return SimpleNamespace(stdout=_HTML_VALIDATE_OUTPUT, stderr="", returncode=1)

    monkeypatch.setattr(subprocess, "run", _run)
    result = html_validate_run_sync("<html></html>")
    assert result["error_count"] == 2
    assert calls[0][0] != "npx"
    assert calls[1][0] == "npx"


def test_pa11y_filters_to_error_type_only(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(_PA11Y_OUTPUT))
    result = pa11y_run_sync("https://example.com")
    assert len(result) == 1
    assert result[0]["message"].startswith("This element's role")
    assert not any("Just a notice" in i["message"] for i in result)


def test_pa11y_empty_stdout_returns_empty_list(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    assert pa11y_run_sync("https://example.com") == []


def test_pa11y_malformed_json_degrades_to_empty_list(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("not json"))
    assert pa11y_run_sync("https://example.com") == []

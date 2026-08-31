"""
Tests for two defects found by getting the suite green (2026-08-31).

Both are about the same thing: something that only worked because a
particular machine happened to be configured a particular way.

1. `import app` performed a live, credentialed Google Sheets call, because
   `app.py` builds `SheetsStorage()` at module scope and its __init__
   authenticated and opened the spreadsheet. CLAUDE.md §8 already documented
   the confusing symptom (an unrelated environment problem surfacing as
   scattered failures across several test files rather than one Sheets
   error). What had NOT been noticed: GitHub Actions configures no secrets,
   so GOOGLE_SHEETS_ID is unset there and every `import app` test had been
   erroring on every push, while §6 claimed they "run for real" in CI. 39
   tests were unreachable outside a fully-credentialed machine.

2. `reply_to` was frozen in BaseSender.__init__, so reassigning `from_email`
   afterwards silently left Reply-To and the unsubscribe mailto pointing at
   the old address — and with FROM_EMAIL unset entirely, every message
   shipped a literal `<mailto:None?subject=Unsubscribe>`: a header that
   parses, looks present to a spam filter, and goes nowhere.

Unit tests only — no network, no credentials, which is precisely the point.
"""

import importlib

import pytest

import config
from emailer.gmail_sender import GmailSender
from emailer.ses_sender import SESSender
from storage.sheets import SheetsStorage


# ---------------------------------------------------------------------------
# 1. Importing the app must not require Google Sheets
# ---------------------------------------------------------------------------

def test_constructing_the_crm_handle_touches_no_network(monkeypatch):
    """
    The constructor used to authenticate and open the spreadsheet. Nothing
    about holding a handle requires either.
    """
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "")
    storage = SheetsStorage()
    assert storage._sheet is None
    assert storage._gc is None


def test_constructing_the_crm_handle_without_config_does_not_raise(monkeypatch):
    """
    A missing legacy-CRM setting must not take the whole web service down.
    Audits and drafts do not need Sheets at all — every write to it is a
    background task already wrapped in try/except.
    """
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "")
    SheetsStorage()  # must not raise


def test_a_missing_sheet_id_still_warns_loudly(monkeypatch, capsys):
    """
    Failing soft must not mean failing silently. Same convention app.py
    already uses for a missing API_KEY.
    """
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "")
    SheetsStorage()
    assert "GOOGLE_SHEETS_ID is not set" in capsys.readouterr().out


def test_actually_using_the_sheet_still_raises_the_same_clear_error(monkeypatch):
    """
    The error did not disappear, it moved. A genuinely misconfigured
    deployment gets the identical message the moment something needs the CRM.
    """
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "")
    storage = SheetsStorage()
    with pytest.raises(ValueError, match="GOOGLE_SHEETS_ID"):
        _ = storage.sheet


def test_the_connection_is_made_once_and_cached(monkeypatch):
    """
    Lazy must not mean per-call: every public method reads self.sheet, so a
    property that reconnected each time would authenticate on every CRM
    operation.
    """
    monkeypatch.setattr(config, "GOOGLE_SHEETS_ID", "sheet-id")
    storage = SheetsStorage()
    calls = []

    def _fake_connect():
        calls.append(1)
        storage._sheet = object()

    storage._connect = _fake_connect
    first = storage.sheet
    second = storage.sheet
    assert first is second
    assert len(calls) == 1


def test_importing_app_connects_to_nothing():
    """
    The regression test for the CI breakage. If this fails, `import app` has
    regained a live dependency and roughly 40 tests stop running anywhere
    without production credentials.
    """
    app = importlib.import_module("app")
    assert app.sheets._sheet is None, "importing app must not open the spreadsheet"


# ---------------------------------------------------------------------------
# Importing a legacy smoke script must not run it
# ---------------------------------------------------------------------------

# The six manual scripts CLAUDE.md §6 documents as "run via python <file>.py".
# They define no test_* function, but pytest still IMPORTS every test_*.py at
# the repo root during collection.
_LEGACY_SMOKE_SCRIPTS = (
    "test_crawl.py", "test_ddg.py", "test_google.py",
    "test_maps.py", "test_playwright_maps.py", "test_ps.py",
)


@pytest.mark.parametrize("script", _LEGACY_SMOKE_SCRIPTS)
def test_a_legacy_smoke_script_does_nothing_when_merely_imported(script):
    """
    Four of these six had unguarded module-level bodies, so every CI push
    fired live DuckDuckGo and Google searches during collection — and
    test_playwright_maps.py launched Chromium and live-scraped Google Maps
    from GitHub's IP ranges, the exact ToS/ban risk CLAUDE.md §8 documents.
    All for zero collected tests. It was also the one script with no
    top-level try/except, so an upstream failure raised during collection and
    failed the whole run.

    Checked structurally: every statement that actually DOES something must
    sit under `if __name__ == "__main__":`. Imports, function/class
    definitions and constants are fine at module level.
    """
    import ast
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    tree = ast.parse(open(path, encoding="utf-8").read())

    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
               ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr, ast.If)
    for node in tree.body:
        assert isinstance(node, allowed), f"{script}: unguarded {type(node).__name__} at module level"
        # A bare expression statement at module level is a call like
        # `asyncio.run(run())` unless it is just a docstring.
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant), (
                f"{script}: unguarded call at module level — wrap it in "
                'if __name__ == "__main__": so pytest can import without running it'
            )
        # A try/except doing real work also counts as running on import; those
        # were the DuckDuckGo and Google cases.
        if isinstance(node, ast.If):
            test = node.test
            is_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            )
            assert is_main_guard, f"{script}: module-level `if` that is not the __main__ guard"

    assert not any(isinstance(n, ast.Try) for n in tree.body), (
        f"{script}: module-level try/except runs on import — move it under the __main__ guard"
    )


# ---------------------------------------------------------------------------
# 2. Reply-To and the unsubscribe header must never be wrong
# ---------------------------------------------------------------------------

def test_reply_to_follows_a_reassigned_sending_address(monkeypatch):
    """
    Frozen in __init__, this desynchronised the moment anything reassigned
    from_email: From showed the new address while Reply-To and the
    unsubscribe mailto kept the old one. A Reply-To that disagrees with From
    is a phishing pattern to a spam filter.
    """
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    sender = SESSender()
    sender.from_email = "someone@example.com"
    assert sender.reply_to == "someone@example.com"


def test_an_explicit_reply_to_still_wins(monkeypatch):
    """The override must keep overriding — this is why the setting exists."""
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "replies@example.com")
    sender = SESSender()
    sender.from_email = "someone@example.com"
    assert sender.reply_to == "replies@example.com"


def test_the_unsubscribe_header_never_contains_the_string_none(monkeypatch):
    """
    The live defect: with FROM_EMAIL and REPLY_TO_EMAIL both unset this
    interpolated Python's None into a real outgoing header.
    """
    monkeypatch.setattr(config, "FROM_EMAIL", None)
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")

    sender = SESSender()
    sender.from_email = None
    msg = sender._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    assert "None" not in (msg["List-Unsubscribe"] or "")
    assert msg["Reply-To"] is None


def test_a_broken_unsubscribe_header_is_omitted_rather_than_shipped(monkeypatch):
    """
    A malformed opt-out mechanism is worse than an absent one: it parses,
    looks present to a filter, and goes nowhere.
    """
    monkeypatch.setattr(config, "FROM_EMAIL", None)
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    monkeypatch.setattr(config, "APP_BASE_URL", "")

    sender = SESSender()
    sender.from_email = None
    msg = sender._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    assert msg["List-Unsubscribe"] is None


def test_one_click_post_never_appears_without_the_header_it_qualifies(monkeypatch):
    """
    RFC 8058: List-Unsubscribe-Post only means anything alongside
    List-Unsubscribe. One without the other is malformed.
    """
    for base_url in ("", "https://app.example.com"):
        monkeypatch.setattr(config, "FROM_EMAIL", None)
        monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
        monkeypatch.setattr(config, "APP_BASE_URL", base_url)
        sender = SESSender()
        sender.from_email = None
        msg = sender._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
        if msg["List-Unsubscribe-Post"]:
            assert msg["List-Unsubscribe"], "one-click flag with no unsubscribe target"


def test_a_normal_send_is_completely_unaffected(monkeypatch):
    """
    The guards must not have changed the ordinary, correctly-configured path
    — this is what every real send looks like.
    """
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    sender = SESSender()
    sender.from_email = "outreach@example.com"

    msg = sender._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    assert msg["Reply-To"] == "outreach@example.com"
    assert "<mailto:outreach@example.com?subject=Unsubscribe>" in msg["List-Unsubscribe"]
    assert "https://app.example.com/unsubscribe" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_the_followup_message_gets_the_same_treatment(monkeypatch):
    """
    Two builders set Reply-To; a guard on only one of them would leave the
    follow-up path emitting the bad header.
    """
    monkeypatch.setattr(config, "FROM_EMAIL", None)
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    sender = SESSender()
    sender.from_email = None
    msg = sender._build_followup_message("lead@example.com", "Re: S", "B", "<orig@x.com>")
    assert msg["Reply-To"] is None
    assert "None" not in (msg["List-Unsubscribe"] or "")


def test_both_transports_still_agree_after_the_change(monkeypatch):
    """
    The invariant test_gmail_sender.py exists to protect: if the two
    transports build different messages, one quietly stops carrying the
    headers Gmail's bulk-sender rules require.
    """
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", None)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "pw")

    ses = SESSender()
    ses.from_email = "outreach@example.com"
    gmail = GmailSender()

    ses_msg = ses._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    gm_msg = gmail._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    assert ses_msg["List-Unsubscribe"] == gm_msg["List-Unsubscribe"]
    assert ses_msg["Reply-To"] == gm_msg["Reply-To"]

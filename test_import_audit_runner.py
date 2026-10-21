"""Tests for importer/audit_runner.py. No browser, no network: capture and judge are fakes."""
import asyncio

import pytest

from importer import audit_runner
from import_test_support import make_row, use_temp_db
from storage import import_store


def _fake_check(url: str) -> None:
    from security_utils import UnsafeURLError
    host = url.split("//", 1)[-1].split("/")[0]
    if not host or host in ("localhost", "127.0.0.1"):
        raise UnsafeURLError("not public")


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    # DNS is not available in tests: treat any non-local host as public.
    monkeypatch.setattr(audit_runner, "_check_url", _fake_check)
    audit_runner._reset_for_tests()
    yield
    audit_runner._reset_for_tests()


def _png(tmp_path, name: str, data: bytes = b"img") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _main_png(n: int) -> bytes:
    """A real 1280x800 capture with a visible 4px magenta box, different per n."""
    import io

    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 800), (250, 250 - n, 250))
    ImageDraw.Draw(img).rectangle((300, 200, 800, 500), outline=(255, 0, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _shot(tmp_path, n: int, *, closeup=True, selector="div.hero", write_main=True) -> dict:
    main = str(tmp_path / f"m{n}.png")
    if write_main:
        (tmp_path / f"m{n}.png").write_bytes(_main_png(n))
    return {
        "path": main,
        "closeup_path": _png(tmp_path, f"c{n}.png", f"close{n}".encode()) if closeup else None,
        "page": "/", "description": f"defect {n}", "impact": "serious",
        "selector": selector, "kind": "desktop",
    }


def _capture_returning(shots, html="<html></html>"):
    calls = []

    async def capture(url, company_name, on_queued=None, on_started=None, **_kw):
        calls.append((url, company_name))
        return "p.png", html, {"flaw_shots": shots}
    capture.calls = calls
    return capture


def _yes(image_bytes, description):
    return True


def _lead(**over):
    return {"id": 1, "domain": "acme.com", "organization": "Acme Bank", **over}


def _make_leads(statuses):
    rows = [make_row(domain=f"site{i}.com", organization=f"Org {i}", email=f"a{i}@x.com", status=s)
            for i, s in enumerate(statuses)]
    batch = import_store.create_batch("f.csv", rows)
    return [lead["id"] for lead in import_store.list_leads(batch)["leads"]]


async def _wait():
    task = audit_runner._task
    if task:
        await task


async def test_done_when_at_least_two_flaw_shots(tmp_path):
    cap = _capture_returning([_shot(tmp_path, 1), _shot(tmp_path, 2)])
    res = await audit_runner.audit_one(_lead(), capture=cap, judge=_yes)
    assert res["audit_status"] == "done"
    assert len(res["flaw_shots"]) == 2
    assert res["audit_error"] is None
    assert cap.calls == [("https://acme.com", "Acme Bank")]


async def test_no_flaws_when_fewer_than_two_shots(tmp_path):
    ids = _make_leads(["ready"])
    cap = _capture_returning([_shot(tmp_path, 1)])
    res = await audit_runner.audit_one(_lead(), capture=cap, judge=_yes)
    assert res["audit_status"] == "no_flaws"
    assert res["flaw_shots"] == []
    audit_runner.start_audit(ids, capture=cap, judge=_yes)
    await _wait()
    lead = import_store.get_lead(ids[0])
    assert lead["audit_status"] == "no_flaws"
    assert lead["flaw_shots"] == []
    assert lead["audit_error"] == "fewer than 2 flaws with a verified screenshot"


async def test_failed_when_capture_returns_no_html():
    cap = _capture_returning([], html=None)
    res = await audit_runner.audit_one(_lead(), capture=cap, judge=_yes)
    assert res["audit_status"] == "failed"
    assert res["audit_error"] and len(res["audit_error"]) <= 300


async def test_failed_when_capture_raises():
    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        raise RuntimeError("boom")
    res = await audit_runner.audit_one(_lead(), capture=cap, judge=_yes)
    assert res["audit_status"] == "failed"
    assert res["audit_error"] == "capture failed: RuntimeError"


@pytest.mark.parametrize("domain", ["", "localhost", "127.0.0.1", "10.0.0.1"])
async def test_unsafe_or_empty_domain_is_failed_not_fetched(domain):
    cap = _capture_returning([])
    res = await audit_runner.audit_one(_lead(domain=domain), capture=cap, judge=_yes)
    assert res["audit_status"] == "failed"
    assert cap.calls == []


async def test_queue_processes_leads_in_order_and_updates_store(tmp_path):
    ids = _make_leads(["ready", "drafted"])
    order = []
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        order.append(url)
        # while capturing, the lead is marked running
        assert import_store.get_lead(ids[len(order) - 1])["audit_status"] == "running"
        return "p", "<html/>", {"flaw_shots": shots}

    assert audit_runner.start_audit(ids, capture=cap, judge=_yes) is True
    assert all(import_store.get_lead(i)["audit_status"] == "queued" for i in ids)
    await _wait()
    assert order == ["https://site0.com", "https://site1.com"]
    for i in ids:
        lead = import_store.get_lead(i)
        assert lead["audit_status"] == "done"
        assert len(lead["flaw_shots"]) == 2
        assert lead["audited_at"]
    assert audit_runner.progress() == {
        "running": False, "total": 2, "done": 2, "no_flaws": 0, "failed": 0, "current": None}


async def test_crash_in_one_lead_does_not_stop_the_loop(tmp_path):
    ids = _make_leads(["ready", "ready"])
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    n = {"c": 0}

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("crash")
        return "p", "<html/>", {"flaw_shots": shots}

    audit_runner.start_audit(ids, capture=cap, judge=_yes)
    await _wait()
    assert import_store.get_lead(ids[0])["audit_status"] == "failed"
    assert import_store.get_lead(ids[0])["audit_error"]
    assert import_store.get_lead(ids[1])["audit_status"] == "done"
    assert audit_runner.progress()["failed"] == 1


async def test_start_while_running_returns_false():
    ids = _make_leads(["ready"])
    gate = asyncio.Event()

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        await gate.wait()
        return "p", "<html/>", {"flaw_shots": []}

    assert audit_runner.start_audit(ids, capture=cap, judge=_yes) is True
    await asyncio.sleep(0)
    assert audit_runner.progress()["running"] is True
    assert audit_runner.start_audit(ids, capture=cap, judge=_yes) is False
    gate.set()
    await _wait()
    assert audit_runner.progress()["running"] is False
    assert import_store.get_lead(ids[0])["audit_status"] == "no_flaws"


async def test_cancel_stops_before_the_next_lead_and_resets_unstarted_leads(tmp_path):
    ids = _make_leads(["ready", "ready", "ready"])
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    seen = []

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        seen.append(url)
        audit_runner.cancel()
        return "p", "<html/>", {"flaw_shots": shots}

    audit_runner.start_audit(ids, capture=cap, judge=_yes)
    await _wait()
    assert len(seen) == 1
    assert import_store.get_lead(ids[0])["audit_status"] == "done"
    assert import_store.get_lead(ids[1])["audit_status"] == "none"
    assert import_store.get_lead(ids[2])["audit_status"] == "none"
    assert audit_runner.progress()["running"] is False


def test_reset_stale_running_resets_leads():
    ids = _make_leads(["ready", "ready", "ready"])
    import_store.update_lead(ids[0], audit_status="running")
    import_store.update_lead(ids[1], audit_status="running")
    import_store.update_lead(ids[2], audit_status="done")
    assert audit_runner.reset_stale_running() == 2
    assert import_store.get_lead(ids[0])["audit_status"] == "none"
    assert import_store.get_lead(ids[1])["audit_status"] == "none"
    assert import_store.get_lead(ids[2])["audit_status"] == "done"


async def test_only_ready_or_drafted_leads_are_audited(tmp_path):
    statuses = ["ready", "drafted", "approved", "sent", "failed", "skipped"]
    ids = _make_leads(statuses)
    cap = _capture_returning([_shot(tmp_path, 1), _shot(tmp_path, 2)])
    assert audit_runner.start_audit(ids, capture=cap, judge=_yes) is True
    await _wait()
    assert len(cap.calls) == 2
    for i in ids[2:]:
        assert import_store.get_lead(i)["audit_status"] == "none"
    assert audit_runner.progress()["total"] == 2


async def test_shot_dropped_when_the_vision_judge_says_the_box_does_not_show_the_defect(tmp_path):
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2), _shot(tmp_path, 3)]

    def judge(b, d):
        return d != "defect 2"

    kept, dropped = await audit_runner.verify_shots(shots, judge)
    assert [s["description"] for s in kept] == ["defect 1", "defect 3"]
    assert len(dropped) == 1 and dropped[0]["description"] == "defect 2" and dropped[0]["reason"]


async def test_shot_without_a_closeup_is_judged_on_its_main_image(tmp_path):
    with_close = _shot(tmp_path, 1)
    no_close = _shot(tmp_path, 2, closeup=False)
    got = {}

    def judge(b, d):
        got[d] = b
        return True

    kept, dropped = await audit_runner.verify_shots([with_close, no_close], judge)
    assert got["defect 1"] == b"close1"
    assert got["defect 2"] == _main_png(2)
    assert len(kept) == 2 and dropped == []


async def test_closeup_path_set_but_file_missing_falls_back_to_main(tmp_path):
    s = _shot(tmp_path, 1)
    (tmp_path / "c1.png").unlink()
    got = {}

    def judge(b, d):
        got["b"] = b
        return True

    kept, _ = await audit_runner.verify_shots([s], judge)
    assert got["b"] == _main_png(1) and len(kept) == 1


async def test_shot_with_no_image_file_is_dropped(tmp_path):
    s = _shot(tmp_path, 1, closeup=False, write_main=False)
    called = []
    kept, dropped = await audit_runner.verify_shots([s], lambda b, d: called.append(1) or True)
    assert kept == [] and called == []
    assert dropped[0]["description"] == "defect 1" and dropped[0]["reason"]


@pytest.mark.parametrize("selector", [
    "#chat-widget", ".cookie-banner", "#onetrust-banner-sdk", ".intercom-lightweight-app",
    "#hubspot-messages-iframe-container", ".CookieConsent", "div.chat_widget"])
async def test_shot_dropped_when_the_flagged_element_is_a_cookie_banner_or_chat_widget(tmp_path, selector):
    called = []
    kept, dropped = await audit_runner.verify_shots(
        [_shot(tmp_path, 1, selector=selector)], lambda b, d: called.append(1) or True)
    assert kept == [] and called == []
    assert len(dropped) == 1 and dropped[0]["reason"]


async def test_dropping_shots_below_two_makes_the_lead_no_flaws_with_reasons_recorded(tmp_path):
    ids = _make_leads(["ready"])
    cap = _capture_returning([_shot(tmp_path, 1), _shot(tmp_path, 2)])
    judge = lambda b, d: d == "defect 1"  # noqa: E731
    res = await audit_runner.audit_one(_lead(), capture=cap, judge=judge)
    assert res["audit_status"] == "no_flaws"
    assert len(res["dropped_shots"]) == 1
    assert res["dropped_shots"][0]["reason"]
    audit_runner.start_audit(ids, capture=cap, judge=judge)
    await _wait()
    lead = import_store.get_lead(ids[0])
    assert lead["audit_status"] == "no_flaws"
    assert res["dropped_shots"][0]["reason"] in lead["audit_error"]
    assert len(lead["audit_error"]) <= 300
    assert lead["flaw_shots"] == []


async def test_judge_error_drops_the_shot_rather_than_trusting_it(tmp_path):
    def judge(b, d):
        raise RuntimeError("network")

    kept, dropped = await audit_runner.verify_shots([_shot(tmp_path, 1)], judge)
    assert kept == [] and len(dropped) == 1 and dropped[0]["reason"]


@pytest.mark.parametrize("selector", ["#chatham-hero", ".chateau-banner", "#snowdrift", ".crispy-nav", "div.hero"])
async def test_lookalike_selectors_are_not_dropped(tmp_path, selector):
    kept, dropped = await audit_runner.verify_shots([_shot(tmp_path, 1, selector=selector)], _yes)
    assert len(kept) == 1 and dropped == []


@pytest.mark.parametrize("domain", [
    "127.0.0.1\\@example.com", "user@example.com", "example.com:8080", "example.com/path",
    "https://example.com", "exa mple.com", "93.184.216.34", "exämple.com",
    "-bad.example.com", "a%2eb.com", "[::1]", "example",
])
async def test_non_plain_hostname_is_failed_before_any_capture(domain):
    cap = _capture_returning([])
    # note: real hostname validation runs; only the DNS step is faked
    res = await audit_runner.audit_one(_lead(domain=domain),
                                       capture=cap, judge=_yes)
    assert cap.calls == []
    assert res["audit_status"] == "failed"
    assert res["audit_error"]


async def test_uppercase_domain_is_lowercased_then_accepted(tmp_path):
    cap = _capture_returning([_shot(tmp_path, 1), _shot(tmp_path, 2)])
    res = await audit_runner.audit_one(_lead(domain="ACME.Com"), capture=cap, judge=_yes)
    assert res["audit_status"] == "done"
    assert cap.calls[0][0] == "https://acme.com"


async def test_queued_lead_approved_before_its_turn_is_not_captured(tmp_path):
    ids = _make_leads(["ready", "ready"])
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    seen = []

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        seen.append(url)
        import_store.update_lead(ids[1], status="approved")  # approved while lead 2 waits
        return "p", "<html/>", {"flaw_shots": shots}

    audit_runner.start_audit(ids, capture=cap, judge=_yes)
    await _wait()
    assert seen == ["https://site0.com"]
    lead = import_store.get_lead(ids[1])
    assert lead["status"] == "approved"
    assert lead["audit_status"] == "none"
    assert lead["flaw_shots"] == [] and lead["audited_at"] is None and not lead["audit_error"]


async def test_lead_sent_during_capture_keeps_its_fields_and_late_result_is_discarded(tmp_path):
    ids = _make_leads(["ready"])
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        import_store.update_lead(ids[0], status="sent")
        return "p", "<html/>", {"flaw_shots": shots}

    audit_runner.start_audit(ids, capture=cap, judge=_yes)
    await _wait()
    lead = import_store.get_lead(ids[0])
    assert lead["status"] == "sent"
    assert lead["flaw_shots"] == [] and lead["audited_at"] is None and not lead["audit_error"]
    assert audit_runner.progress()["done"] == 0


async def test_db_error_in_start_audit_does_not_wedge_the_runner(monkeypatch, tmp_path):
    ids = _make_leads(["ready", "ready"])
    real = import_store.update_lead
    state = {"n": 0}

    def flaky(lead_id, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("db down")
        return real(lead_id, **kw)

    monkeypatch.setattr(import_store, "update_lead", flaky)
    with pytest.raises(RuntimeError):
        audit_runner.start_audit(ids, capture=_capture_returning([]), judge=_yes)
    monkeypatch.setattr(import_store, "update_lead", real)
    assert audit_runner.progress()["running"] is False
    assert all(import_store.get_lead(i)["audit_status"] == "none" for i in ids)
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    assert audit_runner.start_audit(ids, capture=_capture_returning(shots), judge=_yes) is True
    await _wait()
    assert import_store.get_lead(ids[0])["audit_status"] == "done"


# --- a new audit result invalidates a designed email (designed flaw email review) ---

from import_test_support import seed_designed_lead, seed_lead  # noqa: E402

_CLEARED = {"status": "ready", "html_body": None, "design_json": {}, "subject": None,
            "body": None, "review_warnings": [], "source_quote": None}


@pytest.mark.parametrize("found", [2, 0])  # a done result, and a no_flaws one
async def test_a_new_result_clears_a_designed_email_and_sends_the_lead_back_to_ready(tmp_path, found):
    lead_id = seed_designed_lead(tmp_path, review_warnings=["Fact check: old"])
    assert import_store.get_lead(lead_id)["html_body"]
    shots = [_shot(tmp_path, i) for i in range(1, found + 1)]
    audit_runner.start_audit([lead_id], capture=_capture_returning(shots), judge=_yes)
    await _wait()
    lead = import_store.get_lead(lead_id)
    assert {k: lead[k] for k in _CLEARED} == _CLEARED
    assert lead["audit_status"] == ("done" if found else "no_flaws") and lead["audited_at"]
    assert [s["path"] for s in lead["flaw_shots"]] == [s["path"] for s in shots]


async def test_a_designed_email_drafted_during_the_audit_is_cleared_too(tmp_path):
    lead_id = seed_lead(status="drafted", subject="S", body="Hi Jane,\n\nText")
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        import_store.update_lead(lead_id, html_body="<p>designed</p>", design_json={"opening": "o"})
        return "p", "<html/>", {"flaw_shots": shots}

    audit_runner.start_audit([lead_id], capture=cap, judge=_yes)
    await _wait()
    lead = import_store.get_lead(lead_id)
    assert {k: lead[k] for k in _CLEARED} == _CLEARED and lead["audit_status"] == "done"


async def test_a_text_draft_keeps_its_email_when_audited(tmp_path):
    lead_id = seed_lead(status="drafted", subject="S", body="Hi Jane,\n\nText", review_warnings=["w"])
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    audit_runner.start_audit([lead_id], capture=_capture_returning(shots), judge=_yes)
    await _wait()
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "drafted" and lead["subject"] == "S" and lead["body"] == "Hi Jane,\n\nText"
    assert lead["review_warnings"] == ["w"] and lead["audit_status"] == "done"


async def test_a_designed_lead_that_left_the_auditable_statuses_keeps_its_email(tmp_path):
    lead_id = seed_designed_lead(tmp_path)
    before = import_store.get_lead(lead_id)

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        import_store.update_lead(lead_id, status="approved")
        return "p", "<html/>", {"flaw_shots": [_shot(tmp_path, 1), _shot(tmp_path, 2)]}

    audit_runner.start_audit([lead_id], capture=cap, judge=_yes)
    await _wait()
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "approved" and lead["html_body"] == before["html_body"]
    assert lead["flaw_shots"] == before["flaw_shots"] and lead["design_json"] == before["design_json"]


# --- re-audit (fix wave I1) ---

@pytest.mark.parametrize("prior", ["failed", "no_flaws", "done"])
async def test_a_failed_no_flaws_or_done_lead_can_be_re_audited(tmp_path, prior):
    ids = _make_leads(["ready"])
    import_store.update_lead(ids[0], audit_status=prior, audit_error="old reason")
    shots = [_shot(tmp_path, 1), _shot(tmp_path, 2)]
    assert audit_runner.start_audit(ids, capture=_capture_returning(shots), judge=_yes) is True
    await _wait()
    lead = import_store.get_lead(ids[0])
    assert lead["audit_status"] == "done" and lead["audit_error"] is None
    assert [s["path"] for s in lead["flaw_shots"]] == [s["path"] for s in shots]


async def test_cancelling_a_re_audit_restores_each_unstarted_leads_previous_audit(tmp_path):
    (first,) = _make_leads(["ready"])
    designed = seed_designed_lead(tmp_path)
    (failed,) = _make_leads(["drafted"])
    import_store.update_lead(failed, audit_status="failed", audit_error="capture failed: Timeout")
    before = import_store.get_lead(designed)

    async def cap(url, company_name, on_queued=None, on_started=None, **_kw):
        audit_runner.cancel()
        return "p", "<html/>", {"flaw_shots": []}

    audit_runner.start_audit([first, designed, failed], capture=cap, judge=_yes)
    assert import_store.get_lead(designed)["audit_status"] == "queued"
    await _wait()
    after = import_store.get_lead(designed)
    assert after["audit_status"] == "done" and after["audit_error"] == before["audit_error"]
    for key in ("status", "flaw_shots", "html_body", "design_json", "subject", "body"):
        assert after[key] == before[key], key
    from importer import drafting
    assert drafting.shots_match_design(after) is True
    lead = import_store.get_lead(failed)
    assert lead["audit_status"] == "failed" and lead["audit_error"] == "capture failed: Timeout"



# --- the box must be visible at email size (fix wave I3) ---

def _real_shot(tmp_path, n, box):
    from PIL import Image, ImageDraw
    path = tmp_path / f"real{n}.png"
    img = Image.new("RGB", (1280, 800), (250, 250, 250))
    if box:
        ImageDraw.Draw(img).rectangle(box, outline=(255, 0, 255), width=4)
    img.save(path)
    return {"path": str(path), "closeup_path": None, "page": "/", "description": f"defect {n}",
            "impact": "serious", "selector": "div.hero", "kind": "desktop"}


async def test_a_shot_whose_box_vanishes_at_email_size_is_dropped(tmp_path):
    shots = [_real_shot(tmp_path, 1, (300, 200, 800, 500)), _real_shot(tmp_path, 2, (400, 300, 419, 319)),
             _real_shot(tmp_path, 3, None)]
    kept, dropped = await audit_runner.verify_shots(shots, _yes)
    assert [s["description"] for s in kept] == ["defect 1"]
    assert [d["description"] for d in dropped] == ["defect 2", "defect 3"]
    assert all("box" in d["reason"] for d in dropped)

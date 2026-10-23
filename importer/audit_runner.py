"""
Background audit stage for imported leads.

Audits each eligible lead's website (one at a time, the capture holds a single global
Playwright slot) and verifies every flaw screenshot before it can be used in an email:
a shot is kept only if a narrow yes/no vision check confirms the boxed image shows the
defect. Anything doubtful is dropped (fail closed). A lead with fewer than MIN_SHOTS
verified shots is 'no_flaws' and gets no designed email.
"""
import asyncio
import hashlib
import re
from pathlib import Path

from analyzer import design_review, visuals
from importer import email_images
from analyzer.visuals import generate_audit_screenshot
from urllib.parse import urlparse

from security_utils import UnsafeNavigationError, UnsafeURLError, is_plain_hostname, validate_public_url
from storage import import_store

MIN_SHOTS = 1
MAX_TEXT_FINDINGS = 6
# One site that hangs (a never-ending page, a stuck browser) must not hold the
# single audit slot, and every lead queued behind it, forever.
AUDIT_TIMEOUT_S = 360
AUDITABLE_STATUSES = ("ready", "drafted")
_CHROME_SELECTOR = re.compile(
    r"(?i)(?:^|[^a-z])(?:chat|drift|crisp|cookie|cookies|consent|gdpr)(?:[^a-z]|$)"
    r"|onetrust|cookiebot|cookieconsent|intercom|tawk|livechat|zendesk|hubspot-messages"
)
_NO_VERIFIED = "no flaw with a verified screenshot"
_ERROR_MAX = 300

_task: asyncio.Task | None = None
_running = False
_cancel = False
_progress = {"total": 0, "done": 0, "no_flaws": 0, "failed": 0, "current": None}
# Each queued lead's audit_status and audit_error from before this run. A lead
# that ends the run without a saved result (cancelled before its turn, changed
# while it waited or was captured) gets them back: a re-audit that never ran
# must not wipe an earlier 'done' whose shots are still intact (_restore_prior).
_prior: dict[int, tuple[str, str | None]] = {}


def _reset_for_tests() -> None:
    global _task, _running, _cancel
    _task, _running, _cancel = None, False, False
    _progress.update(total=0, done=0, no_flaws=0, failed=0, current=None)
    _prior.clear()


_STALE_SHOTS = ("the previous audit's screenshots changed while a re-audit was discarded "
                "(cancelled or the lead changed); re-audit this lead")


def _restore_prior(lead_id: int) -> None:
    """Give a lead whose run ended without a saved result its earlier audit back.
    A capture writes to the lead's own shot file names, so a discarded re-audit
    may already have overwritten the old shots: 'done' comes back only while
    every stored shot still has the hash it was verified with. Otherwise the
    lead fails closed and asks for a re-audit."""
    status, error = _prior.get(lead_id, ("none", None))
    if status == "done":
        from importer import drafting
        lead = import_store.get_lead(lead_id) or {}
        if not drafting.shots_intact(lead.get("flaw_shots") or []):
            status, error = "failed", _STALE_SHOTS
    import_store.update_lead(lead_id, audit_status=status, audit_error=error)


def _check_url(url: str) -> None:
    validate_public_url(url)


def _read(path) -> bytes | None:
    try:
        data = Path(path).read_bytes() if path else b""
    except (OSError, ValueError):
        return None
    return data or None


def _err(text: str) -> str:
    return text[:_ERROR_MAX]


async def verify_shots(shots: list[dict], judge) -> tuple[list[dict], list[dict]]:
    """Return (kept, dropped). Never trusts a shot on error. Each kept shot gets
    "sha256", the hash of its main image file as verified: drafting and sending
    refuse the shot once the file on disk no longer has that hash."""
    kept, dropped = [], []
    for shot in shots:
        desc = str(shot.get("description") or "")

        def drop(reason: str) -> None:
            dropped.append({"description": desc, "reason": reason})

        if _CHROME_SELECTOR.search(str(shot.get("selector") or "")):
            drop("flagged element is a consent banner or chat widget")
            continue
        image = await asyncio.to_thread(_read, shot.get("closeup_path"))
        if image is None:
            image = await asyncio.to_thread(_read, shot.get("path"))
        if image is None:
            drop("no readable image file")
            continue
        # Before the (paid) vision check: the box must still be visible once the
        # image is scaled for the email, or the reader sees no marker at all.
        try:
            await asyncio.to_thread(email_images.prepare, shot.get("path"))
        except email_images.BoxNotVisibleError:
            drop("the magenta box is too small to see once the image is scaled for email")
            continue
        except Exception as exc:
            drop(f"the image could not be prepared for email ({type(exc).__name__}), so no box can be shown")
            continue
        try:
            confirmed = await asyncio.to_thread(judge, image, desc)
        except Exception as exc:  # fail closed
            drop(f"vision check errored ({type(exc).__name__})")
            continue
        if confirmed is not True:
            drop("vision check did not confirm the defect is visible in the image")
            continue
        main = await asyncio.to_thread(_read, shot.get("path"))
        if main is None:
            drop("no readable image file")
            continue
        kept.append({**shot, "sha256": hashlib.sha256(main).hexdigest()})
    return kept, dropped


def text_findings(extra: dict) -> list[dict]:
    """Measured findings with nothing to box, as {"key", "description"}: the
    visitor-view probes, the designer-view measurements (each quotes its own
    number) and a sideways-scrolling phone page. All deterministic; none comes
    from a model's reading of an image."""
    out: list[dict] = []
    for f in extra.get("presentation_findings") or []:
        if f.get("key") and f.get("description"):
            out.append({"key": f["key"], "description": str(f["description"])})
    for f in design_review.design_findings(extra.get("design_probe")):
        out.append({"key": f["key"], "description": f"{f['title']}. {f['saw']}"})
    if extra.get("mobile_horizontal_overflow"):
        out.append({"key": "mobile_overflow",
                    "description": "On a phone the home page is wider than the screen, so a visitor has to scroll sideways"})
    return out[:MAX_TEXT_FINDINGS]


async def audit_one(lead: dict, capture=generate_audit_screenshot,
                    judge=visuals._vision_confirms_visible_defect) -> dict:
    def result(status, shots=None, dropped=None, error=None):
        return {"audit_status": status, "flaw_shots": shots or [],
                "dropped_shots": dropped or [], "audit_error": error}

    domain = (lead.get("domain") or "").strip().lower()
    if not domain:
        return result("failed", error="lead has no domain")
    if not is_plain_hostname(domain):
        return result("failed", error="domain is not a plain hostname")
    url = f"https://{domain}"
    if urlparse(url).hostname != domain:
        return result("failed", error="domain is not a plain hostname")
    try:
        await asyncio.to_thread(_check_url, url)
    except UnsafeURLError:
        return result("failed", error="domain is not a public website")
    except Exception as exc:
        return result("failed", error=_err(f"url check failed ({type(exc).__name__})"))
    if lead.get("id") is None:
        return result("failed", error="lead has no id")
    try:
        # Per-lead file names: another lead for the same company, or a
        # Dashboard audit of the same site, can never overwrite these shots.
        _path, html, extra = await capture(
            url, lead.get("organization") or "", file_prefix=visuals.import_shot_prefix(lead["id"]),
            raise_on_unsafe_navigation=True)
    except UnsafeNavigationError:
        # The site (or a redirect) pointed the browser at localhost, cloud
        # metadata or the private network; the capture stopped before any shot.
        return result("failed", error="the website redirected to a non-public address, so it was not audited")
    except Exception as exc:
        return result("failed", error=_err(f"capture failed: {type(exc).__name__}"))
    if html is None or extra is None:
        return result("failed", error="capture failed: the website could not be loaded")
    kept, dropped = await verify_shots(list(extra.get("flaw_shots") or []), judge)
    findings = text_findings(extra)
    if len(kept) < MIN_SHOTS:
        reasons = "; ".join(d["reason"] for d in dropped) or _NO_VERIFIED
        # Measured findings need no picture: the text email can still cite them.
        status = "text_only" if findings else "no_flaws"
        return {**result(status, [], dropped, _err(reasons)), "text_findings": findings}
    return {**result("done", kept, dropped), "text_findings": findings}


async def _run(lead_ids: list[int], capture, judge) -> None:
    global _running
    try:
        try:
            for lead_id in lead_ids:
                if _cancel:
                    break
                lead = import_store.get_lead(lead_id)
                if not lead or lead.get("audit_status") != "queued":
                    continue
                if lead.get("status") not in AUDITABLE_STATUSES:
                    _restore_prior(lead_id)
                    continue
                _progress["current"] = lead.get("organization") or lead.get("domain")
                if not import_store.update_lead(
                        lead_id, only_if_status=AUDITABLE_STATUSES, audit_status="running"):
                    continue
                try:
                    res = await asyncio.wait_for(
                        audit_one(lead, capture=capture, judge=judge), timeout=AUDIT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    res = {"audit_status": "failed", "flaw_shots": [],
                           "audit_error": f"the audit took longer than {AUDIT_TIMEOUT_S} seconds and was stopped"}
                except Exception as exc:
                    res = {"audit_status": "failed", "flaw_shots": [],
                           "audit_error": _err(f"audit crashed: {type(exc).__name__}")}
                status = res["audit_status"]
                fields = dict(audit_status=status, flaw_shots=res["flaw_shots"],
                              audit_error=res["audit_error"], audited_at=import_store.now_sql(),
                              text_findings=res.get("text_findings") or [])
                # A designed email pairs its sentences with the old screenshots, so a
                # new result makes it invalid: clear it and send the lead back to ready
                # in the same compare and set write (the user drafts it again).
                saved = import_store.update_lead(
                    lead_id, only_if_status=AUDITABLE_STATUSES, only_if_designed=True, **fields,
                    status="ready", html_body=None, design_json={}, subject=None, body=None,
                    review_warnings=[], source_quote=None)
                if not saved:
                    saved = import_store.update_lead(
                        lead_id, only_if_status=AUDITABLE_STATUSES, only_if_designed=False, **fields)
                if not saved:
                    continue  # lead changed while auditing: discard the result
                _progress["no_flaws" if status == "text_only" else "done" if status == "done" else status] += 1
        finally:
            for lead_id in lead_ids:
                lead = import_store.get_lead(lead_id)
                if lead and lead.get("audit_status") in ("queued", "running"):
                    # No status guard: a lead approved or sent while it was being
                    # captured must not keep a stale 'running'. Only the audit
                    # fields are written; the lead's own status is untouched.
                    _restore_prior(lead_id)
    finally:
        _progress["current"] = None
        _running = False


def start_audit(lead_ids: list[int], *, capture=generate_audit_screenshot, judge=None) -> bool:
    """Queue eligible leads (status ready/drafted) and start the single background run.
    Returns False if a run is already active or nothing is eligible. Call from the event loop."""
    global _task, _running, _cancel
    if _running:
        return False
    eligible, prior = [], {}
    for lead_id in lead_ids:
        lead = import_store.get_lead(lead_id)
        if lead and lead["status"] in AUDITABLE_STATUSES:
            eligible.append(lead_id)
            was = lead.get("audit_status") or "none"
            prior[lead_id] = ("none", None) if was in ("queued", "running") else (was, lead.get("audit_error"))
    if not eligible:
        return False
    loop = asyncio.get_running_loop()
    marked = []
    try:
        for lead_id in eligible:
            import_store.update_lead(lead_id, audit_status="queued", audit_error=None)
            marked.append(lead_id)
    except Exception:
        for lead_id in marked:
            try:
                status, error = prior[lead_id]
                import_store.update_lead(lead_id, audit_status=status, audit_error=error)
            except Exception:
                pass
        raise
    _running, _cancel = True, False
    _prior.clear()
    _prior.update(prior)
    _progress.update(total=len(eligible), done=0, no_flaws=0, failed=0, current=None)
    _task = loop.create_task(_run(eligible, capture, judge or visuals._vision_confirms_visible_defect))
    return True


def progress() -> dict:
    return {"running": _running, **_progress}


def cancel() -> None:
    """Stop before the next lead. Each lead that did not get a saved result goes
    back to the audit status and error it had before this run (a previous
    'done' only while its shots are intact, see _restore_prior)."""
    global _cancel
    if _running:
        _cancel = True


def reset_stale_running() -> int:
    """Leads left at 'running' (or 'queued') by a crashed process go back to 'none'."""
    conn = import_store._connect()
    try:
        n = conn.execute(
            "UPDATE import_leads SET audit_status = 'none' WHERE audit_status IN ('running', 'queued')"
        ).rowcount
        conn.commit()
        return n
    finally:
        conn.close()

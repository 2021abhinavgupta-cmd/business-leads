"""Orchestration for the CSV import queue: upload and the status transitions."""
import base64
import os
import re
from collections import Counter

import config
from importer import compose, csv_rules, drafting, email_html, email_images
from storage import import_store


class FlowError(Exception):
    """A user-facing failure carrying the HTTP status the route should return."""

    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _load(lead_id: int) -> dict:
    lead = import_store.get_lead(lead_id)
    if lead is None:
        raise FlowError(404, "Import lead not found")
    return lead


def upload(filename: str, csv_text: str) -> dict:
    try:
        rows = csv_rules.parse_csv(csv_text)
    except csv_rules.CsvImportError as e:
        raise FlowError(e.status_code, str(e)) from e
    leads = csv_rules.apply_rules(rows, import_store.load_blockers())
    batch_id = import_store.create_batch(filename or "upload.csv", leads)
    ready = [l for l in leads if l["status"] == "ready"]
    return {
        "batch_id": batch_id,
        "total": len(leads),
        "ready": len(ready),
        "skipped": len(leads) - len(ready),
        "skip_breakdown": dict(Counter(l["skip_reason"] for l in leads if l["status"] == "skipped")),
        "us_ready": sum(1 for l in ready if l["country"] == "united states"),
        "mailing_address_set": bool(config.MAILING_ADDRESS),
    }


def restore(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] != "skipped" or lead["skip_reason"] not in csv_rules.RESTORABLE_REASONS:
        raise FlowError(409, "Only government, second contact or manually skipped leads can be restored.")
    reason = csv_rules.recheck(lead, import_store.load_blockers())
    if reason:
        import_store.update_lead(lead_id, skip_reason=reason)
        raise FlowError(409, f"Cannot restore: this lead is now blocked ({reason}).")
    import_store.update_lead(lead_id, status="ready", skip_reason=None)
    return import_store.get_lead(lead_id)


def skip(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("ready", "drafted", "approved", "failed"):
        raise FlowError(409, f"A {lead['status']} lead cannot be skipped.")
    import_store.update_lead(lead_id, status="skipped", skip_reason="manual")
    return import_store.get_lead(lead_id)


def approve(lead_id: int, acknowledge_warnings: bool) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("drafted", "failed") or not (lead.get("subject") and lead.get("body")):
        raise FlowError(409, "Only a drafted lead with a subject and body can be approved.")
    if lead["review_warnings"] and not acknowledge_warnings:
        raise FlowError(409, {
            "message": "This draft was flagged during review and hasn't been acknowledged yet.",
            "warnings": lead["review_warnings"],
            "resend_with": "acknowledge_warnings: true",
        })
    import_store.update_lead(lead_id, status="approved", error=None)
    return import_store.get_lead(lead_id)


def unapprove(lead_id: int) -> dict:
    lead = _load(lead_id)
    if lead["status"] != "approved":
        raise FlowError(409, "Only an approved lead can be un-approved.")
    import_store.update_lead(lead_id, status="drafted")
    return import_store.get_lead(lead_id)


EDITABLE_STATUSES = ("drafted", "approved", "failed")


def save_edit(lead_id: int, subject: str, body: str) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in EDITABLE_STATUSES or not lead.get("body"):
        raise FlowError(409, "Only a drafted lead can be edited.")
    if lead.get("html_body"):
        # The HTML is what the recipient reads; a text only edit would leave it stale.
        raise FlowError(409, "This is a designed email; edit it with the design editor.")
    # A newline in a Subject header would be header injection; collapse it.
    subject, body = " ".join(subject.split()), body.strip()
    if not subject or not body:
        raise FlowError(400, "Subject and body cannot be empty.")
    carried = [w for w in lead["review_warnings"] if compose.is_evidence_warning(w)]
    warnings = compose.run_checks(
        subject, body,
        company=lead.get("organization") or lead.get("domain") or "this lead",
        facts_text=compose.facts_text(lead), homepage_text=None,
        source_quote=lead.get("source_quote") or "",
        check_evidence=False, carried_warnings=carried,
    )
    # An edit invalidates any earlier approval and acknowledgement.
    if len(subject) > compose.MAX_SUBJECT_CHARS:
        warnings.append(compose.subject_length_warning(subject))
    saved = import_store.update_lead(
        lead_id, only_if_status=EDITABLE_STATUSES,
        status="drafted", subject=subject, body=body, review_warnings=warnings,
    )
    if not saved:
        raise FlowError(409, "The lead changed while it was being edited; nothing was saved.")
    return import_store.get_lead(lead_id)


_SHOTS_CHANGED = "The screenshots changed after this email was drafted; draft it again."
_SHOT_MISSING = "A screenshot file is missing on disk; audit this lead again and redraft it."


def _require_designed(lead: dict) -> dict:
    if not lead.get("html_body"):
        raise FlowError(409, "This lead has no designed email.")
    if not drafting.shots_match_design(lead):
        shots = lead.get("flaw_shots") or []
        if any(not (s.get("path") and os.path.isfile(s["path"])) for s in shots):
            raise FlowError(409, _SHOT_MISSING)
        if not drafting.shots_intact(shots):
            raise FlowError(409, drafting.SHOTS_CHANGED_ON_DISK)
        raise FlowError(409, _SHOTS_CHANGED)
    return lead


PREVIEW_FOOTER = "Footer is added when the email is sent."


def preview_html(lead_id: int) -> str:
    """The designed email as the recipient will see it, for an iframe: each cid
    image is the exact JPEG the sender will attach, and the footer marker is
    replaced by a note (the sender writes the real footer)."""
    lead = _require_designed(_load(lead_id))
    try:
        images = dict(email_images.prepare_all(lead["flaw_shots"]))
    except FileNotFoundError:
        raise FlowError(409, _SHOT_MISSING)
    except Exception as e:
        raise FlowError(409, f"A screenshot could not be prepared ({type(e).__name__}); audit this lead again.")

    def data_uri(match):
        data = images.get(match.group(1))
        if data is None:
            raise FlowError(409, _SHOTS_CHANGED)
        return 'src="data:image/jpeg;base64,' + base64.b64encode(data).decode("ascii") + '"'

    html = re.sub(r'src="cid:(flaw\d+)"', data_uri, lead["html_body"])
    if "cid:" in html:
        raise FlowError(409, _SHOTS_CHANGED)
    return html.replace(email_html.FOOTER_MARKER, PREVIEW_FOOTER)



def save_design_edit(lead_id: int, subject: str, opening: str, sections: list[dict]) -> dict:
    """The designed counterpart of save_edit, with the same status rules."""
    lead = _load(lead_id)
    if lead["status"] not in EDITABLE_STATUSES or not lead.get("body"):
        raise FlowError(409, "Only a drafted lead can be edited.")
    _require_designed(lead)
    try:
        out = drafting.redesign(lead, subject, opening, sections, reply_to=config.REPLY_TO_EMAIL)
    except drafting.DesignEditError as e:
        raise FlowError(422, str(e))
    # An edit invalidates any earlier approval and acknowledgement.
    saved = import_store.update_lead(
        lead_id, only_if_status=EDITABLE_STATUSES,
        status="drafted", subject=out["subject"], body=out["body"], html_body=out["html_body"],
        design_json=out["design"], review_warnings=out["review_warnings"],
    )
    if not saved:
        raise FlowError(409, "The lead changed while it was being edited; nothing was saved.")
    return import_store.get_lead(lead_id)

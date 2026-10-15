"""Orchestration for the CSV import queue: upload and the status transitions."""
from collections import Counter

import config
from importer import compose, csv_rules
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


def save_edit(lead_id: int, subject: str, body: str) -> dict:
    lead = _load(lead_id)
    if lead["status"] not in ("drafted", "approved", "failed") or not lead.get("body"):
        raise FlowError(409, "Only a drafted lead can be edited.")
    subject, body = subject.strip(), body.strip()
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
    import_store.update_lead(lead_id, status="drafted", subject=subject, body=body, review_warnings=warnings)
    return import_store.get_lead(lead_id)

"""
Gates for sending an imported lead through /api/send: status and identity,
state that changed since approval, the CAN-SPAM address requirement, the
bounce pause, and warning acknowledgement. Pure functions; app.py supplies
the facts.
"""
from importer import csv_rules

IMPORT_VARIANT = "import-automation"


class SendBlocked(Exception):
    def __init__(self, status_code: int, detail, skip_reason: str | None = None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.skip_reason = skip_reason


def bounce_status(variant_rows: list[dict], min_sent: int, pause_pct: float) -> dict:
    """Bounce rate across this feature's own sends (variant import-automation)."""
    bucket = next((r for r in variant_rows if r.get("variant") == IMPORT_VARIANT), None)
    sent = bucket["sent"] if bucket else 0
    bounced = bucket["bounced"] if bucket else 0
    rate = (bounced / sent * 100) if sent else 0.0
    return {
        "sent": sent, "bounced": bounced, "rate_pct": round(rate, 2),
        "paused": sent >= min_sent and rate > pause_pct,
        "min_sent": min_sent, "pause_pct": pause_pct,
    }


def check_import_send(
    lead: dict | None, *, req_email: str, acknowledge_warnings: bool,
    mailing_address: str, blockers: dict, bounce: dict,
) -> None:
    if lead is None:
        raise SendBlocked(404, "Import lead not found")
    if lead["status"] != "approved":
        raise SendBlocked(409, f"This lead is '{lead['status']}'; only approved leads can be sent.")
    if (req_email or "").strip().lower() != lead["email"].strip().lower():
        raise SendBlocked(409, "The email address does not match the imported lead.")
    reason = csv_rules.recheck(lead, blockers)
    if reason:
        raise SendBlocked(409, f"This lead can no longer be sent ({reason}).", skip_reason=reason)
    if (lead.get("country") or "").strip().lower() == "united states" and not mailing_address:
        raise SendBlocked(
            409,
            "US recipients need a postal address in the email footer (CAN-SPAM). "
            "Set the MAILING_ADDRESS environment variable, then try again.",
        )
    if bounce["paused"]:
        raise SendBlocked(409, {
            "message": (
                f"Import sending is paused: {bounce['bounced']} of {bounce['sent']} sent emails "
                f"bounced ({bounce['rate_pct']}%, limit {bounce['pause_pct']}%)."
            ),
            "code": "import_paused", **bounce,
        })
    if lead["review_warnings"] and not acknowledge_warnings:
        raise SendBlocked(409, {
            "message": "This draft was flagged during review and hasn't been acknowledged yet.",
            "warnings": lead["review_warnings"],
            "resend_with": "acknowledge_warnings: true",
        })

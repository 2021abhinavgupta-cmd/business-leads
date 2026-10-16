"""Draft one import lead: fetch evidence, ask the model, assemble, check."""
from importer import compose, homepage

MAX_SUBJECT_CHARS = compose.MAX_SUBJECT_CHARS
MAX_BODY_WORDS = 60


class DraftError(Exception):
    pass


def draft_lead(lead: dict, auditor, *, your_name: str, fetch=homepage.fetch_homepage_text) -> dict:
    homepage_text = fetch(lead.get("domain") or "")
    result = auditor.generate_import_email(lead, homepage_text, your_name)
    if not result:
        raise DraftError("Every AI provider failed to produce a draft. Try again in a moment.")

    paragraphs = result["paragraphs"]
    # A newline in a Subject header would be header injection; collapse it.
    subject = " ".join(result["subject"].split())
    quote = result.get("source_quote") or ""
    body = compose.compose_body(lead.get("person_name") or "", paragraphs, your_name)
    warnings = compose.run_checks(
        subject, body,
        company=lead.get("organization") or lead.get("domain") or "this lead",
        facts_text=compose.facts_text(lead),
        homepage_text=homepage_text, source_quote=quote,
    )
    if len(subject) > MAX_SUBJECT_CHARS:
        warnings.append(compose.subject_length_warning(subject))
    words = sum(len(p.split()) for p in paragraphs)
    if words > MAX_BODY_WORDS:
        warnings.append(f"Body is {words} words; keep it at {MAX_BODY_WORDS} or fewer.")
    return {
        "subject": subject, "body": body, "review_warnings": warnings,
        "source_quote": quote, "ai_cost": result.get("ai_cost", 0.0),
    }

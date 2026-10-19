"""Draft one import lead: fetch evidence, ask the model, assemble, check."""
import re

from importer import compose, homepage

MAX_SUBJECT_CHARS = compose.MAX_SUBJECT_CHARS
MAX_BODY_WORDS = 130
MAX_ATTEMPTS = 3  # first draft plus two rewrites after a failed fact check


class DraftError(Exception):
    pass


def draft_lead(lead: dict, auditor, *, your_name: str, fetch=homepage.fetch_homepage_text) -> dict:
    homepage_text = fetch(lead.get("domain") or "")
    if homepage_text:
        # The model may not write dashes, so a quoted "17-branch" comes back as
        # "17 branch"; give it, and the quote check, the same dashless text.
        homepage_text = " ".join(re.sub(r"[-‐-―]", " ", homepage_text).split())
    verify = getattr(auditor, "verify_import_email", None)
    feedback: list[str] | None = None
    best = None  # (issue count, result, issues)
    total_cost = 0.0
    checked = False
    for _ in range(MAX_ATTEMPTS):
        if feedback:
            result = auditor.generate_import_email(lead, homepage_text, your_name, feedback=feedback)
        else:
            result = auditor.generate_import_email(lead, homepage_text, your_name)
        if not result:
            if best is None:
                raise DraftError("Every AI provider failed to produce a draft. Try again in a moment.")
            break
        total_cost += result.get("ai_cost", 0.0)
        issues: list[str] = []
        if verify is not None:
            outcome = verify(
                lead, homepage_text, " ".join(result["subject"].split()),
                compose.clean_paragraphs(result["paragraphs"]),
            )
            if outcome is not None:
                checked = True
                total_cost += outcome.get("ai_cost", 0.0)
                issues = outcome["issues"]
        if best is None or len(issues) < best[0]:
            best = (len(issues), result, issues)
        if not issues:
            break
        feedback = issues
    _, result, issues = best

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
    words = sum(len(p.split()) for p in compose.clean_paragraphs(paragraphs))
    if words > MAX_BODY_WORDS:
        warnings.append(f"Body is {words} words; keep it at {MAX_BODY_WORDS} or fewer.")
    warnings.extend(f"Fact check: {i}" for i in issues)
    if verify is not None and not checked:
        warnings.append("Fact check could not run; verify every claim against their website yourself.")
    return {
        "subject": subject, "body": body, "review_warnings": warnings,
        "source_quote": quote, "ai_cost": total_cost,
    }

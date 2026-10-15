"""
Assembly and quality checks for an import email.

The pitch line is appended by code, not written by the model: it must stay
true (the old BaseSender._AUTOMATION_PITCH_LINE says "the issues above were
found", which is false for an import where no audit ran) and it must never
claim that no human reviewed the email, because every draft is reviewed
before it can be sent.
"""
import re

from analyzer.ai_audit import AIAuditor

MAX_SUBJECT_CHARS = 50
_LINK_OR_EMAIL = re.compile(r"https?://|www\.|[\w.+]+@[\w]+\.[\w.]+", re.IGNORECASE)

FIXED_LINE = (
    "Worth mentioning how this reached you: this email was drafted and sent by a system we "
    "built ourselves. That is the kind of automation we build for other teams, so if "
    "repetitive work is eating into your day, it is the same conversation."
)

_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "mx"}


def _first_name(person_name: str) -> str:
    for token in (person_name or "").split():
        token = token.strip(".,")
        if token.lower() in _HONORIFICS:
            continue
        return token if token.replace("'", "").isalpha() else ""
    return ""


def compose_body(person_name: str, paragraphs: list[str], your_name: str) -> str:
    first = _first_name(person_name)
    greeting = f"Hi {first}," if first else "Hi there,"
    parts = [greeting, *[p.strip() for p in paragraphs if p.strip()], FIXED_LINE, your_name]
    return "\n\n".join(parts)


def facts_text(lead: dict) -> str:
    """Everything the model was told about the recipient, for the number check."""
    keys = ("person_name", "person_title", "organization", "industry", "city", "state", "country", "domain")
    return " ".join(str(lead.get(k) or "") for k in keys)


def subject_length_warning(subject: str) -> str:
    return f"Subject is {len(subject)} characters; keep it at {MAX_SUBJECT_CHARS} or fewer."


def is_evidence_warning(warning: str) -> bool:
    return warning.startswith("No homepage evidence") or "source quote" in warning


def run_checks(
    subject: str, body: str, *, company: str, facts_text: str, homepage_text: str | None,
    source_quote: str, check_evidence: bool = True, carried_warnings=(),
) -> list[str]:
    """
    The project's existing static checkers, adapted to an import email. The
    whole body is treated as one paragraph so greeting, model text, pitch line
    and sign off are all covered. In edit mode (check_evidence False) the
    homepage text is no longer to hand, so the evidence warnings from the
    original draft are carried forward instead of recomputed.
    """
    parsed = {
        "email_subject": subject,
        "opening_line": "",
        "flaws": [{"paragraph": body, "source_quote": source_quote or ""}],
    }
    corpus = f"{facts_text}\n{homepage_text or ''}\n{source_quote or ''}"
    warnings = [w for w in (
        AIAuditor._check_spam_trigger_words(parsed, company),
        AIAuditor._check_ai_sounding_phrases(parsed, company),
        AIAuditor._check_forbidden_dashes(parsed, company),
        AIAuditor._check_number_hallucination(parsed, corpus, company),
    ) if w]
    if _LINK_OR_EMAIL.search(f"{subject} {body}"):
        warnings.append("Contains a link or email address; the email should not include any. Review before sending.")
    if check_evidence:
        if not homepage_text or not source_quote:
            warnings.append(
                "No homepage evidence: this draft is based only on the CSV fields, "
                "so it should make no factual claim about the company."
            )
        else:
            quote_warning = AIAuditor._verify_source_quotes(parsed, homepage_text, company)
            if quote_warning:
                warnings.append(quote_warning)
    warnings.extend(carried_warnings)
    return warnings

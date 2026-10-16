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
    "MMGA Agency designs and builds custom automation for businesses, taking repetitive "
    "reporting, data entry and document handling off the team's plate. Would a short "
    "walkthrough of how this could work for your team be useful?"
)

BRAND = "MMGA"


def mask_brand(text: str) -> str:
    """The brand is an acronym on purpose; keep it out of the ALL CAPS check."""
    return (text or "").replace(BRAND, BRAND.title())

_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "mx"}


def _first_name(person_name: str) -> str:
    for token in (person_name or "").split():
        token = token.strip(".,")
        if token.lower() in _HONORIFICS:
            continue
        return token if token.replace("'", "").isalpha() else ""
    return ""


def _drop_brand_sentences(paragraph: str) -> str:
    """MMGA is introduced once, by FIXED_LINE; a model sentence about it is a duplicate."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
    return " ".join(s for s in sentences if BRAND.lower() not in s.lower())


def compose_body(person_name: str, paragraphs: list[str], your_name: str) -> str:
    paragraphs = [_drop_brand_sentences(p) for p in paragraphs]
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
    def masked(text: str) -> str:
        # The company's own acronym (APM, ABC Bank) is not shouting either.
        text = mask_brand(text)
        for word in re.findall(r"\b[A-Z]{3,}\b", company or ""):
            text = re.sub(rf"\b{word}\b", word.title(), text)
        return text

    parsed = {
        "email_subject": masked(subject),
        "opening_line": "",
        "flaws": [{"paragraph": masked(body), "source_quote": source_quote or ""}],
    }
    corpus = f"{facts_text}\n{homepage_text or ''}\n{source_quote or ''}"
    warnings = [w for w in (
        AIAuditor._check_spam_trigger_words(parsed, company),
        AIAuditor._check_ai_sounding_phrases(parsed, company),
        AIAuditor._check_forbidden_dashes(parsed, company),
        AIAuditor._check_number_hallucination(parsed, corpus, company),
    ) if w]
    if subject[:1].islower():
        warnings.append("Subject starts with a lowercase letter; capitalize it before sending.")
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

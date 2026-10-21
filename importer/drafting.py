"""Draft one import lead: fetch evidence, ask the model, assemble, check."""
import hashlib
import os
import re

from importer import compose, email_html, email_images, homepage

MAX_SUBJECT_CHARS = compose.MAX_SUBJECT_CHARS
MAX_BODY_WORDS = 130
MAX_ATTEMPTS = 3  # first draft plus two rewrites after a failed fact check
MIN_DESIGN_SHOTS = 2
MAX_DESIGN_SHOTS = email_html.MAX_SECTIONS
MIN_SUBJECT_WORDS, MAX_SUBJECT_WORDS = 2, 6
PROVIDERS_FAILED = "Every AI provider failed to produce a draft. Try again in a moment."
COULD_NOT_RUN = "Fact check could not run; verify every claim against their website yourself."


class DraftError(Exception):
    pass


class DesignEditError(ValueError):
    """A design edit the user must correct (the route answers 422)."""


# Every visible hyphen or dash, Unicode ones included: hyphen minus, U+2010 to
# U+2015, minus, Armenian hyphen, double oblique hyphen, hyphen bullet, wave
# dash, the two and three em dashes, and the small and fullwidth forms. Each
# becomes a space. The soft hyphen (U+00AD) is an invisible hyphenation hint
# inside a word, so it is deleted instead ("Bank­ing" is "Banking").
# _dashless is the one helper for the homepage text and the designed copy.
_ANY_DASH = re.compile("[\\-‐-―−֊⸗⁃〜⸺⸻﹘﹣－]")
_SOFT_HYPHEN = "­"


def _dashless(text: str | None) -> str | None:
    # The model may not write dashes, so a quoted "17-branch" comes back as
    # "17 branch"; give it, and the quote check, the same dashless text.
    if not text:
        return text
    return " ".join(_ANY_DASH.sub(" ", text.replace(_SOFT_HYPHEN, "")).split())


def _best_attempt(generate, check):
    """
    The verify loop shared by both drafts: up to MAX_ATTEMPTS, each failed
    check feeding its issues back to the generator, keeping the attempt with
    the fewest issues. generate(feedback) returns a result dict or None;
    check(result) returns (issues, cost, checker_ran).
    Returns (result, issues, total_cost, checked).
    """
    feedback: list[str] | None = None
    best = None  # (issue count, result, issues)
    total_cost = 0.0
    checked = False
    for _ in range(MAX_ATTEMPTS):
        result = generate(feedback)
        if not result:
            if best is None:
                raise DraftError(PROVIDERS_FAILED)
            break
        total_cost += result.get("ai_cost", 0.0)
        issues, cost, ran = check(result)
        total_cost += cost
        checked = checked or ran
        if best is None or len(issues) < best[0]:
            best = (len(issues), result, issues)
        if not issues:
            break
        feedback = issues
    _, result, issues = best
    return result, issues, total_cost, checked


def _verify(verify, lead, evidence, subject, paragraphs):
    """One fact check call as (issues, cost, ran); a missing or failed checker is (.., False)."""
    if verify is None:
        return [], 0.0, False
    outcome = verify(lead, evidence, subject, paragraphs)
    if outcome is None:
        return [], 0.0, False
    return outcome["issues"], outcome.get("ai_cost", 0.0), True


def _fact_check_warnings(issues, verify, checked) -> list[str]:
    warnings = [f"Fact check: {i}" for i in issues]
    if verify is not None and not checked:
        warnings.append(COULD_NOT_RUN)
    return warnings


def draft_lead(lead: dict, auditor, *, your_name: str, fetch=homepage.fetch_homepage_text) -> dict:
    homepage_text = _dashless(fetch(lead.get("domain") or ""))
    verify = getattr(auditor, "verify_import_email", None)

    def generate(feedback):
        if feedback:
            return auditor.generate_import_email(lead, homepage_text, your_name, feedback=feedback)
        return auditor.generate_import_email(lead, homepage_text, your_name)

    def check(result):
        return _verify(
            verify, lead, homepage_text, " ".join(result["subject"].split()),
            compose.clean_paragraphs(result["paragraphs"]),
        )

    result, issues, total_cost, checked = _best_attempt(generate, check)

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
    warnings.extend(_fact_check_warnings(issues, verify, checked))
    return {
        "subject": subject, "body": body, "review_warnings": warnings,
        "source_quote": quote, "ai_cost": total_cost,
    }


# ---------------------------------------------------------------------------
# Designed flaw email (2026-10-21): the copy is checked deterministically
# here, because the AI parser only checks its shape, never its content.
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d+")
_LINK = re.compile(
    r"https?://|www\.|[\w.+]+@[\w]+\.[\w.]+"
    r"|\b[a-z0-9]+\.(?:com|net|org|io|co|us|biz|info|ai|app|dev|uk|ca|au|in|de|me)\b",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\+?\(?\d[\d\s().]{6,}\d")
_BRAND = re.compile(r"\bmmga\b", re.IGNORECASE)
_SPAM = re.compile(r"\b(free|guarantee\w*|urgent|cash|winner)\b", re.IGNORECASE)


def _clean(text) -> str:
    return _dashless(str(text or "")) or ""


def _clean_copy(copy: dict) -> dict:
    """The copy with every dash replaced by a space and whitespace collapsed. Never mutates."""
    return {
        **copy,
        "subject": _clean(copy.get("subject")),
        "headline": _clean(copy.get("headline")),
        "opening": _clean(copy.get("opening")),
        "sections": [
            {"index": s.get("index"), "title": _clean(s.get("title")), "sentence": _clean(s.get("sentence"))}
            for s in copy.get("sections") or []
        ],
    }


def _without_organization(text: str, organization: str | None) -> str:
    """text with the lead's own company name (as the cleaner writes it) blanked
    out, so a name like Overstock.com, Cash App or 3M is not a rule break.
    Only the name used as a name: never inside a larger token or a link
    (acme.com, evil.acme.com, info@Overstock.com, Overstock.com/offer, 3M.40);
    a sentence period after it ("We looked at Zillow.") is still a name."""
    name = _clean(organization)
    if not name:
        return text
    return re.sub(rf"(?<![\w.@/]){re.escape(name)}(?![\w@/]|\.\w)", " ", text, flags=re.IGNORECASE)


def copy_issues(copy: dict, flaw_count: int, organization: str | None = None) -> list[str]:
    """Deterministic rule breaks in already cleaned copy, phrased as checker notes.
    The organization's own name is exempt from the number, link and spam rules."""
    subject = copy.get("subject") or ""
    parts = [subject, copy.get("headline") or "", copy.get("opening") or ""]
    for s in copy.get("sections") or []:
        parts += [s.get("title") or "", s.get("sentence") or ""]
    text = " ".join(parts)
    masked = _without_organization(text, organization)
    issues = []
    if not all(parts):
        issues.append("A subject, headline, opening, title or sentence is empty; fill in every one.")
    numbers = sorted({n for n in _NUMBER.findall(masked) if n != str(flaw_count)})
    if numbers:
        issues.append(
            f"Contains a number other than the flaw count ({flaw_count}): {', '.join(numbers)}. "
            "Write no numbers except the count of problems."
        )
    if _LINK.search(masked):
        issues.append("Contains a link or email address; the copy must not include any.")
    if _PHONE.search(text):
        issues.append("Contains a phone number; the copy must not include any.")
    if _BRAND.search(text):
        issues.append("Mentions MMGA; the copy must never mention MMGA.")
    words = len(subject.split())
    if len(subject) > MAX_SUBJECT_CHARS or not MIN_SUBJECT_WORDS <= words <= MAX_SUBJECT_WORDS:
        issues.append(
            f"Subject is {len(subject)} characters and {words} words; it must be "
            f"{MIN_SUBJECT_WORDS} to {MAX_SUBJECT_WORDS} words and at most {MAX_SUBJECT_CHARS} characters."
        )
    spam = sorted({w.lower() for w in _SPAM.findall(masked)})
    if spam:
        issues.append(f"Contains the spam trigger word(s) {', '.join(spam)}; never use them.")
    return issues


def _flaw_evidence(homepage_text: str | None, shots: list[dict]) -> str:
    """Homepage text plus the measured flaws: both are evidence the checker may cite."""
    lines = ["MEASURED FLAWS ON THE SITE:"]
    lines += [f"{_dashless(s.get('page')) or 'the site'}: {_dashless(s.get('description')) or ''}" for s in shots]
    flaws = "\n".join(lines)
    return f"{homepage_text}\n{flaws}" if homepage_text else flaws


def _design_checks(lead: dict, subject: str, body: str, flaw_count: int) -> list[str]:
    """compose.run_checks over the designed text body. The renderer numbers the
    sections and names the HTML version, so those are not hallucinated numbers
    or shouting; the evidence warnings are left out because a designed draft's
    evidence is the measured flaws, which the fact checker covers."""
    facts = f"{compose.facts_text(lead)} {' '.join(str(n) for n in range(1, flaw_count + 1))}"
    return compose.run_checks(
        subject, body.replace("HTML", "Html"),
        company=lead.get("organization") or lead.get("domain") or "this lead",
        facts_text=facts, homepage_text=None, source_quote="", check_evidence=False,
    )


def _render(lead: dict, copy: dict, shots: list[dict], reply_to: str) -> tuple[str, str]:
    return email_html.render(
        copy, shots, first_name=compose._first_name(lead.get("person_name") or ""),
        company=lead.get("organization") or "", reply_to=reply_to,
    )


def shot_fingerprints(shots: list[dict], prepared: list[tuple[str, bytes]] | None = None) -> list[dict]:
    """{"path", "description", "sha256"} per shot, the hash taken over the exact
    bytes email_images.prepare_all produces (what would be attached and sent).
    Raises whatever prepare_all raises when prepared is not given."""
    if prepared is None:
        prepared = email_images.prepare_all(shots)
    return [
        {"path": s.get("path"), "description": s.get("description"),
         "sha256": hashlib.sha256(data).hexdigest()}
        for s, (_cid, data) in zip(shots, prepared)
    ]


def shots_match_design(lead: dict) -> bool:
    """True while the screenshots are the ones the email was drafted with.
    Shot file names are deterministic, so a re-audit overwrites the same paths
    with new images: every shot's path, description and freshly prepared bytes
    must match what was stored at draft time. Never raises."""
    try:
        stored = (lead.get("design_json") or {}).get("shot_fingerprints")
        shots = lead.get("flaw_shots") or []
        if not stored or not isinstance(stored, list) or len(stored) != len(shots):
            return False
        return shot_fingerprints(shots) == stored
    except Exception:
        return False


def is_designable(lead: dict) -> bool:
    shots = lead.get("flaw_shots") or []
    return (
        lead.get("audit_status") == "done"
        and MIN_DESIGN_SHOTS <= len(shots) <= MAX_DESIGN_SHOTS
        and all(s.get("path") and os.path.isfile(s["path"]) for s in shots)
    )


def _as_text(lead, auditor, your_name, fetch, *, reason=None, cost=0.0) -> dict:
    out = draft_lead(lead, auditor, your_name=your_name, fetch=fetch)
    if reason:
        out["review_warnings"].append(f"Drafted as a text email because {reason}")
    out["ai_cost"] += cost
    return {**out, "html_body": None, "design": None}


def draft_designed(lead: dict, auditor, *, your_name: str, reply_to: str,
                   fetch=homepage.fetch_homepage_text) -> dict:
    """
    The designed flaw email for a lead whose audit is done. A lead that cannot
    be designed falls back to draft_lead (html_body and design are then None).
    """
    if not is_designable(lead):
        return _as_text(lead, auditor, your_name, fetch)
    shots = lead["flaw_shots"]
    try:
        # The exact images the sender will attach; a file that cannot be
        # prepared means no designed email, found before any copy is paid for.
        fingerprints = shot_fingerprints(shots, email_images.prepare_all(shots))
    except Exception as e:
        return _as_text(lead, auditor, your_name, fetch,
                        reason=f"a screenshot could not be prepared ({type(e).__name__}).")

    homepage_text = _dashless(fetch(lead.get("domain") or ""))
    evidence = _flaw_evidence(homepage_text, shots)
    verify = getattr(auditor, "verify_import_email", None)
    count = len(shots)
    organization = lead.get("organization")

    def generate(feedback):
        copy = auditor.generate_flaw_email_copy(lead, homepage_text, shots, your_name, feedback=feedback)
        return _clean_copy(copy) if copy else None

    def check(copy):
        issues, cost, ran = _verify(
            verify, lead, evidence, copy["subject"],
            [copy["opening"], *(s["sentence"] for s in copy["sections"])],
        )
        return copy_issues(copy, count, organization) + issues, cost, ran

    copy, issues, total_cost, checked = _best_attempt(generate, check)
    try:
        html, text = _render(lead, copy, shots, reply_to)
    except Exception as e:
        return _as_text(lead, auditor, your_name, fetch, cost=total_cost,
                        reason=f"the designed email could not be built ({e}).")

    warnings = _design_checks(lead, copy["subject"], text, count)
    warnings.extend(_fact_check_warnings(issues, verify, checked))
    return {
        "subject": copy["subject"], "body": text, "html_body": html,
        "design": {"opening": copy["opening"], "headline": copy["headline"], "sections": copy["sections"],
                   "shot_fingerprints": fingerprints},
        "review_warnings": warnings, "source_quote": "", "ai_cost": total_cost,
    }


def redesign(lead: dict, subject: str, opening: str, sections: list[dict], *, reply_to: str) -> dict:
    """
    Apply a human edit of the designed copy (subject, opening, one sentence
    per section; the headline and titles stay). Raises DesignEditError for
    input the user must fix. The copy rules become warnings, not rewrites.
    Returns {"subject", "body", "html_body", "design", "review_warnings"}.
    """
    design = lead.get("design_json") or {}
    old_sections = design.get("sections") or []
    shots = lead.get("flaw_shots") or []
    values = [subject, opening, *(s.get("sentence") for s in sections)]
    if any(not isinstance(v, str) for v in values):
        raise DesignEditError("Subject, opening and every sentence must be text.")
    if any("<" in v or ">" in v for v in values):
        raise DesignEditError("Remove the < and > characters; markup is not allowed.")
    if len(sections) != len(old_sections) or len(sections) != len(shots):
        raise DesignEditError(f"Expected {len(old_sections)} sections, got {len(sections)}.")
    if [s.get("index") for s in sections] != list(range(len(sections))):
        raise DesignEditError("Sections must keep their order, with indexes 0, 1, 2 and so on.")

    copy = _clean_copy({
        "subject": subject, "headline": design.get("headline") or "", "opening": opening,
        "sections": [
            {"index": i, "title": old.get("title") or "", "sentence": new["sentence"]}
            for i, (old, new) in enumerate(zip(old_sections, sections))
        ],
    })
    if not all([copy["subject"], copy["opening"], *(s["sentence"] for s in copy["sections"])]):
        raise DesignEditError("Subject, opening and every sentence must be filled in.")
    try:
        html, text = _render(lead, copy, shots, reply_to)
    except Exception as e:
        raise DesignEditError(f"The designed email could not be built ({e}); draft it again.") from e
    warnings = _design_checks(lead, copy["subject"], text, len(shots))
    warnings.extend(f"Copy check: {i}" for i in copy_issues(copy, len(shots), lead.get("organization")))
    # design keeps the shot fingerprints stored at draft time: an edit changes
    # the words, never which screenshots the email was checked against.
    return {
        "subject": copy["subject"], "body": text, "html_body": html,
        "design": {**design, "opening": copy["opening"], "headline": copy["headline"],
                   "sections": copy["sections"]},
        "review_warnings": warnings,
    }

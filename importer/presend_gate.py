"""
Deterministic last check before an imported lead's email leaves. The model's
instructions are a floor, not a guarantee, so these run in code at send time.
Idea borrowed from the Harvey project's pre-send gate (MIT).

Unlike the review warnings, a failure here cannot be acknowledged away: each
item is something that is wrong whatever the reviewer thinks (an unfilled
placeholder, an empty body, a spam trigger phrase).
"""
import re

MAX_SUBJECT_LEN = 90

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# {{name}}, {name}, [name], [First Name] style placeholders left unfilled.
MERGE_TAG_RE = re.compile(r"\{\{\s*[\w ]+\s*\}\}|\{\s*[\w ]+\s*\}|\[\s*(?:first|last|company|name)[\w ]*\]", re.I)

# Spam-filter triggers and generic AI tells. Whole phrases only.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your", "as an ai", "i'm an ai", "i am an ai",
    "as a language model",
)
_BANNED_RE = re.compile(
    r"(?<![\w])(?:" + "|".join(re.escape(p) for p in BANNED_PHRASES) + r")(?![\w])", re.I
)


def presend_problems(to_email: str, subject: str, body: str) -> list[str]:
    """Every hard problem with this email; empty means it may go."""
    to_email = (to_email or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    problems: list[str] = []

    if not EMAIL_RE.match(to_email):
        problems.append(f"invalid recipient address '{to_email}'")
    if not subject:
        problems.append("empty subject")
    elif len(subject) > MAX_SUBJECT_LEN:
        problems.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN} characters)")
    if not body:
        problems.append("empty body")

    tags = sorted(set(MERGE_TAG_RE.findall(f"{subject} {body}")))
    if tags:
        problems.append(f"unfilled placeholder: {tags[:3]}")

    hits = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(f"{subject} {body}")})
    if hits:
        problems.append(f"spam or generic-AI phrase: {hits[:3]}")
    return problems

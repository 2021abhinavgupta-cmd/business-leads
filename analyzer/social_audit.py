"""
Turn a SocialProfile into (1) a ranked list of concrete issues and (2)
channel-specific outreach copy.

audit_profile is deterministic: code decides the issues, the AI (in
generate_social_outreach) only writes copy about the issues code already
found. Same split as analyzer/flaws.py for websites.

Wording rules for every `detail` string: plain language, no jargon, and no
hyphens or dashes of any kind (the outreach model mirrors the style it is
shown, and the rest of this codebase's generated copy forbids dashes).
"""

import json
import re as _re
from dataclasses import dataclass

from scrapers.social.base import SocialProfile

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class SocialIssue:
    severity: str   # "high" | "medium" | "low"
    label: str
    detail: str


def audit_profile(profile: SocialProfile) -> list[SocialIssue]:
    """Ranked issues for a profile. Empty when the profile could not be
    analyzed or when nothing is wrong."""
    if not profile.analyzed:
        return []

    issues: list[SocialIssue] = []
    p = profile
    plat = p.platform

    inactive = p.posts_last_30_days == 0 or (
        p.last_post_age_days is not None and p.last_post_age_days > 45
    )
    if inactive:
        issues.append(SocialIssue(
            "high", "Inactive account",
            "The account has gone quiet, so anyone who checks it sees a page that looks abandoned.",
        ))

    if not inactive and p.posting_frequency in ("irregular", "weekly") and p.followers > 1000:
        issues.append(SocialIssue(
            "medium", "Posting has slowed",
            "Posts are going out only now and then, which is not enough to stay in front of followers.",
        ))

    if plat in ("instagram", "facebook") and p.followers > 500 and 0 < p.avg_engagement_rate < 0.5:
        issues.append(SocialIssue(
            "medium", "Low engagement",
            "Very few of the followers like or comment, so the posts are barely being seen.",
        ))

    if not p.uses_video:
        issues.append(SocialIssue(
            "medium", "Not using short video",
            "There are no reels or short videos, and that is the format the platform pushes hardest right now.",
        ))

    if plat in ("instagram", "facebook") and not p.has_link_in_bio:
        issues.append(SocialIssue(
            "medium", "No link in the bio",
            "There is no clickable link, so a visitor who wants to buy or book has nowhere to go.",
        ))

    if len(p.bio.strip()) < 20:
        issues.append(SocialIssue(
            "low", "Thin or empty bio",
            "The bio barely says what the business does or why someone should follow.",
        ))

    if plat == "instagram" and p.following > p.followers and p.followers < 2000:
        issues.append(SocialIssue(
            "low", "Following more than followers",
            "The account follows more people than follow it back, which reads as a brand that is still finding its feet.",
        ))

    if p.posts_count < 9:
        issues.append(SocialIssue(
            "low", "Thin content history",
            "There are only a few posts in total, so the page has little for a new visitor to look through.",
        ))

    issues.sort(key=lambda i: _SEVERITY_RANK[i.severity])
    return issues


# ---------------------------------------------------------------------------
# Outreach copy generation
# ---------------------------------------------------------------------------

_CHANNEL_SHAPE = {
    "email": "Write a cold outreach EMAIL. Return a subject line and a body of 60 to 110 words. "
             "The subject must be specific and hint at the problem, never a bland label.",
    "dm": "Write a short Instagram DM. No subject. 2 to 3 sentences, casual, one issue, one soft ask.",
    "whatsapp": "Write a short WhatsApp message. No subject. 2 to 3 sentences, casual and friendly, light on emoji.",
}


def _build_outreach_prompt(company, profile, issues, channel, your_name):
    issue_lines = "\n".join(f"- {i.label}: {i.detail}" for i in issues) or "- (no specific issues found)"
    shape = _CHANNEL_SHAPE.get(channel, _CHANNEL_SHAPE["email"])
    return (
        f"You write outreach for MMGA, a small agency that fixes social media presence for local businesses.\n\n"
        f"Business: {company}\n"
        f"Platform: {profile.platform}\n"
        f"Handle: @{profile.handle}\n\n"
        f"ISSUES FOUND (these are the ONLY facts you may use, do not invent any number, metric or claim beyond this list):\n"
        f"{issue_lines}\n\n"
        f"{shape}\n\n"
        f"Rules:\n"
        f"- Lead with the single most important issue stated as a business problem, not a compliment or pleasantry.\n"
        f"- Plain language a busy owner understands. No marketing jargon.\n"
        f"- Do NOT use hyphens or dashes of any kind anywhere in your output.\n"
        f"- Do NOT use generic AI phrases like 'I noticed', 'In today's competitive landscape', 'I hope this finds you well'.\n"
        f"- One low pressure call to action (offer to send a short list, not demand a meeting).\n"
        f"- Sign off as {your_name}.\n\n"
        f"Return ONLY JSON: {{\"subject\": \"...\", \"body\": \"...\"}}. "
        f"For a DM or WhatsApp message set subject to an empty string."
    )


def _parse_outreach_json(text: str) -> dict | None:
    if not text:
        return None
    fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "body" not in obj:
        return None
    return obj


def _default_ai_call(prompt: str) -> tuple[str, float]:
    """Real provider path — reuse AIAuditor's simple text-in/JSON-out
    fallback chain (the same three providers, in the same order, that the
    History tab's follow-up generator uses: Claude Haiku, then Gemini, then
    GPT-4o-mini). Each returns (raw_text, cost) or None."""
    from analyzer.ai_audit import AIAuditor
    auditor = AIAuditor()
    for call_fn in (
        auditor._call_followup_anthropic,   # noqa: SLF001
        auditor._call_followup_gemini,      # noqa: SLF001
        auditor._call_followup_openai,      # noqa: SLF001
    ):
        result = call_fn(prompt)
        if result is not None:
            return result
    return "", 0.0


def generate_social_outreach(company, profile, issues, channel, your_name="Kshitij", ai_call=None) -> dict:
    """One AI call producing outreach copy for *channel*, grounded entirely in
    *issues*. Returns {"subject", "body", "cost"}; body is "" if the model's
    reply could not be parsed (caller surfaces that, never crashes)."""
    call = ai_call or _default_ai_call
    prompt = _build_outreach_prompt(company, profile, issues, channel, your_name)
    try:
        text, cost = call(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"[SocialOutreach] provider call failed: {e}")
        return {"subject": "", "body": "", "cost": 0.0}
    parsed = _parse_outreach_json(text) or {}
    subject = "" if channel in ("dm", "whatsapp") else str(parsed.get("subject", ""))
    return {"subject": subject, "body": str(parsed.get("body", "")), "cost": float(cost or 0.0)}

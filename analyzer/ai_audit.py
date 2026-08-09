"""
AI Audit module — analyses leads using a Claude Haiku → Gemini 3 Flash →
GPT-4o-mini → OpenRouter (free) fallback chain and returns structured flaw
reports.

The prompt is populated with real Instagram + Website data so the AI
produces specific, number-backed audit results rather than generic advice.
"""

import json
import re
import base64

import anthropic
import warnings
warnings.filterwarnings("ignore", module="google.generativeai")
import google.generativeai as genai
import openai

import config
from analyzer.flaws import compute_score
from scrapers.instagram import InstagramData
from scrapers.website import WebsiteData

# ---------------------------------------------------------------------------
# Score threshold — only contact leads below this.
# Re-exported from config so existing `from analyzer.ai_audit import
# CONTACT_THRESHOLD` imports keep working, but the value is configurable
# (CONTACT_THRESHOLD env var) rather than a hardcoded constant.
# ---------------------------------------------------------------------------
CONTACT_THRESHOLD = config.CONTACT_THRESHOLD

# Low, near-deterministic temperature — this task is "quote exact numbers
# and facts from the data you were given" not creative writing, so we want
# the least-random output the provider allows rather than the typical
# chat-assistant default (which for Anthropic is 1.0, uncomfortably high for
# a fact-citation task). Kept just above 0 rather than exactly 0 so email
# copy still reads naturally instead of robotically repetitive.
_AI_TEMPERATURE = 0.2

# JSON Schema shared by every provider that supports enforced structured
# output (Anthropic tool-use, Gemini response_schema, OpenAI json_schema
# strict mode) — guarantees the exact shape _parse_json expects instead of
# relying on regex-stripping a freeform text reply, which is what used to
# cause every one of _parse_json's silent-failure paths.
_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "flaws": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph": {"type": "string"},
                    # Forces the model to point at the exact line from
                    # FLAWS DETECTED that it is writing about, which turns
                    # grounding verification from an LLM judgment call into
                    # a cheap, deterministic substring match against the
                    # prompt (see _verify_source_quotes). Asking for a
                    # citation alongside each claim is also the single
                    # highest-leverage prompt-level hallucination guard —
                    # a model that must name its source invents less.
                    "source_quote": {"type": "string"},
                },
                "required": ["paragraph", "source_quote"],
            },
        },
        "overall_score": {"type": "integer"},
        "email_subject": {"type": "string"},
        "opening_line": {"type": "string"},
    },
    "required": ["flaws", "overall_score", "email_subject", "opening_line"],
}


class AIAuditor:
    """Run AI-powered audits on lead data with provider fallback."""

    def __init__(self):
        # Configure Gemini
        if config.GEMINI_API_KEY:
            genai.configure(api_key=config.GEMINI_API_KEY)

        # Configure OpenAI
        if config.OPENAI_API_KEY:
            self._openai_client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        else:
            self._openai_client = None

        # Configure Anthropic
        if config.ANTHROPIC_API_KEY:
            self._anthropic_client = anthropic.Anthropic(
                api_key=config.ANTHROPIC_API_KEY,
            )
        else:
            self._anthropic_client = None

        # Configure OpenRouter (free-tier fallback, OpenAI-compatible endpoint)
        if config.OPENROUTER_API_KEY:
            self._openrouter_client = openai.OpenAI(
                api_key=config.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
            )
        else:
            self._openrouter_client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_lead(
        self,
        company: str,
        ig: InstagramData | None,
        web: WebsiteData,
        image_path: str | None = None,
        mobile_image_path: str | None = None,
        rating: str = "",
        reviews_count: int = 0,
    ) -> dict | None:
        """
        Analyse a lead's digital presence via an AI fallback chain.

        Args:
            company:            Business name.
            ig:                 Instagram analytics (may be ``None`` if unavailable).
            web:                Website audit data.
            image_path:         Desktop screenshot (with red-box flaw overlay if found).
            mobile_image_path:  Separate real mobile-viewport screenshot, if captured.
            rating:              Google Business rating (e.g. "4.8"), if the lead came from Maps.
            reviews_count:       Google Business review count, if the lead came from Maps.

        Returns:
            A dict with keys ``flaws``, ``overall_score``,
            ``email_subject``, ``opening_line`` — or ``None`` if every
            AI provider fails.
        """
        prompt = self._build_prompt(company, ig, web, bool(image_path), bool(mobile_image_path), rating, reviews_count)

        base64_image = self._encode_image(image_path)
        base64_mobile_image = self._encode_image(mobile_image_path)

        # Fallback chain: Claude Haiku → Gemini → GPT-4o-mini → OpenRouter (free)
        for call_fn in (
            self._call_anthropic,
            self._call_gemini,
            self._call_openai,
            self._call_openrouter,
        ):
            result = call_fn(prompt, base64_image, base64_mobile_image)
            if result is None:
                continue
                
            raw, cost = result
            parsed = self._parse_json(raw)
            if parsed is not None:
                parsed["ai_cost"] = cost

                # Self-consistency is effectively a seventh review check, and
                # its total-disagreement case is the STRONGEST fabrication
                # signal available here (two independent samples of the same
                # prompt citing no finding in common). It was log-only until
                # 2026-08-09 — the same gap the other checks had before their
                # 2026-08-07 fix, missed then because it lives on this
                # separate code path rather than in the check list below.
                consistency_warning = None
                if config.AI_SELF_CONSISTENCY:
                    second = call_fn(prompt, base64_image, base64_mobile_image)
                    if second is not None:
                        second_parsed = self._parse_json(second[0])
                        parsed["ai_cost"] = cost + second[1]
                        if second_parsed is not None:
                            consistency_warning = self._apply_self_consistency(parsed, second_parsed, company)

                # Each check below used to only print()  its warning — real,
                # useful signal that nobody was actually watching, since
                # nothing reads server/Railway logs while reviewing a draft.
                # They now also return the same text, collected here into
                # review_warnings so the Drafts UI can show a visible flag
                # instead of the warning existing only in a log line no one
                # will read before the email goes out.
                review_warnings = [w for w in (
                    consistency_warning,
                    self._check_number_hallucination(parsed, prompt, company),
                    self._verify_source_quotes(parsed, prompt, company),
                    self._verify_grounding(parsed, prompt, company),
                    self._verify_visual_claims(parsed, company, image_path),
                    self._check_spam_trigger_words(parsed, company),
                    self._check_forbidden_dashes(parsed, company),
                    self._check_body_length(parsed, company, has_image=bool(image_path)),
                ) if w]
                parsed["review_warnings"] = review_warnings
                # overall_score drives should_contact()/the skip-if-too-good
                # decision, but as returned by the AI it's pure self-report
                # with no real connection to the flaw data it was given.
                # Replace it with a deterministic score computed from the
                # actual measured flaws — keep the AI's original under a
                # separate key for visibility, not because anything reads it.
                parsed["ai_reported_score"] = parsed.get("overall_score")
                parsed["overall_score"] = compute_score(getattr(web, "flaws", None) or [])

                # compute_score starts at 100 and subtracts per detected
                # flaw, so FEWER flaws means a HIGHER (healthier) score —
                # but fewer flaws is also exactly what a partially-failed
                # audit produces. Without this, a site we simply couldn't
                # measure scores as "too good to contact" and gets silently
                # discarded by the >CONTACT_THRESHOLD skip in main.py.
                # Measured on one identical site: 8 flaws -> score 19
                # (contacted); the same site with Lighthouse/pa11y/
                # html-validate returning no data -> 2 flaws -> score 76
                # (skipped). That's the default state on a Windows dev box
                # and reachable in Docker, so real leads were being thrown
                # away for want of a measurement, with nothing logged.
                signal_status = getattr(web, "signal_status", None) or {}
                degraded = [name for name, state in signal_status.items() if state != "ok"]
                parsed["partial_coverage"] = degraded
                if degraded:
                    print(f"[AIAuditor] '{company}' audited with partial signal coverage (no data from: {', '.join(sorted(degraded))}) — score {parsed['overall_score']} is an upper bound, real flaw count may be higher.")
                return parsed

        print(f"[AIAuditor] All AI providers failed or returned unparseable output for '{company}' — check API keys/quotas.")
        return None

    # Numbers that show up in nearly every generated email regardless of
    # source data — excluded from the hallucination check below so they
    # don't drown out real mismatches with constant noise. "10" is the
    # "10 minute call" CTA the prompt asks for in every email.
    #
    # This set used to also contain "1", "2" and "3" as "list positions",
    # which was a real false-negative hole: the subtraction only ever
    # applies to a number that appears NOWHERE in the prompt, so a small
    # number reaching it is not a list position — it's a specific,
    # checkable count the model made up ("you have 3 broken links" when
    # the audit found 7, "2 different fonts" when it measured 5). Those
    # are exactly the claims a business owner can verify in a minute and
    # exactly what this check exists to catch. Removing them trades a few
    # more warning banners (safe, a human reads them) for not silently
    # passing a wrong number to a real recipient (not safe).
    _BOILERPLATE_NUMBERS = {"10"}

    @staticmethod
    def _check_number_hallucination(parsed: dict, prompt: str, company: str) -> str | None:
        """
        Sanity check: the prompt instructs the AI to quote exact numbers
        from the real data it was given, but nothing actually verifies it
        did that instead of inventing a plausible-sounding one. Compares
        against the FULL rendered *prompt* text (not just web.flaws) so
        scores/timing numbers mentioned elsewhere in the prompt — e.g.
        WEBSITE DATA's page_speed_score — aren't false positives; a number
        only counts as suspicious if it appears nowhere in anything the AI
        was actually shown.

        Doesn't block or retry sending — too many legitimate false
        positives are still possible (a number split across sentences, a
        rounded figure) — but returns a warning string (also logged) so a
        hallucinated stat is visible before a real business owner receives
        a specific, checkable number that's wrong. Collected by
        analyze_lead into parsed["review_warnings"] for the Drafts UI,
        since a print() nobody watches isn't actually a safety net.
        """
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", prompt))

        email_text = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        email_numbers = set(re.findall(r"\d+(?:\.\d+)?", email_text))

        suspicious = (email_numbers - source_numbers) - AIAuditor._BOILERPLATE_NUMBERS
        if suspicious:
            message = f"Cites number(s) {sorted(suspicious)} not found anywhere in the source data — possible hallucination, review before sending."
            print(f"[AIAuditor] WARNING: email for '{company}' {message[0].lower()}{message[1:]}")
            return message
        return None

    # Classic spam-filter trigger words/phrases (case insensitive substring
    # match) — the prompt already instructs the AI to avoid these, this is
    # the same belt-and-suspenders pattern as _check_number_hallucination /
    # _verify_grounding: a prevention instruction can be ignored, so also
    # detect after the fact and log a warning before a flagged email goes out.
    _SPAM_TRIGGER_WORDS = [
        "free", "guarantee", "guaranteed", "click here", "buy now",
        "act now", "limited time", "no obligation", "risk free",
        "cash bonus", "$$$", "100% free", "% off", "congratulations",
        "winner", "urgent", "don't delete", "dear friend", "act immediately",
        "best price", "no cost", "cash",
    ]

    @staticmethod
    def _check_spam_trigger_words(parsed: dict, company: str) -> str | None:
        """
        Scan of the generated subject + flaw paragraphs + opening line for
        classic spam-filter trigger words/phrases and ALL CAPS shouting,
        both of which independently hurt inbox placement regardless of
        sender reputation. Doesn't block/retry sending, same rationale as
        the other checks in this file. Returns a warning string (also
        logged) collected into parsed["review_warnings"] — see
        _check_number_hallucination's docstring for why.
        """
        subject = parsed.get("email_subject", "") or ""
        opening = parsed.get("opening_line", "") or ""
        paragraphs = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        full_text = f"{subject} {opening} {paragraphs}"
        lower_text = full_text.lower()

        hits = [w for w in AIAuditor._SPAM_TRIGGER_WORDS if w in lower_text]

        all_caps_words = [
            w for w in re.findall(r"\b[A-Za-z]{3,}\b", full_text)
            if w.isupper()
        ]

        if hits or all_caps_words:
            parts = []
            if hits:
                parts.append(f"trigger word(s) {hits}")
            if all_caps_words:
                parts.append(f"ALL CAPS word(s) {all_caps_words}")
            message = f"Contains {' and '.join(parts)} — spam-filter risk, review before sending."
            print(f"[AIAuditor] WARNING: email for '{company}' {message[0].lower()}{message[1:]}")
            return message
        return None

    # Characters the prompt explicitly forbids ("NEVER use hyphens (-) or
    # dashes (—) anywhere in your response"), so that the copy reads like a
    # person typing quickly rather than like generated text.
    _FORBIDDEN_DASHES = ("—", "–", "-")

    @staticmethod
    def _check_forbidden_dashes(parsed: dict, company: str) -> str | None:
        """
        Verify the no-dashes instruction was actually followed.

        Worth checking rather than trusting, for two reasons. First, nothing
        verified it before — it was one instruction among a dozen competing
        CRITICAL INSTRUCTIONs. Second, and more importantly, **the prompt
        itself is written in em dashes**: nearly every instruction line uses
        one, and until 2026-08-09 so did the red-box worked example. A model
        mirrors the style of what it is shown, so a rule stated in prose that
        the prose then violates is a weak rule fighting a strong
        demonstration. Cheap, deterministic, no API call.

        Log-and-flag only, like every other check here — an em dash is a tone
        problem, not a false claim, and blocking a send over one would be
        worse than the dash.
        """
        subject = parsed.get("email_subject", "") or ""
        opening = parsed.get("opening_line", "") or ""
        paragraphs = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        full_text = f"{subject} {opening} {paragraphs}"

        found = sorted({d for d in AIAuditor._FORBIDDEN_DASHES if d in full_text})
        if not found:
            return None

        message = (
            f"Copy contains dash character(s) {found}, which the prompt forbids — "
            "em dashes in particular read as generated text rather than a person typing."
        )
        print(f"[AIAuditor] WARNING: email for '{company}' contains forbidden dash character(s) {found}.")
        return message

    # Below this, an embedded screenshot dominates the message and the
    # text:image ratio itself reads as spammy to some filters regardless of
    # what the text actually says.
    _MIN_BODY_WORD_COUNT = 40

    @staticmethod
    def _check_body_length(parsed: dict, company: str, has_image: bool) -> str | None:
        """
        Flags a body that's too short relative to the embedded screenshot
        it ships with. Only meaningful when an image is actually attached —
        a short text-only email isn't the same spam signal. Returns a
        warning string (also logged), same collection pattern as the other
        checks in this file.
        """
        if not has_image:
            return None
        paragraphs = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        opening = parsed.get("opening_line", "") or ""
        word_count = len(f"{opening} {paragraphs}".split())
        if word_count < AIAuditor._MIN_BODY_WORD_COUNT:
            message = f"Only {word_count} words with an embedded screenshot attached — low text:image ratio can itself look spammy, review before sending."
            print(f"[AIAuditor] WARNING: email for '{company}' is {message[0].lower()}{message[1:]}")
            return message
        return None

    @staticmethod
    def _apply_self_consistency(parsed: dict, second: dict, company: str) -> str | None:
        """
        Keep only the claims that BOTH independent samples chose to write
        about, identified by the source line each one cites.

        Two runs will always word their paragraphs differently, so comparing
        prose would be meaningless — but `source_quote` names the specific
        measured finding behind each claim, which makes agreement checkable
        exactly. A genuine finding sits at the top of the ranked flaw list
        and gets picked by both runs; a fabricated one is a coin flip and
        usually doesn't recur.

        Mutates `parsed` in place, and deliberately refuses to empty it: if
        the two runs happen to overlap on nothing, the original is kept and
        the disagreement logged, since an email with no content at all is a
        worse outcome than one needing review.
        """
        first_flaws = parsed.get("flaws") or []
        second_quotes = {
            AIAuditor._normalise_for_match(f.get("source_quote", ""))
            for f in (second.get("flaws") or [])
            if f.get("source_quote")
        }
        if not first_flaws or not second_quotes:
            return None

        agreed = [
            f for f in first_flaws
            if AIAuditor._normalise_for_match(f.get("source_quote", "")) in second_quotes
        ]
        dropped = len(first_flaws) - len(agreed)

        if not agreed:
            message = (
                "The two independent AI samples cited entirely NO findings in common — "
                "the strongest available signal that these claims may be invented. The first "
                "sample was kept unchanged (an empty email is worse), but read this one closely."
            )
            print(f"[AIAuditor] Self-consistency: the two samples for '{company}' cited entirely different findings — keeping the first sample unchanged, but this email is worth a closer read before sending.")
            return message

        parsed["flaws"] = agreed
        if dropped:
            print(f"[AIAuditor] Self-consistency: dropped {dropped} of {len(first_flaws)} claim(s) for '{company}' that only appeared in one of two samples (kept {len(agreed)}).")
            return (
                f"Dropped {dropped} of {len(first_flaws)} claim(s) that appeared in only one of two "
                f"independent AI samples (kept {len(agreed)}) — a claim that doesn't recur is more "
                "likely to have been invented."
            )
        return None

    @staticmethod
    def _normalise_for_match(text: str) -> str:
        """Collapse whitespace/case so trivial reformatting isn't a mismatch."""
        return " ".join(text.split()).strip().lower()

    @staticmethod
    def _verify_source_quotes(parsed: dict, prompt: str, company: str) -> str | None:
        """
        Check each flaw's `source_quote` actually appears in the prompt.

        This is the cheapest and strictest of the three grounding checks:
        no extra LLM call, no judgment, just a substring match. A quote the
        model invented cannot be found in the source text, so a fabricated
        claim is caught deterministically rather than probabilistically —
        _check_number_hallucination only catches invented *numbers*, and
        _verify_grounding is itself an LLM and can be wrong in both
        directions.

        Doesn't block sending, consistent with the other checks: a
        paraphrase that drifts slightly from the source line is a false
        positive, and blocking a send on that would be worse than flagging
        it for review. Returns a warning string (also logged), collected
        into parsed["review_warnings"].
        """
        flaws = parsed.get("flaws", [])
        if not flaws:
            return None

        haystack = AIAuditor._normalise_for_match(prompt)
        unverified = []
        for i, flaw in enumerate(flaws, start=1):
            quote = (flaw.get("source_quote") or "").strip()
            if not quote:
                unverified.append(f"#{i} (no source quote given)")
                continue
            if AIAuditor._normalise_for_match(quote) not in haystack:
                unverified.append(f"#{i} \"{quote[:80]}\"")

        if unverified:
            message = f"{len(unverified)} claim(s) cite a source quote that does NOT appear in the audit data — likely fabricated, review before sending: {unverified}"
            print(f"[AIAuditor] WARNING: {len(unverified)} claim(s) for '{company}' cite a source quote that does NOT appear in the audit data — likely fabricated, review before sending: {unverified}")
            return message
        return None

    def _verify_grounding(self, parsed: dict, prompt: str, company: str) -> str | None:
        """
        Free-form-claim counterpart to _check_number_hallucination above:
        that regex check only catches invented *numbers*, but a claim like
        "you don't have a mobile version of your site" when you do isn't
        numeric and slides right past it. Fires one more cheap, low-
        temperature LLM call asking a simple yes/no per flaw paragraph:
        "is this claim grounded in the source data, or invented?" — an
        LLM-as-judge pass, independent of which provider generated the
        copy (fixed preference order below, not necessarily the same one).

        Doesn't block/retry sending, exactly like the hallucination check:
        too many legitimate borderline judgment calls (a fair inference vs.
        a fabrication) to safely auto-block on, but a flagged claim should
        be visible before a real business owner receives a specific,
        checkable claim about their own site that's wrong. Returns a
        warning string (also logged), collected into
        parsed["review_warnings"].
        """
        flaws = parsed.get("flaws", [])
        if not flaws:
            return None

        claims = "\n".join(f"{i+1}. {f.get('paragraph', '')}" for i, f in enumerate(flaws))
        judge_prompt = (
            "You are a strict fact checker. Below is SOURCE DATA about a business's "
            "website/social media, followed by a list of numbered CLAIMS written about "
            "that business.\n\n"
            f"SOURCE DATA:\n{prompt[:6000]}\n\n"
            f"CLAIMS:\n{claims}\n\n"
            "For each claim number, decide if it is factually grounded in SOURCE DATA "
            "(a fair paraphrase or reasonable inference counts as grounded) or if it "
            "states something not supported by SOURCE DATA (fabricated/invented).\n"
            'Return ONLY valid JSON: {"unsupported": [claim numbers that are NOT grounded]}. '
            "Empty array if all claims are grounded. No markdown, no explanation."
        )

        try:
            raw = self._call_judge(judge_prompt)
            if not raw:
                return None
            result = self._parse_json_loose(raw)
            unsupported = result.get("unsupported") if result else None
            if unsupported:
                flagged = [claims.splitlines()[i - 1] for i in unsupported if 0 < i <= len(flaws)]
                message = f"Grounding check flagged {len(flagged)} claim(s) as possibly unsupported by source data — review before sending: {flagged}"
                print(f"[AIAuditor] WARNING: {message[0].lower()}{message[1:]} (for '{company}')")
                return message
        except Exception as e:
            print(f"[AIAuditor] Grounding verification skipped (non-critical): {e}")
        return None

    def _call_judge(self, judge_prompt: str) -> str | None:
        """
        Cheapest available text-only call for the grounding check above —
        fixed preference order (Gemini Flash, then Haiku, then GPT-4o-mini),
        independent of which provider actually generated the audit copy
        being checked, since a model re-checking its own output is a weaker
        signal than a second opinion. Falls through silently (returns None)
        if nothing is configured — the grounding check is a bonus safety
        net, not something the audit should ever fail over.
        """
        if config.GEMINI_API_KEY:
            try:
                model = genai.GenerativeModel(
                    "gemini-3.5-flash",
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json", temperature=0.0,
                    ),
                )
                return model.generate_content(judge_prompt).text
            except Exception:
                pass
        if self._anthropic_client:
            try:
                message = self._anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    temperature=0.0,
                    messages=[{"role": "user", "content": judge_prompt}],
                )
                return message.content[0].text
            except Exception:
                pass
        if self._openai_client:
            try:
                response = self._openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": judge_prompt}],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except Exception:
                pass
        return None

    # Words that mark a claim as being about how the site *looks* rather than
    # about anything in the measured audit data. Used to route only those
    # claims to the (more expensive, image-carrying) vision judge below.
    _VISUAL_CLAIM_KEYWORDS = (
        "screenshot", "font", "typography", "align", "spacing", "layout",
        "colour", "color", "cluttered", "blurry", "pixelated", "overlap",
        "looks ", "visual", "design", "red box",
    )

    @staticmethod
    def _looks_like_a_visual_claim(text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword in lowered for keyword in AIAuditor._VISUAL_CLAIM_KEYWORDS)

    def _verify_visual_claims(self, parsed: dict, company: str, image_path: str | None) -> str | None:
        """
        Grounding check for claims about the screenshot, which every other
        check is structurally blind to.

        _verify_grounding sends the judge the prompt TEXT only, so a claim
        like "the fonts in your hero and nav don't match" is judged against
        source data that contains no visual information whatsoever — the
        judge can only guess, in either direction. _verify_source_quotes
        can't help either, since a visual observation has no line in FLAWS
        DETECTED to quote. That left the single highest-risk claim type in
        the email (the recipient is looking at their own site while reading
        it) as the one claim nothing could actually check.

        This sends the real screenshot plus only the visual-sounding claims
        to a vision-capable judge and asks whether each is actually visible
        in the image. Same conventions as the other checks: never blocks a
        send, degrades silently to None if no vision provider is configured,
        and returns a warning string collected into review_warnings.
        """
        if not image_path:
            return None

        flaws = parsed.get("flaws", [])
        visual = [
            (i, f.get("paragraph", ""))
            for i, f in enumerate(flaws, start=1)
            if AIAuditor._looks_like_a_visual_claim(f.get("paragraph", ""))
        ]
        if not visual:
            return None

        base64_image = self._encode_image(image_path)
        if not base64_image:
            return None

        claims = "\n".join(f"{i}. {paragraph}" for i, paragraph in visual)
        judge_prompt = (
            "You are a strict visual fact checker. Attached is a real screenshot of a "
            "business's website. Below are numbered CLAIMS somebody wrote about how that "
            "site looks.\n\n"
            f"CLAIMS:\n{claims}\n\n"
            "For each claim number, decide if what it describes is actually visible in the "
            "attached screenshot. Judge only what you can genuinely see. If a claim describes "
            "a problem that is not there, or that you cannot confirm from the image, mark it "
            "unsupported. Being unable to see something counts as unsupported.\n"
            'Return ONLY valid JSON: {"unsupported": [claim numbers not visible in the image]}. '
            "Empty array if every claim is visibly accurate. No markdown, no explanation."
        )

        try:
            raw = self._call_vision_judge(judge_prompt, base64_image)
            if not raw:
                return None
            result = self._parse_json_loose(raw)
            unsupported = result.get("unsupported") if result else None
            if unsupported:
                numbers = {i for i, _ in visual}
                flagged = [f"#{i}" for i in unsupported if i in numbers]
                if not flagged:
                    return None
                message = (
                    f"Visual check: {len(flagged)} claim(s) about the screenshot could NOT be "
                    f"confirmed in the actual image — review before sending: {flagged}"
                )
                print(f"[AIAuditor] WARNING: {message} (for '{company}')")
                return message
        except Exception as e:
            print(f"[AIAuditor] Visual claim verification skipped (non-critical): {e}")
        return None

    def _call_vision_judge(self, judge_prompt: str, base64_image: str) -> str | None:
        """
        Vision-capable counterpart to _call_judge, same fixed preference
        order and same silent-degradation contract. Separate function
        because the three SDKs each take image content differently, and
        _call_judge's text-only signature is used by the cheaper checks
        that must not pay for an image.
        """
        if config.GEMINI_API_KEY:
            try:
                model = genai.GenerativeModel(
                    "gemini-3.5-flash",
                    generation_config=genai.types.GenerationConfig(
                        response_mime_type="application/json", temperature=0.0,
                    ),
                )
                return model.generate_content([
                    {"mime_type": "image/jpeg", "data": base64.b64decode(base64_image)},
                    judge_prompt,
                ]).text
            except Exception:
                pass
        if self._anthropic_client:
            try:
                message = self._anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    temperature=0.0,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64_image}},
                        {"type": "text", "text": judge_prompt},
                    ]}],
                )
                return message.content[0].text
            except Exception:
                pass
        if self._openai_client:
            try:
                response = self._openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": judge_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ]}],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except Exception:
                pass
        return None

    @staticmethod
    def _parse_json_loose(raw: str) -> dict | None:
        """Same tolerant parsing as _parse_json but without the required-keys check — used for the judge's smaller {"unsupported": [...]} shape."""
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _encode_image(image_path: str | None) -> str | None:
        if not image_path:
            return None
        try:
            with open(image_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            print(f"Failed to read image for AI audit: {e}")
            return None

    def should_contact(self, audit_result: dict) -> bool:
        """
        Return ``True`` if the lead's overall score is below the
        contact threshold (< 70), meaning they need help.

        A partial-coverage audit always returns True regardless of score:
        the score is computed by subtracting per detected flaw, so signals
        that returned no data inflate it, and treating that inflated number
        as "this site is healthy" throws away leads for want of a
        measurement. See analyze_lead for the measured example.
        """
        if audit_result.get("partial_coverage"):
            return True
        return audit_result.get("overall_score", 100) < CONTACT_THRESHOLD

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    # Substrings identifying the deterministic, measured visual/design flaws
    # that _build_flaws() can actually produce (scrapers/website.py) — the
    # font-consistency check and the stretched-image check. Matched on the
    # description rather than the category because both are filed under the
    # broad "content" category alongside non-visual flaws (favicon, thin
    # content), so the category alone can't tell them apart.
    _VISUAL_FLAW_MARKERS = (
        "different fonts on one page",
        "displayed larger than their native resolution",
    )

    @staticmethod
    def _has_visual_evidence(web: WebsiteData) -> bool:
        """
        True if this run actually measured something visual worth claiming.

        Either the red-box pipeline found a real, in-viewport axe-core
        violation to highlight (visual_flaw_context), or one of the two
        deterministic design checks fired. Used to decide whether the prompt
        may *demand* a visual critique — see the visual instruction in
        _build_prompt for why demanding one unconditionally is a bug.
        """
        if getattr(web, "visual_flaw_context", None):
            return True
        for flaw in getattr(web, "flaws", None) or []:
            description = (getattr(flaw, "description", "") or "").lower()
            if any(marker in description for marker in AIAuditor._VISUAL_FLAW_MARKERS):
                return True
        return False

    @staticmethod
    def _build_prompt(
        company: str,
        ig: InstagramData | None,
        web: WebsiteData,
        has_image: bool = False,
        has_mobile_image: bool = False,
        rating: str = "",
        reviews_count: int = 0,
    ) -> str:
        """
        Assemble the audit prompt using real data from *ig* and *web*.
        """
        has_visual_evidence = AIAuditor._has_visual_evidence(web)
        # --- Instagram section ---
        if ig:
            ig_section = (
                f"INSTAGRAM DATA:\n"
                f"- Handle: @{ig.username}\n"
                f"- Followers: {ig.followers}\n"
                f"- Posts last 30 days: {ig.posts_last_30_days} "
                f"({ig.posting_frequency})\n"
                f"- Avg engagement rate: {ig.engagement_rate}% "
                f"(healthy brand avg: 1-3%)\n"
                f"- Uses Reels: {ig.uses_reels}\n"
                f"- Bio: {ig.bio}\n"
                f"- Sample captions: {ig.sample_captions[:3]}\n"
            )
        else:
            ig_section = (
                "INSTAGRAM DATA:\n"
                "- Instagram data was not available for analysis.\n"
                "- IMPORTANT: Do NOT claim the company has no Instagram. They may have one that we couldn't analyze. Skip Instagram-related flaws entirely.\n"
            )

        # --- Website section (headline numbers + context, not flaws) ---
        # A score of 0 here means "both Lighthouse AND the PageSpeed API
        # fallback failed to return anything", NOT "this site genuinely
        # scored zero" — the two are indistinguishable in the raw value
        # (scrapers/website.py falls back to .get(key, 0)). Feeding a
        # phantom "0/100" into a prompt that also says "QUOTE THE EXACT
        # NUMBER" is how the AI ends up confidently telling a real business
        # its site scored 0 out of 100 on a measurement that never ran.
        # Same bug class as the dead PerformanceObserver init script (see
        # analyzer/visuals.py) — a silent failure degrading into a
        # confident, wrong claim. Say "could not be measured" instead, and
        # explicitly forbid inventing a number for it.
        speed_line = (
            f"- Page speed (mobile): {web.page_speed_score}/100\n"
            if web.page_speed_score
            else "- Page speed (mobile): COULD NOT BE MEASURED (measurement tool failed — do NOT mention page speed scores at all, and never claim it scored 0)\n"
        )
        seo_line = (
            f"- SEO score: {web.seo_score}/100\n"
            if web.seo_score
            else "- SEO score: COULD NOT BE MEASURED (measurement tool failed — do NOT mention an SEO score at all, and never claim it scored 0)\n"
        )
        web_section = (
            f"WEBSITE DATA:\n"
            f"{speed_line}"
            f"{seo_line}"
            f"- Tech Stack: {web.technologies}\n"
            f"- Homepage text: {web.homepage_text[:2000]}\n"
        )

        # --- Performance Timing (real browser data — context/color, not a flaw itself) ---
        perf_section = ""
        if getattr(web, 'perf_timing', None) and web.perf_timing:
            pt = web.perf_timing
            perf_section = (
                f"REAL BROWSER PERFORMANCE (measured by our automated browser):\n"
                f"- Time to First Byte (TTFB): {pt.get('ttfb_ms', 'N/A')}ms\n"
                f"- DOM Loaded: {pt.get('dom_load_ms', 'N/A')}ms\n"
                f"- Full Page Load: {pt.get('full_load_ms', 'N/A')}ms\n"
                f"- Page Transfer Size: {pt.get('transfer_size_kb', 'N/A')} KB\n"
            )

        # --- Flaws (reconciled: Lighthouse/PageSpeed, HTML parsing, security
        # headers, structured data, readability, pyseoanalyzer, axe-core, and
        # broken links, deduplicated and ranked in code — see
        # scrapers/website.py:_build_flaws and analyzer/flaws.py. This
        # replaces what used to be five separate raw, unreconciled sections. ---
        flaws_section = ""
        if getattr(web, 'flaws', None):
            flaws_text = "\n".join(f"  - [{f.severity.upper()}] {f.description}" for f in web.flaws)
            flaws_section = f"FLAWS DETECTED (ranked most severe first):\n{flaws_text}\n"

        # --- Deep Brand Context ---
        brand_context_section = ""
        if getattr(web, 'company_context', None):
            brand_context_section = (
                f"DEEP BRAND CONTEXT (Scraped from About/Services pages):\n"
                f"{web.company_context[:3000]}\n"
            )

        # --- Visual Flaw Context ---
        visual_flaw_section = ""
        if getattr(web, 'visual_flaw_context', None):
            visual_flaw_section = (
                f"SCREENSHOT VISUAL FLAW:\n"
                f"{web.visual_flaw_context}\n"
            )

        # --- Signals that returned no data on this run. Without this the
        # AI can't tell "we checked and it was fine" from "we never
        # checked", and will happily imply a whole category is clean based
        # on its absence from FLAWS DETECTED.
        coverage_section = ""
        signal_status = getattr(web, "signal_status", None) or {}
        degraded = sorted(name for name, state in signal_status.items() if state != "ok")
        if degraded:
            coverage_section = (
                "CHECKS THAT RETURNED NO DATA ON THIS RUN: "
                + ", ".join(degraded)
                + "\nThese did NOT run successfully, so problems in those areas would not appear in FLAWS DETECTED above. "
                "Never state or imply that any of these areas is fine, healthy, fast, or passing — you have no evidence either way. "
                "Only write about flaws actually listed above.\n"
            )

        # --- Google Business rating (Maps-sourced leads only) — a
        # personalization hook, not a flaw: a strong rating with a weak
        # website is a compelling contrast ("great reviews but the site
        # doesn't reflect it"), so keep it distinct from FLAWS DETECTED.
        # Only render this when `rating` is a real number: the Playwright
        # Maps fallback stores the literal string "N/A" when it can't read a
        # rating, which would otherwise reach the prompt as
        # "GOOGLE BUSINESS RATING: N/A/5 stars from 0 reviews" — the same
        # unmeasured-value-presented-as-data shape as the phantom 0/100 bug.
        rating_section = ""
        try:
            rating_value = float(rating)
        except (TypeError, ValueError):
            rating_value = None
        if rating_value is not None:
            rating_section = f"GOOGLE BUSINESS RATING: {rating_value}/5 stars from {reviews_count} reviews.\n"

        return (
            f"You are a sharp, conversational digital marketing consultant auditing "
            f"{company}.\n\n"
            f"{ig_section}\n"
            f"{web_section}\n"
            f"{perf_section}\n"
            f"{rating_section}\n"
            f"{flaws_section}\n"
            f"{coverage_section}\n"
            f"{brand_context_section}\n"
            f"{visual_flaw_section}\n"
            "TASK:\n"
            "Pick 3 or 4 of the most severe items from FLAWS DETECTED above (already ranked worst-first) and write about THOSE — don't go hunting for problems yourself, the list is already reconciled and prioritized. Prefer covering DIFFERENT categories (performance, accessibility, visual/layout/typography, broken/dead links, SEO, conversion) over picking multiple flaws from the same category, as long as the list has that variety available — a business owner needs a full picture, not five variations of one issue.\n"
            "Be direct, casual, and extremely friendly. Do not use corporate jargon. Talk like a normal human being reaching out to a peer.\n"
            "CRITICAL INSTRUCTION: NO TECHNICAL JARGON. Never say raw metric names or acronyms like 'ARIA', 'Largest Contentful Paint', 'LCP', 'CLS', 'TBT', 'DOM', or a bare millisecond figure like '17500ms'. Translate every technical finding into plain language a non technical business owner understands. Example: instead of 'ARIA roles missing on interactive elements', say 'a few buttons on your site aren't set up correctly for screen readers'. Instead of 'LCP of 4.2s', say 'the main image on your homepage takes noticeably long to show up'. You can still mention a concrete number when it is easy to understand in context (a score out of 100, a count of broken links, a load time in plain seconds), just never the raw technical term for the metric itself.\n"
            "CRITICAL INSTRUCTION: Every single flaw paragraph must end by stating the concrete business cost in plain terms, not just describe the technical problem. Say what it actually costs them: fewer inquiries, visitors leaving before contacting them, lower search ranking so fewer people find them at all, or people not trusting the business enough to reach out. Tie every flaw back to lost opportunities or lost customers, specifically, not a vague 'this hurts your business'.\n"
            "If FLAWS DETECTED includes any broken or dead links, you must include that as one of your picked flaws — a link that goes nowhere directly loses a visitor who was already interested enough to click, which is a very concrete, easy to explain cost.\n"
            "CRITICAL INSTRUCTION: NEVER use hyphens (-) or dashes (—) anywhere in your response. For example, use '10 minute call' instead of '10-minute call'.\n"
            "CRITICAL INSTRUCTION: NEVER use spam trigger words/phrases anywhere in the subject or body — this includes but is not limited to: free, guarantee/guaranteed, click here, buy now, act now, limited time, no obligation, risk free, cash, $$$, 100% (or any percent-off claim), congratulations, winner, urgent, don't delete, dear friend. NEVER write in ALL CAPS (not even a single word) or use excessive exclamation marks (!!!). Write like a real person emailing a peer, not a marketing blast.\n"
            "If engagement_rate < 1% say exactly that and why it hurts them.\n"
            "If a flaw includes a specific number (score, ms, word count), QUOTE THE EXACT NUMBER in the email (e.g., 'your site scored a 42/100 on mobile speed').\n"
            "If any [ACCESSIBILITY] flaws are in the list, mention the specific violation by name.\n"
            # This example previously demonstrated two things it forbids
            # elsewhere: it contained an em dash (the no-dashes rule three
            # lines above), and it ended "...invisible to screen readers,
            # which is hurting your SEO" — an accessibility problem asserted
            # as an SEO one, which is exactly the unsupported leap that
            # produced a real false claim on a live lead (tubersstudio.com,
            # see CLAUDE.md §8). A worked example is the strongest signal in
            # a prompt, so an example that breaks the rules teaches the model
            # to break them.
            "If SCREENSHOT VISUAL FLAW exists, you MUST explicitly mention the red box in the screenshot "
            "(e.g., 'I attached a screenshot of your homepage. The red box is around a button that screen "
            "readers cannot announce at all, so customers using one have no way to find it'). "
            "Describe only what the flaw text actually says. Do NOT add a consequence it does not state, "
            "and in particular do NOT claim an accessibility problem affects SEO or Google ranking.\n"
            "If their Tech Stack uses Shopify/WordPress/etc, mention it specifically so it feels personalized.\n"
            "CRITICAL INSTRUCTION FOR OPENING LINE: You must read the DEEP BRAND CONTEXT (or Homepage text). Find out exactly what the company sells or does. Your 'opening_line' MUST highly personalize the outreach based on what they actually do (e.g., 'Loved what you guys are doing with luxury real estate marketing in Miami...' or 'Been following your B2B SaaS growth tools...'). DO NOT just say 'Loved what you guys are doing with [Company name]'. Prove you know what they do!\n"
            # The visual instruction is deliberately split in two. It used to
            # be one unconditional "ONE OF YOUR FLAWS MUST BE A VISUAL
            # CRITIQUE", which forced a design criticism on every single
            # email even when nothing visual was actually measured — a site
            # with clean design left the model no honest way to comply, so
            # it invented one. It also directly contradicted the source_quote
            # rule below ("if you cannot copy an exact line for this claim,
            # do not make the claim at all"), since a typography/alignment
            # claim usually has no matching line in FLAWS DETECTED. The
            # demand now only appears when something visual was really
            # detected; otherwise a visual claim is permitted but explicitly
            # optional and must describe only what is plainly visible.
            + (
                (
                    "CRITICAL INSTRUCTION FOR FLAWS: I am attaching a desktop screenshot of their website in the email, and the visual/design problems listed in FLAWS DETECTED above were really measured on this site. ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE covering what was detected (LAYOUT, TYPOGRAPHY, or ALIGNMENT) — anchor it to the detected flaw and to what you can actually see in the image. You MUST mention the screenshot in that flaw text (e.g. 'I noticed in the screenshot we took that your menu overlaps...' or 'the fonts in your hero section and navigation don't match, which looks inconsistent and unprofessional to a first time visitor').\n"
                    if has_visual_evidence
                    else "NOTE ON THE SCREENSHOT: I am attaching a desktop screenshot of their website in the email, but our automated design checks did NOT detect any typography, alignment, or image-quality problem on this site. Do NOT invent a visual criticism to fill a quota — a clean design is a perfectly normal result. Only make a visual claim if something is unmistakably and obviously wrong in the image itself, and if you do, describe only what is plainly visible rather than implying we measured it. Otherwise pick your flaws entirely from FLAWS DETECTED above and do not comment on the design at all.\n"
                )
                if has_image
                else ""
            )
            + ("A SECOND image is also attached showing the site on an actual MOBILE PHONE screen. Compare it against the desktop screenshot and look specifically for mobile only problems: text or buttons cut off or overlapping, horizontal scrolling, tiny unreadable font, a hamburger menu that looks broken, a hero image that doesn't adapt. If you spot a mobile specific issue, make ONE of your flaws about it and say explicitly that it is how the site looks on a phone (e.g. 'on your phone, the navigation menu overlaps your logo').\n" if has_mobile_image else "")
            + ("If GOOGLE BUSINESS RATING is 4 stars or higher, use it as a personalization hook, e.g. contrast their strong reputation with a website flaw ('you've clearly got happy customers, X reviews at Y stars, but the website doesn't reflect that trust'). Do not mention the rating if it is below 4 stars or reviews_count is under 10, it is not a strong enough signal to reference.\n" if rating_value is not None else "")
            + "\n"
            "IMPORTANT: Return ONLY valid JSON. No markdown. No explanation.\n"
            "Use this exact structure:\n"
            "{\n"
            '  "flaws": [\n'
            "    {\n"
            '      "paragraph": "A single, highly conversational, flowing 2 to 3 sentence paragraph explaining the specific problem and the business impact. NO HYPHENS. NO DASHES. Be extremely natural.",\n'
            '      "source_quote": "Copy the ONE line from FLAWS DETECTED above that this paragraph is about, EXACTLY as written, character for character. Do not paraphrase, summarise, reword, or merge two lines. If you cannot copy an exact line for this claim, do not make the claim at all."\n'
            "    }\n"
            "  ],\n"
            '  "overall_score": 45,\n'
            '  "email_subject": "short, engaging, and professional subject line using Title Case",\n'
            '  "opening_line": "friendly personalized opening line without hyphens"\n'
            "}\n"
            )

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    def _call_gemini(self, prompt: str, base64_image: str | None = None, base64_mobile_image: str | None = None) -> tuple[str, float] | None:
        """
        Call Google Gemini (``gemini-3-flash``), forcing native JSON output
        so we don't rely on regex-stripping markdown fences from the reply.
        """
        if not config.GEMINI_API_KEY:
            return None

        try:
            model = genai.GenerativeModel(
                "gemini-3.5-flash",
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_JSON_SCHEMA,
                    temperature=_AI_TEMPERATURE,
                ),
            )
            content = [prompt]
            if base64_image:
                content.append({
                    "mime_type": "image/jpeg",
                    "data": base64_image
                })
            if base64_mobile_image:
                content.append({
                    "mime_type": "image/jpeg",
                    "data": base64_mobile_image
                })
            response = model.generate_content(content)

            # Pricing (paid tier fallback if free quota exhausted): ~$0.075/1M input, $0.30/1M output
            try:
                inp = response.usage_metadata.prompt_token_count
                out = response.usage_metadata.candidates_token_count
                cost = (inp * 0.075 / 1_000_000) + (out * 0.30 / 1_000_000)
            except Exception:
                cost = 0.0001 # fallback estimate

            return response.text, cost
        except Exception as e:
            print(f"Gemini error: {e}")
            return None

    def _call_openai(self, prompt: str, base64_image: str | None = None, base64_mobile_image: str | None = None) -> tuple[str, float] | None:
        """
        Call OpenAI GPT-4o-mini.
        """
        if not self._openai_client:
            return None

        try:
            content = [{"type": "text", "text": prompt}]
            if base64_image:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                })
            if base64_mobile_image:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_mobile_image}"}
                })

            response = self._openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": content}],
                temperature=_AI_TEMPERATURE,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "audit_result",
                        "strict": True,
                        # Derived from _RESPONSE_JSON_SCHEMA rather than
                        # restating the flaw shape, which had drifted out of
                        # sync the moment a field was added to the shared
                        # schema: OpenAI strict mode rejects any property not
                        # declared here, so a hardcoded copy silently becomes
                        # the one provider that can't return the new field.
                        # Strict mode additionally requires
                        # additionalProperties:false at every object level.
                        "schema": {
                            **_RESPONSE_JSON_SCHEMA,
                            "additionalProperties": False,
                            "properties": {
                                **_RESPONSE_JSON_SCHEMA["properties"],
                                "flaws": {
                                    **_RESPONSE_JSON_SCHEMA["properties"]["flaws"],
                                    "items": {
                                        **_RESPONSE_JSON_SCHEMA["properties"]["flaws"]["items"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                        },
                    },
                },
            )
            
            # Pricing: $0.150/1M input, $0.60/1M output
            try:
                inp = response.usage.prompt_tokens
                out = response.usage.completion_tokens
                cost = (inp * 0.150 / 1_000_000) + (out * 0.60 / 1_000_000)
            except Exception:
                cost = 0.0002
                
            return response.choices[0].message.content, cost
        except Exception as e:
            print(f"OpenAI error: {e}")
            return None

    def _call_anthropic(self, prompt: str, base64_image: str | None = None, base64_mobile_image: str | None = None) -> tuple[str, float] | None:
        """
        Call Anthropic Claude Haiku 4.5.
        """
        if not self._anthropic_client:
            return None

        try:
            content = []
            if base64_image:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_image
                    }
                })
            if base64_mobile_image:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_mobile_image
                    }
                })
            content.append({"type": "text", "text": prompt})

            # Forced tool-use instead of freeform text — Anthropic guarantees
            # the tool_use block's "input" matches input_schema, which is
            # what _RESPONSE_JSON_SCHEMA is doing here. Removes an entire
            # class of _parse_json failures (markdown fences, leading prose,
            # truncated JSON) for this provider specifically.
            message = self._anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                temperature=_AI_TEMPERATURE,
                tools=[{
                    "name": "submit_audit_result",
                    "description": "Submit the structured audit result.",
                    "input_schema": _RESPONSE_JSON_SCHEMA,
                }],
                tool_choice={"type": "tool", "name": "submit_audit_result"},
                messages=[{"role": "user", "content": content}],
            )

            # Pricing: $0.25/1M input, $1.25/1M output
            try:
                inp = message.usage.input_tokens
                out = message.usage.output_tokens
                cost = (inp * 0.25 / 1_000_000) + (out * 1.25 / 1_000_000)
            except Exception:
                cost = 0.0006

            tool_use = next((b for b in message.content if b.type == "tool_use"), None)
            if tool_use is None:
                print("Anthropic error: no tool_use block in response")
                return None
            return json.dumps(tool_use.input), cost
        except Exception as e:
            print(f"Anthropic error: {e}")
            return None

    def _call_openrouter(self, prompt: str, base64_image: str | None = None, base64_mobile_image: str | None = None) -> tuple[str, float] | None:
        """
        Call a free-tier OpenRouter model (``google/gemma-4-31b-it:free``).

        Last resort: only reached if Claude, Gemini, and GPT-4o-mini all
        fail or run out of quota — the free-tier model is weaker than the
        paid providers above, but a weaker audit beats no audit at all.
        """
        if not self._openrouter_client:
            return None

        try:
            content = [{"type": "text", "text": prompt}]
            if base64_image:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                })
            if base64_mobile_image:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_mobile_image}"}
                })

            response = self._openrouter_client.chat.completions.create(
                model="google/gemma-4-31b-it:free",
                messages=[{"role": "user", "content": content}],
                temperature=_AI_TEMPERATURE,
                response_format={"type": "json_object"},
            )

            return response.choices[0].message.content, 0.0  # free tier
        except Exception as e:
            print(f"OpenRouter error: {e}")
            return None

    # ------------------------------------------------------------------
    # JSON parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        """
        Safely parse the AI response into a dict.

        Strips common artefacts (markdown fences, leading text) before
        attempting ``json.loads``.
        """
        # Remove markdown code fences
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = cleaned.strip()

        # Try to find the first { … } block
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            return None

        json_str = cleaned[start : end + 1]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        # Validate expected keys exist
        required = {"flaws", "overall_score", "email_subject", "opening_line"}
        if not required.issubset(data.keys()):
            return None

        return data

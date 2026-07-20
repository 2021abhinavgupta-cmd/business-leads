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
# Score threshold — only contact leads below this
# ---------------------------------------------------------------------------
CONTACT_THRESHOLD = 70

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
                },
                "required": ["paragraph"],
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
                self._check_number_hallucination(parsed, prompt, company)
                self._verify_grounding(parsed, prompt, company)
                self._check_spam_trigger_words(parsed, company)
                self._check_body_length(parsed, company, has_image=bool(image_path))
                # overall_score drives should_contact()/the skip-if-too-good
                # decision, but as returned by the AI it's pure self-report
                # with no real connection to the flaw data it was given.
                # Replace it with a deterministic score computed from the
                # actual measured flaws — keep the AI's original under a
                # separate key for visibility, not because anything reads it.
                parsed["ai_reported_score"] = parsed.get("overall_score")
                parsed["overall_score"] = compute_score(getattr(web, "flaws", None) or [])
                return parsed

        print(f"[AIAuditor] All AI providers failed or returned unparseable output for '{company}' — check API keys/quotas.")
        return None

    # Numbers that show up in nearly every generated email regardless of
    # source data (call-duration boilerplate, list positions) — excluded
    # from the hallucination check below so they don't drown out real
    # mismatches with constant noise.
    _BOILERPLATE_NUMBERS = {"10", "1", "2", "3"}

    @staticmethod
    def _check_number_hallucination(parsed: dict, prompt: str, company: str) -> None:
        """
        Free, log-only sanity check: the prompt instructs the AI to quote
        exact numbers from the real data it was given, but nothing actually
        verifies it did that instead of inventing a plausible-sounding one.
        Compares against the FULL rendered *prompt* text (not just
        web.flaws) so scores/timing numbers mentioned elsewhere in the
        prompt — e.g. WEBSITE DATA's page_speed_score — aren't false
        positives; a number only counts as suspicious if it appears nowhere
        in anything the AI was actually shown.

        Doesn't block or retry sending — too many legitimate false
        positives are still possible (a number split across sentences, a
        rounded figure) — but logs a warning so a hallucinated stat is
        visible before a real business owner receives a specific,
        checkable number that's wrong.
        """
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", prompt))

        email_text = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        email_numbers = set(re.findall(r"\d+(?:\.\d+)?", email_text))

        suspicious = (email_numbers - source_numbers) - AIAuditor._BOILERPLATE_NUMBERS
        if suspicious:
            print(f"[AIAuditor] WARNING: email for '{company}' cites number(s) {sorted(suspicious)} not found anywhere in the source prompt — possible hallucination, review before sending.")

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
    def _check_spam_trigger_words(parsed: dict, company: str) -> None:
        """
        Log-only scan of the generated subject + flaw paragraphs + opening
        line for classic spam-filter trigger words/phrases and ALL CAPS
        shouting, both of which independently hurt inbox placement
        regardless of sender reputation. Doesn't block/retry sending, same
        rationale as the other two checks above this one.
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
            print(f"[AIAuditor] WARNING: email for '{company}' contains {' and '.join(parts)} — spam-filter risk, review before sending.")

    # Below this, an embedded screenshot dominates the message and the
    # text:image ratio itself reads as spammy to some filters regardless of
    # what the text actually says.
    _MIN_BODY_WORD_COUNT = 40

    @staticmethod
    def _check_body_length(parsed: dict, company: str, has_image: bool) -> None:
        """
        Log-only: flags a body that's too short relative to the embedded
        screenshot it ships with. Only meaningful when an image is actually
        attached — a short text-only email isn't the same spam signal.
        """
        if not has_image:
            return
        paragraphs = " ".join(f.get("paragraph", "") for f in parsed.get("flaws", []))
        opening = parsed.get("opening_line", "") or ""
        word_count = len(f"{opening} {paragraphs}".split())
        if word_count < AIAuditor._MIN_BODY_WORD_COUNT:
            print(f"[AIAuditor] WARNING: email for '{company}' is only {word_count} words with an embedded screenshot attached — low text:image ratio can itself look spammy, review before sending.")

    def _verify_grounding(self, parsed: dict, prompt: str, company: str) -> None:
        """
        Free-form-claim counterpart to _check_number_hallucination above:
        that regex check only catches invented *numbers*, but a claim like
        "you don't have a mobile version of your site" when you do isn't
        numeric and slides right past it. Fires one more cheap, low-
        temperature LLM call asking a simple yes/no per flaw paragraph:
        "is this claim grounded in the source data, or invented?" — an
        LLM-as-judge pass, independent of which provider generated the
        copy (fixed preference order below, not necessarily the same one).

        Log-only, exactly like the hallucination check: too many legitimate
        borderline judgment calls (a fair inference vs. a fabrication) to
        safely auto-block or auto-retry on, but a flagged claim should be
        visible before a real business owner receives a specific, checkable
        claim about their own site that's wrong.
        """
        flaws = parsed.get("flaws", [])
        if not flaws:
            return

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
                return
            result = self._parse_json_loose(raw)
            unsupported = result.get("unsupported") if result else None
            if unsupported:
                flagged = [claims.splitlines()[i - 1] for i in unsupported if 0 < i <= len(flaws)]
                print(f"[AIAuditor] WARNING: grounding check flagged {len(flagged)} claim(s) for '{company}' as possibly unsupported by source data — review before sending: {flagged}")
        except Exception as e:
            print(f"[AIAuditor] Grounding verification skipped (non-critical): {e}")

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
        """
        return audit_result.get("overall_score", 100) < CONTACT_THRESHOLD

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

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
        web_section = (
            f"WEBSITE DATA:\n"
            f"- Page speed (mobile): {web.page_speed_score}/100\n"
            f"- SEO score: {web.seo_score}/100\n"
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

        # --- Google Business rating (Maps-sourced leads only) — a
        # personalization hook, not a flaw: a strong rating with a weak
        # website is a compelling contrast ("great reviews but the site
        # doesn't reflect it"), so keep it distinct from FLAWS DETECTED.
        rating_section = ""
        if rating:
            rating_section = f"GOOGLE BUSINESS RATING: {rating}/5 stars from {reviews_count} reviews.\n"

        return (
            f"You are a sharp, conversational digital marketing consultant auditing "
            f"{company}.\n\n"
            f"{ig_section}\n"
            f"{web_section}\n"
            f"{perf_section}\n"
            f"{rating_section}\n"
            f"{flaws_section}\n"
            f"{brand_context_section}\n"
            f"{visual_flaw_section}\n"
            "TASK:\n"
            "Pick 2 or 3 of the most severe items from FLAWS DETECTED above (already ranked worst-first) and write about THOSE — don't go hunting for problems yourself, the list is already reconciled and prioritized.\n"
            "Be direct, casual, and extremely friendly. Do not use corporate jargon. Talk like a normal human being reaching out to a peer.\n"
            "CRITICAL INSTRUCTION: NEVER use hyphens (-) or dashes (—) anywhere in your response. For example, use '10 minute call' instead of '10-minute call'.\n"
            "CRITICAL INSTRUCTION: NEVER use spam trigger words/phrases anywhere in the subject or body — this includes but is not limited to: free, guarantee/guaranteed, click here, buy now, act now, limited time, no obligation, risk free, cash, $$$, 100% (or any percent-off claim), congratulations, winner, urgent, don't delete, dear friend. NEVER write in ALL CAPS (not even a single word) or use excessive exclamation marks (!!!). Write like a real person emailing a peer, not a marketing blast.\n"
            "If engagement_rate < 1% say exactly that and why it hurts them.\n"
            "If a flaw includes a specific number (score, ms, word count), QUOTE THE EXACT NUMBER in the email (e.g., 'your site scored a 42/100 on mobile speed').\n"
            "If any [ACCESSIBILITY] flaws are in the list, mention the specific violation by name.\n"
            "If SCREENSHOT VISUAL FLAW exists, you MUST explicitly mention the red box in the screenshot (e.g., 'I attached a screenshot of your site—the red box highlights a button that is completely invisible to screen readers, which is hurting your SEO').\n"
            "If their Tech Stack uses Shopify/WordPress/etc, mention it specifically so it feels personalized.\n"
            "CRITICAL INSTRUCTION FOR OPENING LINE: You must read the DEEP BRAND CONTEXT (or Homepage text). Find out exactly what the company sells or does. Your 'opening_line' MUST highly personalize the outreach based on what they actually do (e.g., 'Loved what you guys are doing with luxury real estate marketing in Miami...' or 'Been following your B2B SaaS growth tools...'). DO NOT just say 'Loved what you guys are doing with [Company name]'. Prove you know what they do!\n"
            + ("CRITICAL INSTRUCTION FOR FLAWS: I am attaching a desktop screenshot of their website in the email. ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE based on the image! Look beyond just the red box (if present) — actually study the screenshot for general visual polish: inconsistent or clashing fonts, mismatched colors, cluttered/unbalanced layout, low-quality or stretched/blurry images, awkward spacing. You MUST mention the screenshot in your flaw text (e.g. 'I noticed in the screenshot we took that your menu overlaps...' or 'the fonts in your hero section and navigation don't match, which looks inconsistent').\n" if has_image else "")
            + ("A SECOND image is also attached showing the site on an actual MOBILE PHONE screen. Compare it against the desktop screenshot and look specifically for mobile only problems: text or buttons cut off or overlapping, horizontal scrolling, tiny unreadable font, a hamburger menu that looks broken, a hero image that doesn't adapt. If you spot a mobile specific issue, make ONE of your flaws about it and say explicitly that it is how the site looks on a phone (e.g. 'on your phone, the navigation menu overlaps your logo').\n" if has_mobile_image else "")
            + ("If GOOGLE BUSINESS RATING is 4 stars or higher, use it as a personalization hook, e.g. contrast their strong reputation with a website flaw ('you've clearly got happy customers, X reviews at Y stars, but the website doesn't reflect that trust'). Do not mention the rating if it is below 4 stars or reviews_count is under 10, it is not a strong enough signal to reference.\n" if rating else "")
            + "\n"
            "IMPORTANT: Return ONLY valid JSON. No markdown. No explanation.\n"
            "Use this exact structure:\n"
            "{\n"
            '  "flaws": [\n'
            "    {\n"
            '      "paragraph": "A single, highly conversational, flowing 2 to 3 sentence paragraph explaining the specific problem and the business impact. NO HYPHENS. NO DASHES. Be extremely natural."\n'
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
                        "schema": {
                            **_RESPONSE_JSON_SCHEMA,
                            "additionalProperties": False,
                            "properties": {
                                **_RESPONSE_JSON_SCHEMA["properties"],
                                "flaws": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {"paragraph": {"type": "string"}},
                                        "required": ["paragraph"],
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

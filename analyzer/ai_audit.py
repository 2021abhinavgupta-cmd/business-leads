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
from scrapers.instagram import InstagramData
from scrapers.website import WebsiteData

# ---------------------------------------------------------------------------
# Score threshold — only contact leads below this
# ---------------------------------------------------------------------------
CONTACT_THRESHOLD = 70


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
                return parsed

        print(f"[AIAuditor] All AI providers failed or returned unparseable output for '{company}' — check API keys/quotas.")
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
                "gemini-3-flash",
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json",
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
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            
            # Pricing: $0.150/1M input, $0.60/1M output
            try:
                inp = response.usage.prompt_tokens
                out = response.usage.completion_tokens
                cost = (inp * 0.150 / 1_000_000) + (out * 0.60 / 1_000_000)
            except Exception:
                cost = 0.0002
                
            return response.choices[0].message.content, cost
        except Exception:
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

            message = self._anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": content}],
            )
            
            # Pricing: $0.25/1M input, $1.25/1M output
            try:
                inp = message.usage.input_tokens
                out = message.usage.output_tokens
                cost = (inp * 0.25 / 1_000_000) + (out * 1.25 / 1_000_000)
            except Exception:
                cost = 0.0006
                
            return message.content[0].text, cost
        except Exception:
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
                temperature=0.7,
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

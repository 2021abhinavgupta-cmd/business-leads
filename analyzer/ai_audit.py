"""
AI Audit module — analyses leads using a Claude Haiku → Gemini → GPT-4o-mini
fallback chain and returns structured flaw reports.

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_lead(
        self,
        company: str,
        ig: InstagramData | None,
        web: WebsiteData,
        image_path: str | None = None
    ) -> dict | None:
        """
        Analyse a lead's digital presence via an AI fallback chain.

        Args:
            company: Business name.
            ig:      Instagram analytics (may be ``None`` if unavailable).
            web:     Website audit data.

        Returns:
            A dict with keys ``flaws``, ``overall_score``,
            ``email_subject``, ``opening_line`` — or ``None`` if every
            AI provider fails.
        """
        prompt = self._build_prompt(company, ig, web, bool(image_path))

        base64_image = None
        if image_path:
            try:
                with open(image_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            except Exception as e:
                print(f"Failed to read image for AI audit: {e}")

        # Fallback chain: Claude Haiku → Gemini → GPT-4o-mini
        for call_fn in (
            self._call_anthropic,
            self._call_gemini,
            self._call_openai,
        ):
            result = call_fn(prompt, base64_image)
            if result is None:
                continue
                
            raw, cost = result
            parsed = self._parse_json(raw)
            if parsed is not None:
                parsed["ai_cost"] = cost
                return parsed

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
        has_image: bool = False
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
                "- No Instagram profile found (this is itself a problem)\n"
            )

        # --- Website section ---
        web_section = (
            f"WEBSITE DATA:\n"
            f"- Page speed (mobile): {web.page_speed_score}/100\n"
            f"- SEO score: {web.seo_score}/100\n"
            f"- Has clear CTA: {web.has_cta}\n"
            f"- Has testimonials: {web.has_testimonials}\n"
            f"- Has blog: {web.has_blog}\n"
            f"- Raw issues found: {web.issues}\n"
            f"- Tech Stack: {web.technologies}\n"
            f"- Homepage text: {web.homepage_text[:2000]}\n"
        )
        
        # --- Deep Brand Context ---
        brand_context_section = ""
        if getattr(web, 'company_context', None):
            brand_context_section = (
                f"DEEP BRAND CONTEXT (Scraped from About/Services pages):\n"
                f"{web.company_context[:3000]}\n"
            )

        return (
            f"You are a sharp, conversational digital marketing consultant auditing "
            f"{company}.\n\n"
            f"{ig_section}\n"
            f"{web_section}\n"
            f"{brand_context_section}\n"
            "TASK:\n"
            "Find 2 or 3 painful, specific problems using THEIR real numbers and the visual screenshot.\n"
            "Be direct, casual, and extremely friendly. Do not use corporate jargon. Talk like a normal human being reaching out to a peer.\n"
            "CRITICAL INSTRUCTION: NEVER use hyphens (-) or dashes (—) anywhere in your response. For example, use '10 minute call' instead of '10-minute call'.\n"
            "If engagement_rate < 1% say exactly that and why it hurts them.\n"
            "If page_speed_score or load_time is slow, QUOTE THE EXACT NUMBER in the email (e.g., 'your site scored a 42/100 on mobile speed').\n"
            "If their Tech Stack uses Shopify/WordPress/etc, mention it specifically so it feels personalized.\n"
            "CRITICAL INSTRUCTION FOR OPENING LINE: You must read the DEEP BRAND CONTEXT (or Homepage text). Find out exactly what the company sells or does. Your 'opening_line' MUST highly personalize the outreach based on what they actually do (e.g., 'Loved what you guys are doing with luxury real estate marketing in Miami...' or 'Been following your B2B SaaS growth tools...'). DO NOT just say 'Loved what you guys are doing with [Company name]'. Prove you know what they do!\n"
            + ("CRITICAL INSTRUCTION FOR FLAWS: I am attaching a mobile screenshot of their website in the email. ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE based on the image! You MUST mention the screenshot in your flaw text (e.g. 'I noticed in the screenshot we took that your mobile menu overlaps...').\n" if has_image else "") + 
            "\n"
            "IMPORTANT: Return ONLY valid JSON. No markdown. No explanation.\n"
            "Use this exact structure:\n"
            "{\n"
            '  "flaws": [\n'
            "    {\n"
            '      "paragraph": "A single, highly conversational, flowing 2 to 3 sentence paragraph explaining the specific problem and the business impact. NO HYPHENS. NO DASHES. Be extremely natural."\n'
            "    }\n"
            "  ],\n"
            '  "overall_score": 45,\n'
            '  "email_subject": "ultra casual lowercase subject line without hyphens",\n'
            '  "opening_line": "friendly personalized opening line without hyphens"\n'
            "}\n"
            )

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    def _call_gemini(self, prompt: str, base64_image: str | None = None) -> tuple[str, float] | None:
        """
        Call Google Gemini Flash (``gemini-2.0-flash``).
        """
        if not config.GEMINI_API_KEY:
            return None

        try:
            model = genai.GenerativeModel("gemini-2.0-flash")
            content = [prompt]
            if base64_image:
                content.append({
                    "mime_type": "image/jpeg",
                    "data": base64_image
                })
            response = model.generate_content(content)
            
            # Pricing: $0.075/1M input, $0.30/1M output
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

    def _call_openai(self, prompt: str, base64_image: str | None = None) -> tuple[str, float] | None:
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

            response = self._openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": content}],
                temperature=0.7,
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

    def _call_anthropic(self, prompt: str, base64_image: str | None = None) -> tuple[str, float] | None:
        """
        Call Anthropic Claude Haiku.
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
            content.append({"type": "text", "text": prompt})

            message = self._anthropic_client.messages.create(
                model="claude-3-5-haiku-latest",
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

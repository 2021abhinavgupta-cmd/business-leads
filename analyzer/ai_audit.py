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
            raw = call_fn(prompt, base64_image)
            if raw is None:
                continue

            parsed = self._parse_json(raw)
            if parsed is not None:
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
            f"- Homepage text: {web.homepage_text[:400]}\n"
        )

        return (
            f"You are a sharp, conversational digital marketing consultant auditing "
            f"{company}.\n\n"
            f"{ig_section}\n"
            f"{web_section}\n"
            "TASK:\n"
            "Find the 2 most painful, specific problems using THEIR "
            "real numbers.\n"
            "Be direct, casual, and friendly. Do not use corporate jargon. "
            "Talk like a normal human being reaching out to a peer.\n"
            "If engagement_rate < 1% say exactly that and why it hurts them.\n"
            "If their Tech Stack uses Shopify/WordPress/etc, mention it specifically so it feels personalized (e.g. 'Since you guys use Shopify...').\n"
            + ("CRITICAL: An image screenshot of their website is attached. Actively critique their visual design, typography, layout, or mobile responsiveness based on the image.\n" if has_image else "") + 
            "\n"
            "IMPORTANT: Return ONLY valid JSON. No markdown. "
            "No explanation.\n"
            "Use this exact structure:\n"
            "{\n"
            '  "flaws": [\n'
            "    {\n"
            '      "area": "Instagram or Website",\n'
            '      "headline": "specific one-line problem with their '
            'actual numbers (casual tone)",\n'
            '      "detail": "2 sentence explanation referencing their '
            'real data (casual tone)",\n'
            '      "impact": "what this costs them in business terms"\n'
            "    }\n"
            "  ],\n"
            '  "overall_score": 45,\n'
            '  "email_subject": "ultra-casual, lowercase subject line (e.g. \'quick question about your mobile site\')",\n'
            '  "opening_line": "friendly, personalized opening line (e.g. \'Loved what you guys are doing with [company], but noticed something while checking out your site\')"\n'
            "}\n"
            )

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    def _call_gemini(self, prompt: str, base64_image: str | None = None) -> str | None:
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
            return response.text
        except Exception:
            return None

    def _call_openai(self, prompt: str, base64_image: str | None = None) -> str | None:
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
            return response.choices[0].message.content
        except Exception:
            return None

    def _call_anthropic(self, prompt: str, base64_image: str | None = None) -> str | None:
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
                model="claude-3-5-haiku-20241022",
                max_tokens=1024,
                messages=[{"role": "user", "content": content}],
            )
            return message.content[0].text
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

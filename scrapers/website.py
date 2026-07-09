"""
Website auditor — comprehensive website analysis for lead qualification.

Performs four audit steps:
    1. Reachability & load-time measurement (httpx)
    2. Google PageSpeed Insights (performance + SEO + mobile scores)
    3. HTML parsing with BeautifulSoup (CTA, testimonials, blog, contact)
    4. Issue generation (plain-English list of problems found)
"""

import re
import time
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup
import trafilatura
from Wappalyzer import Wappalyzer, WebPage
import warnings
warnings.filterwarnings("ignore", message=".*looks like a URL.*")

import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

_CTA_KEYWORDS = [
    "contact us", "get in touch", "book", "call", "whatsapp",
    "get started", "buy now", "order", "free consultation",
]

_TESTIMONIAL_KEYWORDS = [
    "review", "testimonial", "★", "trusted by",
    "clients", "rating",
]

_BLOG_PATHS = ["/blog", "/news", "/articles"]

_PHONE_PATTERN = re.compile(
    r"(\+?\d{1,4}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
)
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Thresholds for issue detection
_SLOW_LOAD_MS = 3000
_LOW_PERF_SCORE = 50
_LOW_SEO_SCORE = 60
_LOW_MOBILE_SCORE = 50


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class WebsiteData:
    """Structured audit result for a single website."""

    url: str
    reachable: bool
    load_time_ms: int
    page_speed_score: int
    seo_score: int
    mobile_score: int
    has_cta: bool
    has_contact: bool
    has_testimonials: bool
    has_blog: bool
    has_ssl: bool
    meta_title: str
    meta_description: str
    h1_tags: list[str] = field(default_factory=list)
    homepage_text: str = ""
    technologies: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Auditor class
# ---------------------------------------------------------------------------
class WebsiteScraper:
    """Comprehensive website auditor for lead qualification."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def audit_website(self, url: str) -> WebsiteData:
        """
        Run a full 4-step audit on *url*.

        Args:
            url: The website URL to audit (e.g. ``"https://example.com"``).

        Returns:
            A ``WebsiteData`` instance. If the site is unreachable, most
            fields will be zeroed/empty and ``reachable`` will be False.
        """
        url = self._normalise_url(url)

        # Step 1 — Reachability & load time
        reachable, html, load_time_ms, has_ssl = await self._check_reachability(url)

        if not reachable:
            return WebsiteData(
                url=url,
                reachable=False,
                load_time_ms=0,
                page_speed_score=0,
                seo_score=0,
                mobile_score=0,
                has_cta=False,
                has_contact=False,
                has_testimonials=False,
                has_blog=False,
                has_ssl=False,
                meta_title="",
                meta_description="",
                technologies=[],
                issues=["Website is unreachable or returned an error"],
            )

        # Step 2 — PageSpeed Insights
        perf_score, seo_score, mobile_score = await self._pagespeed(url)

        # Step 3 — HTML analysis
        parsed = self._parse_html(html)
        technologies = self._detect_technologies(url)

        # Step 4 — Build issues list
        issues = self._build_issues(
            load_time_ms=load_time_ms,
            perf_score=perf_score,
            seo_score=seo_score,
            mobile_score=mobile_score,
            has_ssl=has_ssl,
            parsed=parsed,
        )

        return WebsiteData(
            url=url,
            reachable=True,
            load_time_ms=load_time_ms,
            page_speed_score=perf_score,
            seo_score=seo_score,
            mobile_score=mobile_score,
            has_cta=parsed["has_cta"],
            has_contact=parsed["has_contact"],
            has_testimonials=parsed["has_testimonials"],
            has_blog=parsed["has_blog"],
            has_ssl=has_ssl,
            meta_title=parsed["meta_title"],
            meta_description=parsed["meta_description"],
            h1_tags=parsed["h1_tags"],
            homepage_text=parsed["homepage_text"],
            technologies=technologies,
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Step 1 — Reachability
    # ------------------------------------------------------------------

    async def _check_reachability(
        self, url: str
    ) -> tuple[bool, str, int, bool]:
        """
        Fetch *url* and measure response time.

        Returns:
            (reachable, html, load_time_ms, has_ssl)
        """
        try:
            start = time.perf_counter()
            response = await self.client.get(url)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            response.raise_for_status()

            has_ssl = str(response.url).startswith("https://")
            return True, response.text, elapsed_ms, has_ssl

        except Exception:
            return False, "", 0, False

    # ------------------------------------------------------------------
    # Step 2 — PageSpeed Insights
    # ------------------------------------------------------------------

    async def _pagespeed(self, url: str) -> tuple[int, int, int]:
        """
        Query Google PageSpeed Insights for *url*.

        Returns:
            (performance_score, seo_score, mobile_score)
            Each score is 0-100. Returns (0, 0, 0) on failure.
        """
        if not config.PAGESPEED_KEY:
            return 0, 0, 0

        try:
            response = await self.client.get(
                PAGESPEED_URL,
                params={
                    "url": url,
                    "strategy": "mobile",
                    "key": config.PAGESPEED_KEY,
                },
            )
            response.raise_for_status()
            data = response.json()

            categories = (
                data
                .get("lighthouseResult", {})
                .get("categories", {})
            )

            perf = categories.get("performance", {})
            seo = categories.get("seo", {})

            perf_score = int((perf.get("score") or 0) * 100)
            seo_score = int((seo.get("score") or 0) * 100)
            # Mobile strategy already gives the mobile-optimised score;
            # use performance as a proxy for mobile readiness.
            mobile_score = perf_score

            return perf_score, seo_score, mobile_score

        except Exception:
            return 0, 0, 0

    # ------------------------------------------------------------------
    # Step 3 — HTML parsing
    # ------------------------------------------------------------------

    def _parse_html(self, html: str) -> dict:
        """
        Extract audit signals from raw *html*.

        Returns a dict with keys:
            meta_title, meta_description, h1_tags, homepage_text,
            has_cta, has_contact, has_testimonials, has_blog
        """
        soup = BeautifulSoup(html, "html.parser")
        
        # Extract clean text via Trafilatura (fallback to beautifulsoup if it fails)
        extracted_text = trafilatura.extract(html)
        if not extracted_text:
            extracted_text = soup.get_text(separator=" ", strip=True)
        page_text = extracted_text.lower()

        # Meta tags
        meta_title = ""
        if soup.title and soup.title.string:
            meta_title = soup.title.string.strip()

        meta_description = ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag and desc_tag.get("content"):
            meta_description = desc_tag["content"].strip()

        # H1 tags
        h1_tags = [h1.get_text(strip=True) for h1 in soup.find_all("h1")]

        # CTA detection
        has_cta = any(kw in page_text for kw in _CTA_KEYWORDS)

        # Testimonials detection
        has_testimonials = any(kw in page_text for kw in _TESTIMONIAL_KEYWORDS)

        # Contact detection — phone or email on page
        has_contact = bool(
            _PHONE_PATTERN.search(page_text) or _EMAIL_PATTERN.search(page_text)
        )

        # Blog detection — any anchor href containing blog-like paths
        has_blog = False
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].lower()
            if any(path in href for path in _BLOG_PATHS):
                has_blog = True
                break

        # Homepage text (first 2000 chars for AI audit use)
        homepage_text = page_text[:2000]

        return {
            "meta_title": meta_title,
            "meta_description": meta_description,
            "h1_tags": h1_tags,
            "homepage_text": homepage_text,
            "has_cta": has_cta,
            "has_contact": has_contact,
            "has_testimonials": has_testimonials,
            "has_blog": has_blog,
        }

    # ------------------------------------------------------------------
    # Step 4 — Issue builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_issues(
        *,
        load_time_ms: int,
        perf_score: int,
        seo_score: int,
        mobile_score: int,
        has_ssl: bool,
        parsed: dict,
    ) -> list[str]:
        """
        Generate a list of plain-English issues from audit results.
        """
        issues: list[str] = []

        if load_time_ms > _SLOW_LOAD_MS:
            issues.append(
                f"Website loads slowly ({load_time_ms}ms). "
                f"Aim for under {_SLOW_LOAD_MS}ms."
            )

        if perf_score and perf_score < _LOW_PERF_SCORE:
            issues.append(
                f"Website takes too long to load on mobile "
                f"(score: {perf_score}/100)"
            )

        if seo_score and seo_score < _LOW_SEO_SCORE:
            issues.append(
                f"Poor SEO optimisation (score: {seo_score}/100). "
                f"Meta tags, headings, or structured data may be missing."
            )

        if mobile_score and mobile_score < _LOW_MOBILE_SCORE:
            issues.append(
                f"Website is not well-optimised for mobile devices "
                f"(score: {mobile_score}/100)"
            )

        if not has_ssl:
            issues.append(
                "Website does not use HTTPS. Visitors see a "
                "\"Not Secure\" warning in the browser."
            )

        if not parsed["meta_title"]:
            issues.append(
                "Missing page title (<title> tag). "
                "This hurts search engine rankings."
            )

        if not parsed["meta_description"]:
            issues.append(
                "Missing meta description. Search engines will "
                "generate their own snippet, which may not be ideal."
            )

        if not parsed["h1_tags"]:
            issues.append(
                "No H1 heading found on the homepage. "
                "Every page should have exactly one H1."
            )

        if not parsed["has_cta"]:
            issues.append(
                "No clear call-to-action found on the homepage. "
                "Visitors don't know what step to take next."
            )

        if not parsed["has_contact"]:
            issues.append(
                "No visible phone number or email address on the "
                "homepage. Potential customers can't reach you easily."
            )

        if not parsed["has_testimonials"]:
            issues.append(
                "No testimonials, reviews, or social proof found. "
                "Adding trust signals can boost conversion rates."
            )

        if not parsed["has_blog"]:
            issues.append(
                "No blog or content section detected. "
                "Regular content helps with SEO and audience trust."
            )

        return issues

    # ------------------------------------------------------------------
    # Step 5 — Tech stack detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_technologies(url: str) -> list[str]:
        """Detect the tech stack of the website using Wappalyzer."""
        try:
            wappalyzer = Wappalyzer.latest()
            webpage = WebPage.new_from_url(url)
            techs = wappalyzer.analyze(webpage)
            # Return list of tech names
            return list(techs)[:10] if techs else []
        except Exception as e:
            print(f"Error detecting tech stack for {url}: {e}")
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_url(url: str) -> str:
        """Ensure *url* has a scheme prefix."""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url

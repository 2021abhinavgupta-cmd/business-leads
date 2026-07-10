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
from markdownify import markdownify as md
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
    company_context: str = ""
    technologies: list[str] = field(default_factory=list)
    instagram_url: str = ""
    accessibility_violations: list[dict] = field(default_factory=list)
    broken_links: list[dict] = field(default_factory=list)
    perf_timing: dict = field(default_factory=dict)
    lighthouse_scores: dict = field(default_factory=dict)
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

    async def audit_website(self, url: str, html: str | None = None, extra_audit_data: dict | None = None) -> WebsiteData:
        """
        Run a full 4-step audit on *url*.

        Args:
            url: The website URL to audit (e.g. ``"https://example.com"``).
            html: Pre-rendered HTML content (from Playwright).
            extra_audit_data: Dict with accessibility_violations, broken_links, perf_timing from Playwright.

        Returns:
            A ``WebsiteData`` instance. If the site is unreachable or no html is provided, most
            fields will be zeroed/empty and ``reachable`` will be False.
        """
        url = self._normalise_url(url)
        has_ssl = str(url).startswith("https://")
        
        if not html:
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

        # We are using Playwright now, so use real timing data if available.
        extra = extra_audit_data or {}
        perf_timing = extra.get("perf_timing", {})
        load_time_ms = perf_timing.get("full_load_ms", 1500)  # Real value or default

        # Step 2 — Lighthouse CLI (primary) → PageSpeed API (fallback)
        lighthouse_scores = {}
        try:
            from analyzer.lighthouse import run_lighthouse
            lighthouse_scores = await run_lighthouse(url)
        except Exception as e:
            print(f"[Lighthouse] Import/run error: {e}")
        
        if lighthouse_scores:
            perf_score = lighthouse_scores.get("performance", 0)
            seo_score = lighthouse_scores.get("seo", 0)
            mobile_score = perf_score  # Performance IS mobile score
            print(f"[Audit] Using Lighthouse scores: perf={perf_score}, seo={seo_score}")
        else:
            # Fallback to PageSpeed Insights API
            perf_score, seo_score, mobile_score = await self._pagespeed(url)

        # Step 3 — HTML analysis (Crawl4AI enhanced → markdownify fallback)
        parsed = self._parse_html(html)
        technologies = self._detect_technologies(url)
        
        # Step 3.5 — Deep Brand Crawl (trafilatura → Jina Reader fallback)
        company_context = await self._deep_crawl(url, html)

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
            company_context=company_context,
            technologies=technologies,
            instagram_url=parsed.get("instagram_url", ""),
            accessibility_violations=extra.get("accessibility_violations", []),
            broken_links=extra.get("broken_links", []),
            perf_timing=perf_timing,
            lighthouse_scores=lighthouse_scores,
            issues=issues,
        )

    # (Removed _check_reachability as we now use Playwright for rendering HTML)

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
    # Step 2.5 — Deep Context Crawling
    # ------------------------------------------------------------------

    async def _deep_crawl(self, base_url: str, html: str) -> str:
        """
        Scan homepage for 'about' and 'service' links, fetch them asynchronously,
        and extract clean text. Uses trafilatura as primary, Jina Reader as fallback.
        """
        import urllib.parse
        import asyncio

        soup = BeautifulSoup(html, "html.parser")
        target_keywords = ['about', 'service', 'product', 'work', 'what-we-do', 'solution']
        
        target_urls = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].lower()
            if any(kw in href for kw in target_keywords):
                full_url = urllib.parse.urljoin(base_url, anchor["href"])
                # Only crawl internal links
                if full_url.startswith("http") and urllib.parse.urlparse(base_url).netloc in full_url:
                    target_urls.add(full_url)
        
        # Limit to 3 context pages to prevent extreme latency
        target_urls = list(target_urls)[:3]
        
        async def fetch_and_extract(url):
            # Primary: trafilatura
            try:
                res = await self.client.get(url, timeout=10)
                if res.status_code == 200:
                    text = trafilatura.extract(res.text)
                    if text:
                        return f"--- CONTEXT FROM {url} ---\n{text[:1500]}"
            except Exception:
                pass
            
            # Fallback: Jina Reader (free, no API key needed)
            try:
                jina_url = f"https://r.jina.ai/{url}"
                res = await self.client.get(jina_url, timeout=10, headers={"Accept": "text/plain"})
                if res.status_code == 200 and res.text.strip():
                    print(f"[Jina] Fallback succeeded for {url}")
                    return f"--- CONTEXT FROM {url} (via Jina Reader) ---\n{res.text[:1500]}"
            except Exception as e:
                print(f"[Jina] Fallback also failed for {url}: {e}")
            
            return ""

        context_parts = []
        if target_urls:
            results = await asyncio.gather(*[fetch_and_extract(u) for u in target_urls])
            for r in results:
                if r:
                    context_parts.append(r)
                    
        return "\n\n".join(context_parts)

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
        
        # Remove useless boilerplate tags for cleaner markdown
        for tag in soup(["script", "style", "noscript", "svg", "img"]):
            tag.decompose()
            
        # Primary: Crawl4AI for superior LLM-ready markdown extraction
        # Fallback: markdownify if Crawl4AI is unavailable
        markdown_text = ""
        try:
            from crawl4ai import AsyncWebCrawler
            # Crawl4AI can extract from raw HTML without launching its own browser
            import asyncio
            
            async def _crawl4ai_extract():
                async with AsyncWebCrawler(verbose=False) as crawler:
                    result = await crawler.arun(url="raw:html", raw_html=html)
                    return result.markdown if result and result.markdown else ""
            
            # Try to run in existing event loop or create new one
            try:
                loop = asyncio.get_running_loop()
                # Already in async context, can't run nested - use markdownify
                raise RuntimeError("In async context")
            except RuntimeError:
                markdown_text = md(str(soup), strip=['a'], heading_style="ATX").strip()
                print("[Parse] Using markdownify (async context)")
        except ImportError:
            markdown_text = md(str(soup), strip=['a'], heading_style="ATX").strip()
            print("[Parse] Crawl4AI not installed, using markdownify fallback")
        except Exception as e:
            markdown_text = md(str(soup), strip=['a'], heading_style="ATX").strip()
            print(f"[Parse] Crawl4AI error ({e}), using markdownify fallback")
        
        # We also need the raw text for keyword searching (CTAs, testimonials)
        page_text = soup.get_text(separator=" ", strip=True).lower()

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

        # Homepage text (converted to LLM-ready Markdown, truncated to 3000 chars)
        homepage_text = markdown_text[:3000]

        # Instagram link extraction
        instagram_url = ""
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "instagram.com" in href.lower():
                instagram_url = href
                break

        return {
            "meta_title": meta_title,
            "meta_description": meta_description,
            "h1_tags": h1_tags,
            "homepage_text": homepage_text,
            "has_cta": has_cta,
            "has_contact": has_contact,
            "has_testimonials": has_testimonials,
            "has_blog": has_blog,
            "instagram_url": instagram_url,
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
        # DISABLED: Wappalyzer is synchronous and takes 10-20 seconds, causing Railway 502 timeouts.
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

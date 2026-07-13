"""
Website auditor — comprehensive website analysis for lead qualification.

Performs four audit steps:
    1. Reachability & load-time measurement (httpx)
    2. Google PageSpeed Insights (performance + SEO + mobile scores)
    3. HTML parsing with BeautifulSoup (CTA, testimonials, blog, contact)
    4. Flaw reconciliation — every signal above, plus security headers,
       structured data, readability, and a real crawl-based SEO pass, gets
       normalized into one ranked list of Flaw objects (see analyzer/flaws.py)
       instead of being dumped raw into the AI prompt.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import extruct
import httpx
import textstat
from bs4 import BeautifulSoup
import trafilatura
from markdownify import markdownify as md
from wappalyzer import analyze as wappalyzer_analyze
import warnings
warnings.filterwarnings("ignore", message=".*looks like a URL.*")

import config
from analyzer.flaws import Flaw, rank as rank_flaws

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

# Business/conversion signals — these matter to the business owner reading
# the email (can a customer actually book/call/see prices?) independent of
# any technical score, which is what an audit that only surfaces Lighthouse/
# axe-core numbers misses entirely.
_BOOKING_PLATFORM_SIGNATURES = [
    "opentable", "resy.com", "sevenrooms", "exploretock", "tock.com",
    "reserve.google.com", "calendly.com", "acuityscheduling.com",
    "squareup.com/appointments", "setmore.com", "booksy.com", "cal.com",
    "bookatable", "yelp.com/reservations",
]
_MENU_PRICING_KEYWORDS = ["menu", "pricing", "price list", "our prices"]
_BUSINESS_SCHEMA_TYPES = {
    "localbusiness", "restaurant", "foodestablishment", "store",
    "professionalservice", "organization", "product", "menu",
    "cafeorcoffeeshop", "bar", "hotel", "beautysalon", "dentist",
    "medicalbusiness", "homeandconstructionbusiness", "autorepair",
    "realestateagent", "attorney", "accountingservice",
}

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
_THIN_CONTENT_WORDS = 200
_LOW_READABILITY_SCORE = 30  # Flesch Reading Ease; below this = "very difficult"
_MIN_WORDS_FOR_READABILITY = 50  # too little text and the score is meaningless noise

_AXE_SEVERITY_MAP = {"critical": "critical", "serious": "high", "moderate": "medium", "minor": "low"}
_MAX_CONSISTENT_FONTS = 3  # most well-designed sites use 1-2 type families, occasionally 3


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
    visual_flaw_context: str = ""
    flaws: list[Flaw] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Auditor class
# ---------------------------------------------------------------------------
class WebsiteScraper:
    """Comprehensive website auditor for lead qualification."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=60,
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
        # Determine HTTPS from what the browser actually loaded (after any
        # redirects), not just the string we requested — a lead's stored URL
        # is often "http://..." even when the site immediately redirects to
        # HTTPS, which used to produce a false "no HTTPS" flaw.
        final_url = (extra_audit_data or {}).get("final_url") or url
        has_ssl = str(final_url).startswith("https://")

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
                flaws=[Flaw(category="tech", severity="critical", description="Website is unreachable or returned an error")],
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
            print(f"[Audit] Using Lighthouse CLI scores: perf={perf_score}, seo={seo_score}")
        else:
            # Fallback to PageSpeed Insights API
            lighthouse_scores = await self._pagespeed(url)
            perf_score = lighthouse_scores.get("performance", 0)
            seo_score = lighthouse_scores.get("seo", 0)
            mobile_score = perf_score
            print(f"[Audit] Using PageSpeed API scores: perf={perf_score}, seo={seo_score}")

        # Step 3 — HTML analysis (Crawl4AI enhanced → markdownify fallback)
        parsed = await self._parse_html(html)
        technologies = await self._detect_technologies(url)

        # Step 3.5 — Deep Brand Crawl (trafilatura → Jina Reader fallback)
        company_context = await self._deep_crawl(url, html)

        # Step 3.6 — Additional flaw signals: structured data, readability,
        # security headers, and a real crawl-based SEO pass (pyseoanalyzer).
        has_structured_data = self._check_structured_data(html)
        has_business_schema = self._check_business_schema_type(html) if has_structured_data else False
        readability_score = self._check_readability(parsed["homepage_text"])
        security_flaws = self._check_security_headers(extra.get("response_headers", {}), has_ssl)
        seo_page = await self._run_pyseoanalyzer(url)
        robots_blocked = await self._check_robots_disallow_all(url)
        missing_alt_count = self._check_missing_alt_images(html)

        # Step 4 — Reconcile every signal above into one ranked flaw list,
        # instead of feeding the AI prompt raw, unreconciled tool output.
        flaws = self._build_flaws(
            load_time_ms=load_time_ms,
            perf_score=perf_score,
            seo_score=seo_score,
            mobile_score=mobile_score,
            best_practices_score=lighthouse_scores.get("best_practices", 0),
            lcp_ms=lighthouse_scores.get("lcp_ms"),
            cls=lighthouse_scores.get("cls"),
            tbt_ms=lighthouse_scores.get("tbt_ms"),
            has_ssl=has_ssl,
            parsed=parsed,
            has_structured_data=has_structured_data,
            has_business_schema=has_business_schema,
            readability_score=readability_score,
            security_flaws=security_flaws,
            seo_page=seo_page,
            accessibility_violations=extra.get("accessibility_violations", []),
            broken_links=extra.get("broken_links", []),
            font_families=extra.get("font_families", []),
            stretched_images=extra.get("stretched_images", 0),
            robots_blocked=robots_blocked,
            missing_alt_count=missing_alt_count,
            console_errors=extra.get("console_errors", []),
            mixed_content_urls=extra.get("mixed_content_urls", []),
            mobile_horizontal_overflow=extra.get("mobile_horizontal_overflow", False),
            duplicate_title_pages=extra.get("duplicate_title_pages", []),
            duplicate_meta_pages=extra.get("duplicate_meta_pages", []),
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
            visual_flaw_context=extra.get("visual_flaw_context", ""),
            flaws=flaws,
        )

    # (Removed _check_reachability as we now use Playwright for rendering HTML)

    # ------------------------------------------------------------------
    # Step 2 — PageSpeed Insights
    # ------------------------------------------------------------------

    async def _pagespeed(self, url: str) -> dict:
        """
        Query Google PageSpeed Insights for *url* twice (concurrently, so it
        doesn't cost extra wall-clock time) and average the numeric results.

        PageSpeed scores have real run-to-run variance from network/server
        crawling noise, not just the site's own actual performance —
        live-observed the exact same site scoring 26/100 on one run and
        32/100 on another within this same session. A single run can catch
        a fluke (cold CDN cache, momentary load spike) and an email will
        still quote it as a precise, checkable fact. Falls back to
        whichever single run succeeded if the other fails/returns empty,
        rather than losing data over one flaky call.
        """
        first, second = await asyncio.gather(
            self._pagespeed_once(url), self._pagespeed_once(url)
        )
        if not first:
            return second
        if not second:
            return first

        averaged: dict = {}
        for key in set(first) | set(second):
            v1, v2 = first.get(key), second.get(key)
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                averaged[key] = (v1 + v2) / 2
            else:
                averaged[key] = v1 if v1 is not None else v2

        for key in ("performance", "seo", "accessibility", "best_practices", "lcp_ms", "tbt_ms"):
            if averaged.get(key) is not None:
                averaged[key] = round(averaged[key])

        return averaged

    async def _pagespeed_once(self, url: str) -> dict:
        """
        Single PageSpeed Insights call — see _pagespeed() above, which
        always calls this twice and averages to smooth run-to-run noise.

        Returns:
            Dict with keys: performance, seo, accessibility, best_practices (each 0-100)
            Returns empty dict on failure.
        """
        if not config.PAGESPEED_KEY:
            print("[PageSpeed API] PAGESPEED_KEY is not set — skipping, scores will be 0. Set it in the environment (this is the fallback used whenever the Lighthouse CLI is unavailable).")
            return {}

        try:
            # PageSpeed API requires explicitly requesting multiple categories
            params = [
                ("url", url),
                ("strategy", "mobile"),
                ("key", config.PAGESPEED_KEY),
                ("category", "performance"),
                ("category", "seo"),
                ("category", "accessibility"),
                ("category", "best-practices"),
            ]
            response = await self.client.get(
                PAGESPEED_URL,
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            lighthouse_result = data.get("lighthouseResult", {})
            categories = lighthouse_result.get("categories", {})
            if not categories:
                print(f"[PageSpeed API] Response for {url} had no lighthouseResult.categories — raw keys: {list(data.keys())}")
                return {}

            from analyzer.lighthouse import _extract_core_web_vitals

            return {
                "performance": int((categories.get("performance", {}).get("score") or 0) * 100),
                "seo": int((categories.get("seo", {}).get("score") or 0) * 100),
                "accessibility": int((categories.get("accessibility", {}).get("score") or 0) * 100),
                "best_practices": int((categories.get("best-practices", {}).get("score") or 0) * 100),
                **_extract_core_web_vitals(lighthouse_result.get("audits", {})),
            }
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")[:300]
            print(f"[PageSpeed API] Failed for {url}: {e} {body}")
            return {}

    # ------------------------------------------------------------------
    # Step 2.5 — Deep Context Crawling
    # ------------------------------------------------------------------

    async def _deep_crawl(self, base_url: str, html: str) -> str:
        """
        Scan homepage for 'about' and 'service' links, fetch them asynchronously,
        and extract clean text. Uses trafilatura as primary, Jina Reader as fallback.
        """
        import urllib.parse

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

    async def _parse_html(self, html: str) -> dict:
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

        # Separate copy with <nav> also stripped, used ONLY for markdown/
        # readability extraction — NOT for the `soup` object below, since
        # has_blog/has_cta/has_contact/instagram-link detection scan for
        # anchors and keywords that commonly live in the nav (e.g. a "Blog"
        # link in the main menu) and would break if nav were removed there.
        # Live-verified on a real gaming e-commerce site: its <nav> alone was
        # 500+ words of category links ("HyperX Keyboards", "Razer Mouse",
        # etc) that dominated the first 3000 chars of homepage_text, pushed
        # out the actual hero/marketing copy, and made the Flesch readability
        # score meaningless (a huge list of short noun phrases with almost no
        # sentence punctuation scores as "very hard to read" even though it's
        # not real prose at all).
        markdown_soup = BeautifulSoup(str(soup), "html.parser")
        for tag in markdown_soup.find_all("nav"):
            tag.decompose()
        cleaned_html = str(markdown_soup)

        # Primary: Crawl4AI for superior LLM-ready markdown extraction (run in a
        # worker thread with its own event loop so it doesn't collide with the
        # event loop already running this coroutine).
        # Fallback: markdownify if Crawl4AI is unavailable, times out, or errors.
        markdown_text = await self._extract_markdown(cleaned_html, markdown_soup)

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

        # --- Business/conversion signals — matter to the owner reading the
        # email regardless of any technical score. ---

        # Click-to-call / click-to-WhatsApp — <a> tags survive the earlier
        # decompose() (only script/style/noscript/svg/img were removed), so
        # scanning soup anchors is safe here.
        has_click_to_call = False
        has_whatsapp_link = False
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].lower()
            if href.startswith("tel:"):
                has_click_to_call = True
            if "wa.me/" in href or "api.whatsapp.com" in href:
                has_whatsapp_link = True

        # Reservation/booking widget — checked against the RAW html string,
        # not soup, because booking platforms are almost always embedded via
        # <script src="..."> or <iframe src="...">, both of which were
        # already decompose()'d above.
        html_lower = html.lower()
        has_booking_widget = any(sig in html_lower for sig in _BOOKING_PLATFORM_SIGNATURES)

        # Menu/pricing visibility — link text or href suggesting a menu or
        # price list exists somewhere on the site.
        has_menu_or_pricing = False
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].lower()
            link_text = anchor.get_text(strip=True).lower()
            if any(kw in href or kw in link_text for kw in _MENU_PRICING_KEYWORDS):
                has_menu_or_pricing = True
                break

        # Homepage text (converted to LLM-ready Markdown, truncated to 3000 chars).
        # Crawl4AI's own markdown output keeps raw [text](url) link syntax (unlike
        # the markdownify fallback, which strips anchors via strip=['a']) — strip
        # it here so link noise doesn't skew the readability score or distract the
        # AI's personalization/opening-line generation.
        clean_markdown = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", markdown_text)
        homepage_text = clean_markdown[:3000]

        # Instagram link extraction
        instagram_url = ""
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "instagram.com" in href.lower():
                instagram_url = href
                break

        # Canonical tag
        has_canonical = soup.find("link", rel="canonical") is not None

        # Robots meta — flag if the page actively tells search engines not to index it
        is_noindexed = False
        robots_tag = soup.find("meta", attrs={"name": "robots"})
        if robots_tag and robots_tag.get("content"):
            is_noindexed = "noindex" in robots_tag["content"].lower()

        # Open Graph tags — presence of at least title/description/image
        has_og_tags = bool(
            soup.find("meta", property="og:title")
            or soup.find("meta", property="og:description")
            or soup.find("meta", property="og:image")
        )

        # Mobile viewport meta tag — its absence is a strong "not mobile
        # optimized" signal independent of the Lighthouse mobile score.
        viewport_tag = soup.find("meta", attrs={"name": "viewport"})
        has_viewport_meta = bool(viewport_tag and viewport_tag.get("content"))

        # Favicon — missing one is a small but very visible polish flaw
        # (shows as a broken/blank browser tab icon).
        has_favicon = bool(
            soup.find("link", rel=lambda r: r and "icon" in r.lower())
        )

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
            "has_canonical": has_canonical,
            "is_noindexed": is_noindexed,
            "has_og_tags": has_og_tags,
            "has_viewport_meta": has_viewport_meta,
            "has_favicon": has_favicon,
            "has_click_to_call": has_click_to_call,
            "has_whatsapp_link": has_whatsapp_link,
            "has_booking_widget": has_booking_widget,
            "has_menu_or_pricing": has_menu_or_pricing,
        }

    # ------------------------------------------------------------------
    # Step 3 (cont.) — Crawl4AI markdown extraction
    # ------------------------------------------------------------------

    async def _extract_markdown(self, html: str, soup) -> str:
        """Run Crawl4AI off-thread with a timeout; fall back to markdownify."""
        try:
            markdown_text = await asyncio.wait_for(
                asyncio.to_thread(self._run_crawl4ai_sync, html), timeout=15
            )
            if markdown_text and len(markdown_text) > 20:
                # Guard against Crawl4AI silently returning near-empty/placeholder
                # output (confirmed live: url="raw:html" doesn't actually parse
                # raw_html in crawl4ai 0.9.1 and returns just "html" — using a
                # temp file:// URL below instead, but keeping this length guard
                # as a safety net against any similar failure mode).
                print("[Parse] Using Crawl4AI markdown extraction")
                return markdown_text
            print(f"[Parse] Crawl4AI returned suspiciously short output ({len(markdown_text or '')} chars), using markdownify fallback")
        except ImportError:
            print("[Parse] Crawl4AI not installed, using markdownify fallback")
        except asyncio.TimeoutError:
            print("[Parse] Crawl4AI timed out, using markdownify fallback")
        except Exception as e:
            print(f"[Parse] Crawl4AI error ({e}), using markdownify fallback")

        return md(str(soup), strip=['a'], heading_style="ATX").strip()

    @staticmethod
    def _run_crawl4ai_sync(html: str) -> str:
        """
        Runs in a worker thread (via asyncio.to_thread), so it's safe to spin up
        its own event loop with asyncio.run() without colliding with the caller's.

        Writes *html* to a temp file and crawls it via a file:// URL. The
        documented url="raw:html", raw_html=html shortcut was tried first and
        confirmed broken in crawl4ai 0.9.1 (live-tested: it doesn't parse
        raw_html at all — returns the literal string "html").
        """
        import os
        import tempfile

        from crawl4ai import AsyncWebCrawler

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_path = f.name

        async def _crawl4ai_extract():
            async with AsyncWebCrawler(verbose=False) as crawler:
                result = await crawler.arun(url=f"file://{tmp_path}")
                return result.markdown if result and result.markdown else ""

        try:
            return asyncio.run(_crawl4ai_extract())
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Step 3.6 — Additional flaw signals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_structured_data(html: str) -> bool:
        """Schema.org/JSON-LD/OpenGraph presence via extruct (https://github.com/scrapinghub/extruct)."""
        try:
            data = extruct.extract(html, syntaxes=["json-ld", "microdata"])
            return bool(data.get("json-ld") or data.get("microdata"))
        except Exception as e:
            print(f"[Extruct] Structured data check failed: {e}")
            return False

    @staticmethod
    def _check_business_schema_type(html: str) -> bool:
        """
        True if structured data is present AND uses a business-relevant
        @type (LocalBusiness, Restaurant, Organization, Product, etc) rather
        than only generic types like WebSite/BreadcrumbList that don't power
        Google's local rich results (star ratings, hours, price range shown
        directly in the search snippet). Only meaningful to call when
        _check_structured_data already returned True — a site with zero
        structured data is already covered by that separate flaw.
        """
        try:
            data = extruct.extract(html, syntaxes=["json-ld", "microdata"])
        except Exception:
            return False

        types_found = set()
        for entry in data.get("json-ld", []):
            raw_type = entry.get("@type")
            values = raw_type if isinstance(raw_type, list) else [raw_type]
            types_found.update(str(t).lower() for t in values if t)
        for entry in data.get("microdata", []):
            raw_type = entry.get("type")
            values = raw_type if isinstance(raw_type, list) else [raw_type]
            types_found.update(str(t).rsplit("/", 1)[-1].lower() for t in values if t)

        return bool(types_found & _BUSINESS_SCHEMA_TYPES)

    async def _check_robots_disallow_all(self, url: str) -> bool:
        """
        Fetch /robots.txt and check for a blanket "Disallow: /" under a
        User-agent block that applies to us (User-agent: *). This is a much
        bigger and more common SEO killer than the <meta name="robots"
        content="noindex"> check above — it silently removes the entire site
        from Google, and unlike the meta tag most site owners never notice
        because the page itself renders completely normally in a browser.
        """
        try:
            parsed = urlparse(self._normalise_url(url))
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            response = await self.client.get(robots_url, timeout=5)
            if response.status_code != 200:
                return False
            lines = response.text.splitlines()
        except Exception:
            return False

        applies_to_us = False
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                applies_to_us = value == "*"
            elif key == "disallow" and applies_to_us and value == "/":
                return True
        return False

    @staticmethod
    def _check_missing_alt_images(html: str) -> int:
        """
        Count <img> tags missing the alt attribute ENTIRELY — not alt="",
        which is the correct WCAG pattern for decorative images and must not
        be flagged. This is deliberately narrower than pyseoanalyzer's own
        "missing alt tag" warning, which is filtered out in _build_flaws
        below precisely because it false-positives on alt="" images (see the
        comment there) — this check exists to recover that signal properly
        instead of just discarding it.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            return sum(1 for img in soup.find_all("img") if img.get("alt") is None)
        except Exception:
            return 0

    @staticmethod
    def _check_readability(text: str) -> float | None:
        """Flesch Reading Ease score of the homepage copy, or None if there's too little text to score meaningfully."""
        if len(text.split()) < _MIN_WORDS_FOR_READABILITY:
            return None
        try:
            return textstat.flesch_reading_ease(text)
        except Exception as e:
            print(f"[Textstat] Readability check failed: {e}")
            return None

    @staticmethod
    def _check_security_headers(headers: dict, has_ssl: bool) -> list[Flaw]:
        """Missing HTTP security headers, from the response Playwright already fetched (no extra request)."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        flaws: list[Flaw] = []

        if has_ssl and "strict-transport-security" not in headers:
            flaws.append(Flaw(
                category="security", severity="high",
                description="Missing HSTS header — browsers won't force HTTPS on repeat visits, leaving room for a network attacker to downgrade connections to plain HTTP.",
            ))
        if "x-frame-options" not in headers and "content-security-policy" not in headers:
            flaws.append(Flaw(
                category="security", severity="high",
                description="Missing X-Frame-Options header — the site can be embedded in a hidden iframe on another page (clickjacking risk).",
            ))
        if "content-security-policy" not in headers:
            flaws.append(Flaw(
                category="security", severity="medium",
                description="No Content-Security-Policy header — weaker defense against XSS if a vulnerability is ever found in the site's own code or a third-party script.",
            ))
        if "x-content-type-options" not in headers:
            flaws.append(Flaw(
                category="security", severity="medium",
                description="Missing X-Content-Type-Options header — browsers may MIME-sniff responses instead of trusting the declared content type.",
            ))
        if "referrer-policy" not in headers:
            flaws.append(Flaw(
                category="security", severity="low",
                description="No Referrer-Policy header set — full URLs (including any sensitive query parameters) can leak to third-party sites via the Referer header.",
            ))
        return flaws

    async def _run_pyseoanalyzer(self, url: str) -> dict:
        """
        Real crawl-based SEO pass via python-seo-analyzer
        (https://github.com/sethblack/python-seo-analyzer). Makes its own HTTP
        request to *url* (separate from the Playwright fetch) — accepted
        tradeoff for real word-count/duplicate-content/heading checks that
        can't be derived from the HTML already in hand. follow_links=False
        keeps it to just the one page, no site-wide crawl.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run_pyseoanalyzer_sync, url), timeout=15
            )
        except asyncio.TimeoutError:
            print(f"[SEOAnalyzer] Timed out for {url}")
            return {}
        except Exception as e:
            print(f"[SEOAnalyzer] Failed for {url}: {e}")
            return {}

    @staticmethod
    def _run_pyseoanalyzer_sync(url: str) -> dict:
        from pyseoanalyzer import analyze as seo_analyze
        output = seo_analyze(url, follow_links=False)
        pages = output.get("pages") or []
        return pages[0] if pages else {}

    # ------------------------------------------------------------------
    # Step 4 — Flaw reconciliation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_flaws(
        *,
        load_time_ms: int,
        perf_score: int,
        seo_score: int,
        mobile_score: int,
        best_practices_score: int,
        lcp_ms: int | None = None,
        cls: float | None = None,
        tbt_ms: int | None = None,
        has_ssl: bool,
        parsed: dict,
        has_structured_data: bool,
        readability_score: float | None,
        security_flaws: list[Flaw],
        seo_page: dict,
        accessibility_violations: list[dict],
        broken_links: list[dict],
        font_families: list[str] | None = None,
        stretched_images: int = 0,
        robots_blocked: bool = False,
        missing_alt_count: int = 0,
        console_errors: list[str] | None = None,
        mixed_content_urls: list[str] | None = None,
        mobile_horizontal_overflow: bool = False,
        duplicate_title_pages: list[str] | None = None,
        duplicate_meta_pages: list[str] | None = None,
        has_business_schema: bool = False,
    ) -> list[Flaw]:
        """
        Reconcile every audit signal (Lighthouse/PageSpeed, HTML parsing,
        security headers, structured data, readability, pyseoanalyzer,
        axe-core, broken links) into one severity-ranked flaw list. This is
        what the AI prompt consumes instead of raw per-tool dumps — see
        analyzer/flaws.py for why.
        """
        flaws: list[Flaw] = list(security_flaws)

        if load_time_ms > _SLOW_LOAD_MS:
            flaws.append(Flaw("performance", "high", f"Website loads slowly ({load_time_ms}ms). Aim for under {_SLOW_LOAD_MS}ms."))

        if perf_score and perf_score < _LOW_PERF_SCORE:
            flaws.append(Flaw("performance", "high", f"Website takes too long to load on mobile (score: {perf_score}/100)."))

        if seo_score and seo_score < _LOW_SEO_SCORE:
            flaws.append(Flaw("seo", "medium", f"Poor SEO optimisation (score: {seo_score}/100). Meta tags, headings, or structured data may be missing."))

        if mobile_score and mobile_score < _LOW_MOBILE_SCORE:
            flaws.append(Flaw("performance", "high", f"Website is not well-optimised for mobile devices (score: {mobile_score}/100)."))

        if best_practices_score and best_practices_score < _LOW_SEO_SCORE:
            flaws.append(Flaw("tech", "medium", f"Lighthouse best-practices score is low ({best_practices_score}/100) — likely outdated libraries, console errors, or missing image dimensions."))

        # Core Web Vitals — real units, not just the opaque 0-100 performance
        # score above, so the AI can quote exact numbers per the prompt's
        # "quote the exact number" instruction (see analyzer/ai_audit.py).
        # Thresholds are Google's own "good" cutoffs.
        if lcp_ms is not None and lcp_ms > 2500:
            sev = "critical" if lcp_ms > 4000 else "high"
            flaws.append(Flaw("performance", sev, f"Largest Contentful Paint takes {lcp_ms / 1000:.1f}s to render (Google's 'good' threshold is 2.5s) — the main hero content is visibly slow to appear."))

        if cls is not None and cls > 0.1:
            sev = "high" if cls > 0.25 else "medium"
            flaws.append(Flaw("performance", sev, f"Cumulative Layout Shift score is {cls:.2f} (Google's 'good' threshold is 0.10) — page content visibly jumps around while loading, which can cause visitors to mis-click."))

        if tbt_ms is not None and tbt_ms > 200:
            sev = "high" if tbt_ms > 600 else "medium"
            flaws.append(Flaw("performance", sev, f"Total Blocking Time is {tbt_ms}ms (Google's 'good' threshold is 200ms) — the page is unresponsive to clicks/taps for a noticeable stretch while it loads."))

        if not has_ssl:
            flaws.append(Flaw("security", "critical", "Website does not use HTTPS. Visitors see a \"Not Secure\" warning in the browser."))

        if not parsed["meta_title"]:
            flaws.append(Flaw("seo", "high", "Missing page title (<title> tag). This hurts search engine rankings."))

        if not parsed["meta_description"]:
            flaws.append(Flaw("seo", "medium", "Missing meta description. Search engines will generate their own snippet, which may not be ideal."))

        if not parsed["h1_tags"]:
            flaws.append(Flaw("seo", "medium", "No H1 heading found on the homepage. Every page should have exactly one H1."))

        if not parsed["has_cta"]:
            flaws.append(Flaw("content", "high", "No clear call-to-action found on the homepage. Visitors don't know what step to take next."))

        if not parsed["has_contact"]:
            flaws.append(Flaw("content", "high", "No visible phone number or email address on the homepage. Potential customers can't reach you easily."))

        if not parsed["has_testimonials"]:
            flaws.append(Flaw("content", "low", "No testimonials, reviews, or social proof found. Adding trust signals can boost conversion rates."))

        if not parsed["has_blog"]:
            flaws.append(Flaw("content", "low", "No blog or content section detected. Regular content helps with SEO and audience trust."))

        if parsed.get("is_noindexed"):
            flaws.append(Flaw("seo", "critical", "Homepage has a <meta name=\"robots\" content=\"noindex\"> tag — this actively tells Google NOT to index the page. It may be invisible in search results."))

        if not parsed.get("has_canonical"):
            flaws.append(Flaw("seo", "low", "No canonical tag found — minor duplicate-content SEO risk if the page is reachable via multiple URL variants."))

        if not parsed.get("has_og_tags"):
            flaws.append(Flaw("content", "low", "Missing Open Graph tags — links shared on Facebook/LinkedIn/WhatsApp won't show a preview image or description."))

        if not has_structured_data:
            flaws.append(Flaw("seo", "medium", "No structured data (Schema.org/JSON-LD) found — missing out on rich results (ratings, business info) in Google search."))
        elif not has_business_schema:
            flaws.append(Flaw("seo", "low", "Structured data is present but doesn't use a business type like LocalBusiness/Restaurant/Organization — missing out on rich results (star ratings, hours, price range) shown directly in the Google search snippet."))

        if readability_score is not None and readability_score < _LOW_READABILITY_SCORE:
            flaws.append(Flaw("content", "medium", f"Homepage copy scores {readability_score:.0f}/100 on the Flesch Reading Ease scale (very difficult to read) — simplifying the language could improve conversion."))

        if not parsed.get("has_viewport_meta"):
            flaws.append(Flaw("performance", "high", "No mobile viewport meta tag — the page will render at desktop width and appear tiny/unusable on phones."))

        if not parsed.get("has_favicon"):
            flaws.append(Flaw("content", "low", "No favicon found — the browser tab shows a blank/broken icon, which looks unpolished."))

        if font_families and len(font_families) > _MAX_CONSISTENT_FONTS:
            flaws.append(Flaw("content", "medium", f"Uses {len(font_families)} different fonts on one page ({', '.join(font_families[:5])}) — inconsistent typography reads as unpolished/unprofessional."))

        if stretched_images >= 1:
            plural = "s are" if stretched_images > 1 else " is"
            flaws.append(Flaw("content", "medium", f"{stretched_images} image{plural} displayed larger than their native resolution and will look blurry/pixelated to visitors."))

        # --- Business/conversion flaws — matter to the owner reading the
        # email regardless of technical score, and were previously entirely
        # absent (the audit only ever measured performance/SEO/accessibility/
        # security, never "can a customer actually convert on this page"). ---

        if not parsed.get("has_booking_widget"):
            flaws.append(Flaw("conversion", "low", "No online reservation or booking widget detected (no OpenTable, Resy, Calendly, or similar) — customers have to call or email to book instead of completing it in one click, which loses conversions."))

        if not parsed.get("has_click_to_call") and not parsed.get("has_whatsapp_link"):
            flaws.append(Flaw("conversion", "medium", "No click-to-call or WhatsApp link found anywhere on the page — mobile visitors have to manually copy and dial your number instead of tapping to call, unnecessary friction that loses easy conversions."))

        if not parsed.get("has_menu_or_pricing"):
            flaws.append(Flaw("conversion", "medium", "No menu or pricing information findable on the site — visitors have to contact you just to find out what you charge, which is friction most people won't bother with."))

        word_count = seo_page.get("word_count")
        if word_count is not None and word_count < _THIN_CONTENT_WORDS:
            flaws.append(Flaw("seo", "medium", f"Homepage has thin content (~{word_count} words) — search engines tend to rank thin pages lower."))

        # pyseoanalyzer's "Anchor missing title tag" warnings are extremely
        # numerous on most sites and not a meaningful flaw on their own —
        # filtered out to avoid drowning out real signal. "Image missing alt
        # tag" is also filtered: live-verified it fires on images with
        # alt="" (explicitly empty), which is the *correct* WCAG pattern for
        # decorative images, not a bug — axe-core (our authoritative a11y
        # signal) correctly did not flag the same images. It also dumps the
        # raw <img> tag (full srcset included) into the warning text, which
        # is unreadable noise even on the rare case it's right.
        seo_warnings = [
            w for w in seo_page.get("warnings", [])
            if "Anchor missing title tag" not in w and "Image missing alt tag" not in w
        ]
        for warning in seo_warnings[:5]:
            flaws.append(Flaw("seo", "medium", warning))

        # axe-core is the authoritative accessibility signal (Lighthouse's own
        # accessibility score is also axe-core-derived internally, computed
        # independently — rather than surface both and let the AI guess which
        # to trust, axe-core's detailed violations win and Lighthouse's raw
        # accessibility number is dropped from the prompt entirely).
        for violation in accessibility_violations[:5]:
            impact = violation.get("impact", "minor")
            flaws.append(Flaw(
                "accessibility",
                _AXE_SEVERITY_MAP.get(impact, "low"),
                f"[{impact.upper()}] {violation.get('help', '')} ({violation.get('nodes_count', 0)} instance(s) on the page)",
            ))

        if broken_links:
            count = len(broken_links)
            example = broken_links[0].get("url", "")
            flaws.append(Flaw(
                "content",
                "high" if count > 5 else "medium",
                f"{count} broken link(s)/image(s) found on the homepage, e.g. {example}",
            ))

        if robots_blocked:
            flaws.append(Flaw("seo", "critical", "robots.txt blocks ALL search engines from crawling the site (\"Disallow: /\" under \"User-agent: *\") — the site is likely invisible on Google entirely, even though it renders completely normally in a browser so nobody would notice by just looking at it."))

        if missing_alt_count >= 1:
            plural = "s" if missing_alt_count > 1 else ""
            flaws.append(Flaw("accessibility", "medium", f"{missing_alt_count} image{plural} missing an alt attribute entirely (not just an empty alt for decorative images) — screen reader users get no description at all, and it hurts image SEO."))

        if console_errors:
            count = len(console_errors)
            example = console_errors[0][:150]
            flaws.append(Flaw("tech", "high" if count >= 5 else "medium", f"{count} JavaScript error(s) detected while the page loaded (e.g. \"{example}\") — often means a broken button, form, or other interactive feature that visitors can't actually use."))

        if mixed_content_urls:
            count = len(mixed_content_urls)
            example = mixed_content_urls[0]
            flaws.append(Flaw("security", "high", f"{count} resource(s) loaded over insecure HTTP on an HTTPS page (mixed content), e.g. {example} — browsers show a partial \"Not Secure\" warning and may silently block the resource."))

        if mobile_horizontal_overflow:
            flaws.append(Flaw("performance", "high", "Page requires horizontal scrolling on a mobile phone screen — some content is wider than the viewport, a jarring experience for the majority of visitors who are on mobile."))

        if duplicate_title_pages and len(set(duplicate_title_pages)) >= 2:
            pages = ", ".join(sorted(set(duplicate_title_pages)))
            flaws.append(Flaw("seo", "medium", f"Multiple pages ({pages}) share the exact same page title — search engines can't tell them apart, which hurts rankings for both."))

        if duplicate_meta_pages and len(set(duplicate_meta_pages)) >= 2:
            pages = ", ".join(sorted(set(duplicate_meta_pages)))
            flaws.append(Flaw("seo", "low", f"Multiple pages ({pages}) share the exact same meta description — Google may show a generic or duplicate snippet in search results for one of them."))

        return rank_flaws(flaws)

    # ------------------------------------------------------------------
    # Step 5 — Tech stack detection
    # ------------------------------------------------------------------

    async def _detect_technologies(self, url: str) -> list[str]:
        """
        Detect the tech stack of the website using wappalyzer-next
        (https://github.com/s0md3v/wappalyzer-next).

        Uses scan_type="fast" (a single HTTP request, no browser, no extra DNS/JS
        probing) — measured ~5s in testing vs. ~13s for "balanced" and 10-20s+ for
        "full" (which launches its own headless Chromium via the Wappalyzer
        extension). "full" would also stack a second concurrent browser launch on
        top of the Playwright screenshot semaphore and risk the same Railway OOM
        this project already works around elsewhere. Still runs in a worker
        thread with a hard timeout so it can't block the event loop; on
        timeout/error it degrades to an empty list instead of failing the audit.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run_wappalyzer_sync, url), timeout=10
            )
        except asyncio.TimeoutError:
            print(f"[Wappalyzer] Timed out for {url}, skipping tech detection")
            return []
        except Exception as e:
            print(f"[Wappalyzer] Failed for {url}: {e}")
            return []

    @staticmethod
    def _run_wappalyzer_sync(url: str) -> list[str]:
        results = wappalyzer_analyze(url=url, scan_type="fast", timeout=8)
        techs = results.get(url) or next(iter(results.values()), {})
        return sorted(techs.keys())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_url(url: str) -> str:
        """Ensure *url* has a scheme prefix."""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url

import hashlib
import os
import asyncio
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "data", "screenshots")

# Create temporary directory for screenshots
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Global Semaphore to limit Playwright concurrency to 1.
# This prevents Out of Memory (OOM) crashes on Railway's 500MB instances.
_PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(1)

# Beyond the homepage, also crawl up to this many internal about/services/
# contact-style pages so accessibility, broken-link, and visual checks
# aren't limited to just the homepage. Kept small (each page adds a full
# Playwright navigation + axe-core run) to stay within Railway's 500MB
# instance budget alongside the single-browser semaphore above.
_EXTRA_PAGE_KEYWORDS = ['about', 'service', 'pricing', 'contact', 'product', 'work', 'solution']
_MAX_EXTRA_PAGES = 2
_SEVERITY_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


def normalise_url(url: str) -> str:
    """Ensure *url* has a scheme prefix (mirrors scrapers.website._normalise_url)."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def make_screenshot_filename(company_name: str, url: str) -> str:
    """
    Build a screenshot filename from company name + a short hash of the
    (normalised) URL, so two companies that sanitize to the same name
    don't overwrite each other's screenshot.
    """
    safe_name = "".join(c if c.isalnum() else "_" for c in company_name.lower())
    url_hash = hashlib.md5(normalise_url(url).encode()).hexdigest()[:8]
    return f"{safe_name}_{url_hash}_audit.jpg"


async def _discover_extra_urls_via_sitemap(base_url: str) -> list[str]:
    """
    Try /sitemap.xml first for page discovery — catches real pages that
    aren't linked from the homepage nav (old blog posts, footer-only pages,
    pages behind a hamburger menu JS never renders for us), which the
    anchor-keyword scan below would miss entirely.
    """
    parsed = urlparse(base_url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"

    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            response = await client.get(sitemap_url)
            if response.status_code != 200:
                return []
            root = ET.fromstring(response.content)
    except Exception:
        return []

    base_netloc = parsed.netloc
    found = []
    seen = set()
    # Namespace-agnostic match on tag name — sitemap XML namespaces vary
    # (and sitemap index files nest <sitemap><loc> instead of <url><loc>),
    # so matching any element literally named "loc" covers both shapes.
    for elem in root.iter():
        if not elem.tag.endswith("loc") or not elem.text:
            continue
        loc = elem.text.strip()
        if not any(kw in loc.lower() for kw in _EXTRA_PAGE_KEYWORDS):
            continue
        if urlparse(loc).netloc != base_netloc:
            continue
        normalised = loc.split("#")[0].rstrip("/")
        if normalised in seen or normalised == base_url.rstrip("/"):
            continue
        seen.add(normalised)
        found.append(loc)
        if len(found) >= _MAX_EXTRA_PAGES:
            break
    return found


def _discover_extra_urls(html: str, base_url: str) -> list[str]:
    """
    Find up to _MAX_EXTRA_PAGES internal about/services/contact-style links
    on the homepage worth auditing too, so accessibility/broken-link/visual
    checks aren't limited to just the homepage.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc

    found = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].lower()
        if not any(kw in href for kw in _EXTRA_PAGE_KEYWORDS):
            continue
        full_url = urljoin(base_url, anchor["href"])
        if urlparse(full_url).netloc != base_netloc:
            continue
        normalised = full_url.split("#")[0].rstrip("/")
        if normalised in seen or normalised == base_url.rstrip("/"):
            continue
        seen.add(normalised)
        found.append(full_url)
        if len(found) >= _MAX_EXTRA_PAGES:
            break
    return found


def _page_label(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path if path else "/"


async def _audit_current_page(page, context, label: str, screenshot_bytes: bytes) -> dict:
    """
    Run every per-page check (accessibility, broken links, fonts, stretched
    images, title/meta-description) against whatever page is currently
    loaded in *page*. Violations and broken links get tagged with *label*
    when it's not the homepage, so the AI prompt (and the human reading the
    email) knows which page a flaw came from.
    """
    violations, visual_flaw = await _run_axe_audit(page)
    broken_links, links_checked = await _check_broken_assets(page, context)
    font_families = await _check_font_consistency(page)
    stretched_images = await _check_stretched_images(page)

    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        meta_description = await page.evaluate(
            """() => {
                const tag = document.querySelector('meta[name="description"]');
                return tag ? tag.content.trim() : '';
            }"""
        )
    except Exception:
        meta_description = ""

    # Tag with the source page via a clean separate key (not baked into
    # "help"/appended to the text) so violations/links found on multiple
    # pages can be consolidated into one entry later instead of appearing
    # as N near-duplicate flaws — see _consolidate_by_id in
    # generate_audit_screenshot below.
    for v in violations:
        v["page"] = label
    for l in broken_links:
        l["found_on"] = label

    return {
        "label": label,
        "screenshot_bytes": screenshot_bytes,
        "violations": violations,
        "visual_flaw": visual_flaw,
        "broken_links": broken_links,
        "links_checked": links_checked,
        "font_families": font_families,
        "stretched_images": stretched_images,
        "title": title,
        "meta_description": meta_description,
    }


def _consolidate_by_key(items: list[dict], key: str, page_field: str) -> list[dict]:
    """
    Merge items (accessibility violations or broken links) that are the SAME
    underlying issue found on multiple pages into ONE entry with a combined
    "pages" list, instead of one near-duplicate entry per page. Without
    this, the same violation/link on N pages occupies N slots in the
    top-15 severity-ranked list the AI picks its "2-3 most severe" flaws
    from — live-observed exactly this on a real site: "Links must have
    discernible text" appeared 3 separate times (homepage, /about-us,
    mobile view) for what is really one fix.
    """
    merged: dict = {}
    order: list = []
    for item in items:
        k = item.get(key) or item.get("help", "")
        if k not in merged:
            merged[k] = dict(item)
            merged[k]["pages"] = []
            order.append(k)
        entry = merged[k]
        page = item.get(page_field)
        if page and page not in entry["pages"]:
            entry["pages"].append(page)
        if "nodes_count" in item:
            entry["nodes_count"] = max(entry.get("nodes_count", 0), item.get("nodes_count", 0))
    return [merged[k] for k in order]


def _find_duplicate_page_labels(pages_checked: list[dict], key: str) -> list[str]:
    """
    Return the page labels that share an identical (non-empty) *key* value
    with at least one other crawled page — e.g. two pages with the exact
    same <title>, a common thin-SEO mistake that's only detectable now that
    multiple pages get crawled per lead.
    """
    from collections import Counter

    pairs = [(p["label"], (p.get(key) or "").strip()) for p in pages_checked]
    pairs = [(label, value) for label, value in pairs if value]
    counts = Counter(value for _, value in pairs)
    return [label for label, value in pairs if counts[value] > 1]


_AUDIT_SCREENSHOT_RETRIES = 3


async def generate_audit_screenshot(url: str, company_name: str) -> tuple[str | None, str | None, dict | None]:
    """
    Thin retry wrapper around _generate_audit_screenshot_once. A single
    failure anywhere in that function's multi-page crawl (homepage + extra
    pages + mobile revisit + axe-core + broken-link checks) used to hard-fail
    the whole audit and report "Website is unreachable" even when the real
    site was fine — live-verified 2026-07-20 against a real hotel-chain site
    (itchotels.com) that returned a clean 200 and full HTML on a second,
    isolated attempt seconds after the pipeline reported it as down. Large
    sites behind Akamai/Cloudflare bot management are especially prone to
    intermittently challenging a single headless-browser request without
    the site actually being down. Retries twice (3 attempts total, bumped
    from 2 on 2026-08-07 after a real lead — see scrapers/website.py's httpx
    fallback — hit exactly two failures in a row) with a short delay before
    giving up and reporting unreachable, same "transient blip, not a dead
    site" assumption as SES's Throttling retry.
    """
    last_result = (None, None, None)
    for attempt in range(_AUDIT_SCREENSHOT_RETRIES):
        last_result = await _generate_audit_screenshot_once(url, company_name)
        if last_result[1] is not None:  # html_content present = success
            return last_result
        if attempt < _AUDIT_SCREENSHOT_RETRIES - 1:
            print(f"[Visuals] Audit attempt {attempt + 1} failed for {url} — retrying (likely transient network/bot-detection blip, not a dead site)...")
            await asyncio.sleep(5)
    return last_result


async def _generate_audit_screenshot_once(url: str, company_name: str) -> tuple[str | None, str | None, dict | None]:
    """
    Takes a desktop screenshot of the URL, runs accessibility + broken link
    audits across the homepage plus up to _MAX_EXTRA_PAGES internal pages
    (discovered via sitemap.xml, falling back to about/services/contact-style
    links found on the homepage), and returns a tuple of (filepath,
    html_content, extra_audit_data). Also captures a separate real
    mobile-viewport (390x844) screenshot of the homepage for the AI to
    compare against, since desktop screenshots miss mobile-only problems.

    The attached (desktop) screenshot is whichever audited page had the most
    severe accessibility violation with a valid bounding box, falling back
    to the homepage if no page had one.

    extra_audit_data contains:
        - accessibility_violations: list of axe-core violations across all audited pages
        - broken_links: list of broken URLs found across all audited pages
        - perf_timing: dict with real browser timing metrics (homepage only)
        - response_headers: dict of HTTP response headers from the homepage load (for security-header checks)
        - pages_audited: list of page paths that were actually crawled
        - mobile_image_path: path to the separate mobile-viewport screenshot, or None if it failed
        - console_errors: JS console error messages captured across every page visited
        - mixed_content_urls: HTTP resource URLs loaded on an HTTPS page
        - mobile_horizontal_overflow: True if the homepage requires horizontal scrolling at 390px width
        - duplicate_title_pages / duplicate_meta_pages: page labels sharing an identical title/meta description

    Returns (None, None, None) on failure. Called by generate_audit_screenshot
    above, which retries this once before accepting a failure as real.
    """
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        async with _PLAYWRIGHT_SEMAPHORE:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)

                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                # Real, in-browser Core Web Vitals (not Lighthouse's lab-
                # simulated ones) via PerformanceObserver, same primitives
                # Google's own web-vitals JS library wraps — no dependency
                # needed since these are native browser APIs. Registered as
                # a context-level init script so it attaches before ANY
                # page script runs on every navigation, which matters for
                # LCP/CLS specifically since both can fire within the first
                # few hundred ms of a real page load.
                #
                # MUST be a bare script body, NOT wrapped in "() => { ... }".
                # Playwright's add_init_script evaluates the string as a JS
                # expression/statement — a lone arrow function expression is
                # valid JS that just constructs a function value and never
                # calls it, so the body silently never runs. Live-verified
                # 2026-07-31: this was true on EVERY site tested, including
                # example.com, not just one flaky site — window.__webVitals
                # was always undefined, so _get_real_web_vitals always fell
                # back to Lighthouse's throttled/simulated LCP number, which
                # runs 5-10x higher than a real unthrottled page load. This
                # is what produced a "hero content takes 17.5s" flaw on a
                # site that a real visitor saw render in ~2s.
                await context.add_init_script("""
                    window.__webVitals = { lcp: null, clsSum: 0, tbtMs: 0 };
                    try {
                        new PerformanceObserver((list) => {
                            const entries = list.getEntries();
                            const last = entries[entries.length - 1];
                            if (last) window.__webVitals.lcp = last.startTime;
                        }).observe({ type: 'largest-contentful-paint', buffered: true });
                    } catch (e) {}
                    try {
                        new PerformanceObserver((list) => {
                            for (const entry of list.getEntries()) {
                                if (!entry.hadRecentInput) window.__webVitals.clsSum += entry.value;
                            }
                        }).observe({ type: 'layout-shift', buffered: true });
                    } catch (e) {}
                    try {
                        new PerformanceObserver((list) => {
                            for (const entry of list.getEntries()) {
                                const blocking = entry.duration - 50;
                                if (blocking > 0) window.__webVitals.tbtMs += blocking;
                            }
                        }).observe({ type: 'longtask', buffered: true });
                    } catch (e) {}
                """)

                page = await context.new_page()

                # Listeners persist for the page's whole lifetime, so attaching
                # them once here captures console errors and mixed-content
                # requests across the homepage, every extra page crawled below,
                # AND the mobile-viewport revisit — no need to re-attach.
                console_errors: list[str] = []
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" and len(console_errors) < 10 else None)

                mixed_content_urls: set[str] = set()
                def _track_mixed_content(request):
                    try:
                        if request.url.startswith("http://") and page.url.startswith("https://"):
                            mixed_content_urls.add(request.url)
                    except Exception:
                        pass
                page.on("request", _track_mixed_content)

                # wait_until="domcontentloaded" fires as soon as the page itself is
                # parsed and usable — NOT "load", which waits for every single
                # resource (analytics beacons, chat widgets, ad iframes, web fonts)
                # to finish, and never fires within any reasonable timeout on a lot
                # of real small-business sites even though the site is fine and
                # loads instantly for an actual visitor. Live-verified: a site that
                # curl'd back in 2.5s was being reported as fully "unreachable"
                # (0/100 scores, "your website is down" email) purely because one
                # hung third-party script kept the "load" event from ever firing.
                # A generous timeout is still kept as a safety net for genuinely
                # slow/dead servers — Playwright runs through a single shared
                # browser (see _PLAYWRIGHT_SEMAPHORE below), so a truly unbounded
                # wait on one bad site would freeze every other lead queued behind
                # it, not just fail the one lead.
                response = await page.goto(url, timeout=120000, wait_until="domcontentloaded")

                # Response headers, reused for the security-headers check — this is
                # the request we're already making for the screenshot, so capturing
                # headers here is free (no extra network call).
                response_headers = dict(response.headers) if response else {}

                # The final URL after any redirects (e.g. http:// -> https://,
                # or a bare domain -> www subdomain) — used for the HTTPS
                # check instead of the raw input URL string, since a lead's
                # stored URL is often "http://..." even when the site
                # immediately redirects to HTTPS.
                final_url = page.url

                # The `load` event fires before CSS fade-in animations finish and
                # before lazy-loaded hero images/cookie-banner widgets settle, so a
                # screenshot taken immediately after goto() can capture a half-faded,
                # not-yet-rendered page that doesn't match what a real visitor sees.
                # Best-effort wait for network activity to quiet down, then a fixed
                # settle delay for CSS transitions — never fail the audit over this.
                #
                # 10s here specifically (not the 1.5s used for extra/mobile pages
                # below) because the HOMEPAGE is where full-screen JS preloaders/
                # intro animations live (a spinner or logo-reveal that gates the
                # real content) — live-verified 2026-08-07 as a real contributor
                # to false "site is unreachable" audits: on a slow run, axe-core
                # and the screenshot could fire while a preloader was still up,
                # not because the site was actually down.
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(10000)

                # Web fonts settle AFTER networkidle on plenty of sites (a font
                # requested from inside a stylesheet that itself loaded late),
                # and text rendered mid-swap either shows in the fallback face
                # or, during the block period, not at all. document.fonts.ready
                # is the browser's own "all @font-face loads have resolved"
                # signal, so this waits for the real thing rather than guessing
                # with another sleep.
                try:
                    await page.evaluate("() => document.fonts.ready")
                except Exception:
                    pass

                # Scroll the full height and come back. Entrance animations
                # (AOS/GSAP/Framer and every theme that ships one) hold their
                # elements at opacity 0 until an IntersectionObserver fires,
                # and lazy-loaded images do not decode until they approach the
                # viewport — neither happens for a browser that never scrolls,
                # so the screenshot captures placeholders and invisible text
                # that a real visitor never sees. Measured on
                # namasteyogaclasses.com: 22 elements at opacity 0 before this
                # pass, 11 after. Best-effort — never fail the audit over it.
                try:
                    await page.evaluate(
                        """async () => {
                            const step = window.innerHeight;
                            const height = document.body.scrollHeight;
                            for (let y = 0; y < height; y += step) {
                                window.scrollTo(0, y);
                                await new Promise(r => setTimeout(r, 150));
                            }
                            window.scrollTo(0, 0);
                        }"""
                    )
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Can this browser draw text AT ALL? A container with no font
                # packages installed renders images and shapes normally while
                # every text node comes out blank, which is indistinguishable
                # from a genuine design flaw to the vision model and produces
                # an attached screenshot of the prospect's site with nothing
                # written on it. See the fonts-* block in the Dockerfile.
                text_renderable = await _can_render_text(page)

                # --- 1. Take screenshot ---
                screenshot_bytes = await page.screenshot(full_page=False)

                # --- 2. Grab fully rendered HTML ---
                html_content = await page.content()

                # --- 3. Capture real performance timing from the browser ---
                perf_timing = await _get_performance_timing(page)
                real_web_vitals = await _get_real_web_vitals(page)

                # --- 4. Run every per-page check on the homepage ---
                pages_checked = [await _audit_current_page(page, context, "/", screenshot_bytes)]

                # --- 5. Crawl a few internal pages and run the same checks on them ---
                # Sitemap.xml first (catches real pages the nav doesn't link to);
                # falls back to scanning the homepage's own anchor tags.
                extra_urls = await _discover_extra_urls_via_sitemap(final_url)
                if not extra_urls:
                    extra_urls = _discover_extra_urls(html_content, final_url)
                for extra_url in extra_urls:
                    label = _page_label(extra_url)
                    try:
                        await page.goto(extra_url, timeout=60000, wait_until="domcontentloaded")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        extra_screenshot = await page.screenshot(full_page=False)
                        pages_checked.append(await _audit_current_page(page, context, label, extra_screenshot))
                    except Exception as e:
                        # A slow/broken subpage shouldn't sink the whole audit —
                        # just skip it and keep whatever pages did succeed.
                        print(f"[Visuals] Skipping extra page {extra_url}: {e}")

                # --- 6. A real mobile-viewport screenshot, separate from the
                # desktop one above. Desktop screenshots systematically miss
                # mobile-only problems (overflow, unreadable font, tap targets
                # too close together) that most visitors to a small-business
                # site will actually hit, since most traffic is from phones.
                # Also runs axe-core AGAIN at mobile width — WCAG's tap-target
                # rules behave differently at 390px than at 1280px, so this
                # catches mobile-specific violations the desktop pass misses —
                # plus a direct horizontal-overflow check, which desktop can
                # never trigger at 1280px on a responsive site.
                mobile_screenshot_bytes = None
                mobile_horizontal_overflow = False
                mobile_violations: list[dict] = []
                try:
                    await page.goto(final_url, timeout=60000, wait_until="domcontentloaded")
                    await page.set_viewport_size({"width": 390, "height": 844})
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                    mobile_screenshot_bytes = await page.screenshot(full_page=False)

                    try:
                        mobile_horizontal_overflow = await page.evaluate(
                            "() => document.documentElement.scrollWidth > window.innerWidth + 5"
                        )
                    except Exception:
                        pass

                    mobile_violations, _ = await _run_axe_audit(page)
                    for v in mobile_violations:
                        v["page"] = "mobile view"
                except Exception as e:
                    print(f"[Visuals] Mobile screenshot/checks failed (non-critical): {e}")

                await browser.close()

        # Pick whichever audited page has the most severe visual flaw (red-box
        # evidence) to attach to the email — falls back to the homepage if no
        # page had a violation with a usable bounding box.
        best = pages_checked[0]
        for candidate in pages_checked[1:]:
            if candidate["visual_flaw"] and (
                not best["visual_flaw"]
                or _SEVERITY_RANK.get(candidate["visual_flaw"].get("impact"), 4)
                < _SEVERITY_RANK.get(best["visual_flaw"].get("impact"), 4)
            ):
                best = candidate

        # Draw analysis box on the chosen image
        img = Image.open(BytesIO(best["screenshot_bytes"])).convert("RGB")

        visual_flaw_context = ""
        if best["visual_flaw"]:
            draw = ImageDraw.Draw(img)
            box = best["visual_flaw"]["box"]
            # Pad the box slightly for better visibility
            pad = 5
            padded_box = [max(0, box[0]-pad), max(0, box[1]-pad), box[2]+pad, box[3]+pad]
            draw.rectangle(padded_box, outline="red", width=4)
            page_note = f" on the {best['label']} page" if best["label"] != "/" else ""
            visual_flaw_context = f"The red box in the screenshot highlights an accessibility flaw{page_note}: {best['visual_flaw']['description']}."

        filepath = os.path.join(SCREENSHOTS_DIR, make_screenshot_filename(company_name, url))
        img.save(filepath, format="JPEG", quality=85)

        mobile_filepath = None
        if mobile_screenshot_bytes:
            mobile_img = Image.open(BytesIO(mobile_screenshot_bytes)).convert("RGB")
            mobile_filepath = os.path.join(
                SCREENSHOTS_DIR,
                make_screenshot_filename(company_name, url).replace("_audit.jpg", "_mobile.jpg"),
            )
            mobile_img.save(mobile_filepath, format="JPEG", quality=85)

        # Aggregate signals across every page audited, most severe first.
        # Consolidated by id/url first — the same violation or broken link
        # found on multiple pages becomes ONE entry with a combined page
        # list, not N near-duplicate entries crowding the top-15 the AI
        # picks its "2-3 most severe" flaws from.
        all_violations = _consolidate_by_key(
            [v for p in pages_checked for v in p["violations"]] + mobile_violations,
            key="id", page_field="page",
        )
        all_violations.sort(key=lambda v: _SEVERITY_RANK.get(v.get("impact", ""), 4))

        all_broken_links = _consolidate_by_key(
            [l for p in pages_checked for l in p["broken_links"]],
            key="url", page_field="found_on",
        )
        all_font_families = sorted({f for p in pages_checked for f in p["font_families"]})
        total_stretched_images = sum(p["stretched_images"] for p in pages_checked)

        duplicate_title_pages = _find_duplicate_page_labels(pages_checked, "title")
        duplicate_meta_pages = _find_duplicate_page_labels(pages_checked, "meta_description")

        extra_audit_data = {
            "accessibility_violations": all_violations[:15],
            "broken_links": all_broken_links[:15],
            # Total assets actually probed across every page audited. Zero
            # means the scan never ran, which must not read as "no broken
            # links" — see _check_broken_assets.
            "checked_links": sum(p.get("links_checked", 0) for p in pages_checked),
            "perf_timing": perf_timing,
            "response_headers": response_headers,
            "visual_flaw_context": visual_flaw_context,
            "font_families": all_font_families,
            "stretched_images": total_stretched_images,
            "console_errors": console_errors,
            "mixed_content_urls": sorted(mixed_content_urls)[:5],
            "mobile_horizontal_overflow": bool(mobile_horizontal_overflow),
            "duplicate_title_pages": duplicate_title_pages,
            "duplicate_meta_pages": duplicate_meta_pages,
            "final_url": final_url,
            "pages_audited": [p["label"] for p in pages_checked],
            "mobile_image_path": mobile_filepath,
            "real_web_vitals": real_web_vitals,
            "text_renderable": text_renderable,
        }

        return filepath, html_content, extra_audit_data

    except Exception as e:
        print(f"Failed to generate visual evidence for {url}: {e}")
        return None, None, None


async def _get_performance_timing(page) -> dict:
    """Extract real browser performance metrics from Navigation Timing API."""
    try:
        timing = await page.evaluate("""() => {
            const perf = performance.getEntriesByType('navigation')[0];
            if (!perf) return null;
            return {
                dns_ms: Math.round(perf.domainLookupEnd - perf.domainLookupStart),
                connect_ms: Math.round(perf.connectEnd - perf.connectStart),
                ttfb_ms: Math.round(perf.responseStart - perf.requestStart),
                dom_load_ms: Math.round(perf.domContentLoadedEventEnd - perf.startTime),
                full_load_ms: Math.round(perf.loadEventEnd - perf.startTime),
                transfer_size_kb: Math.round((perf.transferSize || 0) / 1024),
            };
        }""")
        if timing:
            return timing
    except Exception as e:
        print(f"[Perf] Failed to extract timing: {e}")
    return {}


async def _get_real_web_vitals(page) -> dict:
    """
    Read back the LCP/CLS/TBT accumulated by the PerformanceObserver init
    script registered in generate_audit_screenshot(). These are real,
    measured-in-browser numbers (the same primitives Google's own
    web-vitals library uses), not Lighthouse's lab-simulated equivalents —
    scrapers/website.py prefers these over lighthouse_scores' lcp_ms/cls/
    tbt_ms when both are available, since a real measurement beats a
    simulated one. Returns {} (falls back to Lighthouse entirely) if the
    observers never attached (older browser, or the page navigated before
    the init script's observers had anything to report).
    """
    try:
        raw = await page.evaluate("() => window.__webVitals || null")
        if not raw:
            return {}
        lcp = raw.get("lcp")
        cls = raw.get("clsSum")
        tbt = raw.get("tbtMs")
        return {
            "lcp_ms": round(lcp) if isinstance(lcp, (int, float)) else None,
            "cls": round(cls, 3) if isinstance(cls, (int, float)) else None,
            "tbt_ms": round(tbt) if isinstance(tbt, (int, float)) else None,
        }
    except Exception as e:
        print(f"[Perf] Failed to read real web vitals: {e}")
        return {}


async def _run_axe_audit(page) -> tuple[list, dict | None]:
    """Run axe-core accessibility engine on the current page."""
    try:
        from axe_playwright_python.async_playwright import Axe
        axe = Axe()
        results = await axe.run(page)
        
        violations = []
        for v in results.response.get("violations", []):
            violations.append({
                "id": v.get("id", ""),
                "impact": v.get("impact", ""),  # critical, serious, moderate, minor
                "description": v.get("description", ""),
                "help": v.get("help", ""),
                "nodes": v.get("nodes", []),
                "nodes_count": len(v.get("nodes", [])),
            })
        
        # Sort by severity: critical > serious > moderate > minor
        severity_order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
        violations.sort(key=lambda x: severity_order.get(x["impact"], 4))
        
        # Interconnect: Find a visible node to draw a real red box around.
        # bounding_box() reports position in full-DOCUMENT coordinates, not
        # clipped to the viewport — an element below the fold (a footer
        # iframe, a booking widget halfway down the page) still returns a
        # "valid" non-null box, but the screenshot this gets drawn onto is
        # page.screenshot(full_page=False) — viewport-only. Live-verified
        # 2026-08-07 against a real lead (lp.zooty.in/anjaneya-dental-care):
        # a `frame-title` violation on an off-screen iframe produced a box
        # at y=4858 on an 800px-tall image (invisible), and on a different
        # run the same site's `region` violation resolved to a full hero
        # wrapper `<div>` — in-bounds at the top, but tall enough to fill
        # nearly the whole visible screenshot, which is indistinguishable
        # from "no specific flaw was highlighted" even though a box WAS
        # drawn. Viewport dimensions are read from the page itself (not
        # hardcoded) since mobile passes use a different viewport.
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        visual_flaw = None
        for v in violations:
            for node in v.get("nodes", []):
                target_selectors = node.get("target", [])
                if not target_selectors:
                    continue
                selector = target_selectors[0]
                try:
                    if isinstance(selector, list):
                        selector = selector[0]

                    # Skip root level elements as they don't make for good visual highlights
                    if selector.lower() in ["html", "body", "head"]:
                        continue

                    # Get exact coordinates of the flawed element. 3s, not
                    # Playwright's default — this was the one 1s timeout left
                    # over from before the rest of this function's waits were
                    # generously bumped (see the homepage settle delay above);
                    # a late-appearing/animating element now has more than a
                    # blink to become measurable before this candidate is
                    # given up on and the loop moves to the next node.
                    box = await page.locator(selector).first.bounding_box(timeout=3000)
                    if box and box["width"] > 0 and box["height"] > 0:
                        # Reject anything not fully inside the captured viewport —
                        # a partially/fully off-screen box either draws invisibly
                        # or, worse, still shows up but highlights nothing specific.
                        if (
                            box["x"] < 0 or box["y"] < 0
                            or box["x"] + box["width"] > viewport["width"]
                            or box["y"] + box["height"] > viewport["height"]
                        ):
                            continue
                        # Skip if the element covers most of the visible screenshot
                        # (e.g. a hero wrapper div) — not a "specific" flaw to highlight.
                        if box["width"] > viewport["width"] * 0.9 and box["height"] > viewport["height"] * 0.9:
                            continue

                        visual_flaw = {
                            "box": [box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]],
                            "description": v["help"],
                            "impact": v.get("impact", "minor"),
                        }
                        break
                except Exception:
                    pass
            if visual_flaw:
                break
        
        # Clean up nodes array to save memory
        for v in violations:
            v.pop("nodes", None)
            
        print(f"[Axe] Found {len(violations)} accessibility violations.")
        return violations[:10], visual_flaw
        
    except Exception as e:
        print(f"[Axe] Accessibility audit failed (non-critical): {e}")
        return [], None


async def _check_font_consistency(page) -> list:
    """
    Collect distinct font-family stacks actually rendered on visible text.
    Too many different fonts on one page is a classic "doesn't look
    professional/cohesive" symptom — cheap to detect via computed styles,
    no visual-model call needed.
    """
    try:
        families = await page.evaluate("""() => {
            const seen = new Set();
            const els = document.querySelectorAll('h1, h2, h3, h4, h5, h6, p, a, button, span, li, label');
            for (const el of els) {
                // Skip elements with no visible text — an empty heading or an
                // icon-only span's "font" isn't a typography signal.
                if (!el.textContent || !el.textContent.trim()) continue;
                // Skip screen-reader-only elements (skip-links etc) — visible
                // to nobody sighted, so irrelevant to visual typography.
                // Common utility class names; live-verified a real site's
                // skip-link ("visually-hidden") slips past a geometry-only
                // check since off-screen-positioning techniques (left:
                // -9999px) don't shrink the bounding box like clip-based
                // ones do.
                if (/sr-only|screen-reader|visually-?hidden/i.test(el.className)) continue;
                const rect = el.getBoundingClientRect();
                // Skip zero-size AND the classic 1px clip-based hidden technique.
                if (rect.width <= 2 || rect.height <= 2) continue;
                const family = getComputedStyle(el).fontFamily;
                if (family) seen.add(family.split(',')[0].replace(/['"]/g, '').trim());
            }
            return Array.from(seen);
        }""")
        return families or []
    except Exception as e:
        print(f"[Visuals] Font consistency check failed (non-critical): {e}")
        return []


async def _check_stretched_images(page) -> int:
    """
    Count visible <img> elements displayed significantly larger than their
    natural (source) resolution — a classic cause of blurry/pixelated
    images that immediately reads as unpolished.
    """
    try:
        count = await page.evaluate("""() => {
            const imgs = document.querySelectorAll('img');
            let stretched = 0;
            for (const img of imgs) {
                const rect = img.getBoundingClientRect();
                if (rect.width < 40 || rect.height < 40) continue; // ignore icons
                if (!img.naturalWidth || !img.naturalHeight) continue;
                if (rect.width > img.naturalWidth * 1.4 || rect.height > img.naturalHeight * 1.4) {
                    stretched++;
                }
            }
            return stretched;
        }""")
        return count or 0
    except Exception as e:
        print(f"[Visuals] Stretched image check failed (non-critical): {e}")
        return 0


async def _can_render_text(page) -> bool:
    """
    True if this browser can actually draw text.

    Measures a hidden probe span in each generic family. When no font packages
    are installed — the default for python:*-slim, which is what this project
    deploys on — the glyphs have no outlines and every run collapses to zero
    width, so the page screenshots with images and colours intact and not one
    word visible. Nothing downstream can tell that apart from a site whose
    text genuinely doesn't show, so it has to be measured rather than assumed.

    Returns True on any error: an unverifiable probe must not be reported as
    a rendering failure.
    """
    try:
        width = await page.evaluate(
            """() => {
                const probe = document.createElement('span');
                probe.textContent = 'ABCDEFGabcdefg0123456789';
                probe.style.cssText = 'position:absolute;left:-9999px;top:0;'
                    + 'visibility:hidden;font-size:48px;white-space:nowrap;';
                document.body.appendChild(probe);
                let widest = 0;
                for (const family of ['serif', 'sans-serif', 'monospace']) {
                    probe.style.fontFamily = family;
                    widest = Math.max(widest, probe.getBoundingClientRect().width);
                }
                probe.remove();
                return widest;
            }"""
        )
    except Exception as exc:
        print(f"[Visuals] Font-rendering probe failed (assuming text renders): {exc}")
        return True

    # 24 characters at 48px cannot legitimately measure under ~50px unless
    # nothing is being drawn.
    if width < 50:
        print(
            f"[Visuals] WARNING: this browser cannot render text (probe width {width}px). "
            "The screenshot will show images but no words, and any visual critique of it "
            "would be describing the container's missing fonts, not the site. "
            "Install the fonts-* packages listed in the Dockerfile."
        )
        return False
    return True


# Hosts that refuse automated clients regardless of whether the link works.
# Their 400/403/429 says "you are not a browser", not "this link is dead", but
# the probe cannot tell the difference and the flaw copy that results tells a
# business owner their Facebook link is broken when it opens fine for every
# real visitor. Live-verified on yogahouse.in: of 111 assets probed, the only
# failure was facebook.com/theyogahousemumbai, which returns 400 to BOTH HEAD
# and GET, so the existing GET retry cannot rescue it. This gets worse from a
# datacenter IP like the Railway deploy, where more of these refuse outright.
#
# Excluded rather than probed-and-ignored: they occupy slots in the capped
# asset sample, so probing them costs coverage of links we can actually judge.
_BOT_HOSTILE_HOSTS = (
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "tiktok.com", "pinterest.com", "threads.net", "whatsapp.com",
    "wa.me", "t.me",
)


def _is_bot_hostile(url: str) -> bool:
    """True if *url* points at a host known to block automated requests."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in _BOT_HOSTILE_HOSTS)


async def _check_broken_assets(page, context) -> tuple[list, int]:
    """
    Check for broken links and images on the page.

    Returns (broken, checked_count). The count matters: this whole function
    degrades to an empty list on any failure, and an empty list is also what
    a perfectly clean page produces — so without a count of what was actually
    probed, a crashed scan is indistinguishable from "no broken links", and
    the audit would report a clean bill of health it never earned.
    """
    broken = []
    checked = 0
    try:
        # Extract all links and images
        # `a.href` is the DOM-RESOLVED absolute URL, so <a href="#content">
        # arrives here as "https://site.com/#content" and sails through a
        # startsWith('http') filter as if it were an ordinary outbound link.
        # It then gets probed, and any hiccup on that request reports the
        # page's own skip-link as broken — live-observed on yogahouse.in,
        # where the drafted email told the owner "the anchor link to your
        # content section goes nowhere" about a link that returns 200 and
        # cannot go anywhere but the current page. Compare against the raw
        # attribute so same-page fragments are excluded at the source.
        assets = await page.evaluate("""() => {
            const isSamePageFragment = (a) => {
                const raw = a.getAttribute('href') || '';
                if (raw.startsWith('#')) return true;
                try {
                    const u = new URL(a.href);
                    return u.hash && (u.origin + u.pathname + u.search)
                        === (location.origin + location.pathname + location.search);
                } catch (e) { return false; }
            };
            const links = Array.from(document.querySelectorAll('a[href]'))
                .filter(a => !isSamePageFragment(a))
                .map(a => ({type: 'link', url: a.href, text: a.textContent.trim().substring(0, 50)}))
                .filter(l => l.url.startsWith('http'));
            const images = Array.from(document.querySelectorAll('img[src]'))
                .map(img => ({type: 'image', url: img.src, text: img.alt || 'no alt text'}))
                .filter(i => i.url.startsWith('http'));
            return [...links.slice(0, 15), ...images.slice(0, 10)];
        }""")

        assets = [a for a in assets if not _is_bot_hostile(a["url"])]
        
        # Check each asset with a HEAD request first (fast, no body download).
        # Some servers/WAFs specifically reject or rate-limit HEAD probes from
        # automated tools while GET works fine for real visitors — live-
        # observed this exact flakiness (same site, same code: 6 "broken"
        # links on one run, 0 on the next) — so a HEAD failure retries once
        # via GET before being trusted as a genuinely broken link.
        for asset in assets:
            status = None
            try:
                response = await context.request.head(asset["url"], timeout=20000)
                status = response.status
            except Exception:
                status = None

            if status is not None and status < 400:
                continue

            try:
                response = await context.request.get(asset["url"], timeout=20000)
                status = response.status
            except Exception:
                status = "unreachable"

            if status == "unreachable" or (isinstance(status, int) and status >= 400):
                broken.append({
                    "type": asset["type"],
                    "url": asset["url"],
                    "text": asset["text"],
                    "status": status
                })
        
        checked = len(assets)
        print(f"[Links] Checked {checked} assets, found {len(broken)} broken.")

    except Exception as e:
        print(f"[Links] Broken asset check failed (non-critical): {e}")

    return broken[:10], checked  # Cap at 10

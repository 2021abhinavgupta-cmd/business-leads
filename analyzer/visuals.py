import hashlib
import os
import asyncio
from io import BytesIO
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "data", "screenshots")

# Create temporary directory for screenshots
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Global Semaphore to limit Playwright concurrency to 1.
# This prevents Out of Memory (OOM) crashes on Railway's 500MB instances.
_PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(1)


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


async def generate_audit_screenshot(url: str, company_name: str) -> tuple[str | None, str | None, dict | None]:
    """
    Takes a mobile screenshot of the URL, runs accessibility + broken link audits,
    and returns a tuple of (filepath, html_content, extra_audit_data).
    
    extra_audit_data contains:
        - accessibility_violations: list of axe-core violations
        - broken_links: list of broken URLs found on the page
        - perf_timing: dict with real browser timing metrics
    
    Returns (None, None, None) on failure.
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
                
                page = await context.new_page()
                
                # Navigate and wait for page load (increased timeout for better accuracy)
                await page.goto(url, timeout=60000, wait_until="load")
                
                # --- 1. Take screenshot ---
                screenshot_bytes = await page.screenshot(full_page=False)
                
                # --- 2. Grab fully rendered HTML ---
                html_content = await page.content()
                
                # --- 3. Capture real performance timing from the browser ---
                perf_timing = await _get_performance_timing(page)
                
                # --- 4. Run axe-core accessibility audit ---
                accessibility_violations, visual_flaw = await _run_axe_audit(page)
                
                # --- 5. Check for broken links/images ---
                broken_links = await _check_broken_assets(page, context)
                
                await browser.close()
            
        # Draw analysis box on the image
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        
        visual_flaw_context = ""
        if visual_flaw:
            draw = ImageDraw.Draw(img)
            box = visual_flaw["box"]
            # Pad the box slightly for better visibility
            pad = 5
            padded_box = [max(0, box[0]-pad), max(0, box[1]-pad), box[2]+pad, box[3]+pad]
            draw.rectangle(padded_box, outline="red", width=4)
            visual_flaw_context = f"The red box in the screenshot highlights an accessibility flaw: {visual_flaw['description']}."
        
        filepath = os.path.join(SCREENSHOTS_DIR, make_screenshot_filename(company_name, url))
        img.save(filepath, format="JPEG", quality=85)
        
        extra_audit_data = {
            "accessibility_violations": accessibility_violations,
            "broken_links": broken_links,
            "perf_timing": perf_timing,
            "visual_flaw_context": visual_flaw_context
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
        
        # Interconnect: Find a visible node to draw a real red box around
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
                        
                    # Get exact coordinates of the flawed element
                    box = await page.locator(selector).first.bounding_box(timeout=1000)
                    if box and box["width"] > 0 and box["height"] > 0:
                        # Skip if the element covers > 90% of the screen (e.g. wrapper divs, html, body)
                        if box["width"] > 1150 and box["height"] > 720:
                            continue
                            
                        visual_flaw = {
                            "box": [box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]],
                            "description": v["help"]
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


async def _check_broken_assets(page, context) -> list:
    """Check for broken links and images on the page."""
    broken = []
    try:
        # Extract all links and images
        assets = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({type: 'link', url: a.href, text: a.textContent.trim().substring(0, 50)}))
                .filter(l => l.url.startsWith('http'));
            const images = Array.from(document.querySelectorAll('img[src]'))
                .map(img => ({type: 'image', url: img.src, text: img.alt || 'no alt text'}))
                .filter(i => i.url.startsWith('http'));
            return [...links.slice(0, 15), ...images.slice(0, 10)];
        }""")
        
        # Check each asset with a HEAD request (fast, no body download)
        for asset in assets:
            try:
                response = await context.request.head(asset["url"], timeout=10000)
                status = response.status
                if status >= 400:
                    broken.append({
                        "type": asset["type"],
                        "url": asset["url"],
                        "text": asset["text"],
                        "status": status
                    })
            except Exception:
                # Timeout or unreachable = broken
                broken.append({
                    "type": asset["type"],
                    "url": asset["url"],
                    "text": asset["text"],
                    "status": "unreachable"
                })
        
        print(f"[Links] Checked {len(assets)} assets, found {len(broken)} broken.")
        
    except Exception as e:
        print(f"[Links] Broken asset check failed (non-critical): {e}")
    
    return broken[:10]  # Cap at 10

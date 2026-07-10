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
                    viewport={'width': 390, 'height': 844},
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
                    is_mobile=True,
                    has_touch=True
                )
                
                page = await context.new_page()
                
                # Navigate and wait for network idle
                await page.goto(url, timeout=20000, wait_until="networkidle")
                
                # --- 1. Take screenshot ---
                screenshot_bytes = await page.screenshot(full_page=False)
                
                # --- 2. Grab fully rendered HTML ---
                html_content = await page.content()
                
                # --- 3. Capture real performance timing from the browser ---
                perf_timing = await _get_performance_timing(page)
                
                # --- 4. Run axe-core accessibility audit ---
                accessibility_violations = await _run_axe_audit(page)
                
                # --- 5. Check for broken links/images ---
                broken_links = await _check_broken_assets(page, context)
                
                await browser.close()
            
        # Draw analysis box on the image
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        width, height = img.size
        box = [20, 100, width - 20, 300]
        draw.rectangle(box, outline="red", width=5)
        
        safe_name = "".join([c if c.isalnum() else "_" for c in company_name.lower()])
        filepath = os.path.join(SCREENSHOTS_DIR, f"{safe_name}_audit.jpg")
        img.save(filepath, format="JPEG", quality=85)
        
        extra_audit_data = {
            "accessibility_violations": accessibility_violations,
            "broken_links": broken_links,
            "perf_timing": perf_timing,
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


async def _run_axe_audit(page) -> list:
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
                "nodes_count": len(v.get("nodes", [])),
            })
        
        # Sort by severity: critical > serious > moderate > minor
        severity_order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
        violations.sort(key=lambda x: severity_order.get(x["impact"], 4))
        
        print(f"[Axe] Found {len(violations)} accessibility violations.")
        return violations[:10]  # Cap at 10 most severe
        
    except Exception as e:
        print(f"[Axe] Accessibility audit failed (non-critical): {e}")
        return []


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
                response = await context.request.head(asset["url"], timeout=5000)
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

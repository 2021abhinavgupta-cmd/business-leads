"""
Gathers what the PDF report is built from: one clean look at a home page.

Separate from the audit pipeline on purpose. The audit's screenshots carry a
marker box for the cold email, and its heavy tools (Lighthouse, pa11y, an AI
draft) take minutes. A report needs clean first-screen pictures, the
visitor-view and designer-view measurements, and the search-visibility probes,
which is a short browser visit. It reuses the audit's guarded helpers, so the
same protections apply: the browser may not be pointed at a private network
(SSRF guard), captures are deterministic, and one browser at a time.

Everything returned is a measurement or a picture; judgement happens nowhere.
"""
import asyncio
import io

import httpx
from PIL import Image

from analyzer import design_review, seo_probes
from analyzer import visuals

DESKTOP = {"width": 1440, "height": 900}
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SETTLE_MS = 6000
_MOBILE_SETTLE_MS = 4000


def _jpeg(png_or_jpeg: bytes, quality: int = 82) -> bytes:
    out = io.BytesIO()
    Image.open(io.BytesIO(png_or_jpeg)).convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


async def _settle(page, ms: int) -> None:
    """Let the page finish what a visitor would wait for, without ever failing."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        await page.evaluate("() => document.fonts && document.fonts.ready")
    except Exception:
        pass
    await page.wait_for_timeout(ms)
    try:
        await visuals._suppress_overlays(page)
    except Exception:
        pass


async def _shot(page) -> bytes | None:
    try:
        return _jpeg(await visuals._capture(page))
    except Exception as e:
        print(f"[Report] screenshot failed (non-critical): {e}")
        return None


async def collect(website: str) -> dict:
    """
    Visit `website` and return the raw material for a report. Raises only when
    the page cannot be loaded at all; every individual measurement that fails
    simply comes back empty.
    """
    out: dict = {
        "final_url": website, "html": "", "response_headers": {},
        "desktop_image": None, "mobile_image": None, "card_image": None, "card_label": "",
        "presentation_findings": [], "design_probe": None, "real_web_vitals": {},
        "mobile_overflow": None, "mobile_scroll_width": None, "text_renderable": True,
        "seo_findings": [],
    }
    sem = await visuals._acquire_playwright_slot()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                # --- desktop ---
                ctx = await browser.new_context(
                    viewport=DESKTOP, user_agent=_DESKTOP_UA, service_workers="block")
                blocked: list[str] = []
                await ctx.route("**/*", visuals._make_ssrf_route_handler(blocked))
                hops: list = []
                ctx.on("request", visuals._make_redirect_hop_recorder(hops))
                await ctx.add_init_script(visuals._web_vitals_init_script())
                page = await ctx.new_page()
                response = await visuals._guarded_goto(
                    page, website, blocked, wait_until="domcontentloaded", timeout=90000)
                await asyncio.to_thread(visuals._assert_redirect_hops_public, hops)
                await _settle(page, _SETTLE_MS)

                out["final_url"] = page.url
                out["html"] = await page.content()
                try:
                    out["response_headers"] = dict(response.headers) if response else {}
                except Exception:
                    pass
                out["text_renderable"] = await visuals._can_render_text(page)
                out["desktop_image"] = await _shot(page)
                out["real_web_vitals"] = await visuals._get_real_web_vitals(page)

                out["presentation_findings"] = await visuals._probe_presentation(page)
                out["design_probe"] = await design_review.probe_design(page)

                shots = await visuals._capture_presentation_shots(page, out["presentation_findings"])
                if shots:
                    flaw = shots[0].get("visual_flaw") or {}
                    out["card_image"] = _jpeg(shots[0]["screenshot_bytes"]) if shots[0].get("screenshot_bytes") else None
                    out["card_label"] = flaw.get("description", "")
                await ctx.close()

                # --- phone ---
                mctx = await browser.new_context(**visuals._MOBILE_DEVICE, service_workers="block")
                mblocked: list[str] = []
                await mctx.route("**/*", visuals._make_ssrf_route_handler(mblocked))
                mpage = await mctx.new_page()
                try:
                    await visuals._guarded_goto(
                        mpage, website, mblocked, wait_until="domcontentloaded", timeout=90000)
                    await _settle(mpage, _MOBILE_SETTLE_MS)
                    out["mobile_image"] = await _shot(mpage)
                    dims = await mpage.evaluate(
                        "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
                    out["mobile_scroll_width"] = int(dims["sw"])
                    out["mobile_overflow"] = dims["sw"] > dims["iw"] + 2
                except Exception as e:
                    print(f"[Report] mobile pass failed (non-critical): {e}")
                await mctx.close()
            finally:
                await browser.close()
    finally:
        visuals._release_playwright_slot(sem)

    # Search-visibility probes need no browser.
    try:
        html, final = out["html"], out["final_url"]
        found = seo_probes.html_findings(html, final) + seo_probes.header_findings(out["response_headers"])
        async with httpx.AsyncClient(
            headers={"User-Agent": _DESKTOP_UA}, follow_redirects=True, timeout=15
        ) as client:
            found += await seo_probes.probe_findings(client, website, final, html)
        out["seo_findings"] = found
    except Exception as e:
        print(f"[Report] search probes skipped (non-critical): {e}")
    return out

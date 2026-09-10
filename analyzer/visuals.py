import base64
import hashlib
import json
import os
import re
import asyncio
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from PIL import Image

import config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "data", "screenshots")

# Create temporary directory for screenshots
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Global Semaphore to limit Playwright concurrency to 1.
# This prevents Out of Memory (OOM) crashes on Railway's 500MB instances.
# Every Playwright caller (audits here + the Maps scraper fallback in
# scrapers/google_maps.py) MUST go through _acquire_playwright_slot /
# _release_playwright_slot rather than `async with` this object directly,
# so the wedge-recovery below actually protects them.
_PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)

# Who currently holds the slot, and since when. Used purely to notice when
# the semaphore has gone into a ghost-locked state and heal it — see
# _acquire_playwright_slot.
_playwright_holder: "asyncio.Task | None" = None
_playwright_acquired_at = 0.0

# Longer than the worst realistic Playwright run: page.goto alone caps at
# 120s, and on top of that one run does the extra-page crawl, the mobile
# revisit and a Lighthouse subprocess. A slot held longer than this, or
# held by a task that has already finished, is a leak, not a real run.
_PLAYWRIGHT_MAX_HOLD = 360


async def _acquire_playwright_slot(on_queued=None, on_started=None):
    """
    Acquire the single global Playwright slot, healing it first if it has
    wedged.

    Background: a bare asyncio.Semaphore(1) can be left permanently
    "locked" with no holder if a task that is *waiting* to acquire it is
    cancelled — a CPython asyncio cancel-race that bites on the 3.11 build
    Railway runs. Both the /api/audit orphan-cancel and the operator's
    Cancel button cancel exactly such waiting tasks, and once the
    semaphore wedged every later audit/search hung forever at "Queued".

    Recovery is twofold:
      1. Ghost-lock check up front — if the semaphore says busy but the
         recorded holder task is gone / already done, or it has been held
         past _PLAYWRIGHT_MAX_HOLD, rebuild the semaphore and take the
         fresh one immediately (no waiting behind nobody).
      2. Hard ceiling on the wait — if a genuine holder somehow never
         releases, wait_for eventually fires, and we rebuild rather than
         block the caller indefinitely.

    Returns the semaphore object actually acquired; pass THAT same object
    back to _release_playwright_slot (the module global may be rebuilt
    again by a later caller).
    """
    global _PLAYWRIGHT_SEMAPHORE, _playwright_holder, _playwright_acquired_at

    if _PLAYWRIGHT_SEMAPHORE.locked():
        holder = _playwright_holder
        held_for = time.monotonic() - _playwright_acquired_at
        if holder is None or holder.done() or held_for > _PLAYWRIGHT_MAX_HOLD:
            state = "gone" if holder is None else ("done" if holder.done() else "alive")
            print(
                f"[Playwright] slot looked wedged (holder={state}, held "
                f"{held_for:.0f}s) — resetting so this run isn't stuck behind a ghost"
            )
            _PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)
            _playwright_holder = None
            _playwright_acquired_at = 0.0

    if _PLAYWRIGHT_SEMAPHORE.locked() and on_queued is not None:
        on_queued()

    sem = _PLAYWRIGHT_SEMAPHORE
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_PLAYWRIGHT_MAX_HOLD * 1.2)
    except asyncio.TimeoutError:
        print("[Playwright] slot never came free within the ceiling — forcing a fresh one")
        _PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)
        sem = _PLAYWRIGHT_SEMAPHORE
        await sem.acquire()

    _playwright_holder = asyncio.current_task()
    _playwright_acquired_at = time.monotonic()
    if on_started is not None:
        on_started()
    return sem


def _release_playwright_slot(sem) -> None:
    """Release a slot taken via _acquire_playwright_slot. Safe to call even
    if the global semaphore was rebuilt in the meantime (this sem is then
    just an orphan nobody waits on) or if it was already released."""
    global _playwright_holder, _playwright_acquired_at
    _playwright_holder = None
    _playwright_acquired_at = 0.0
    try:
        sem.release()
    except (ValueError, RuntimeError):
        pass

# Beyond the homepage, also crawl up to this many internal about/services/
# contact-style pages so accessibility, broken-link, and visual checks
# aren't limited to just the homepage. Kept small (each page adds a full
# Playwright navigation + axe-core run) to stay within Railway's 500MB
# instance budget alongside the single-browser semaphore above.
_EXTRA_PAGE_KEYWORDS = ['about', 'service', 'pricing', 'contact', 'product', 'work', 'solution']
_MAX_EXTRA_PAGES = 2
_SEVERITY_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

# Real mobile emulation for the phone-viewport pass.
#
# This used to be `set_viewport_size({390, 844})` called on the DESKTOP context
# AFTER navigating, which is not a phone — it is a narrow desktop window. The
# context kept its desktop Chrome user agent, is_mobile/has_touch stayed False
# and device_scale_factor stayed 1, so any site that branches on the user agent
# served its desktop build and Chromium's mobile viewport-meta behaviour never
# engaged at all. The screenshot was then attached to an email describing it as
# the site "on mobile", and the model was asked to make mobile-specific claims
# from it. A device descriptor has to set all five properties together for the
# render to be genuinely mobile.
#
# The user agent is Android Chrome, NOT iOS Safari, deliberately: we drive
# Chromium, and Playwright's own iPhone descriptors declare
# defaultBrowserType "webkit". Serving a site an iOS Safari UA while rendering
# in Chromium invites it to send WebKit-specific CSS/JS to an engine that
# handles it differently, which would be a new way for the screenshot to
# disagree with what a real visitor sees. Engine and UA agree here.
#
# 390x844 keeps the CSS viewport this pipeline has always used (and that
# CLAUDE.md documents). device_scale_factor 2 renders text at retina sharpness
# without the ~1080px-wide image a real Pixel's 2.75 would produce, since this
# capture is emailed as an attachment.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}
_MOBILE_DEVICE = {
    "viewport": _MOBILE_VIEWPORT,
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
}

# Consent banners, chat bubbles and newsletter popups are not the prospect's
# design work, but they sit on top of it — and a capture taken with one up
# hands the vision model a picture whose most prominent element belongs to a
# third party. Hidden before axe-core runs rather than only before the
# screenshot, so the violations we report and the image we attach describe the
# same page; a violation inside a widget we then hid would be a finding the
# recipient cannot see and did not author.
#
# Matched on id/class substrings and common vendor attributes rather than
# heuristics about position, because a "fixed element near the bottom of the
# viewport" is just as often the site's own sticky call-to-action, which IS
# their design and must stay.
_OVERLAY_SUPPRESSION_SELECTORS = (
    "#onetrust-consent-sdk", "#onetrust-banner-sdk", ".onetrust-pc-dark-filter",
    "#CybotCookiebotDialog", "#cookiescript_injected", "#cookie-law-info-bar",
    "#cmplz-cookiebanner-container", ".cc-window", ".cookie-notice-container",
    "#moove_gdpr_cookie_info_bar", "#gdpr-cookie-message", "#hs-eu-cookie-confirmation",
    "[id*='cookie-banner']", "[class*='cookie-banner']", "[class*='cookie-consent']",
    "[aria-label*='cookie' i]", "[aria-label*='consent' i]",
    "#tidio-chat", "#intercom-container", "#hubspot-messages-iframe-container",
    "#drift-widget", "#crisp-chatbox", ".zsiq_floatmain", "#launcher",
    "[id*='livechat']", "[class*='whatsapp-float']",
    # Expanded 2026-09-08 — the original list covered the vendors verified
    # against real leads at the time it was written, but was never claimed
    # to be exhaustive (CLAUDE.md §8 flags "will miss consent vendors nobody
    # has hit yet" as a known, open gap). These are other real, widely
    # deployed CMPs (consent management platforms) and chat widgets not
    # already covered above — added proactively rather than one at a time
    # as each is hit live.
    "#usercentrics-root", "[id^='usercentrics']", "#uc-banner",
    "#truste-consent-track", ".truste_box_overlay", "#trustarc-banner-container",
    "#consent_blackbar", ".qc-cmp2-container", "#qc-cmp2-container",
    "[id^='sp_message_container']", "#iubenda-cs-banner", ".iubenda-cs-container",
    "#termly-code-snippet-support", "#BorlabsCookieBox", ".BorlabsCookie",
    ".osano-cm-window", "#osano-cm-widget", "#didomi-host", ".didomi-popup-container",
    ".klaro", "#klaro", ".fc-consent-root", ".fc-dialog-container", "#fc-cta-consent",
    "#ccc-notify", "#ccc", "#coiPage-1", "#CookieConsent", "#cookiefirst-root",
    "#ketch-consent-banner", "[class*='gdpr' i]", "[id*='gdpr' i]",
    "[class*='consent-banner' i]", "[id*='consent-banner' i]",
    "#freshworks-container", "#olark-wrapper", "#tawkchat-container",
    "iframe[id^='tawkchat']", "#chatra", "#chat-widget-container",
)

# A highlight that fills the screenshot reads as "the whole page is wrong"
# rather than pointing at anything, and one that barely covers a few pixels is
# invisible once the image is scaled down inside a mail client.
#
# Measured as a fraction of viewport AREA, not as width-and-height each being
# near-full. The old check was an AND on both dimensions, which let a hero
# wrapper that was tall but a little narrower than the viewport through — a
# real, live-reproduced miss (CLAUDE.md §8, lp.zooty.in). Area catches that
# case and the tall-narrow one the dimension check never could, in one number.
_MAX_HIGHLIGHT_AREA_FRACTION = 0.5
_MIN_HIGHLIGHT_PX = 12

# Marker id for the injected highlight, so it can be removed again and so a
# stray one from a previous page can never be screenshotted twice.
_HIGHLIGHT_OVERLAY_ID = "__lead_audit_highlight__"
_HIGHLIGHT_LABEL_ID = _HIGHLIGHT_OVERLAY_ID + "_label"

# Reported live (2026-09-08): a recipient mistook a site's own native red
# "urgent" icon near a Contact Us button for the marker this pipeline draws,
# because red is exactly the color a real site's own UI already reaches for
# (error states, alert badges, "sale" ribbons). A marker that can be confused
# with real content is not doing its job, no matter how correctly it is
# positioned. Magenta essentially never appears in ordinary site design, and
# Set-of-Mark visual-grounding research (arXiv:2310.11441; see also the
# WebMarker project, github.com/reidbarber/webmarker) finds a bare colored box
# is still ambiguous on its own — pairing it with a short text label is what
# actually removes the ambiguity, for a human reader and for the vision judge
# alike. See test_red_box_viewport.py (name kept, now reads magenta pixels).
_HIGHLIGHT_COLOR = "#ff00ff"
_HIGHLIGHT_LABEL_TEXT = "MMGA FLAGGED THIS"


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


def make_mobile_screenshot_filename(company_name: str, url: str) -> str:
    """
    Filename of the MOBILE capture for the same lead.

    Both this and the desktop name are now needed outside this module (the
    send path attaches both images), so the "_audit.jpg" -> "_mobile.jpg"
    convention lives here once rather than being re-derived with a string
    replace at each call site — two copies of a filename rule is how one of
    them ends up looking for a file the other never wrote.
    """
    return make_screenshot_filename(company_name, url).replace("_audit.jpg", "_mobile.jpg")


def make_closeup_screenshot_filename(company_name: str, url: str) -> str:
    """
    Filename of the CLOSE UP crop of the highlighted element, when one was
    captured. Same "_audit.jpg" -> "_closeup.jpg" convention as the mobile
    name above, for the same reason: the send path re-derives this filename
    from scratch rather than being handed a path, so a second definition of
    the suffix is how it ends up looking for a file this module never wrote.
    """
    return make_screenshot_filename(company_name, url).replace("_audit.jpg", "_closeup.jpg")


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


async def _suppress_overlays(page) -> None:
    """
    Hide consent banners, chat bubbles and newsletter popups.

    Run BEFORE axe-core, not just before the screenshot, so the violations we
    report and the image we attach describe the same page. A violation raised
    inside a widget we then hid would be a finding the recipient cannot see in
    the attachment and did not author in the first place.

    Best-effort and never fatal: a site with none of these selectors is the
    normal case, and failing to hide a banner is much less bad than failing
    the audit.
    """
    try:
        await page.add_style_tag(
            content=", ".join(_OVERLAY_SUPPRESSION_SELECTORS)
            + " { display: none !important; }"
        )
    except Exception as e:
        print(f"[Visuals] Overlay suppression skipped (non-critical): {e}")


async def _highlight_element(page, candidates: list[dict]) -> dict | None:
    """
    Draw the red highlight IN THE BROWSER, on the first candidate that is
    genuinely visible and well-sized, and return what was highlighted.

    This replaces measuring a bounding box in Python and then drawing a
    rectangle onto the image bytes with Pillow. That older approach captured
    the screenshot at one moment and measured the element several seconds
    later — after page.content(), the performance-timing reads and axe-core's
    own multi-second scan — and Playwright's own documentation is explicit
    that bounding-box coordinates are viewport-relative and only safe to reuse
    "assuming the page is static". These pages are not static: a rotating
    carousel, a banner that finishes animating in, or lazy content reflowing
    above the element all move it without moving the already-captured image,
    and the box then lands on whatever happens to occupy those coordinates.
    Nothing downstream could detect that, because a misplaced box looks
    exactly like a correctly placed one.

    Injecting an absolutely-positioned outline into the DOM removes the
    failure mode rather than guarding against it: the measurement and the draw
    happen in the same synchronous JavaScript execution, so no layout change
    can occur between them, and the browser then renders the outline into the
    screenshot itself. The box cannot be misaligned with the image because the
    same engine produced both, at the same instant.

    Returns a dict describing what was actually boxed, or None if no candidate
    qualified — in which case no box is drawn and no red-box claim is made.
    """
    if not candidates:
        return None

    try:
        return await page.evaluate(
            """(payload) => {
                const { candidates, maxArea, minPx, overlayId, labelId, color, labelText } = payload;
                const vw = window.innerWidth;
                const vh = window.innerHeight;

                // Never stack two highlights, e.g. if a previous page's
                // overlay somehow survived a navigation.
                const stale = document.getElementById(overlayId);
                if (stale) stale.remove();
                const staleLabel = document.getElementById(labelId);
                if (staleLabel) staleLabel.remove();

                for (const candidate of candidates) {
                    let el = null;
                    try {
                        el = document.querySelector(candidate.selector);
                    } catch (e) {
                        continue;  // axe can emit selectors querySelector rejects
                    }
                    if (!el) continue;

                    const rect = el.getBoundingClientRect();
                    if (!rect) continue;

                    // Too small to see once a mail client scales the image down.
                    if (rect.width < minPx || rect.height < minPx) continue;

                    // Must sit entirely inside the captured viewport. An
                    // element below the fold returns a perfectly valid box
                    // whose coordinates are simply not in the picture.
                    if (rect.left < 0 || rect.top < 0) continue;
                    if (rect.right > vw || rect.bottom > vh) continue;

                    // Covering most of the frame highlights nothing in particular.
                    if ((rect.width * rect.height) > (vw * vh * maxArea)) continue;

                    // Present in the layout but not actually drawn.
                    const style = getComputedStyle(el);
                    if (style.visibility === 'hidden') continue;
                    if (style.display === 'none') continue;
                    if (parseFloat(style.opacity) === 0) continue;

                    const box = document.createElement('div');
                    box.id = overlayId;
                    // position:fixed with viewport coordinates matches a
                    // full_page=False screenshot exactly. Appended to
                    // documentElement so no ancestor transform/filter can
                    // reparent the fixed positioning context.
                    box.style.cssText = [
                        'position:fixed',
                        'left:' + (rect.left - 4) + 'px',
                        'top:' + (rect.top - 4) + 'px',
                        'width:' + (rect.width + 8) + 'px',
                        'height:' + (rect.height + 8) + 'px',
                        'border:4px solid ' + color,
                        'border-radius:3px',
                        'box-sizing:border-box',
                        'background:transparent',
                        'pointer-events:none',
                        'z-index:2147483647'
                    ].join(';');
                    document.documentElement.appendChild(box);

                    // A labeled tag next to the box, not just a colored
                    // outline on its own — a bare box can still be mistaken
                    // for the site's own design (see the comment on
                    // _HIGHLIGHT_COLOR). Placed above the box when there is
                    // room, below it otherwise, so it never renders off the
                    // top of the captured viewport.
                    const label = document.createElement('div');
                    label.id = labelId;
                    const labelHeight = 22;
                    const spaceAbove = (rect.top - 4) >= labelHeight;
                    const labelTop = spaceAbove ? (rect.top - 4 - labelHeight) : (rect.bottom + 4);
                    // Solid black with white text — deliberately NEITHER
                    // uses the marker color, so the pixel tests (which scan
                    // for that color specifically to find the box) see the
                    // box and only the box. A filled tag painted in the same
                    // color as the outline would be indistinguishable from
                    // it by color and would throw off every "exactly where
                    // is the box" assertion, since the label sits outside
                    // the box's own bounding rectangle.
                    label.textContent = labelText;
                    label.style.cssText = [
                        'position:fixed',
                        'left:' + Math.max(0, rect.left - 4) + 'px',
                        'top:' + labelTop + 'px',
                        'background:#111111',
                        'color:#ffffff',
                        'font:bold 12px Arial, sans-serif',
                        'padding:2px 8px',
                        'border-radius:2px',
                        'pointer-events:none',
                        'z-index:2147483647',
                        'white-space:nowrap'
                    ].join(';');
                    document.documentElement.appendChild(label);

                    return {
                        selector: candidate.selector,
                        description: candidate.description,
                        impact: candidate.impact,
                        x: rect.left,
                        y: rect.top,
                        width: rect.width,
                        height: rect.height
                    };
                }
                return null;
            }""",
            {
                "candidates": candidates,
                "maxArea": _MAX_HIGHLIGHT_AREA_FRACTION,
                "minPx": _MIN_HIGHLIGHT_PX,
                "overlayId": _HIGHLIGHT_OVERLAY_ID,
                "labelId": _HIGHLIGHT_LABEL_ID,
                "color": _HIGHLIGHT_COLOR,
                "labelText": _HIGHLIGHT_LABEL_TEXT,
            },
        )
    except Exception as e:
        print(f"[Visuals] Could not draw the highlight (non-critical): {e}")
        return None


async def _remove_highlight(page) -> None:
    """Take the highlight (box and label) back off, so later checks measure the real page."""
    try:
        await page.evaluate(
            """(ids) => {
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
            }""",
            [_HIGHLIGHT_OVERLAY_ID, _HIGHLIGHT_LABEL_ID],
        )
    except Exception:
        pass


# How far the close up crop extends beyond the marked element's own edges,
# and the smallest a crop is ever allowed to be. Both matter for the same
# reason: this pipeline's own _MIN_HIGHLIGHT_PX floor allows highlighting an
# element as small as 12x12 (a checkbox, an icon-only button) — a bare crop
# of exactly that element would be a 12x12 pixel image, technically correct
# and practically useless once attached to an email. Padding the crop out
# keeps enough surrounding page (label text, sibling controls, the section
# it sits in) for it to read as "this spot on your actual page" rather than
# an abstract colored rectangle floating in white space.
_CLOSEUP_PADDING_PX = 40
_CLOSEUP_MIN_SIZE_PX = 240


async def _capture_closeup(page, rect: dict) -> bytes | None:
    """
    A generously padded crop centered on the marked element, taken while the
    highlight is still on it — so the crop itself shows the marked border in
    context, not a bare, possibly tiny sliver of content.

    Takes the rect straight from the highlight result (_highlight_element's
    return value) rather than re-querying the element by selector. Two
    reasons: this crop can then never disagree with where the box itself was
    actually drawn, since both come from the same measurement; and it
    sidesteps re-locating an element that could, in principle, have gone
    stale between the highlight call and this one (inside an iframe
    _run_axe_audit's candidates don't reach, for instance).

    Uses page.screenshot(clip=...) rather than an element handle's own
    .screenshot() specifically to get the padding: an element screenshot
    crops to EXACTLY that element's bounding box with no way to widen it.

    Best-effort and non-fatal: degrades to None rather than failing the
    audit — the full-page highlighted screenshot is still there either way.
    """
    try:
        viewport = page.viewport_size
        if not viewport:
            return None
        vw, vh = viewport["width"], viewport["height"]

        crop_w = min(max(rect["width"] + _CLOSEUP_PADDING_PX * 2, _CLOSEUP_MIN_SIZE_PX), vw)
        crop_h = min(max(rect["height"] + _CLOSEUP_PADDING_PX * 2, _CLOSEUP_MIN_SIZE_PX), vh)

        # Centered on the element, then clamped so the crop stays fully
        # inside the captured viewport rather than being cut off at an edge
        # (or, worse, clipped against page content that was never captured
        # at all outside the viewport bounds).
        cx = rect["x"] + rect["width"] / 2
        cy = rect["y"] + rect["height"] / 2
        x = max(0, min(cx - crop_w / 2, vw - crop_w))
        y = max(0, min(cy - crop_h / 2, vh - crop_h))

        return await page.screenshot(
            clip={"x": x, "y": y, "width": crop_w, "height": crop_h},
            animations="disabled", caret="hide",
        )
    except Exception as e:
        print(f"[Visuals] Could not capture close up crop (non-critical): {e}")
        return None


async def _capture(page) -> bytes:
    """
    Screenshot the viewport with the two options that make a capture
    reproducible.

    animations="disabled" is Playwright's own guarantee here: finite CSS
    animations, CSS transitions and Web Animations are fast-forwarded to
    completion (so an entrance fade lands at its final state instead of
    whatever opacity it happened to be at), and infinite ones are cancelled to
    their initial state and resumed afterwards. That is exactly the problem
    the manual scroll pass in the main flow was approximating by hand, and it
    covers the cases scrolling cannot: a hero that cross-fades on a timer, a
    counter mid-count, a slider between slides.

    caret="hide" removes a blinking text cursor, which otherwise lands in the
    capture on any site that autofocuses a search or newsletter field.
    """
    return await page.screenshot(full_page=False, animations="disabled", caret="hide")


async def _audit_current_page(page, context, label: str) -> dict:
    """
    Run every per-page check (accessibility, broken links, fonts, stretched
    images, title/meta-description) against whatever page is currently
    loaded in *page*, and capture this page's screenshot. Violations and
    broken links get tagged with *label* when it's not the homepage, so the
    AI prompt (and the human reading the email) knows which page a flaw came
    from.

    The screenshot is taken HERE, immediately after the highlight is injected,
    rather than being captured by the caller and passed in. The old signature
    took `screenshot_bytes` from a capture made before axe-core ran, which is
    what allowed the image and the red box to describe different moments in
    the page's life — see _highlight_element.
    """
    violations, candidates, borderline_candidates = await _run_axe_audit(page)
    # Stretched-image candidates run before the highlight so a blurry photo
    # can compete for the box alongside axe's visually-apparent violations —
    # appended after axe's, so a real accessibility issue still wins ties.
    stretched_images, stretched_candidates = await _check_stretched_images(page)
    candidates = candidates + stretched_candidates

    # Highlight, capture (full page, then a tight crop of the same element
    # while it's still marked), un-highlight — in that order and with nothing
    # in between, so both images show the element axe-core actually objected
    # to, and the checks below still measure the unmodified page.
    visual_flaw = await _highlight_element(page, candidates)
    closeup_bytes = None

    # The safe allowlist produced nothing at all for this page — today's
    # existing outcome here is simply no box. Before settling for that,
    # give one borderline (real axe rule, real element, just not on the
    # hardcoded "definitely visible" list) a chance: highlight it with the
    # exact same measure-and-draw machinery above (so it's still subject to
    # every viewport/size/visibility guard _highlight_element already
    # enforces), then ask a vision model one narrow yes/no question about
    # the resulting crop. A "no", an error, or no vision provider configured
    # all fall through to the untouched original behaviour — this can only
    # ever ADD a vision-confirmed box where there was none, never replace or
    # downgrade an already-qualifying safe candidate. See
    # _vision_confirms_visible_defect's docstring for why this doesn't
    # reopen the free-form-vision-critique hallucination risk this file's
    # own history (CLAUDE.md, 2026-08-31) already fixed once.
    if visual_flaw is None and borderline_candidates:
        candidate_flaw = await _highlight_element(page, borderline_candidates)
        if candidate_flaw:
            candidate_crop = await _capture_closeup(page, candidate_flaw)
            confirmed = False
            if candidate_crop:
                confirmed = await asyncio.to_thread(
                    _vision_confirms_visible_defect, candidate_crop, candidate_flaw["description"]
                )
            if confirmed:
                visual_flaw = candidate_flaw
                closeup_bytes = candidate_crop  # already the right crop — page hasn't changed since
            else:
                await _remove_highlight(page)

    screenshot_bytes = await _capture(page)
    if visual_flaw and closeup_bytes is None:
        closeup_bytes = await _capture_closeup(page, visual_flaw)
    await _remove_highlight(page)

    broken_links, links_checked = await _check_broken_assets(page, context)
    font_families = await _check_font_consistency(page)

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
        "closeup_bytes": closeup_bytes,
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


async def generate_audit_screenshot(
    url: str,
    company_name: str,
    on_queued: "Callable[[], None] | None" = None,
    on_started: "Callable[[], None] | None" = None,
) -> tuple[str | None, str | None, dict | None]:
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
        last_result = await _generate_audit_screenshot_once(url, company_name, on_queued=on_queued, on_started=on_started)
        if last_result[1] is not None:  # html_content present = success
            return last_result
        if attempt < _AUDIT_SCREENSHOT_RETRIES - 1:
            print(f"[Visuals] Audit attempt {attempt + 1} failed for {url} — retrying (likely transient network/bot-detection blip, not a dead site)...")
            await asyncio.sleep(5)
    return last_result


async def _generate_audit_screenshot_once(
    url: str,
    company_name: str,
    on_queued: "Callable[[], None] | None" = None,
    on_started: "Callable[[], None] | None" = None,
) -> tuple[str | None, str | None, dict | None]:
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
        - closeup_image_path: tight crop of just the marked element on the desktop capture, or None if no box was drawn
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
        # Only one audit/search can hold this semaphore at a time (see its
        # own comment above) — a second caller arriving while it's held
        # doesn't fail or hang mysteriously, it just queues silently behind
        # whichever one got there first. That queueing used to be invisible
        # to the operator: two leads both showed "Loading site & capturing
        # screenshots" at once, one of them actually stuck doing nothing.
        # `on_queued` (optional, only app.py's dashboard path passes it) lets
        # the caller report an honest "waiting for another audit" state for
        # as long as the wait actually lasts, rather than a progress step
        # that silently means two different things depending on luck.
        _sem = await _acquire_playwright_slot(on_queued=on_queued, on_started=on_started)
        try:
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
                # requests across the homepage and every extra page crawled
                # below. The mobile pass now runs in its own context (real
                # device emulation can only be set at context creation), so it
                # re-attaches these two listeners itself rather than inheriting
                # them — see the mobile block further down.
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

                # --- 1. Grab fully rendered HTML ---
                html_content = await page.content()

                # --- 2. Capture real performance timing from the browser ---
                perf_timing = await _get_performance_timing(page)
                real_web_vitals = await _get_real_web_vitals(page)

                # --- 3. Hide third-party consent/chat overlays ---
                # Before axe-core runs, so the violations reported and the
                # image attached describe the same page. A cookie wall is also
                # the single most common reason the attached screenshot showed
                # a grey scrim instead of the prospect's homepage.
                await _suppress_overlays(page)

                # --- 4. Run every per-page check on the homepage, INCLUDING
                # its screenshot. The capture happens inside this call, right
                # after the highlight is injected — it is no longer taken up
                # here and passed down, because that gap is what let the box
                # and the image describe different moments.
                pages_checked = [await _audit_current_page(page, context, "/")]

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
                        await _suppress_overlays(page)
                        pages_checked.append(await _audit_current_page(page, context, label))
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
                mobile_visual_flaw = None
                mobile_context = None
                try:
                    # A SEPARATE context carrying a real device descriptor.
                    # Resizing the desktop context's viewport (what this used
                    # to do) keeps the desktop user agent, is_mobile=False,
                    # has_touch=False and device_scale_factor=1 — a narrow
                    # desktop window, not a phone. Sites that branch on the
                    # user agent kept serving their desktop build into an
                    # image the email then described as "on mobile". The
                    # descriptor has to be applied at context creation, so
                    # this cannot be fixed by resizing after navigation.
                    mobile_context = await browser.new_context(**_MOBILE_DEVICE)
                    mobile_page = await mobile_context.new_page()

                    # Same listeners as the desktop page — the previous
                    # implementation reused one page, so console errors and
                    # mixed-content requests seen only at mobile width were
                    # captured for free. Re-attached here so moving to a
                    # separate context doesn't quietly lose that coverage.
                    mobile_page.on(
                        "console",
                        lambda msg: console_errors.append(msg.text)
                        if msg.type == "error" and len(console_errors) < 10 else None,
                    )

                    def _track_mobile_mixed_content(request):
                        try:
                            if request.url.startswith("http://") and mobile_page.url.startswith("https://"):
                                mixed_content_urls.add(request.url)
                        except Exception:
                            pass
                    mobile_page.on("request", _track_mobile_mixed_content)

                    await mobile_page.goto(final_url, timeout=60000, wait_until="domcontentloaded")
                    try:
                        await mobile_page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await mobile_page.wait_for_timeout(3000)

                    # The desktop capture waits on fonts and scrolls the page
                    # to trigger entrance animations and lazy images; the
                    # mobile one used to do neither, so it was systematically
                    # the more under-rendered of the two — and it is the one
                    # the model is asked to find mobile-only problems in.
                    try:
                        await mobile_page.evaluate("() => document.fonts.ready")
                    except Exception:
                        pass
                    try:
                        await mobile_page.evaluate(
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
                        await mobile_page.wait_for_timeout(1500)
                    except Exception:
                        pass

                    # Measured BEFORE the overlays are hidden: a consent
                    # banner wider than the screen is a real horizontal
                    # overflow for a real visitor, and hiding it first would
                    # report the site as fine when it is not.
                    try:
                        mobile_horizontal_overflow = await mobile_page.evaluate(
                            "() => document.documentElement.scrollWidth > window.innerWidth + 5"
                        )
                    except Exception:
                        pass

                    await _suppress_overlays(mobile_page)

                    # Vision-gated borderline fallback (see _audit_current_page)
                    # is desktop-only, same reasoning as the close-up crop below
                    # not existing for mobile — so the third return value is
                    # discarded here rather than threaded through.
                    mobile_violations, mobile_candidates, _mobile_borderline = await _run_axe_audit(mobile_page)
                    for v in mobile_violations:
                        v["page"] = "mobile view"

                    # The mobile capture gets the same treatment as the
                    # desktop one: highlight in-browser, capture, un-highlight.
                    # No separate close up crop here — at 390px wide the box
                    # already occupies a much bigger share of the frame than
                    # it does on the 1280px desktop capture, so the ambiguity
                    # the close up exists to remove is far less of a problem
                    # on this image to begin with.
                    mobile_visual_flaw = await _highlight_element(mobile_page, mobile_candidates)
                    mobile_screenshot_bytes = await _capture(mobile_page)
                    await _remove_highlight(mobile_page)
                    if mobile_visual_flaw:
                        print(f"[Visuals] Mobile highlight drawn on {mobile_visual_flaw['selector']}")
                except Exception as e:
                    print(f"[Visuals] Mobile screenshot/checks failed (non-critical): {e}")
                finally:
                    if mobile_context is not None:
                        try:
                            await mobile_context.close()
                        except Exception:
                            pass

                await browser.close()
        finally:
            _release_playwright_slot(_sem)

        # Pick whichever audited page has the most severe visual flaw
        # (magenta-box evidence) to attach to the email — falls back to the
        # homepage if no page had a violation the browser could actually
        # outline.
        best = pages_checked[0]
        for candidate in pages_checked[1:]:
            if candidate["visual_flaw"] and (
                not best["visual_flaw"]
                or _SEVERITY_RANK.get(candidate["visual_flaw"].get("impact"), 4)
                < _SEVERITY_RANK.get(best["visual_flaw"].get("impact"), 4)
            ):
                best = candidate

        # The box is ALREADY in these bytes — the browser rendered it as
        # part of the page (see _highlight_element), so Pillow's only job here
        # is the JPEG conversion. Nothing is drawn onto the image after the
        # fact any more, which is what removes the possibility of the outline
        # and the pixels underneath it describing different moments.
        img = Image.open(BytesIO(best["screenshot_bytes"])).convert("RGB")

        visual_flaw_context = ""
        if best["visual_flaw"]:
            page_note = f" on the {best['label']} page" if best["label"] != "/" else ""
            visual_flaw_context = (
                f"The magenta box in the desktop screenshot highlights an accessibility "
                f"flaw{page_note}: {best['visual_flaw']['description']}."
            )

        filepath = os.path.join(SCREENSHOTS_DIR, make_screenshot_filename(company_name, url))
        img.save(filepath, format="JPEG", quality=85)

        # A tight crop of just the marked element, saved separately when one
        # was actually captured (see _capture_closeup) — a second, unambiguous
        # image next to the full-page one, rather than relying on the marker
        # alone to make clear what it points at.
        closeup_filepath = None
        if best.get("closeup_bytes"):
            closeup_img = Image.open(BytesIO(best["closeup_bytes"])).convert("RGB")
            closeup_filepath = os.path.join(
                SCREENSHOTS_DIR, make_closeup_screenshot_filename(company_name, url)
            )
            closeup_img.save(closeup_filepath, format="JPEG", quality=85)

        mobile_filepath = None
        if mobile_screenshot_bytes:
            mobile_img = Image.open(BytesIO(mobile_screenshot_bytes)).convert("RGB")
            mobile_filepath = os.path.join(
                SCREENSHOTS_DIR, make_mobile_screenshot_filename(company_name, url)
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
            # Set only when a box was genuinely drawn into the mobile capture
            # too. Kept separate from the desktop one so the prompt can say
            # which image a box is in — both are attached to the email now,
            # and "the magenta box" is ambiguous across two pictures.
            "mobile_visual_flaw_context": (
                f"The magenta box in the mobile screenshot highlights an accessibility flaw: "
                f"{mobile_visual_flaw['description']}."
                if mobile_visual_flaw else ""
            ),
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
            "closeup_image_path": closeup_filepath,
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


_VISUALLY_APPARENT_AXE_RULES = frozenset({
    "color-contrast",
    "color-contrast-enhanced",
    "target-size",
    "empty-heading",
    "empty-table-header",
    "link-in-text-block",
})
# Most axe-core rules catch a problem with no visible symptom at all — a
# missing aria-label, an unlabeled landmark, a duplicate id, a missing lang
# attribute. The flagged element renders completely normally to a sighted
# person, so boxing it in the audit screenshot reads as arbitrary to the
# person receiving the email: nothing about the marked spot actually looks
# wrong, and it usually has nothing to do with whatever the copy is about.
# Reported live from a real send — a Maps-search agriculture lead's box
# landed on a small share icon with no explanation a recipient could
# connect to anything. Checked axe-core's own rule-descriptions.md
# (dequelabs/axe-core) for the rules whose failure a sighted person can
# actually notice by looking, rather than guessing: contrast that's
# visibly washed out, a target that's visibly cramped, a heading/table
# header that's visibly blank, a link that doesn't visually stand out from
# its paragraph. Only the highlight candidate list is filtered — the full
# `violations` list (accessibility flaw text/counts) is unaffected.


def _call_vision_gate_raw(prompt: str, image_bytes: bytes) -> str | None:
    """
    Fixed-preference-order vision call (Gemini -> Claude -> GPT-4o-mini),
    same contract as AIAuditor._call_vision_judge in ai_audit.py — kept as
    its own function purely so _vision_confirms_visible_defect's config-
    gating and JSON-parsing logic can be unit-tested by monkeypatching this
    one boundary, without mocking three different SDKs' internals. Returns
    the first provider's raw text, or None if all configured providers
    failed or none is configured.
    """
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    if config.GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            model = genai.GenerativeModel(
                "gemini-3.5-flash",
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json", temperature=0.0,
                ),
            )
            return model.generate_content([
                {"mime_type": "image/jpeg", "data": image_bytes},
                prompt,
            ]).text
        except Exception as e:
            print(f"[Visuals] Vision gate (Gemini) failed: {e}")

    if config.ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                temperature=0.0,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64_image}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            return message.content[0].text
        except Exception as e:
            print(f"[Visuals] Vision gate (Claude) failed: {e}")

    if config.OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                ]}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"[Visuals] Vision gate (GPT-4o-mini) failed: {e}")

    return None


def _vision_confirms_visible_defect(image_bytes: bytes, axe_description: str) -> bool:
    """
    Narrow yes/no vision gate for ONE borderline axe-core violation — a real
    rule on a real, already-located element, just not one on the
    _VISUALLY_APPARENT_AXE_RULES allowlist above. Only ever called from
    _audit_current_page when that allowlist produced zero candidates at all,
    so the choice this function makes is "no box" (today's existing
    behaviour) vs "a vision-confirmed box" — it can never take away an
    already-qualifying safe candidate.

    Requested 2026-09-09: "cant that api of claude go on the website himself
    and find the issue like the actual claude does live" — i.e. use AI
    vision as the actual issue-finder, not just the fixed rule allowlist.
    Deliberately NOT built as free-form "look at this screenshot and tell me
    what's wrong" — this file already tried that shape once and it
    hallucinated real, false criticisms on real live leads (see CLAUDE.md's
    2026-08-31 "Visual claims were generated freely and judged afterwards"
    entry, and the whole reason `_verify_visual_claims` exists). This
    function's only degree of freedom is a boolean: the location was already
    measured by the browser (not guessed by the model), and the description
    that ships in the email/box label is always the CALLER's axe_description
    — this function's own reasoning about WHY never reaches the copy. That
    is what makes broadening past the hardcoded allowlist safe rather than a
    regression of the fix above.

    Plain synchronous function (blocking AI SDK calls, same shape as every
    method on AIAuditor) — callers must wrap it in asyncio.to_thread, same
    convention as every other blocking call in an async route in this
    codebase. Same silent-degradation contract as every AI call here: never
    raises, degrades to False (the existing "no candidates" outcome) on any
    error or missing provider.
    """
    if not (config.GEMINI_API_KEY or config.ANTHROPIC_API_KEY or config.OPENAI_API_KEY):
        return False

    prompt = (
        "An automated accessibility scanner flagged the element shown in this cropped "
        f"screenshot for: \"{axe_description}\". Judge ONLY whether this specific element "
        "has a problem a sighted person would actually notice just by looking at it — not "
        "whether the underlying code/markup issue is real (assume it is), and not a matter "
        "of taste or style. If nothing about it looks visibly wrong to an ordinary viewer, "
        "say so.\n"
        'Return ONLY valid JSON: {"visible": true or false}. No markdown, no explanation.'
    )
    raw = _call_vision_gate_raw(prompt, image_bytes)
    if not raw:
        return False
    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            return False
        result = json.loads(cleaned[start:end + 1])
        return result.get("visible") is True
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


async def _run_axe_audit(page) -> tuple[list, list[dict], list[dict]]:
    """
    Run the axe-core accessibility engine on the current page.

    Returns (violations, highlight_candidates, borderline_candidates). The
    highlight_candidates are selectors ranked most-severe-first, filtered to
    _VISUALLY_APPARENT_AXE_RULES so the box only ever lands on something a
    sighted person can actually notice; _highlight_element picks and draws
    one. This function no longer resolves coordinates itself — see the
    comment on the candidate list below.

    borderline_candidates are every OTHER violation — real axe-core rules on
    real elements, just not ones this file has hardcoded as "definitely has
    a visible symptom". _audit_current_page only reaches for these when the
    safe list above produced nothing at all, and only after a vision model
    confirms the specific element actually looks wrong — see
    _vision_confirms_visible_defect.
    """
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
        
        # Build a severity-ordered list of elements that COULD be highlighted.
        #
        # This function deliberately no longer decides which one wins, and no
        # longer measures anything. Every geometry check (in-viewport, big
        # enough to see, not covering the whole frame, actually painted) now
        # happens inside _highlight_element's single evaluate() call, at the
        # same instant the outline is drawn and the screenshot is taken.
        # Measuring here and drawing later is precisely what let the box drift
        # away from the picture on any page with a carousel or a late-settling
        # layout — see _highlight_element's docstring.
        candidates = []
        borderline_candidates = []

        def _add_candidates(rule_list, target_list, allowlist_only):
            for v in rule_list:
                in_allowlist = v["id"] in _VISUALLY_APPARENT_AXE_RULES
                if allowlist_only != in_allowlist:
                    continue
                for node in v.get("nodes", []):
                    target_selectors = node.get("target", [])
                    if not target_selectors:
                        continue
                    selector = target_selectors[0]
                    if isinstance(selector, list):
                        if not selector:
                            continue
                        selector = selector[0]
                    if not isinstance(selector, str):
                        continue
                    # Root-level elements make no useful highlight — boxing
                    # <body> is the same as boxing nothing.
                    if selector.strip().lower() in ("html", "body", "head"):
                        continue
                    target_list.append({
                        "selector": selector,
                        "description": v.get("help", ""),
                        "impact": v.get("impact", "minor"),
                    })

        _add_candidates(violations, candidates, allowlist_only=True)

        # color-contrast/-enhanced routinely lands in axe-core's "incomplete"
        # bucket instead of "violations" — live-verified here: a plain
        # white-on-white element with an explicit 1:1 contrastRatio still
        # comes back "incomplete", not "violation", a known quirk of how
        # axe-core's contrast check flags results for manual confirmation
        # rather than an ambiguous measurement. The underlying defect reads
        # exactly as washed-out to a human either way, so it is still a
        # legitimate highlight candidate — added after every definitive
        # violation, since certain should still outrank needs-review.
        incomplete_contrast = [
            {
                "id": v.get("id", ""),
                "impact": v.get("impact", "minor"),
                "help": v.get("help", ""),
                "nodes": v.get("nodes", []),
            }
            for v in results.response.get("incomplete", [])
            if v.get("id") in ("color-contrast", "color-contrast-enhanced")
        ]
        _add_candidates(incomplete_contrast, candidates, allowlist_only=True)

        # Everything else axe found — a real rule, a real element, just not
        # one this file has hardcoded as "definitely has a visible symptom".
        # Only ever consulted when `candidates` above comes up completely
        # empty (see _audit_current_page), and only after a vision model
        # confirms the specific highlighted element actually looks wrong —
        # never used to widen what gets boxed on a page that already has a
        # safe candidate.
        _add_candidates(violations, borderline_candidates, allowlist_only=False)

        # Violations are already sorted most-severe-first above, so this list
        # inherits that order. Capped because it is serialised into a single
        # evaluate() payload and the browser stops at the first one that
        # qualifies anyway.
        candidates = candidates[:40]
        # Only the first candidate _highlight_element manages to actually
        # qualify (in viewport, big enough, visible) ever costs a vision
        # call — see _audit_current_page — so this cap is generous headroom
        # for viewport/size rejects, not a cost lever in itself.
        borderline_candidates = borderline_candidates[:10]

        # Clean up nodes array to save memory
        for v in violations:
            v.pop("nodes", None)

        print(f"[Axe] Found {len(violations)} accessibility violations.")
        return violations[:10], candidates, borderline_candidates

    except Exception as e:
        print(f"[Axe] Accessibility audit failed (non-critical): {e}")
        return [], [], []


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


async def _check_stretched_images(page) -> tuple[int, list[dict]]:
    """
    Count visible <img> elements displayed significantly larger than their
    natural (source) resolution — a classic cause of blurry/pixelated
    images that immediately reads as unpolished.

    Also returns each offending element as a highlight candidate, in the
    same shape _run_axe_audit's candidates use. A stretched, pixelated
    photo is exactly the kind of thing a business owner can see is wrong
    just by looking at it — a much better highlight target than most
    axe-core rules (see _VISUALLY_APPARENT_AXE_RULES) — so these are fed
    into the same candidate list rather than only ever being described in
    prose. Each offending <img> gets a one-off `data-mmga-flaw` marker
    attribute so it has a selector _highlight_element can re-find; the
    attribute is inert and harmless left on the page afterwards.
    """
    try:
        markers = await page.evaluate("""() => {
            const imgs = document.querySelectorAll('img');
            const found = [];
            let idx = 0;
            for (const img of imgs) {
                const rect = img.getBoundingClientRect();
                if (rect.width < 40 || rect.height < 40) continue; // ignore icons
                if (!img.naturalWidth || !img.naturalHeight) continue;
                if (rect.width > img.naturalWidth * 1.4 || rect.height > img.naturalHeight * 1.4) {
                    const marker = 'mmga-stretched-' + idx;
                    img.setAttribute('data-mmga-flaw', marker);
                    found.push(marker);
                    idx++;
                }
            }
            return found;
        }""")
        markers = markers or []
        candidates = [
            {
                "selector": f'[data-mmga-flaw="{marker}"]',
                "description": "This image is stretched beyond its real resolution and looks blurry or pixelated",
                "impact": "moderate",
            }
            for marker in markers
        ]
        return len(markers), candidates
    except Exception as e:
        print(f"[Visuals] Stretched image check failed (non-critical): {e}")
        return 0, []


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

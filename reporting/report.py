"""
Builds the design and conversion report from what reporting/collect.py measured.

build_report() and render_html() are pure (no network, no browser), so what the
report says is unit-tested; render_pdf() is the only part that needs Chromium.

Accuracy rules carried over from the rest of the project: every finding quotes
a measurement; a thing is listed as "already working" only when a measurement
says so; nothing is said about a measurement that failed.
"""
import base64
from datetime import date
from html import escape

from analyzer import design_review
from analyzer.visuals import _acquire_playwright_slot, _release_playwright_slot

INK = "#2a1f14"
DARK = "#1f1610"
CREAM = "#fff8ef"
PANEL = "#f1e3cf"
ACCENT = "#d9480f"
GREEN = "#2f7d4f"

_RANK = {"serious": 0, "moderate": 1, "minor": 2}
_TAG = {"serious": "Fix first", "moderate": "Next", "minor": "Worth doing"}

_PRESENTATION = {
    "hero_video_no_headline": (
        "The first screen is a video player",
        "Lead with one sentence: who you help and with what, then a clear next step such as "
        "\"Let's talk\" and \"See our work\". Keep the video as a silent loop behind or beside "
        "the words, and let a button open the full version with sound.",
        "desktop_image",
    ),
    "no_headline_first_screen": (
        "The first screen has no headline",
        "Lead with one sentence: who you help and with what, in real text, then a clear next step.",
        "desktop_image",
    ),
    "proof_leaves_site": (
        "Project cards send visitors away",
        "Give your strongest projects a short page of their own: the task, what you did, one result "
        "and a few images. Keep the outside link at the bottom of the page, not instead of it.",
        "card_image",
    ),
    "contact_form_no_reply_time": (
        "The contact form never says when you'll reply",
        "Add one line under the button, for example \"We reply within one working day\", and offer "
        "WhatsApp as the faster way.",
        None,
    ),
}


# Findings about something that is absent are shown on the first screen, where it is missing.
_FIRST_SCREEN_PICTURE = {
    "no_main_heading": "Home page, desktop, first screen: no line here is marked as the main heading.",
    "no_first_screen_cta": "Home page, desktop, first screen: nothing here asks the visitor to get in touch.",
}


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def build_report(company: str, website: str, collected: dict, *, contact_email: str = "",
                 today: date | None = None) -> dict:
    """Turn raw measurements into a structured report. Pure."""
    c = collected or {}
    findings: list[dict] = []

    for f in c.get("presentation_findings") or []:
        title, advice, image_key = _PRESENTATION.get(
            f.get("key"), (None, None, None))
        if not title:
            continue
        findings.append({
            "key": f["key"], "title": title, "saw": _sentence(f.get("description")),
            "advice": advice, "impact": f.get("impact", "moderate"),
            "image": c.get(image_key) if image_key else None,
            "caption": ("Home page, desktop, first screen." if image_key == "desktop_image"
                        else "The home page, scrolled to the project cards." if image_key == "card_image"
                        else ""),
        })

    if c.get("mobile_overflow") is True and c.get("mobile_scroll_width"):
        findings.append({
            "key": "mobile_sideways", "title": "On a phone the page scrolls sideways",
            "saw": (f"On a phone-sized screen (390px wide) the page is {c['mobile_scroll_width']}px wide, "
                    "so a visitor can drag the page sideways and part of it sits off screen."),
            "advice": "Find the section wider than the screen (often an image, a table or a fixed-width "
                      "block) and let it shrink to the screen width.",
            "impact": "moderate", "image": c.get("mobile_image"),
            "caption": "Home page, phone-sized screen, first screen.",
        })

    shots = c.get("design_shots") or {}
    for d in design_review.design_findings(c.get("design_probe")):
        shot = shots.get(d["key"]) or {}
        image, caption = shot.get("image"), shot.get("caption", "")
        if not image and d["key"] in _FIRST_SCREEN_PICTURE and c.get("desktop_image"):
            image, caption = c["desktop_image"], _FIRST_SCREEN_PICTURE[d["key"]]
        findings.append({
            "key": d["key"], "title": d["title"], "saw": d["saw"], "advice": d["advice"],
            "impact": d["impact"], "image": image, "caption": caption,
        })

    # A finding of the same key only once, then most important first (stable).
    seen, unique = set(), []
    for f in findings:
        if f["key"] not in seen:
            seen.add(f["key"])
            unique.append(f)
    unique.sort(key=lambda f: _RANK.get(f["impact"], 1))
    for i, f in enumerate(unique, 1):
        f["number"] = i
        f["tag"] = _TAG.get(f["impact"], "Next")

    positives = list(design_review.design_positives(c.get("design_probe")))
    lcp = (c.get("real_web_vitals") or {}).get("lcp_ms")
    if isinstance(lcp, (int, float)) and 0 < lcp <= 2500:
        positives.append(
            f"The main content appeared after {lcp / 1000:.1f} seconds in our test browser, which is quick.")
    if str(c.get("final_url") or "").startswith("https://"):
        positives.append("The site is served securely over https.")
    if c.get("mobile_overflow") is False:
        positives.append("The page fits a phone screen without sideways scrolling.")

    appendix = [_sentence(s["description"]) for s in (c.get("seo_findings") or []) if s.get("description")]
    if isinstance(lcp, (int, float)) and lcp > 2500:
        appendix.append(
            f"The main content appeared after {lcp / 1000:.1f} seconds in our test browser on a desktop connection.")

    return {
        "company": (company or "").strip() or website, "website": website,
        "findings": unique, "positives": positives, "appendix": appendix,
        "desktop_image": c.get("desktop_image"), "mobile_image": c.get("mobile_image"),
        "contact_email": contact_email, "date": (today or date.today()).strftime("%d %B %Y"),
    }


# --------------------------------------------------------------------------- HTML

def _img(data: bytes | None, alt: str, extra_style: str = "") -> str:
    if not data:
        return ""
    return (f'<img alt="{escape(alt)}" style="display:block;width:100%;max-height:150mm;object-fit:cover;'
            f'object-position:top;border-radius:6px;border:1px solid {PANEL};{extra_style}" '
            f'src="data:image/jpeg;base64,{base64.b64encode(data).decode()}">')


def _finding_block(f: dict) -> str:
    img = ""
    if f.get("image"):
        img = (f'<div style="margin-top:6mm">{_img(f["image"], f["title"])}'
               f'<p class="cap">{escape(f.get("caption") or "")}</p></div>')
    return (
        f'<div class="finding"><div class="fhead"><span class="num">{f["number"]:02d}</span>'
        f'<div><span class="tag t-{escape(f["impact"])}">{escape(f["tag"])}</span>'
        f'<h2>{escape(f["title"])}</h2></div></div>'
        f'<div class="cols"><div><h4>What we saw</h4><p>{escape(f["saw"])}</p></div>'
        f'<div><h4>What we\'d do</h4><p>{escape(f["advice"])}</p></div></div>{img}</div>'
    )


_CSS = f"""
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: Arial, Helvetica, 'Liberation Sans', sans-serif; color:{INK}; background:{CREAM}; }}
.page {{ width:210mm; height:297mm; padding:18mm 18mm 16mm; position:relative; overflow:hidden;
         page-break-after:always; background:{CREAM}; }}
.page.dark {{ background:{DARK}; color:{CREAM}; }}
.kicker {{ font-size:9pt; font-weight:bold; letter-spacing:2px; text-transform:uppercase; color:{ACCENT}; margin:0 0 4mm; }}
h1 {{ font-size:40pt; line-height:1.0; margin:0 0 6mm; text-transform:uppercase; letter-spacing:-0.5px; }}
h1.big {{ font-size:54pt; color:#f6c9a8; }}
h2 {{ font-size:18pt; line-height:1.15; margin:1mm 0 0; text-transform:uppercase; }}
h4 {{ font-size:8pt; letter-spacing:2px; text-transform:uppercase; color:{ACCENT}; margin:0 0 2mm; }}
p {{ font-size:10.5pt; line-height:1.55; margin:0 0 3mm; }}
.cap {{ font-size:8.5pt; color:#6b5a48; margin:2mm 0 0; }}
.footer {{ position:absolute; left:18mm; right:18mm; bottom:9mm; display:flex; justify-content:space-between;
           font-size:8pt; letter-spacing:1px; text-transform:uppercase; color:#8a7a68; }}
.dark .footer {{ color:#a99884; }}
.finding {{ margin-bottom:9mm; }}
.fhead {{ display:flex; gap:6mm; align-items:flex-start; margin-bottom:4mm; }}
.num {{ font-size:34pt; font-weight:bold; color:{ACCENT}; line-height:1; min-width:17mm; }}
.tag {{ display:inline-block; font-size:7.5pt; font-weight:bold; letter-spacing:1.5px; text-transform:uppercase;
        padding:1mm 3mm; border-radius:10mm; background:{PANEL}; color:{INK}; }}
.tag.t-serious {{ background:{ACCENT}; color:#fff; }}
.cols {{ display:flex; gap:8mm; }} .cols > div {{ flex:1; }}
.box {{ background:{DARK}; color:{CREAM}; border-radius:4mm; padding:7mm; }}
.box p {{ color:{CREAM}; }}
.ok {{ list-style:none; padding:0; margin:0; }}
.ok li {{ font-size:10pt; line-height:1.5; padding:2.4mm 0 2.4mm 7mm; border-bottom:1px solid {PANEL}; position:relative; }}
.ok li:before {{ content:''; position:absolute; left:0; top:4.2mm; width:2.6mm; height:2.6mm; border-radius:50%; background:{GREEN}; }}
.first {{ counter-reset:n; list-style:none; padding:0; margin:0; }}
.first li {{ position:relative; padding:0 0 5mm 13mm; font-size:10.5pt; line-height:1.5; }}
.first li:before {{ counter-increment:n; content:counter(n,decimal-leading-zero); position:absolute; left:0; top:-1mm;
                    font-size:17pt; font-weight:bold; color:{ACCENT}; }}
.first b {{ display:block; font-size:11.5pt; }}
.app li {{ font-size:10pt; line-height:1.5; margin-bottom:3mm; }}
.cta {{ background:{ACCENT}; color:#fff; border-radius:4mm; padding:8mm; margin-top:10mm; }}
.cta p {{ color:#fff; font-size:12pt; }} .cta h2 {{ color:#fff; margin-bottom:3mm; }}
"""


def _footer(left: str, n: int) -> str:
    return f'<div class="footer"><span>{escape(left)}</span><span>{n:02d}</span></div>'


def render_html(report: dict) -> str:
    """The whole report as one self-contained HTML document (images inlined)."""
    company, website = report["company"], report["website"]
    findings = report["findings"]
    brand = "MMGA Agency"
    pages: list[str] = []
    n = 1

    pages.append(
        f'<section class="page dark"><p class="kicker">Design &amp; conversion review - {escape(report["date"])}</p>'
        f'<div style="margin-top:34mm"><p class="kicker" style="color:#f6c9a8">A free review for</p>'
        f'<h1 class="big">{escape(company)}</h1>'
        f'<p style="font-size:13pt;letter-spacing:3px;text-transform:uppercase;color:{ACCENT};font-weight:bold">'
        f'{escape(website)}</p>'
        f'<p style="font-size:13pt;max-width:120mm;margin-top:10mm">What a first-time visitor sees on your home '
        f'page, what makes them trust you, and what stands between them and getting in touch.</p></div>'
        f'<div class="footer"><span>Prepared by MMGA Agency, Mumbai</span><span>{len(findings)} findings</span></div></section>'
    )
    n += 1

    top = findings[:3]
    firsts = "".join(f"<li><b>{escape(f['title'])}</b>{escape(f['advice'])}</li>" for f in top) or (
        "<li><b>Nothing urgent</b>The measurements below found no serious problem.</li>")
    works = "".join(f"<li>{escape(p)}</li>" for p in report["positives"]) or (
        "<li>We found nothing we could confirm as a clear strength from measurements alone.</li>")
    pages.append(
        f'<section class="page"><p class="kicker">The short version</p>'
        f'<h1>{len(findings)} thing{"s" if len(findings) != 1 else ""} worth fixing</h1>'
        f'<p style="max-width:150mm">We looked at your home page the way a new visitor would, on a desktop and on a '
        f'phone, and measured what we saw. Every point below rests on a number or a screenshot, not on opinion.</p>'
        f'<div class="cols" style="margin-top:8mm"><div class="box"><h4>Do these first</h4>'
        f'<ol class="first" style="color:{CREAM}">{firsts}</ol></div>'
        f'<div><h4>Already working, keep it</h4><ul class="ok">{works}</ul></div></div>'
        f'{_footer(brand, n)}</section>'
    )
    n += 1

    # Findings with a picture get a page each; the rest are paired two to a page.
    queue: list[dict] = []
    for f in findings:
        if f.get("image"):
            if queue:
                pages.append(f'<section class="page">{"".join(_finding_block(x) for x in queue)}{_footer(brand, n)}</section>')
                n += 1
                queue = []
            pages.append(f'<section class="page">{_finding_block(f)}{_footer(brand, n)}</section>')
            n += 1
        else:
            queue.append(f)
            if len(queue) == 2:
                pages.append(f'<section class="page">{"".join(_finding_block(x) for x in queue)}{_footer(brand, n)}</section>')
                n += 1
                queue = []
    if queue:
        pages.append(f'<section class="page">{"".join(_finding_block(x) for x in queue)}{_footer(brand, n)}</section>')
        n += 1

    if report["appendix"]:
        items = "".join(f"<li>{escape(a)}</li>" for a in report["appendix"])
        pages.append(
            f'<section class="page"><p class="kicker">Appendix - not the point of this review</p>'
            f'<h1>Under the hood</h1><p>A few smaller things found while measuring.</p>'
            f'<ul class="app" style="padding-left:6mm">{items}</ul>{_footer(brand, n)}</section>'
        )
        n += 1

    contact = escape(report.get("contact_email") or "")
    reply = (f"Reply to this email" if not contact else f"Reply to this email or write to {contact}")
    pages.append(
        f'<section class="page dark"><p class="kicker">Where to start</p><h1>What we\'d do first</h1>'
        f'<ol class="first" style="color:{CREAM}">'
        + "".join(f"<li><b>{escape(f['title'])}</b>{escape(f['advice'])}</li>" for f in top)
        + f'</ol><div class="cta"><h2>Want us to do it?</h2><p>{reply} and we\'ll send a fixed quote for '
        f'whichever part you\'d like to start with. No call needed.</p></div>'
        f'<div class="footer"><span>MMGA Agency - mmga.agency</span><span>{n:02d}</span></div></section>'
    )

    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<title>Design and conversion review for {escape(company)}</title><style>{_CSS}</style></head>'
            f'<body>{"".join(pages)}</body></html>')


# --------------------------------------------------------------------------- PDF

async def render_pdf(html: str) -> bytes:
    """HTML to an A4 PDF with Chromium. Holds the shared browser slot."""
    from playwright.async_api import async_playwright
    sem = await _acquire_playwright_slot()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html, wait_until="load")
                return await page.pdf(format="A4", print_background=True,
                                      margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
            finally:
                await browser.close()
    finally:
        _release_playwright_slot(sem)


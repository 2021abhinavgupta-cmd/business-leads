"""
The designer's view of a home page, measured rather than judged.

A designer reviewing a site looks at the typefaces, the sizes, the heading
order, whether buttons look like one family, how wide the text runs and
whether the first screen asks for anything. Each of those is a number in the
browser, so each is measured here with getComputedStyle / getBoundingClientRect
and only then turned into a sentence that quotes the number. Nothing here asks
a model what it thinks of the design.

These are observations for the PDF report (sent when someone asks for the full
review), not claims for the cold email: "most polished sites use two or three
typefaces" is a guideline, not a defect a recipient can verify in one click.

`design_findings` / `design_positives` are pure, so the rules are unit-tested
without a browser; `probe_design` runs the measurement on a loaded page and
returns nothing at all if it cannot measure.
"""
import re
import statistics

# Names that are not a real choice of typeface.
_GENERIC_FAMILIES = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-sans-serif",
    "ui-serif", "ui-monospace", "-apple-system", "blinkmacsystemfont", "inherit", "initial",
}
_ICON_FONT = re.compile(r"icon|awesome|glyph|symbol|material|dashicons|ionicons|fontello|bootstrap-icons", re.I)

_MIN_FAMILY_SHARE = 0.03      # a family must carry 3% of the text to count
_MIN_SIZE_SHARE = 0.01
_MAX_TYPEFACES = 3
_MAX_TEXT_SIZES = 9
_SMALL_TEXT_PX = 12
_SMALL_TEXT_SHARE = 0.10
_WIDE_LINE_CHARS = 95
_MIN_BUTTON_SHAPES = 3

_CTA_WORDS = (
    r"contact|get in touch|let.?s talk|talk to|book|call|whatsapp|get started|get a quote|quote|"
    r"free|enquir|inquir|schedule|appointment|demo|consult|buy|order|shop|sign up|subscribe|apply|visit"
)

_DESIGN_PROBE_JS = """(cfg) => {
    const vw = window.innerWidth, vh = window.innerHeight;
    const shown = (el) => {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const fonts = {}, sizes = {};
    let chars = 0, smallChars = 0;
    for (const el of document.body.querySelectorAll('*')) {
        if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH', 'IFRAME'].includes(el.tagName.toUpperCase())) continue;
        let text = '';
        for (const n of el.childNodes) if (n.nodeType === 3) text += n.textContent;
        text = text.replace(/\\s+/g, ' ').trim();
        if (text.length < 2 || !shown(el)) continue;
        const s = getComputedStyle(el);
        const family = (s.fontFamily.split(',')[0] || '').replace(/["']/g, '').trim();
        const size = Math.round(parseFloat(s.fontSize) || 0);
        fonts[family] = (fonts[family] || 0) + text.length;
        sizes[size] = (sizes[size] || 0) + text.length;
        chars += text.length;
        if (size > 0 && size < cfg.smallPx) smallChars += text.length;
    }
    const headings = [];
    for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
        if (!shown(h) || !(h.textContent || '').trim()) continue;
        headings.push({level: parseInt(h.tagName[1], 10), size: Math.round(parseFloat(getComputedStyle(h).fontSize) || 0)});
    }
    const buttons = [];
    const btnSel = 'button, [role=button], input[type=submit], input[type=button], a[class*=btn], a[class*=button], a[class*=cta]';
    for (const b of document.querySelectorAll(btnSel)) {
        if (!shown(b) || b.closest('nav, footer')) continue;
        const r = b.getBoundingClientRect();
        if (r.width < 60 || r.height < 28 || (b.textContent || '').trim().length < 2) continue;
        const s = getComputedStyle(b);
        const radius = parseFloat(s.borderTopLeftRadius) || 0;
        const bucket = radius < 4 ? 'square' : (radius >= r.height / 2 - 1 ? 'pill' : 'rounded');
        buttons.push(bucket);
    }
    const lineLengths = [];
    for (const p of document.querySelectorAll('main p, article p, section p, p')) {
        if (p.closest('nav, footer, header') || !shown(p)) continue;
        const text = (p.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text.length < 120) continue;
        const range = document.createRange();
        range.selectNodeContents(p);
        const tops = new Set();
        for (const r of range.getClientRects()) if (r.width > 4) tops.add(Math.round(r.top / 4));
        if (tops.size >= 3) lineLengths.push(text.length / tops.size);
        if (lineLengths.length >= 40) break;
    }
    const ctaRe = new RegExp(cfg.cta, 'i');
    let interactive = 0, firstScreenCta = false;
    for (const a of document.querySelectorAll('a[href], button, input[type=submit]')) {
        if (!shown(a)) continue;
        const r = a.getBoundingClientRect();
        if (!(r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw)) continue;
        interactive++;
        const label = ((a.textContent || '') + ' ' + (a.getAttribute('aria-label') || '') + ' ' + (a.getAttribute('href') || '')).slice(0, 200);
        if (ctaRe.test(label)) firstScreenCta = true;
    }
    return {fonts, sizes, chars, smallChars, headings, buttons, lineLengths, interactive, firstScreenCta};
}"""


def _family_name(raw: str) -> str:
    return (raw or "").strip().strip("\"'")


# Font files are often registered per weight ("General Sans Semibold"); a
# weight is not a different typeface, so trailing weight/style words are
# stripped and the counts of one family are added together.
_WEIGHT_WORDS = re.compile(
    r"[\s\-_]+(thin|hairline|extra\s*light|ultra\s*light|light|regular|normal|book|roman|medium|"
    r"semi\s*bold|demi\s*bold|bold|extra\s*bold|ultra\s*bold|heavy|black|italic|oblique|variable|"
    r"\d{3})$", re.I)


def _base_family(name: str) -> str:
    clean = _family_name(name)
    previous = None
    while previous != clean:
        previous = clean
        clean = _WEIGHT_WORDS.sub("", clean).strip()
    return clean or _family_name(name)


def _typefaces(fonts: dict, chars: int) -> list[str]:
    """Real typefaces carrying a meaningful share of the text, biggest first.
    Weights of one family count once, and their text is added together."""
    merged: dict[str, tuple[str, int]] = {}
    for name, count in (fonts or {}).items():
        base = _base_family(name)
        if not base or base.lower() in _GENERIC_FAMILIES or _ICON_FONT.search(base):
            continue
        shown, total = merged.get(base.lower(), (base, 0))
        merged[base.lower()] = (shown, total + int(count or 0))
    out = []
    for shown, total in sorted(merged.values(), key=lambda kv: -kv[1]):
        if chars and total / chars < _MIN_FAMILY_SHARE:
            continue
        out.append(shown)
    return out


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _finding(key: str, title: str, saw: str, advice: str, impact: str) -> dict:
    return {"key": key, "title": title, "saw": saw, "advice": advice, "impact": impact}


def design_findings(probe: dict | None) -> list[dict]:
    """Observations that read as problems. Each quotes the number it rests on."""
    if not probe or not isinstance(probe, dict) or not probe.get("chars"):
        return []
    out: list[dict] = []
    chars = int(probe.get("chars") or 0)

    faces = _typefaces(probe.get("fonts") or {}, chars)
    if len(faces) > _MAX_TYPEFACES:
        out.append(_finding(
            "many_typefaces", f"{len(faces)} typefaces on one page",
            f"The home page sets its text in {len(faces)} different typefaces: {_joined(faces)}. "
            "Each one is a different personality, and together they read as several designers "
            "rather than one brand.",
            "Choose one typeface for headlines and one for reading text, and use weight and size "
            "for everything else.",
            "moderate" if len(faces) >= 5 else "minor",
        ))

    sizes = {int(k): v for k, v in (probe.get("sizes") or {}).items() if int(k) > 0}
    used = [s for s, c in sizes.items() if chars and c / chars >= _MIN_SIZE_SHARE]
    if len(used) > _MAX_TEXT_SIZES:
        out.append(_finding(
            "many_text_sizes", f"{len(used)} different text sizes",
            f"The home page uses {len(used)} different text sizes, from {min(used)}px to {max(used)}px. "
            "Without a clear scale, nothing tells the visitor what to read first.",
            "Set a short scale (for example six sizes) and reuse it: one for the main heading, one for "
            "section titles, one for body text, one for small print.",
            "minor",
        ))

    small = int(probe.get("smallChars") or 0)
    if chars and small / chars >= _SMALL_TEXT_SHARE:
        out.append(_finding(
            "small_text", f"{round(100 * small / chars)}% of the text is very small",
            f"About {round(100 * small / chars)}% of the text on the home page is smaller than "
            f"{_SMALL_TEXT_PX}px, which is hard to read on a phone and for anyone over 40.",
            f"Keep reading text at 16px or larger, and use {_SMALL_TEXT_PX}px only for legal lines and captions.",
            "moderate",
        ))

    headings = probe.get("headings") or []
    h1s = [h for h in headings if h.get("level") == 1]
    if not h1s:
        out.append(_finding(
            "no_main_heading", "No main heading",
            "The home page has no main heading (h1), so search engines and screen readers get no "
            "title for the page, and visitors get no single line saying what it is.",
            "Make the headline that says who you help and with what the main heading.",
            "moderate",
        ))
    elif len(h1s) > 1:
        out.append(_finding(
            "many_main_headings", f"{len(h1s)} main headings",
            f"The home page has {len(h1s)} main headings. A page should have one, the line that says "
            "what it is about.",
            "Keep one main heading and make the others section titles.",
            "minor",
        ))
    levels = [h.get("level") for h in headings if isinstance(h.get("level"), int)]
    skip = next(((a, b) for a, b in zip(levels, levels[1:]) if b - a > 1), None)
    if skip and h1s:
        out.append(_finding(
            "skipped_heading_level", f"Headings jump from h{skip[0]} to h{skip[1]}",
            f"A heading level is skipped (h{skip[0]} straight to h{skip[1]}), so the outline of the page "
            "is broken for anyone using a screen reader or skimming by headings.",
            "Step down one level at a time.",
            "minor",
        ))

    buckets = sorted(set(probe.get("buttons") or []))
    if len(buckets) >= _MIN_BUTTON_SHAPES:
        out.append(_finding(
            "button_shapes", f"Buttons come in {len(buckets)} shapes",
            f"Buttons on the home page use {len(buckets)} different corner styles ({', '.join(buckets)}), "
            "so the same action looks different from section to section.",
            "Use one button shape everywhere and change only the colour for primary and secondary.",
            "minor",
        ))

    lengths = [float(x) for x in (probe.get("lineLengths") or []) if x]
    if len(lengths) >= 3 and statistics.median(lengths) >= _WIDE_LINE_CHARS:
        out.append(_finding(
            "wide_text_lines", f"Text lines run about {round(statistics.median(lengths))} characters",
            f"Paragraphs run about {round(statistics.median(lengths))} characters per line. "
            "Anything past roughly 75 makes the eye lose its place on the way back to the next line.",
            "Cap the text column at about 65 to 75 characters wide.",
            "minor",
        ))

    if (probe.get("interactive") or 0) >= 1 and not probe.get("firstScreenCta"):
        out.append(_finding(
            "no_first_screen_cta", "Nothing on the first screen asks for contact",
            "Among the buttons and links visible on the first screen we found none that asks the "
            "visitor to contact, book, call or get a quote.",
            "Put one clear action (for example Let's talk) on the first screen, in the header and again "
            "under the headline.",
            "moderate",
        ))
    return out


def design_positives(probe: dict | None) -> list[str]:
    """Things that measured well. Only stated when the measurement supports it."""
    if not probe or not isinstance(probe, dict) or not probe.get("chars"):
        return []
    out: list[str] = []
    chars = int(probe.get("chars") or 0)
    faces = _typefaces(probe.get("fonts") or {}, chars)
    if 1 <= len(faces) <= _MAX_TYPEFACES:
        out.append(f"The text uses {len(faces)} typeface{'s' if len(faces) != 1 else ''} ({_joined(faces)}), "
                   "which keeps the page looking like one brand.")
    h1s = [h for h in (probe.get("headings") or []) if h.get("level") == 1]
    if len(h1s) == 1:
        out.append("The page has exactly one main heading.")
    if probe.get("firstScreenCta"):
        out.append("The first screen has a button or link that asks the visitor to get in touch.")
    if chars and (int(probe.get("smallChars") or 0) / chars) < 0.03:
        out.append("Almost all of the text is a comfortable reading size.")
    lengths = [float(x) for x in (probe.get("lineLengths") or []) if x]
    if len(lengths) >= 3 and 40 <= statistics.median(lengths) <= 85:
        out.append("Paragraphs run a comfortable line length for reading.")
    return out


async def probe_design(page) -> dict | None:
    """Measure the loaded home page. None means it could not be measured."""
    try:
        probe = await page.evaluate(
            _DESIGN_PROBE_JS, {"smallPx": _SMALL_TEXT_PX, "cta": _CTA_WORDS}
        )
        return probe if isinstance(probe, dict) else None
    except Exception as e:
        print(f"[Design] Probe failed (non-critical): {e}")
        return None

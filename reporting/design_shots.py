"""
Pictures for the designer's-view findings in the PDF report.

Each finding in analyzer/design_review.py is a measurement about the page
(too many typefaces, a skipped heading level, lines that run too wide). A
reader understands those far faster with the thing in front of them, so this
module goes back to the loaded page and shows exactly the elements the
measurement was about, in one of two ways:

  box      scroll to the first matching element and outline every match that is
           on screen with a labelled box (wide text lines, tiny text)
  collage  crop each matching element out of the page, label the crops and stack
           them (typefaces, headings, button shapes), because the matches are
           usually spread over the whole page and never share one screen

Nothing here judges anything. The elements are the same ones the measurement
counted, found with the same visibility rules, and every picture comes from the
browser, never from a model. A shot that cannot be made is simply absent and
the finding stays text-only, like the other text-only findings.
"""
import io

from PIL import Image, ImageDraw, ImageFont

from analyzer import design_review, visuals

_MARK = "data-mmga-d"
_OVERLAY_CLASS = "mmga-d-ov"
_COLOR = "#ff00ff"
_MAX_TILES = 6

_HELPERS = """
const shown = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
};
for (const e of document.querySelectorAll('[data-mmga-d]')) e.removeAttribute('data-mmga-d');
const marked = [];
const mark = (el, label) => { const id = String(marked.length); el.setAttribute('data-mmga-d', id); marked.push({id, label}); };
const clip = (t, n) => { t = (t || '').replace(/\\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '...' : t; };
"""


def _js(body: str) -> str:
    return "(cfg) => {" + _HELPERS + body + "\nreturn marked;}"


_HEADINGS_JS = _js("""
const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
    .filter(h => shown(h) && (h.textContent || '').trim());
const level = (h) => parseInt(h.tagName[1], 10);
if (cfg.mode === 'many_h1') {
    for (const h of heads.filter(h => level(h) === 1).slice(0, cfg.max)) mark(h, 'h1: ' + clip(h.textContent, 36));
} else {
    for (let i = 0; i + 1 < heads.length; i++) {
        if (level(heads[i + 1]) - level(heads[i]) > 1) {
            mark(heads[i], 'h' + level(heads[i]) + ': ' + clip(heads[i].textContent, 36));
            mark(heads[i + 1], 'h' + level(heads[i + 1]) + ': ' + clip(heads[i + 1].textContent, 36));
            break;
        }
    }
}
""")

_WIDE_JS = _js("""
const found = [];
for (const p of document.querySelectorAll('p')) {
    if (p.closest('nav, footer, header') || !shown(p)) continue;
    const text = (p.textContent || '').replace(/\\s+/g, ' ').trim();
    if (text.length < 120) continue;
    if (parseFloat(getComputedStyle(p).fontSize) < 13) continue;
    let overlay = false;
    for (let a = p; a && a !== document.body; a = a.parentElement) {
        const pos = getComputedStyle(a).position;
        if (pos === 'fixed' || pos === 'sticky') { overlay = true; break; }
    }
    if (overlay) continue;
    const range = document.createRange();
    range.selectNodeContents(p);
    const tops = new Set();
    for (const r of range.getClientRects()) if (r.width > 4) tops.add(Math.round(r.top / 4));
    if (tops.size >= 3) found.push({p, chars: Math.round(text.length / tops.size)});
}
found.sort((a, b) => b.chars - a.chars);
for (const f of found.slice(0, 2)) mark(f.p, 'about ' + f.chars + ' characters per line');
""")

_SMALL_JS = _js("""
const out = [];
for (const el of document.body.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH', 'IFRAME'].includes(el.tagName.toUpperCase())) continue;
    let text = '';
    for (const n of el.childNodes) if (n.nodeType === 3) text += n.textContent;
    text = text.replace(/\\s+/g, ' ').trim();
    if (text.length < 6 || !shown(el)) continue;
    const size = Math.round(parseFloat(getComputedStyle(el).fontSize) || 0);
    if (size > 0 && size < cfg.smallPx) out.push({el, size});
}
for (const o of out.slice(0, 6)) mark(o.el, o.size + 'px text');
""")

_FACES_JS = _js("""
const best = {};
for (const el of document.body.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH', 'IFRAME'].includes(el.tagName.toUpperCase())) continue;
    let text = '';
    for (const n of el.childNodes) if (n.nodeType === 3) text += n.textContent;
    text = text.replace(/\\s+/g, ' ').trim();
    if (text.length < 6 || !shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 14 || r.height > 160) continue;
    const family = ((getComputedStyle(el).fontFamily.split(',')[0]) || '').replace(/["']/g, '').trim();
    if (!best[family] || text.length > best[family].len) best[family] = {el, len: text.length};
}
for (const [family, b] of Object.entries(best)) { mark(b.el, family); }
""")

_BUTTONS_JS = _js("""
const seen = {};
const sel = 'button, [role=button], input[type=submit], input[type=button], a[class*=btn], a[class*=button], a[class*=cta]';
for (const b of document.querySelectorAll(sel)) {
    if (!shown(b) || b.closest('nav, footer')) continue;
    const r = b.getBoundingClientRect();
    if (r.width < 60 || r.height < 28 || (b.textContent || '').trim().length < 2) continue;
    const radius = parseFloat(getComputedStyle(b).borderTopLeftRadius) || 0;
    const bucket = radius < 4 ? 'square' : (radius >= r.height / 2 - 1 ? 'pill' : 'rounded');
    if (seen[bucket]) continue;
    seen[bucket] = true;
    mark(b, {square: 'Square corners', rounded: 'Rounded corners', pill: 'Pill shaped'}[bucket]);
}
""")

_DRAW_JS = """(payload) => {
    const vw = window.innerWidth, vh = window.innerHeight;
    let drawn = 0;
    for (const el of document.querySelectorAll('[data-mmga-d]')) {
        const r = el.getBoundingClientRect();
        if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
        const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
        const left = Math.max(r.left, 0), right = Math.min(r.right, vw);
        const box = document.createElement('div');
        box.className = payload.cls;
        box.style.cssText = ['position:fixed', 'left:' + (left - 3) + 'px', 'top:' + (top - 3) + 'px',
            'width:' + (right - left + 6) + 'px', 'height:' + (bottom - top + 6) + 'px',
            'border:3px solid ' + payload.color, 'border-radius:3px', 'box-sizing:border-box',
            'pointer-events:none', 'z-index:2147483647'].join(';');
        document.documentElement.appendChild(box);
        const label = document.createElement('div');
        label.className = payload.cls;
        label.textContent = payload.labels[el.getAttribute('data-mmga-d')] || '';
        const above = top - 3 >= 24;
        label.style.cssText = ['position:fixed', 'left:' + Math.max(0, left - 3) + 'px',
            'top:' + (above ? top - 3 - 22 : bottom + 4) + 'px', 'background:#111', 'color:#fff',
            'font:bold 12px Arial,sans-serif', 'padding:2px 8px', 'border-radius:2px',
            'pointer-events:none', 'z-index:2147483647', 'white-space:nowrap'].join(';');
        document.documentElement.appendChild(label);
        drawn++;
    }
    return drawn;
}"""


async def _clear(page) -> None:
    try:
        await page.evaluate(
            """(cls) => {
                for (const e of document.querySelectorAll('.' + cls)) e.remove();
                for (const e of document.querySelectorAll('[data-mmga-d]')) e.removeAttribute('data-mmga-d');
                window.scrollTo(0, 0);
            }""", _OVERLAY_CLASS)
    except Exception:
        pass


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def build_collage(tiles: list[tuple[str, bytes]], width: int = 960) -> bytes | None:
    """Stack labelled crops into one JPEG. Pure, so it is tested without a browser."""
    prepared: list[tuple[str, Image.Image]] = []
    for label, data in tiles:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            continue
        inner = width - 48
        if img.width > inner:
            img = img.resize((inner, max(1, round(img.height * inner / img.width))))
        if img.height > 110:                       # a crop is a sample, not the whole block
            img = img.crop((0, 0, img.width, 170))
        prepared.append((label, img))
    if not prepared:
        return None
    pad, strip = 14, 30
    height = pad + sum(strip + img.height + pad for _, img in prepared)
    canvas = Image.new("RGB", (width, height), "#fff8ef")
    draw = ImageDraw.Draw(canvas)
    font = _font(18)
    y = pad
    for label, img in prepared:
        draw.text((24, y + 4), label, fill="#d9480f", font=font)
        y += strip
        draw.rectangle((23, y - 1, 24 + img.width, y + img.height), fill="#ffffff", outline="#f1e3cf")
        canvas.paste(img, (24, y))
        y += img.height + pad
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=84)
    return out.getvalue()


async def _marked(page, js: str, cfg: dict) -> list[dict]:
    result = await page.evaluate(js, cfg)
    return result if isinstance(result, list) else []


async def _crop_tiles(page, marked: list[dict]) -> list[tuple[str, bytes]]:
    tiles = []
    for m in marked[:_MAX_TILES]:
        try:
            loc = page.locator(f'[{_MARK}="{m["id"]}"]').first
            await loc.scroll_into_view_if_needed(timeout=3000)
            png = await loc.screenshot(animations="disabled", caret="hide", timeout=5000)
            tiles.append((m["label"], png))
        except Exception as e:
            print(f"[Report] crop skipped (non-critical): {e}")
    return tiles


async def _box_shot(page, marked: list[dict]) -> bytes | None:
    if not marked:
        return None
    await page.evaluate(
        "(id) => { const e = document.querySelector('[data-mmga-d=\"' + id + '\"]');"
        " if (e) e.scrollIntoView({block: 'center'}); }", marked[0]["id"])
    await page.wait_for_timeout(500)
    drawn = await page.evaluate(_DRAW_JS, {
        "cls": _OVERLAY_CLASS, "color": _COLOR, "labels": {m["id"]: m["label"] for m in marked}})
    if not drawn:
        return None
    png = await visuals._capture(page)
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, format="JPEG", quality=82)
    return out.getvalue()


async def _faces(page, probe: dict) -> bytes | None:
    marked = await _marked(page, _FACES_JS, {})
    faces = design_review._typefaces(probe.get("fonts") or {}, int(probe.get("chars") or 0))
    by_base = {}
    for m in marked:
        by_base.setdefault(design_review._base_family(m["label"]).lower(), m)
    picked = []
    for name in faces[:_MAX_TILES]:
        m = by_base.get(name.lower())
        if m:
            picked.append({"id": m["id"], "label": name})
    return build_collage(await _crop_tiles(page, picked)) if picked else None


async def capture(page, probe: dict | None, findings: list[dict]) -> dict[str, dict]:
    """key -> {"image": jpeg bytes, "caption": str} for the findings that can be shown."""
    shots: dict[str, dict] = {}
    keys = {f["key"] for f in findings}
    jobs = []
    if "many_main_headings" in keys:
        jobs.append(("many_main_headings", "collage", _HEADINGS_JS, {"mode": "many_h1", "max": _MAX_TILES},
                     "Each strip is one of the page's main headings (h1), cut out of the page."))
    if "skipped_heading_level" in keys:
        jobs.append(("skipped_heading_level", "collage", _HEADINGS_JS, {"mode": "skip"},
                     "The two headings where the order jumps, cut out of the page."))
    if "wide_text_lines" in keys:
        jobs.append(("wide_text_lines", "box", _WIDE_JS, {},
                     "The text blocks with the longest lines, boxed on the page."))
    if "small_text" in keys:
        jobs.append(("small_text", "box", _SMALL_JS, {"smallPx": design_review._SMALL_TEXT_PX},
                     "Some of the smallest text, boxed on the page."))
    if "button_shapes" in keys:
        jobs.append(("button_shapes", "collage", _BUTTONS_JS, {},
                     "One button of each corner style found on the page."))
    for key, mode, js, cfg, caption in jobs:
        try:
            marked = await _marked(page, js, cfg)
            image = (build_collage(await _crop_tiles(page, marked)) if mode == "collage"
                     else await _box_shot(page, marked))
            if image:
                shots[key] = {"image": image, "caption": caption}
        except Exception as e:
            print(f"[Report] design picture skipped for {key} (non-critical): {e}")
        finally:
            await _clear(page)
    if "many_typefaces" in keys and probe:
        try:
            image = await _faces(page, probe)
            if image:
                shots["many_typefaces"] = {
                    "image": image,
                    "caption": "A line of text in each typeface the page uses, cut out of the page."}
        except Exception as e:
            print(f"[Report] design picture skipped for many_typefaces (non-critical): {e}")
        finally:
            await _clear(page)
    return shots

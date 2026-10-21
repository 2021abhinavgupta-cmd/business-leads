"""Option B ("warm light") email renderer.

Table based, inline styles only, no remote resources. The sender inserts the
footer at the literal marker <!--FOOTER-->. Pure function: no network, no DB.
"""
from html import escape
from urllib.parse import quote

from PIL import Image

FOOTER_MARKER = "<!--FOOTER-->"
IMG_WIDTH = 520
FALLBACK_HEIGHT = 325
MAX_SECTIONS = 5

CREAM = "#fff8ef"
INK = "#2a1f14"
ACCENT = "#d9480f"
PANEL = "#f1e3cf"
LIGHT = "#fffdf9"
FONT = "Arial,Helvetica,sans-serif"

AUTOMATION_NOTE = (
    "This email, its screenshots and the review were all produced by automation "
    "MMGA built. MMGA builds the same for compliance, finance and operations teams."
)
BUTTON_TEXT = "Want the full review? Reply yes"


def _e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _img_height(path) -> int:
    try:
        with Image.open(path) as im:
            w, h = im.size
        if w <= 0 or h <= 0:
            return FALLBACK_HEIGHT
        return max(1, round(h * IMG_WIDTH / w))
    except Exception:
        return FALLBACK_HEIGHT


def _section(n: int, section: dict, shot: dict) -> str:
    raw_title = (section.get("title") or "").strip()
    title = _e(raw_title)
    alt = _e(raw_title or f"Screenshot {n}")
    sentence = _e(section.get("sentence"))
    height = _img_height((shot or {}).get("path"))
    return (
        f'<tr><td bgcolor="{CREAM}" style="background-color:{CREAM};padding:8px 40px 24px 40px;">'
        f'<p style="margin:0 0 6px 0;font-family:{FONT};font-size:12px;font-weight:bold;'
        f'letter-spacing:1px;color:{ACCENT};">{n}</p>'
        f'<h2 style="margin:0 0 8px 0;font-family:{FONT};font-size:19px;line-height:25px;color:{INK};">{title}</h2>'
        f'<p style="margin:0 0 14px 0;font-family:{FONT};font-size:15px;line-height:23px;color:{INK};">{sentence}</p>'
        f'<img src="cid:flaw{n}" alt="{alt}" width="{IMG_WIDTH}" height="{height}" '
        f'style="display:block;border:0;">'
        f'</td></tr>'
    )


def _mailto(reply_to: str, subject: str) -> str:
    parts = (reply_to or "").strip().split()
    address = parts[0].split("?")[0] if parts else ""
    if not address:
        return ""
    return f"mailto:{quote(address, safe='@+.-_')}?subject={quote('Re: ' + subject, safe='')}"


def _button(reply_to: str, subject: str) -> str:
    label = _e(BUTTON_TEXT)
    text_style = (
        f'font-family:{FONT};font-size:15px;font-weight:bold;color:{LIGHT};text-decoration:none;'
    )
    href = _mailto(reply_to, subject)
    if href:
        inner = (
            f'<a href="{_e(href)}" style="display:inline-block;background-color:{ACCENT};'
            f'{text_style}">{label}</a>'
        )
    else:
        inner = f'<span style="{text_style}">{label}</span>'
    # Bulletproof cell button: padding and background live on the td so Outlook
    # (Word engine) does not shrink it.
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{ACCENT}" style="background-color:{ACCENT};border-radius:6px;'
        f'padding:13px 26px;">{inner}</td>'
        f'</tr></table>'
    )


def render(copy: dict, shots: list[dict], *, first_name: str, company: str,
           reply_to: str) -> tuple[str, str]:
    subject = copy.get("subject") or ""
    headline = copy.get("headline") or ""
    opening = copy.get("opening") or ""
    sections = copy.get("sections") or []
    href_ok = bool(_mailto(reply_to, subject))
    greeting = f"Hi {first_name.strip()}," if (first_name or "").strip() else "Hi there,"
    company_tag = (company or "").strip()
    tag = f"A quick look at {company_tag}" if company_tag else "A quick look at your website"

    if not 1 <= len(shots) <= MAX_SECTIONS:
        raise ValueError(f"expected 1 to {MAX_SECTIONS} shots, got {len(shots)}")
    if len(sections) != len(shots):
        raise ValueError(f"{len(sections)} sections but {len(shots)} shots")
    for i, section in enumerate(sections):
        if section.get("index") != i:
            raise ValueError(f"section {i} has index {section.get('index')!r}, expected {i}")

    rows = [_section(i + 1, section, shots[i]) for i, section in enumerate(sections)]

    html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_e(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background-color:{CREAM};" bgcolor="{CREAM}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{CREAM}" style="background-color:{CREAM};"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{CREAM}" style="width:600px;max-width:600px;background-color:{CREAM};">'
        f'<tr><td bgcolor="{CREAM}" style="background-color:{CREAM};padding:32px 40px 12px 40px;">'
        f'<p style="margin:0 0 10px 0;font-family:{FONT};font-size:12px;font-weight:bold;'
        f'letter-spacing:1px;text-transform:uppercase;color:{ACCENT};">{_e(tag)}</p>'
        f'<h1 style="margin:0 0 16px 0;font-family:{FONT};font-size:26px;line-height:32px;color:{INK};">{_e(headline)}</h1>'
        f'<p style="margin:0 0 10px 0;font-family:{FONT};font-size:15px;line-height:23px;color:{INK};">{_e(greeting)}</p>'
        f'<p style="margin:0;font-family:{FONT};font-size:15px;line-height:23px;color:{INK};">{_e(opening)}</p>'
        f'</td></tr>'
        + "".join(rows)
        + f'<tr><td bgcolor="{CREAM}" style="background-color:{CREAM};padding:8px 40px 28px 40px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="{INK}" style="background-color:{INK};border-radius:8px;padding:24px 28px;">'
        f'<p style="margin:0 0 8px 0;font-family:{FONT};font-size:12px;font-weight:bold;'
        f'letter-spacing:1px;text-transform:uppercase;color:{ACCENT};">One more thing</p>'
        f'<p style="margin:0 0 18px 0;font-family:{FONT};font-size:15px;line-height:23px;color:{CREAM};">'
        f'{_e(AUTOMATION_NOTE)}</p>'
        + _button(reply_to, subject)
        + '</td></tr></table></td></tr>'
        f'<tr><td bgcolor="{PANEL}" style="background-color:{PANEL};padding:20px 40px;'
        f'font-family:{FONT};font-size:12px;line-height:18px;color:{INK};">'
        f'{FOOTER_MARKER}</td></tr>'
        '</table></td></tr></table></body></html>'
    )

    reply_text = "Want the full review? Just reply yes." if href_ok else (
        "Want the full review? Just reply to this email."
    )
    lines = [headline, "", greeting, "", opening, ""]
    for i, section in enumerate(sections):
        lines += [f"{i + 1}. {section.get('title') or ''}", section.get("sentence") or "", ""]
    lines += [
        "The screenshots are in the HTML version of this email.",
        "",
        AUTOMATION_NOTE,
        "",
        reply_text,
    ]
    return html, "\n".join(lines)

"""
The warm-light theme (same palette as the Import designed email) for ordinary
first-touch emails: the reviewed body text laid out in the theme, with any
audit screenshots in a panel underneath. Pure function: no network, no DB.

The text the reviewer edited stays the source of truth. This only changes how
it is drawn, so editing a draft in the dashboard still changes the email.
The footer is inserted by the sender at FOOTER_MARKER, like the designed email.
"""
from html import escape

from importer.email_html import ACCENT, CREAM, FONT, FOOTER_MARKER, INK, PANEL

IMG_MAX_WIDTH = 520


def _e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _paragraphs(body: str) -> str:
    """Blank-line separated blocks become paragraphs; single newlines stay breaks."""
    blocks = [b for b in (body or "").replace("\r\n", "\n").strip().split("\n\n") if b.strip()]
    out = []
    for i, block in enumerate(blocks):
        lines = "<br>".join(_e(line.strip()) for line in block.split("\n") if line.strip())
        bottom = "0" if i == len(blocks) - 1 else "14px"
        out.append(
            f'<p style="margin:0 0 {bottom} 0;font-family:{FONT};font-size:15px;'
            f'line-height:24px;color:{INK};">{lines}</p>'
        )
    return "".join(out)


def _evidence(images: list[tuple[str, str]]) -> str:
    """images: (content-id, caption) for each capture that really exists."""
    if not images:
        return ""
    figures = "".join(
        f'<p style="margin:16px 0 8px 0;font-family:{FONT};font-size:14px;line-height:21px;'
        f'color:{INK};">{_e(caption)}</p>'
        f"<img src='cid:{_e(cid)}' alt='Website audit screenshot' width='{IMG_MAX_WIDTH}' "
        f"style='display:block;border:0;width:100%;max-width:{IMG_MAX_WIDTH}px;height:auto;"
        f"border-radius:6px;'>"
        for cid, caption in images
    )
    return (
        f'<tr><td bgcolor="{CREAM}" style="background-color:{CREAM};padding:4px 40px 24px 40px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{PANEL}" style="background-color:{PANEL};border-radius:8px;padding:20px 24px;">'
        f'<p style="margin:0;font-family:{FONT};font-size:12px;font-weight:bold;letter-spacing:1px;'
        f'text-transform:uppercase;color:{ACCENT};">What we saw on your site</p>'
        f'{figures}</td></tr></table></td></tr>'
    )


def render(subject: str, body: str, images: list[tuple[str, str]] | None = None,
           pixel: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_e(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background-color:{CREAM};" bgcolor="{CREAM}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{CREAM}" style="background-color:{CREAM};"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{CREAM}" style="width:600px;max-width:600px;background-color:{CREAM};">'
        f'<tr><td bgcolor="{CREAM}" style="background-color:{CREAM};padding:32px 40px 8px 40px;">'
        f'<p style="margin:0 0 18px 0;font-family:{FONT};font-size:12px;font-weight:bold;'
        f'letter-spacing:1px;text-transform:uppercase;color:{ACCENT};">MMGA</p>'
        f'{_paragraphs(body)}</td></tr>'
        + _evidence(images or [])
        + f'<tr><td bgcolor="{PANEL}" style="background-color:{PANEL};padding:20px 40px;'
        f'font-family:{FONT};font-size:12px;line-height:18px;color:{INK};">{FOOTER_MARKER}</td></tr>'
        f'</table>{pixel}</td></tr></table></body></html>'
    )

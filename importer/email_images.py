"""Prepare screenshots for embedding in the designed email.

Pure functions: no network, no database. Images are only ever scaled, never
cropped, because the magenta box that marks the flaw could sit anywhere.
"""
import io
import os

from PIL import Image, ImageChops

MAX_HEIGHT = 1500
START_QUALITY = 70
QUALITY_FLOOR = 40
QUALITY_STEP = 5


CREAM_RGB = (255, 248, 239)  # #fff8ef, the email page color
WIDTH_FALLBACKS = (520, 460)

# The flaw is marked by a 4px #ff00ff border drawn at capture size (1280 wide
# on desktop). Scaled for the email that border is about 1.9px at 600 wide and
# 1.6px at 520, and resampling plus JPEG blur it further, so a small box can
# all but vanish. A pixel counts as box when it is still strongly magenta.
# Measured on prepared 600 wide images of a 1280 wide page (white, black, grey,
# cream and noisy backgrounds): the smallest box the capture draws (a 20px
# square) keeps 12 to 40 such pixels, a 40x24 box 60 to 117, a 60x30 box 61
# to 161, a 200x60 box 220 or more, a page without a box 0; a mobile capture
# (780 wide, 8px border) keeps over 500. Below MIN_BOX_PIXELS (about a 9px
# square outline at email size) the box is too small to read as a marker.
BOX_RB_MIN = 170
BOX_G_MAX = 100
MIN_BOX_PIXELS = 30


class BoxNotVisibleError(ValueError):
    """The magenta box is too small to see once the image is scaled for email."""


def magenta_pixel_count(data: bytes) -> int:
    """How many pixels of the (prepared) image are strongly magenta."""
    with Image.open(io.BytesIO(data)) as im:
        r, g, b = im.convert("RGB").split()
    high_r = r.point(lambda v: 255 if v >= BOX_RB_MIN else 0)
    high_b = b.point(lambda v: 255 if v >= BOX_RB_MIN else 0)
    low_g = g.point(lambda v: 255 if v <= BOX_G_MAX else 0)
    mask = ImageChops.multiply(ImageChops.multiply(high_r, high_b), low_g)
    return mask.histogram()[255]


def _flatten(img: Image.Image) -> Image.Image:
    """Composite any transparency onto the cream page color, then to RGB."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if not has_alpha:
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    page = Image.new("RGBA", rgba.size, CREAM_RGB + (255,))
    return Image.alpha_composite(page, rgba).convert("RGB")


def _scaled(img: Image.Image, width: int) -> Image.Image:
    w, h = img.size
    new_w = min(width, w)
    new_h = round(h * new_w / w)
    if new_h > MAX_HEIGHT:
        new_h = MAX_HEIGHT
        new_w = max(1, round(w * MAX_HEIGHT / h))
    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, max(1, new_h)), Image.LANCZOS)
    return img


def _encode(img: Image.Image, max_bytes: int) -> bytes:
    quality = START_QUALITY
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or quality <= QUALITY_FLOOR:
            return data
        quality -= QUALITY_STEP


def prepare(path: str, *, width: int = 600, max_bytes: int = 90_000, require_box: bool = True) -> bytes:
    """The JPEG for the email. Raises FileNotFoundError, and BoxNotVisibleError
    when require_box is set and the prepared image keeps fewer than
    MIN_BOX_PIXELS magenta box pixels."""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    return prepare_bytes(raw, name=path, width=width, max_bytes=max_bytes, require_box=require_box)


def prepare_bytes(raw: bytes, *, name: str = "", width: int = 600, max_bytes: int = 90_000,
                  require_box: bool = True) -> bytes:
    """prepare() for image bytes already read (and hash checked) by the caller,
    so the bytes checked are the bytes sent. *name* only labels messages."""
    path = name or "image"
    with Image.open(io.BytesIO(raw)) as src:
        img = _flatten(src)

    data = _encode(_scaled(img, width), max_bytes)
    if len(data) > max_bytes:
        for fallback in WIDTH_FALLBACKS:
            if fallback >= width:
                continue
            data = _encode(_scaled(img, fallback), max_bytes)
            if len(data) <= max_bytes:
                break
    if len(data) > max_bytes:
        print(f"[email_images] {path} is {len(data)} bytes after compression, over {max_bytes}")
    if require_box:
        found = magenta_pixel_count(data)
        if found < MIN_BOX_PIXELS:
            # A small box vanishes when the whole page is shrunk to email width.
            # Zoom in on it instead: crop around the box at full resolution so its
            # border keeps its thickness. Deterministic, so drafting and sending
            # produce the same bytes.
            zoomed = _zoom_on_box(img, width, max_bytes)
            if zoomed is not None and magenta_pixel_count(zoomed) >= MIN_BOX_PIXELS:
                return zoomed
            raise BoxNotVisibleError(
                f"{path}: the magenta box keeps {found} visible pixels at email size, "
                f"fewer than {MIN_BOX_PIXELS}"
            )
    return data


ZOOM_CROP_WIDTH = 640
ZOOM_CROP_HEIGHT = 420


def _zoom_on_box(img: Image.Image, width: int, max_bytes: int) -> bytes | None:
    """The page cropped around its magenta box, or None if no box can be found."""
    r, g, b = img.split()
    mask = ImageChops.multiply(
        ImageChops.multiply(r.point(lambda v: 255 if v >= BOX_RB_MIN else 0),
                            b.point(lambda v: 255 if v >= BOX_RB_MIN else 0)),
        g.point(lambda v: 255 if v <= BOX_G_MAX else 0),
    )
    box = mask.getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    w, h = img.size
    if (right - left) * (bottom - top) > 0.5 * w * h:
        return None  # stray magenta all over the page, not one marker
    cw, ch = min(w, ZOOM_CROP_WIDTH), min(h, ZOOM_CROP_HEIGHT)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    x0 = max(0, min(cx - cw // 2, w - cw))
    y0 = max(0, min(cy - ch // 2, h - ch))
    return _encode(_scaled(img.crop((x0, y0, x0 + cw, y0 + ch)), width), max_bytes)


def prepare_all(shots: list[dict], sources: list[bytes] | None = None) -> list[tuple[str, bytes]]:
    """(cid, jpeg bytes) for each shot's main image, in order. Never the closeup.
    With *sources* (each shot's file bytes, as drafting.read_intact returned
    them) the files are not read again."""
    if sources is None:
        return [(f"flaw{n}", prepare(shot["path"])) for n, shot in enumerate(shots, 1)]
    if len(sources) != len(shots):
        raise ValueError("one source per shot is required")
    return [(f"flaw{n}", prepare_bytes(raw, name=str(shot.get("path") or "")))
            for n, (shot, raw) in enumerate(zip(shots, sources), 1)]

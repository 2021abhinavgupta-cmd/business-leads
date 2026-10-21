"""Prepare screenshots for embedding in the designed email.

Pure functions: no network, no database. Images are only ever scaled, never
cropped, because the magenta box that marks the flaw could sit anywhere.
"""
import io
import os

from PIL import Image

MAX_HEIGHT = 1500
START_QUALITY = 70
QUALITY_FLOOR = 40
QUALITY_STEP = 5


CREAM_RGB = (255, 248, 239)  # #fff8ef, the email page color
WIDTH_FALLBACKS = (520, 460)


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


def prepare(path: str, *, width: int = 600, max_bytes: int = 90_000) -> bytes:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    with Image.open(path) as src:
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
    return data


def prepare_all(shots: list[dict]) -> list[tuple[str, bytes]]:
    """(cid, jpeg bytes) for each shot's main image, in order. Never the closeup."""
    return [(f"flaw{n}", prepare(shot["path"])) for n, shot in enumerate(shots, 1)]

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


def prepare(path: str, *, width: int = 600, max_bytes: int = 90_000) -> bytes:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    with Image.open(path) as src:
        img = src.convert("RGB")

    w, h = img.size
    new_w = min(width, w)
    new_h = round(h * new_w / w)
    if new_h > MAX_HEIGHT:
        new_h = MAX_HEIGHT
        new_w = max(1, round(w * MAX_HEIGHT / h))
    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, max(1, new_h)), Image.LANCZOS)

    quality = START_QUALITY
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or quality <= QUALITY_FLOOR:
            return data
        quality -= QUALITY_STEP


def prepare_all(shots: list[dict]) -> list[tuple[str, bytes]]:
    """(cid, jpeg bytes) for each shot's main image, in order. Never the closeup."""
    return [(f"flaw{n}", prepare(shot["path"])) for n, shot in enumerate(shots, 1)]

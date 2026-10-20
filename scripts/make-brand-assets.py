#!/usr/bin/env python3
"""Regenerate brand images from assets/src/poster.jpg (Edwin's poster).

Outputs into assets/static/img/:
  poster-{480,800,1200}.{avif,webp}  hero art at responsive sizes
  poster-1200.jpg                    fallback for very old browsers
  og.jpg                             1200x630 crop for Open Graph / social cards
  favicon.png (64), favicon-32.png, apple-touch-icon.png (180)  drawn record glyph

Needs Pillow >= 11 (AVIF support). Run once after replacing the poster; the
outputs are committed so the normal build has no image dependencies.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets/src/poster.jpg"
OUT = ROOT / "assets/static/img"
OUT.mkdir(parents=True, exist_ok=True)

im = Image.open(SRC).convert("RGB")
W, H = im.size

for size in (480, 800, 1200):
    r = im.resize((size, round(H * size / W)), Image.LANCZOS)
    r.save(OUT / f"poster-{size}.webp", "WEBP", quality=64, method=6)
    r.save(OUT / f"poster-{size}.avif", "AVIF", quality=46, speed=4)
    if size == 1200:
        r.save(OUT / "poster-1200.jpg", "JPEG", quality=80, optimize=True, progressive=True)

# OG crop: the face, the record and the top of the mug (1.905:1)
crop_h = round(W / (1200 / 630))
top = 150
og = im.crop((0, top, W, top + crop_h)).resize((1200, 630), Image.LANCZOS)
og.save(OUT / "og.jpg", "JPEG", quality=74, optimize=True, progressive=True)


def record_icon(px: int) -> Image.Image:
    """Charcoal record with a mustard label on a cream rounded square."""
    s = px * 4  # supersample
    ic = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(ic)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(244, 236, 216, 255))
    pad = s // 10
    d.ellipse((pad, pad, s - pad, s - pad), fill=(34, 29, 24, 255))
    for k in (0.36, 0.30, 0.24):  # groove rings
        r = round(s * k)
        c = s // 2
        d.ellipse((c - r, c - r, c + r, c + r), outline=(70, 60, 50, 255), width=max(1, s // 90))
    c, r = s // 2, round(s * 0.15)
    d.ellipse((c - r, c - r, c + r, c + r), fill=(201, 147, 47, 255))
    r = max(2, round(s * 0.03))
    d.ellipse((c - r, c - r, c + r, c + r), fill=(34, 29, 24, 255))
    return ic.resize((px, px), Image.LANCZOS)


record_icon(64).save(OUT / "favicon.png")
record_icon(32).save(OUT / "favicon-32.png")
record_icon(180).convert("RGB").save(OUT / "apple-touch-icon.png")
print("wrote", sorted(p.name for p in OUT.iterdir()))

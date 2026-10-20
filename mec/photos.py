"""Self-hosted WebP copies of Edwin's own eBay photos (lead image per record).

Cards and the main record-page image use these local files instead of
hotlinking i.ebayimg.com: faster, cacheable, and they keep working if eBay
changes its image URLs. Extra gallery photos still link to eBay's CDN.

Cache: data/enrichment/photos.json {item_id: {url, files, fetched_at}}; images in
data/assets/photos/ (gitignored: regenerated from eBay on demand, ~50 KB/record).
Only the lead image URL is tracked; when Edwin changes it, the file is replaced.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import urllib.request

from .util import DATA, load_json, now_iso, write_json

CACHE = DATA / "enrichment" / "photos.json"
ASSETS = DATA / "assets" / "photos"
SIZES = {"thumb": 300, "large": 720}
UA = "MusicEdCafe/1.0 ( sammcgrail@gmail.com )"


def _fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if not (r.headers.get("Content-Type") or "").startswith("image/"):
                    return None
                return r.read(10_000_000)
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def run(records: list[dict], log=print) -> dict:
    from PIL import Image
    cache = load_json(CACHE, {}) or {}
    ASSETS.mkdir(parents=True, exist_ok=True)
    stats = {"fetched": 0, "cached": 0, "failed": 0}
    wanted = set()
    for r in records:
        if not r.get("ebay_images"):
            continue
        url = r["ebay_images"][0]
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        e = cache.get(r["id"]) or {}
        files_ok = e.get("url") == url and all((ASSETS / f).exists() for f in (e.get("files") or {}).values()
                                              if isinstance(f, str))
        if files_ok:
            stats["cached"] += 1
            wanted |= {f for f in e["files"].values() if isinstance(f, str)}
            continue
        src = re.sub(r"/s-l\d+\.(jpg|jpeg|png|webp)$", "/s-l1600.jpg", url)
        data = _fetch(src) or _fetch(url)
        if not data:
            stats["failed"] += 1
            log(f"{r['id']}: could not fetch {src}")
            continue
        im = Image.open(io.BytesIO(data)).convert("RGB")
        files = {}
        for name, px in SIZES.items():
            c = im.copy()
            c.thumbnail((px, px), Image.LANCZOS)
            fn = f"{key}-{px}.webp"
            c.save(ASSETS / fn, "WEBP", quality=70 if px < 400 else 74, method=5)
            files[name], files[f"{name}_w"], files[f"{name}_h"] = fn, c.size[0], c.size[1]
        cache[r["id"]] = {"url": url, "files": files, "fetched_at": now_iso()}
        wanted |= {fn for fn in files.values() if isinstance(fn, str)}
        stats["fetched"] += 1
        time.sleep(0.15)
    for p in ASSETS.glob("*.webp"):  # drop files for replaced photos
        if p.name not in wanted:
            p.unlink()
    write_json(CACHE, cache)
    return stats

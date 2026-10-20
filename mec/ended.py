"""Edwin's own photos for SOLD / ended listings, via Browse getItem by legacy id.

Browse search only lists active items, but `get_item_by_legacy_id` with the
plain application token still answers for many recently ended listings
(verified 2026-10-20: items that ended in late July 2026 and later came back
200 with images; some earlier ones return 404 errorId 11003). eBay purges ended
listings from Browse after a while, so the answer is cached permanently here:

  data/enrichment/ended_listings.json
    {item_id: {"status": "found" | "not_found" | "other_seller" | "error",
               "images": [...], "title", "seller", "item_end_date",
               "error", "checked_at"}}

Committed to git on purpose: once eBay forgets an ended listing this file is the
only copy of its image URLs. A "found" entry is never re-fetched or downgraded;
"not_found" / "error" entries are retried only with --retry.

merge.py uses these images only when the record has no other eBay photo
(api / public page), and before editorial images, stock art and placeholder.
"""
from __future__ import annotations

import time
import urllib.parse

from . import browse
from .util import DATA, load_json, now_iso, write_json

CACHE = DATA / "enrichment" / "ended_listings.json"
SOURCE_LABEL = "eBay listing photo (Browse API lookup of the ended listing)"


def load() -> dict:
    return load_json(CACHE, {}) or {}


def run(ids: list[str], seller: str, retry: bool = False, spacing: float = 1.0,
        marketplace: str = "EBAY_US", log=print) -> dict:
    creds = browse.credentials()
    if not creds:
        return {"result": "skipped", "reason": "no credentials"}
    token = browse.get_token(*creds)
    h = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace,
         "Accept": "application/json"}
    cache = load()
    stats = {"found": 0, "not_found": 0, "other_seller": 0, "error": 0, "cached": 0}
    for iid in ids:
        e = cache.get(iid) or {}
        if e.get("status") == "found" or (e and not retry):
            stats["cached"] += 1
            continue
        url = (f"{browse.API}/buy/browse/v1/item/get_item_by_legacy_id?"
               + urllib.parse.urlencode({"legacy_item_id": iid}))
        entry = {"checked_at": now_iso()}
        try:
            full = browse._req("GET", url, h)
        except browse.BrowseError as ex:
            msg = str(ex)
            entry["status"] = "not_found" if ("HTTP 404" in msg or "11003" in msg) else "error"
            entry["error"] = msg[:300]
        else:
            sel = ((full.get("seller") or {}).get("username") or "")
            imgs = [x.get("imageUrl") for x in [full.get("image") or {}] + (full.get("additionalImages") or [])
                    if x.get("imageUrl")]
            entry.update({"title": full.get("title"), "seller": sel,
                          "item_end_date": full.get("itemEndDate"), "images": imgs})
            entry["status"] = "found" if sel.lower() == seller.lower() else "other_seller"
        cache[iid] = entry
        stats[entry["status"]] += 1
        log(f"{iid}: {entry['status']} {len(entry.get('images') or [])} images")
        write_json(CACHE, cache)
        time.sleep(spacing)
    write_json(CACHE, cache)
    return {"result": "ok", **stats}


def images_for(cache: dict, iid: str) -> list[str]:
    e = cache.get(iid) or {}
    return list(e.get("images") or []) if e.get("status") == "found" else []

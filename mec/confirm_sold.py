"""Confirm eBay sales for records the sync could only see as "unavailable" / "ended".

Browse search drops a listing once it sells, and the sync's direct lookup of a
known-active id then sees it "listed with 0 available" (status "unavailable"),
or a complete snapshot no longer contains it (status "ended"). Neither says it
SOLD. This step asks eBay directly with Browse get_item_by_legacy_id (same
application token and client as mec/ended.py). Verified live 2026-10-21: a sold
listing answers 200 with

    estimatedAvailabilities[0].estimatedSoldQuantity >= 1
    estimatedAvailabilities[0].estimatedAvailabilityStatus  OUT_OF_STOCK
    itemEndDate

Rules:
  * sold quantity >= 1 AND available quantity 0  -> status "sold" (status_source
    "ebay_api"), via store.apply_sales, which records a `sales` entry shaped like
    the CSV path and goes through ChangeSet.set_status, so the Discogs listing
    sync then deletes the record's Discogs listing like any other eBay sale.
    sold_at = itemEndDate's date (if not in the future), else the date the sync
    first saw quantity 0 (status_changed_at of an "unavailable" record), else today.
    Sold price = the item's price.
  * sold quantity 0 (Edwin ended it himself) -> left as it is.
  * lookup fails / 404 -> nothing changes; retried at the next sync.
  Every lookup that does not confirm a sale counts one try in the record's
  `sold_check`; after MAX_TRIES the record is no longer looked up, until its
  status changes again (a relist then a new end starts a fresh count).

Images from a successful lookup are merged into data/enrichment/ended_listings.json
(only when that id has no "found" entry yet: precedence and the never-downgrade
rule of mec/ended.py are unchanged), so sold records keep Edwin's photos.

The result goes to sync-state.json under "last_sold_confirm" only. It never
calls store.record_attempt, so the eBay sync's last_result / complete_snapshot
(which gate Discogs deletes) are untouched.
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone

from . import browse, ended, store
from .util import SYNC_STATE, load_json, now_iso, write_json

SOURCE = "ebay_api"
MAX_TRIES = 5
STATUSES = ("unavailable", "ended")


def candidates(items: dict[str, dict]) -> list[str]:
    """Unavailable / ended records with no confirmed sale and tries left."""
    out = []
    for iid, r in sorted(items.items()):
        if r.get("status") not in STATUSES or r.get("discogs_sale") or r.get("sales"):
            continue
        chk = r.get("sold_check") or {}
        if chk.get("for_change") == r.get("status_changed_at") and \
                (chk.get("tries", 0) >= MAX_TRIES or chk.get("result") == "not_sold_final"):
            continue
        out.append(iid)
    return out


def _availability(full: dict) -> tuple[int, int | None]:
    """(sold quantity, available quantity) from a getItem response."""
    av = (full.get("estimatedAvailabilities") or [{}])[0] or {}
    sold = int(av.get("estimatedSoldQuantity") or 0)
    avail = av.get("estimatedAvailableQuantity")
    if av.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
        avail = 0
    return sold, (int(avail) if avail is not None else None)


def _sold_date(full: dict, rec: dict, today: str) -> tuple[str, str]:
    end = (full.get("itemEndDate") or "")[:10]
    if end and end <= today:
        return end, "itemEndDate"
    if rec.get("status") == "unavailable" and rec.get("status_changed_at"):
        return rec["status_changed_at"][:10], "first sync that saw quantity 0"
    return today, "date of this check"


def _note(rec: dict, result: str, **kw) -> dict:
    chk = rec.get("sold_check") or {}
    if chk.get("for_change") != rec.get("status_changed_at"):
        chk = {"for_change": rec.get("status_changed_at"), "tries": 0}
    chk.update(kw, result=result, checked_at=now_iso())
    if result != "sold":
        chk["tries"] = chk.get("tries", 0) + 1
    rec["sold_check"] = chk
    return chk


def run(seller: str, ids: list[str] | None = None, marketplace: str = "EBAY_US", token: str | None = None,
        spacing: float = 0.5, dry_run: bool = False, log=print) -> dict:
    cs = store.ChangeSet()
    todo = candidates(cs.items) if ids is None else [i for i in ids if i in cs.items
                                                     and cs.items[i].get("status") != "sold"]
    stats = {"checked": 0, "sold": 0, "not_sold": 0, "not_found": 0, "error": 0, "other_seller": 0,
             "candidates": len(todo), "sold_ids": []}
    if not todo:
        return {"result": "ok", **stats}
    if token is None:
        creds = browse.credentials()
        if not creds:
            return {"result": "skipped", "reason": "no credentials", **stats}
        token = browse.get_token(*creds)
    h = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace,
         "Accept": "application/json"}
    today = datetime.now(timezone.utc).date().isoformat()
    cache = ended.load()
    cache_dirty = False
    sales = []
    for n, iid in enumerate(todo):
        rec = cs.items[iid]
        if n:
            time.sleep(spacing)
        url = (f"{browse.API}/buy/browse/v1/item/get_item_by_legacy_id?"
               + urllib.parse.urlencode({"legacy_item_id": iid}))
        stats["checked"] += 1
        try:
            full = browse._req("GET", url, h)
        except browse.BrowseError as ex:
            msg = str(ex)
            kind = "not_found" if ("HTTP 404" in msg or "11003" in msg) else "error"
            stats[kind] += 1
            _note(rec, kind, error=msg[:200])
            cs.touch(iid)
            log(f"{iid}: lookup {kind} (try {rec['sold_check']['tries']}/{MAX_TRIES}); nothing changed")
            continue
        sel = ((full.get("seller") or {}).get("username") or "")
        if sel.lower() != seller.lower():
            stats["other_seller"] += 1
            _note(rec, "other_seller", seller=sel)
            cs.touch(iid)
            continue
        imgs = [x.get("imageUrl") for x in [full.get("image") or {}] + (full.get("additionalImages") or [])
                if x.get("imageUrl")]
        if imgs and (cache.get(iid) or {}).get("status") != "found":
            cache[iid] = {"checked_at": now_iso(), "status": "found", "title": full.get("title"),
                          "seller": sel, "item_end_date": full.get("itemEndDate"), "images": imgs,
                          "via": "confirm_sold"}
            cache_dirty = True
        sold_q, avail = _availability(full)
        if sold_q >= 1 and avail == 0:
            date, why = _sold_date(full, rec, today)
            price = full.get("price") or {}
            sales.append({"item_id": iid, "title": full.get("title"), "price": price.get("value"),
                          "currency": price.get("currency") or "USD", "quantity": sold_q, "date": date,
                          "raw": {"legacyItemId": full.get("legacyItemId"), "itemEndDate": full.get("itemEndDate"),
                                  "estimatedAvailabilities": full.get("estimatedAvailabilities"),
                                  "price": full.get("price"), "checked_at": cs.ts}})
            _note(rec, "sold", sold_quantity=sold_q, sold_at_from=why)
            stats["sold"] += 1
            stats["sold_ids"].append(iid)
            log(f"{iid}: SOLD (quantity {sold_q}) on {date} ({why}) for {price.get('value')}")
        else:
            # 0 sold: Edwin ended it (or it is still listed). Ended + 0 sold with a
            # past end date is final; anything else is re-checked up to MAX_TRIES.
            end = (full.get("itemEndDate") or "")[:10]
            final = sold_q == 0 and rec.get("status") == "ended" and end and end <= today
            _note(rec, "not_sold_final" if final else "not_sold", sold_quantity=sold_q, available=avail)
            stats["not_sold"] += 1
            log(f"{iid}: not sold (sold {sold_q}, available {avail}); left {rec.get('status')}")
        cs.touch(iid)
    if dry_run:
        return {"result": "dry_run", **stats}
    if sales:
        store.apply_sales(cs, sales, SOURCE, price_note="listing price at sale (Browse getItem)")
    cs.commit()
    if cache_dirty:
        write_json(ended.CACHE, cache)
    out = {"result": "ok", **stats}
    st = load_json(SYNC_STATE, {}) or {}
    st["last_sold_confirm"] = {"at": now_iso(), **{k: v for k, v in out.items()}}
    write_json(SYNC_STATE, st)
    return out

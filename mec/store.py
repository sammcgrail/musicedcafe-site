"""The source store: one JSON file per eBay item ID under data/source/items/.

These files hold what eBay told us, plus the status our sync logic derived from
it. Nothing in here is edited by hand; editorial copy lives in data/editorial/.

Safety rules (also covered by tests/test_import.py):
  * every change set is staged in memory and written only when the whole import
    succeeded; a failed or partial sync writes nothing but its error to
    sync-state.json
  * an item becomes "ended" only when a SUCCESSFUL and COMPLETE active-listing
    snapshot no longer contains it, and never more than max_end_fraction of the
    active catalog in one go (a mass disappearance is treated as a broken feed)
  * an item becomes "sold" only when eBay says so explicitly (a sales /
    transaction report, or the listing page says "This listing sold")
"""
from __future__ import annotations

import os
from pathlib import Path

from .util import (SOURCE_DIR, SYNC_STATE, append_log, config, dump_json, load_json,
                   now_iso)

ITEMS_DIR = SOURCE_DIR / "items"


class SyncRefused(Exception):
    pass


def listing_url(item_id: str) -> str:
    return f"https://www.ebay.com/itm/{item_id}"


def load_items() -> dict[str, dict]:
    out = {}
    if ITEMS_DIR.exists():
        for p in sorted(ITEMS_DIR.glob("*.json")):
            rec = load_json(p)
            if rec and rec.get("item_id"):
                out[rec["item_id"]] = rec
    return out


def new_item(item_id: str, ts: str) -> dict:
    return {
        "item_id": item_id,
        "listing_url": listing_url(item_id),
        "status": "unknown",
        "status_source": None,
        "status_changed_at": ts,
        "first_seen_at": ts,
        "last_seen_active_at": None,
        "sold_at": None,
        "sales": [],
        "price": None,
        "quantity_available": None,
        "raw": {},
    }


class ChangeSet:
    """Collects new item states; commit() writes them all or nothing."""

    def __init__(self):
        self.items = load_items()
        self.dirty: set[str] = set()
        self.ts = now_iso()

    def get(self, item_id: str, create=True) -> dict | None:
        if item_id not in self.items and create:
            self.items[item_id] = new_item(item_id, self.ts)
            self.dirty.add(item_id)
        return self.items.get(item_id)

    def touch(self, item_id: str):
        self.dirty.add(item_id)

    def set_status(self, rec: dict, status: str, source: str, reason: str):
        if rec.get("status") != status:
            rec["status"] = status
            rec["status_changed_at"] = self.ts
        rec["status_source"] = source
        rec["status_reason"] = reason
        self.dirty.add(rec["item_id"])

    def commit(self) -> int:
        """Stage every dirty file as a temp file first, then rename them all."""
        ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        staged = []
        try:
            for iid in sorted(self.dirty):
                path = ITEMS_DIR / f"{iid}.json"
                tmp = path.with_name(f".stage-{iid}.json")
                tmp.write_text(dump_json(self.items[iid]), encoding="utf-8")
                staged.append((tmp, path))
        except BaseException:
            for tmp, _ in staged:
                tmp.unlink(missing_ok=True)
            raise
        for tmp, path in staged:
            os.replace(tmp, path)
        return len(staged)


# --------------------------------------------------------------------- sync state

def read_state() -> dict:
    return load_json(SYNC_STATE, {}) or {
        "last_success": None, "last_success_source": None, "last_attempt": None,
        "last_attempt_source": None, "last_result": None, "errors": [], "history": []}


def record_attempt(source: str, ok: bool, summary: dict, errors: list[str] | None = None,
                   skipped: bool = False):
    from .util import write_json
    st = read_state()
    ts = now_iso()
    st["last_attempt"], st["last_attempt_source"] = ts, source
    st["last_result"] = "skipped" if skipped else ("ok" if ok else "failed")
    st["errors"] = (errors or [])[:50]
    if ok and not skipped:
        st["last_success"], st["last_success_source"] = ts, source
        st["last_success_summary"] = summary
    entry = {"at": ts, "source": source, "result": st["last_result"], "summary": summary,
             "errors": (errors or [])[:10]}
    st["history"] = ([entry] + st.get("history", []))[:30]
    write_json(SYNC_STATE, st)
    append_log(entry)
    return st


# --------------------------------------------------------------------- applying data

def apply_sales(cs: ChangeSet, sales: list[dict], source: str) -> dict:
    """sales: [{item_id, title, price, currency, quantity, date, raw}] - each is an
    eBay-confirmed sale. Marks the item sold unless a LATER complete active
    snapshot still listed it (multi-quantity listings keep selling)."""
    created = updated = still_active = 0
    for s in sales:
        iid = s["item_id"]
        existed = iid in cs.items
        rec = cs.get(iid)
        created += 0 if existed else 1
        updated += 1 if existed else 0
        key = (s.get("date"), s.get("price"), s.get("quantity"))
        known = {(x.get("date"), x.get("price"), x.get("quantity")) for x in rec["sales"]}
        if key not in known:
            rec["sales"].append({"date": s.get("date"), "price": s.get("price"),
                                 "currency": s.get("currency"), "quantity": s.get("quantity"),
                                 "source": source})
            rec["sales"].sort(key=lambda x: x.get("date") or "")
        rec["raw"][source] = s["raw"]
        if s.get("title"):
            rec["title"] = s["title"]
        if rec.get("discogs_sale"):
            # also sold on Discogs: keep the Discogs sale as the status source
            rec["discogs_sale"].setdefault("ebay_sale_reported_at", cs.ts)
            cs.touch(iid)
            continue
        last_sale = rec["sales"][-1]
        rec["sold_at"] = last_sale.get("date")
        active_later = (rec.get("status") == "active" and rec.get("last_seen_active_at")
                        and (rec["last_seen_active_at"][:10] > (rec["sold_at"] or "")))
        if active_later:
            still_active += 1
        else:
            if rec.get("price") is None or rec.get("status") != "active":
                rec["price"] = {"value": last_sale.get("price"), "currency": last_sale.get("currency"),
                                "source": f"{source}: item subtotal (sale price)"}
            cs.set_status(rec, "sold", source, f"sale on {rec['sold_at']} confirmed by {source}")
            rec["quantity_available"] = 0
        cs.touch(iid)
    return {"sales": len(sales), "items_created": created, "items_updated": updated,
            "kept_active_multi_qty": still_active}


def apply_active_snapshot(cs: ChangeSet, items: list[dict], source: str, complete: bool,
                          force: bool = False) -> dict:
    """items: [{item_id, title, price, currency, quantity, raw, fields?}] from a
    source that lists ACTIVE listings. If `complete`, anything previously active
    and absent from this snapshot is ended (guarded)."""
    seen = set()
    created = 0
    for it in items:
        iid = it["item_id"]
        seen.add(iid)
        if iid not in cs.items:
            created += 1
        rec = cs.get(iid)
        rec["raw"][source] = it["raw"]
        if it.get("title"):
            rec["title"] = it["title"]
        qty = it.get("quantity")
        ds = rec.get("discogs_sale")
        if ds and not ds.get("relist_confirmed_at"):
            # Sold on Discogs (mec/discogs.py). eBay still listing it means Edwin
            # has not ended it there yet: note that, but never flip it back to
            # active. Only his explicit relist confirmation can (discogs.confirm_relist).
            ds["ebay_seen_active_at"], ds["ebay_quantity"] = cs.ts, qty
            if it.get("listing"):
                rec["listing"] = it["listing"]
            cs.touch(iid)
            continue
        if it.get("price") is not None:
            rec["price"] = {"value": it["price"], "currency": it.get("currency") or "USD",
                            "source": f"{source}: current price"}
        rec["quantity_available"] = qty
        if it.get("listing"):
            rec["listing"] = it["listing"]
        rec["last_seen_active_at"] = cs.ts
        if qty is not None and int(qty) <= 0:
            cs.set_status(rec, "unavailable", source, "listed with 0 available")
        else:
            cs.set_status(rec, "active", source, f"listed as active by {source}")
        cs.touch(iid)
    ended = []
    if complete:
        for iid, rec in cs.items.items():
            ds = rec.get("discogs_sale")
            if ds and iid not in seen and not ds.get("ebay_listing_gone_at"):
                ds["ebay_listing_gone_at"] = cs.ts   # Edwin ended it on eBay: clears "End on eBay"
                cs.touch(iid)
        active_before = [i for i, r in cs.items.items()
                         if r.get("status") == "active" and i not in seen]
        n_active = sum(1 for r in cs.items.values() if r.get("status") == "active")
        frac = float(config().get("sync", {}).get("max_end_fraction", 0.5))
        if active_before and not force and len(active_before) > 3 and \
                len(active_before) > frac * max(n_active, 1):
            raise SyncRefused(
                f"refusing to end {len(active_before)} of {n_active} active items in one sync "
                f"(limit {int(frac * 100)}%). If they really all ended, re-run with --force.")
        for iid in active_before:
            rec = cs.items[iid]
            cs.set_status(rec, "ended", source,
                          f"not returned by a complete {source} snapshot on {cs.ts[:10]}; "
                          "eBay did not say it sold")
            ended.append(iid)
    return {"active_listed": len(items), "items_created": created, "ended": len(ended),
            "complete_snapshot": complete}

"""eBay Browse API sync (application token, no seller login needed).

Ready to run as soon as EBAY_CLIENT_ID / EBAY_CLIENT_SECRET exist in .env.
Without them it records "skipped: no credentials" and touches no data.

What it does:
  1. client-credentials token  POST https://api.ebay.com/identity/v1/oauth2/token
     scope https://api.ebay.com/oauth/api_scope (token lives 2 h)
  2. item_summary/search with filter=sellers:{<seller>} for each configured
     category (Browse needs q or a category; 176985 = Vinyl Records is a leaf).
     buyingOptions defaults to FIXED_PRICE only, so we ask for
     FIXED_PRICE|AUCTION|BEST_OFFER explicitly. limit 200, offset < 10,000.
  3. getItem for each hit: item specifics (localizedAspects), description,
     images, condition, availability. A stored getItem payload is reused while
     it is younger than browse.item_refresh_hours (default 24) and the search
     summary still shows the same price, title and bids, so an hourly sync costs
     a handful of calls and the full getItem pass happens about once a day.
A snapshot counts as COMPLETE only if every page and every getItem succeeded
and the number of items fetched equals the reported total. Only a complete
snapshot can end listings (see store.apply_active_snapshot).

Browse sees ACTIVE listings only. Sold history comes from the transaction CSV
(or, later, the Trading API with seller OAuth - see docs/EBAY-INTEGRATION.md).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store
from .util import config

API = "https://api.ebay.com"
TOKEN_URL = f"{API}/identity/v1/oauth2/token"
SCOPE = "https://api.ebay.com/oauth/api_scope"
PAGE = 200
OFFSET_CAP = 10_000


class BrowseError(Exception):
    pass


def credentials() -> tuple[str, str] | None:
    cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None


def _req(method, url, headers, data=None, timeout=30, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode("utf-8", "replace")
            last = BrowseError(f"HTTP {e.code} for {url.split('?')[0]}: {body}")
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            raise last
        except urllib.error.URLError as e:
            last = BrowseError(f"network error for {url.split('?')[0]}: {e.reason}")
            time.sleep(2 ** attempt)
    raise last


def get_token(cid: str, secret: str) -> str:
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    res = _req("POST", TOKEN_URL, {"Authorization": f"Basic {basic}",
                                   "Content-Type": "application/x-www-form-urlencoded"}, data)
    if "access_token" not in res:
        raise BrowseError("token response had no access_token")
    return res["access_token"]


def _to_item(full: dict, summ: dict) -> dict:
    legacy = full.get("legacyItemId") or summ.get("legacyItemId") or full["itemId"].split("|")[1]
    price = (full.get("price") or full.get("currentBidPrice") or {})
    avail = (full.get("estimatedAvailabilities") or [{}])[0]
    qty = avail.get("estimatedAvailableQuantity")
    if qty is None and avail.get("estimatedAvailabilityStatus") == "IN_STOCK":
        qty = 1
    if avail.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
        qty = 0
    opts = full.get("buyingOptions") or []
    listing = {"type": "AUCTION" if "AUCTION" in opts else "FIXED_PRICE",
               "start_date": (full.get("itemCreationDate") or "")[:10] or None,
               "end_date": (full.get("itemEndDate") or "")[:10] or None,
               "bids": full.get("bidCount") or 0, "buy_it_now": None}
    return {"item_id": str(legacy), "title": full.get("title"), "price": price.get("value"),
            "currency": price.get("currency"), "quantity": qty, "raw": full, "listing": listing}


def _now_iso() -> str:
    from .util import now_iso
    return now_iso()


def _reusable(c: dict, summ: dict, max_age_s: float, now: float) -> bool:
    """Is a stored getItem payload still good for this search summary?"""
    from datetime import datetime
    try:
        age = now - datetime.fromisoformat(c["fetched_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    if age < 0 or age > max_age_s:
        return False
    full = c["full"]
    def v(d, k):
        return ((d.get(k) or {}).get("value"))
    return (v(summ, "price") == v(full, "price") and v(summ, "currentBidPrice") == v(full, "currentBidPrice")
            and (summ.get("title") or None) == (full.get("title") or None)
            and int(summ.get("bidCount") or 0) == int(full.get("bidCount") or 0))


def fetch_snapshot(seller: str, categories: list[str], marketplace: str = "EBAY_US",
                   token: str | None = None, log=print) -> tuple[list[dict], bool, list[str]]:
    """Returns (items, complete, errors)."""
    creds = credentials()
    if token is None:
        if not creds:
            raise BrowseError("no credentials")
        token = get_token(*creds)
    h = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace,
         "Accept": "application/json"}
    summaries, errors, complete = {}, [], True
    totals, search_calls = {}, 0
    # Browse paging over an unsorted result set is not stable (a 339-item run
    # returned 333 unique ids). Page through several sort orders and union them
    # until the union reaches the reported total.
    for cat in categories:
        total = None
        for sort in ("newlyListed", "price", "-price", "endingSoonest"):
            offset = 0
            while True:
                q = {"category_ids": cat, "limit": PAGE, "offset": offset, "sort": sort,
                     "filter": f"sellers:{{{seller}}},buyingOptions:{{FIXED_PRICE|AUCTION|BEST_OFFER}}"}
                url = f"{API}/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(q)
                search_calls += 1
                try:
                    res = _req("GET", url, h)
                except BrowseError as e:
                    errors.append(str(e))
                    complete = False
                    break
                total = res.get("total", 0)
                totals[cat] = total
                for sm in res.get("itemSummaries", []) or []:
                    sm["_category_query"] = cat
                    summaries[sm["itemId"]] = sm
                offset += PAGE
                if offset >= total:
                    break
                if offset >= OFFSET_CAP:
                    errors.append(f"category {cat}: {total} results exceed the 10,000 offset cap")
                    complete = False
                    break
            got = sum(1 for sm in summaries.values() if sm.get("_category_query") == cat)
            log(f"category {cat}: total={total} unique so far={got} (after sort {sort})")
            if total is not None and got >= total:
                break
    items = []
    # getItem is the expensive part (one call per listing). Reuse the stored
    # payload when it is younger than max_age_hours and the search summary still
    # agrees on price, title and bids; a sold listing drops out of search and is
    # looked up directly below, so reuse never hides a sale.
    # set by sync() for one call only, so a stale cache never leaks into the next caller
    cached = fetch_snapshot.__dict__.pop("cached", None) or {}
    max_age = float(fetch_snapshot.__dict__.pop("max_age_hours", 0) or 0) * 3600
    stats = {"search_calls": search_calls, "getitem_calls": 0, "getitem_reused": 0, "direct_calls": 0}
    now = time.time()
    for item_id, s in summaries.items():
        c = cached.get(item_id)
        if c and max_age and _reusable(c, s, max_age, now):
            items.append(dict(_to_item(c["full"], s), fetched_at=c["fetched_at"]))
            stats["getitem_reused"] += 1
            continue
        url = f"{API}/buy/browse/v1/item/{urllib.parse.quote(item_id, safe='')}"
        stats["getitem_calls"] += 1
        try:
            full = _req("GET", url, h)
        except BrowseError as e:
            errors.append(str(e))
            complete = False
            continue
        items.append(dict(_to_item(full, s), fetched_at=_now_iso()))
        time.sleep(0.2)
    # Any id we believe is active but search did not return: ask for it directly.
    known_active = getattr(fetch_snapshot, "known_active", set())
    got_ids = {str(i["item_id"]) for i in items}
    direct = {}
    for legacy in sorted(known_active - got_ids):
        url = f"{API}/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={legacy}"
        stats["direct_calls"] += 1
        try:
            full = _req("GET", url, h)
        except BrowseError as e:
            direct[legacy] = f"not returned: {str(e)[:160]}"
            continue
        sel = ((full.get("seller") or {}).get("username") or "").lower()
        if sel and sel != seller.lower():
            direct[legacy] = f"belongs to seller {sel}"
            continue
        if full.get("itemId") in summaries:
            continue
        summaries[full["itemId"]] = {"itemId": full["itemId"], "_direct": True}
        items.append(dict(_to_item(full, full), fetched_at=_now_iso()))
        direct[legacy] = "active (found by direct lookup, missing from search)"
        time.sleep(0.2)
    fetch_snapshot.direct = direct
    fetch_snapshot.stats = stats
    # reconcile: every reported result fetched, every getItem succeeded
    expected = sum(totals.values())
    if len(summaries) < expected or len(items) != len(summaries):
        complete = False
        errors.append(f"reconcile: search totals {totals} (sum {expected}), summaries {len(summaries)}, "
                      f"items fetched {len(items)}")
    fetch_snapshot.last_totals = totals
    return items, complete, errors


def coverage(items: list[dict]) -> dict:
    """Compare the API's active set with what the store currently holds as active."""
    from .util import DATA, write_json
    known = store.load_items()
    api_ids = {i["item_id"] for i in items}
    active_ids = {k for k, r in known.items() if r.get("status") == "active"}
    rep = {
        "api_active": len(api_ids), "store_active": len(active_ids),
        "api_only": sorted(api_ids - active_ids), "store_only_active": sorted(active_ids - api_ids),
        "api_ids_previously_sold": sorted(i for i in api_ids if known.get(i, {}).get("status") == "sold"),
        "auctions_in_api": sum(1 for i in items if (i.get("listing") or {}).get("type") == "AUCTION"),
        "totals_by_category": getattr(fetch_snapshot, "last_totals", {}),
        "direct_lookups": getattr(fetch_snapshot, "direct", {}),
    }
    rep["titles"] = {i: (known.get(i) or {}).get("title") for i in rep["store_only_active"]}
    rep["titles"].update({i["item_id"]: i.get("title") for i in items if i["item_id"] in rep["api_only"]})
    write_json(DATA / "sync-coverage.json", rep)
    return rep


def sync(force: bool = False, log=print, dry_run: bool = False) -> dict:
    cfg = config()
    if not credentials():
        st = store.record_attempt("browse_api", False, {"note": "no credentials, skipped"},
                                  skipped=True)
        log("no eBay API credentials (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET): skipped, no data touched")
        return {"result": "skipped", "state": st}
    seller = cfg.get("ebay_seller_username")
    if not seller:
        store.record_attempt("browse_api", False, {}, ["ebay_seller_username is not set"])
        return {"result": "failed", "errors": ["ebay_seller_username is not set"]}
    cats = cfg.get("browse", {}).get("category_ids") or ["176985"]
    known = store.load_items()
    fetch_snapshot.known_active = {k for k, r in known.items() if r.get("status") == "active"}
    # stored getItem payloads of active records, keyed by Browse itemId (see fetch_snapshot)
    fetch_snapshot.max_age_hours = cfg.get("browse", {}).get("item_refresh_hours", 24)
    fetch_snapshot.cached = {
        r["raw"]["browse_api"]["itemId"]: {"full": r["raw"]["browse_api"], "fetched_at": r["api_item_fetched_at"]}
        for r in known.values()
        if r.get("status") == "active" and r.get("api_item_fetched_at")
        and ((r.get("raw") or {}).get("browse_api") or {}).get("itemId")}
    try:
        items, complete, errors = fetch_snapshot(seller, cats, cfg.get("marketplace", "EBAY_US"), log=log)
    except BrowseError as e:
        store.record_attempt("browse_api", False, {}, [str(e)])
        return {"result": "failed", "errors": [str(e)]}
    cov = coverage(items)
    cov_summary = {k: (len(v) if isinstance(v, list) else v) for k, v in cov.items() if k != "titles"}
    cov_summary["api_calls"] = dict(getattr(fetch_snapshot, "stats", {}) or {})
    if dry_run:
        return {"result": "dry_run", "complete": complete, "errors": errors, "coverage": cov_summary}
    if errors or not complete:
        # partial data never changes the catalog
        store.record_attempt("browse_api", False, {"fetched": len(items), "coverage": cov_summary},
                             errors or ["incomplete"])
        return {"result": "failed", "errors": errors, "fetched": len(items), "coverage": cov_summary}
    # Ending listings needs trust that the seller filter sees ALL of Edwin's listings.
    # Until config browse.trusted_complete is true (set once coverage was confirmed
    # against a Seller Hub export), the API snapshot only adds and refreshes.
    trusted = bool(cfg.get("browse", {}).get("trusted_complete"))
    cs = store.ChangeSet()
    try:
        summary = store.apply_active_snapshot(cs, items, "browse_api", complete=trusted, force=force)
        summary["coverage"] = cov_summary
        summary["ending_enabled"] = trusted
    except store.SyncRefused as e:
        store.record_attempt("browse_api", False, {"fetched": len(items)}, [str(e)])
        return {"result": "refused", "errors": [str(e)]}
    summary["files_written"] = cs.commit()
    store.record_attempt("browse_api", True, summary)
    return {"result": "ok", **summary}

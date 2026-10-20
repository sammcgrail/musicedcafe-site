"""Best-effort read of the PUBLIC eBay store page for currently active listings.

This is the stop-gap until API keys exist. It is deliberately conservative:
  * plain logged-out GET, no cookies, no CAPTCHA handling; a challenge page or
    an error page is reported and nothing changes
  * results are imported as a PARTIAL snapshot: it can add or refresh active
    items but can never end or sell anything (a store page can hide listings by
    region, paging or A/B layout, so absence proves nothing)

Observed 2026-10-18 from this server: the store page title said "25+ Listings"
but the body said "We are sold out" (the visitor's region may filter listings),
and the seller search page returned an error or a security check. So this
usually finds nothing; the API path is the real fix.
"""
from __future__ import annotations

import html as htmllib
import re
import urllib.error
import urllib.request

from . import store
from .public_pages import UA
from .util import config


def scrape_store() -> dict:
    url = config()["ebay_store_url"]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html",
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            page = r.read(6_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"result": "blocked", "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"result": "error", "detail": f"{type(e).__name__}: {e}"}
    if re.search(r"Security Measure|Pardon Our Interruption|captcha", page, re.I):
        return {"result": "blocked", "detail": "eBay served a security check; stopping (not bypassed)"}
    sold_out = "We are sold out" in page
    items = {}
    for m in re.finditer(r'href="https://www\.ebay\.com/itm/(\d{9,15})[^"]*"[^>]*>(.*?)</a>', page, re.S):
        iid, inner = m.group(1), m.group(2)
        title = htmllib.unescape(re.sub(r"<[^>]+>", " ", inner)).strip()
        title = re.sub(r"\s+", " ", title)
        if iid not in items or (title and not items[iid]["title"]):
            items[iid] = {"item_id": iid, "title": title or None, "price": None, "currency": None,
                          "quantity": None, "raw": {"store_page_title": title}}
    if not items:
        return {"result": "none_found", "store_says_sold_out": sold_out,
                "detail": "no listing links on the public store page; nothing changed"}
    cs = store.ChangeSet()
    res = store.apply_active_snapshot(cs, list(items.values()), "store_page", complete=False)
    res["files_written"] = cs.commit()
    store.record_attempt("store_page", True, res)
    return {"result": "ok", **res}

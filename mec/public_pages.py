"""Polite public-page enrichment for known item IDs.

For each item ID we already know about (from a CSV or the API) this fetches two
PUBLIC, logged-out pages and stores them verbatim as source data:

  1. the listing description  https://itm.ebaydesc.com/itmdesc/<id>   (plain HTTPS)
  2. the item page            https://www.ebay.com/itm/<id>           (needs a real
     browser; uses a local camofox-browser REST endpoint if CAMOFOX_URL is set)

Rules we keep (eBay's fence, not ours to climb):
  * no login, no cookies we were not given, no CAPTCHA solving
  * one request every few seconds; back off on an error page; stop asking for
    item pages after several consecutive blocks
  * results are raw captures in data/source/public/<id>.json - never hand-edited

The item page is the only public place images and item specifics appear for a
sold listing. When it is blocked the record keeps a placeholder sleeve.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .util import SOURCE_DIR, load_json, now_iso, write_json

PUBLIC_DIR = SOURCE_DIR / "public"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
EXTRACT_JS = (Path(__file__).parent / "extract_item_page.js").read_text()


def fetch_description(item_id: str, timeout: float = 20) -> tuple[str | None, str | None]:
    # same query string the item page itself uses for its description iframe
    url = (f"https://itm.ebaydesc.com/itmdesc/{item_id}?t=0&excSoj=1&ver=1&excTrk=1&lsite=0"
           "&ittenable=false&domain=ebay.com&descgauge=1&cspheader=1&oneClk=2&secureDesc=1&lang=en")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(2_000_000).decode("utf-8", "replace")
            if r.status != 200 or len(body) < 200:
                return None, f"http {r.status}, {len(body)} bytes"
            from .normalize import html_to_text
            if len(html_to_text(body)) < 20:
                return None, "empty description shell (rendered client-side)"
            return body, None
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:  # network
        return None, f"{type(e).__name__}: {e}"


class Camofox:
    """Minimal client for a local camofox-browser (anti-bot Firefox) container."""

    def __init__(self, base: str, user: str = "musicedcafe"):
        self.base, self.user = base.rstrip("/"), user

    def _call(self, method, path, body=None, timeout=90):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def extract(self, url: str, settle: float = 4.0) -> dict:
        tab = self._call("POST", "/tabs", {"userId": self.user, "sessionKey": "mec"})["tabId"]
        try:
            self._call("POST", f"/tabs/{tab}/navigate",
                       {"userId": self.user, "sessionKey": "mec", "url": url,
                        "waitUntil": "domcontentloaded"})
            time.sleep(settle)
            return self._call("POST", f"/tabs/{tab}/evaluate",
                              {"userId": self.user, "expression": EXTRACT_JS})["result"]
        finally:
            try:
                self._call("DELETE", f"/tabs/{tab}?userId={self.user}")
            except Exception:
                pass


def is_blocked(page: dict) -> bool:
    t = (page or {}).get("page_title") or ""
    return (not page) or t.startswith("Error Page") or "Security Measure" in t or not page.get("title")


def enrich(item_ids: list[str], spacing: float = 6.0, max_blocks: int = 4,
           refresh: bool = False, log=print) -> dict:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    cam_url = os.environ.get("CAMOFOX_URL")
    cam = Camofox(cam_url) if cam_url else None
    stats = {"desc_ok": 0, "desc_fail": 0, "page_ok": 0, "page_blocked": 0, "page_skipped": 0}
    consecutive_blocks = 0
    for n, iid in enumerate(item_ids, 1):
        path = PUBLIC_DIR / f"{iid}.json"
        rec = load_json(path, {}) or {"item_id": iid}
        changed = False
        from .normalize import html_to_text
        if refresh or len(html_to_text(rec.get("description_html"))) < 20:
            html, err = fetch_description(iid)
            if html:
                rec["description_html"], rec["description_fetched_at"] = html, now_iso()
                stats["desc_ok"] += 1
                changed = True
            else:
                rec["description_error"] = err
                stats["desc_fail"] += 1
            time.sleep(spacing / 2)
        have_page = rec.get("item_page") and not is_blocked(rec["item_page"])
        if cam and (refresh or not have_page):
            if consecutive_blocks >= max_blocks:
                stats["page_skipped"] += 1
            else:
                try:
                    page = cam.extract(f"https://www.ebay.com/itm/{iid}")
                except Exception as e:
                    page = {"error": f"{type(e).__name__}: {e}"}
                if is_blocked(page):
                    consecutive_blocks += 1
                    stats["page_blocked"] += 1
                    rec["item_page_error"] = (page or {}).get("page_title") or page.get("error")
                    log(f"[{n}/{len(item_ids)}] {iid}: item page blocked ({consecutive_blocks} in a row); backing off")
                    time.sleep(spacing * 10 * consecutive_blocks)
                else:
                    consecutive_blocks = 0
                    page.pop("url", None)
                    rec["item_page"], rec["item_page_fetched_at"] = page, now_iso()
                    rec.pop("item_page_error", None)
                    stats["page_ok"] += 1
                    changed = True
                time.sleep(spacing)
        if changed or not path.exists():
            write_json(path, rec)
        log(f"[{n}/{len(item_ids)}] {iid}: desc={'y' if rec.get('description_html') else 'n'} "
            f"page={'y' if rec.get('item_page') and not is_blocked(rec['item_page']) else 'n'}")
    return stats

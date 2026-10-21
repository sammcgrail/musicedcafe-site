#!/usr/bin/env python3
"""Tiny admin sidecar for Music Ed Cafe (stdlib only, no database).

Routes (all /admin/api/* need  Authorization: Bearer <ADMIN_TOKEN>):
  GET  /admin/                 the admin page (static HTML, holds no secrets)
  GET  /admin/api/status       sync state, counts, review summary
  GET  /admin/api/review       full missing-fields / review report
  POST /admin/api/refresh      eBay API sync now, then rebuild (inert without API keys)
  POST /admin/api/rebuild      rebuild the static site from current data
  POST /admin/api/csv?dry=1&mode=auto|complete|partial   body = CSV text
  POST /admin/api/enrich       fetch public descriptions/photos for items missing them (background)
  GET  /admin/api/merch        merch preorder reservations + totals per item/option
  GET  /admin/api/merch.csv    the same reservations as CSV
  POST /admin/api/merch/delete?id=<reservation id>
  GET  /admin/api/sell         "Sell Your Records" submissions (names, emails, phones: never logged) + review status
  GET  /admin/api/sell.csv     the same submissions as CSV
  POST /admin/api/sell/delete?id=<submission id>   (also deletes its photos and review)
  Sell offer calculator (mec/sell_offer.py; PRIVATE, admin token only):
  GET  /admin/api/sell/detail?id=          submission + photos list + per-record valuation and offers
  GET  /admin/api/sell/photo?id=&p=r1-front|ov1&size=thumb|full|original   a stored photo (never served by nginx)
  GET  /admin/api/sell/valuation.csv?id=   the valuation as CSV
  GET  /admin/api/sell/rules               offer rules (data/sell/offer_rules.json, defaults until saved)
  POST /admin/api/sell/rules               save offer rules
  POST /admin/api/sell/review              {id, status?, ship_method?, final_offer?, note?, records?: {key: {...}},
                                            add_extra?, remove_extra?}
  POST /admin/api/sell/search              {id, key, catno?, artist?, title?}  Discogs candidates (read-only client)
  POST /admin/api/sell/pick                {id, key, release_id, refresh?}   choose a release + fetch value data
  POST /sell/submit            public sell-your-records form: multipart/form-data with photos (mec/sell_upload.py:
                               streamed to disk, 8 MB per photo, 120 MB per request) or urlencoded. JSON reply when
                               Accept: application/json, otherwise a 303 redirect to GET /sell/submit?r=<id>
  POST /merch/reserve          public merch reservation form (urlencoded; JSON reply when Accept: application/json,
                               otherwise a 303 redirect to GET /merch/reserve?r=<id>, the thank-you page)
  Discogs (mec/discogs.py; every call is inert without DISCOGS_TOKEN):
  GET  /admin/api/discogs                 counts, settings, End-on-eBay list, last match / sync / poll
  GET  /admin/api/discogs/records?status= per-record rows: eBay title/photo next to the Discogs candidates
  GET  /admin/api/discogs/alerts          the Alerts feed (Discogs sales, double sales, sync errors)
  GET  /admin/api/discogs/plan            dry run of a listing sync (read-only client)
  POST /admin/api/discogs/match           match records to releases (background; never lists anything)
  POST /admin/api/discogs/decide          {id, action: approve|pick|manual|skip|reset|set_sleeve, release_id?, sleeve_condition?, by?}
  POST /admin/api/discogs/approve-confident {expected, by?}   bulk approve, needs the count the human confirmed
  POST /admin/api/discogs/toggle          {id, on, by?}   per-record "list on Discogs"
  POST /admin/api/discogs/list-all-approved {expected, by?}
  POST /admin/api/discogs/settings        price rule, allow_offers, comment template, new-listing status
  POST /admin/api/discogs/sync-listings   push approved+toggled records (background) {force?}
  POST /admin/api/discogs/check-now       poll Discogs orders + sold inventory now (read-only)
  POST /admin/api/discogs/alerts/ack      {ids?}  (no ids = all)
  POST /admin/api/discogs/ebay-ended      {id}  "I ended it on eBay"
  POST /admin/api/discogs/relist          {id, confirm: true}  undo a Discogs sale (eBay must still show qty > 0)
  POST /admin/api/discogs/quickpick       {apply?, expected?, exclude_title_mismatch?}  Edwin's quick rule (no API call)
  GET  /admin/api/discogs/enable-preview  quick + confident records the bulk enable would turn on, and the needs-grade queue
  POST /admin/api/discogs/enable-quick-confident {expected, by?}
  GET  /admin/verify                      "Verify pressing" phone page (static shell, same token sign-in as /admin/)
  GET  /admin/api/discogs/verify/queue?q=&show=unverified|all
  GET  /admin/api/discogs/verify/record?id=      one record + its candidate releases (cached identifiers)
  GET  /admin/api/discogs/verify/release?item=&rid=   lazily GET /releases/{rid} (paced, cached in data/discogs/releases/)
  POST /admin/api/discogs/verify/match    {id, side_a, side_b, lacquer?, stamper?}  auto-match scores
  POST /admin/api/discogs/verify/confirm  {id, release_id, side_a, side_b, lacquer?, stamper?, score?, by?}
  POST /admin/api/discogs/verify/skip     {id, by?}   "can't read"
  GET  /ebay/account-deletion  eBay Marketplace Account Deletion challenge (needed for a production keyset)
  POST /ebay/account-deletion  acknowledges notifications (this site stores no eBay buyer data)

Also runs the scheduled eBay sync: every sync.interval_minutes (60), or once a
day at sync.daily_at_utc when the interval is null. Each run: Browse sync, then
(on success) sold confirmation, photos, rebuild, optional Discogs push. The
Instagram import inside it runs once per UTC day. With no API keys the
scheduled run records "skipped: no credentials" and changes nothing.
And a Discogs sales poll every 15 minutes (DISCOGS_POLL_MINUTES): read-only on
Discogs, records "skipped: no DISCOGS_TOKEN" without a token.
Uploaded CSVs are processed in memory and never written to disk or logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get("MEC_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MEC_ROOT", str(ROOT))

from mec import merch, sell, sell_offer, sell_upload, store  # noqa: E402
from mec.cli import _load_env  # noqa: E402

_load_env()
TOKEN = os.environ.get("ADMIN_TOKEN", "")
PAGE = (Path(__file__).parent / "index.html").read_bytes()
VERIFY_PAGE = (Path(__file__).parent / "verify.html").read_bytes()
_REC_CACHE = {"at": 0.0, "records": None}
LOCK = threading.Lock()
JOB = {"running": None, "started": None, "last": None}
MAX_BODY = 10 * 1024 * 1024
MERCH_MAX_BODY = 8 * 1024
MERCH_RL_SHORT = merch.RateLimiter(5, 600)      # per IP: 5 reservations / 10 minutes
MERCH_RL_DAY = merch.RateLimiter(20, 86400)     # per IP: 20 / day
MERCH_RL_ALL = merch.RateLimiter(500, 86400)    # whole site: 500 / day (stops a flood filling the disk)
SELL_MAX_BODY = 16 * 1024                      # urlencoded (no photos)
SELL_MAX_UPLOAD = sell_upload.MAX_REQUEST      # multipart with photos; Caddy's cap sits just above this
SELL_UPLOADS = threading.BoundedSemaphore(3)   # concurrent photo uploads (each streams to disk)
SELL_MIN_FREE_DISK = 2 * 1024 ** 3
SELL_RL_SHORT = sell.RateLimiter(5, 600)       # per IP: 5 submissions / 10 minutes
SELL_RL_DAY = sell.RateLimiter(20, 86400)      # per IP: 20 / day
SELL_RL_ALL = sell.RateLimiter(300, 86400)     # whole site: 300 / day
DISCOGS_POLL_SECONDS = max(5, int(os.environ.get("DISCOGS_POLL_MINUTES") or 15)) * 60
MERCH_ORIGINS = {"https://musicedcafe.com", "https://www.musicedcafe.com", "https://musicedcafe.sebland.com"}


def log(msg: str):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def run_job(name: str, fn):
    """Run fn under the global lock; returns (http_status, payload)."""
    if not LOCK.acquire(blocking=False):
        return 409, {"error": f"busy: {JOB['running']} is running"}
    JOB["running"], JOB["started"] = name, time.time()
    try:
        res = fn()
        JOB["last"] = {"job": name, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": True}
        return 200, res
    except Exception as e:  # report, never crash the server
        log(f"{name} failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        JOB["last"] = {"job": name, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "ok": False, "error": str(e)}
        return 500, {"error": f"{type(e).__name__}: {e}"}
    finally:
        JOB["running"] = None
        LOCK.release()


def do_build():
    from mec.site import build
    return build()


def instagram_due(now: datetime | None = None) -> bool:
    """The Instagram import runs once per UTC day (keyed off its own state file),
    even though the eBay sync is hourly: it saves Graph API calls."""
    from mec import instagram
    from mec.util import load_json
    now = now or datetime.now(timezone.utc)
    last = (load_json(instagram.STATE, {}) or {}).get("last_attempt") or ""
    return last[:10] != now.date().isoformat()


def do_refresh():
    from mec.browse import sync
    res = sync()
    res.pop("state", None)
    try:  # Instagram blog posts: independent of eBay, never blocks the sync; daily only
        from mec import instagram
        res["instagram"] = instagram.sync(log=log) if instagram_due() else {"result": "skipped",
                                                                            "reason": "already ran today (UTC)"}
    except Exception as e:
        res["instagram"] = {"result": "failed", "error": str(e)[:200]}
    if res.get("result") == "ok":
        from mec import merge, photos
        try:  # sold confirmation for unavailable / ended records (mec/confirm_sold.py)
            from mec import confirm_sold
            from mec.util import config
            cfg = config()
            res["sold_confirm"] = confirm_sold.run(cfg["ebay_seller_username"],
                                                   marketplace=cfg.get("marketplace", "EBAY_US"), log=log)
        except Exception as e:  # never blocks the rebuild; retried at the next sync
            res["sold_confirm"] = {"result": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            res["photos"] = photos.run(merge.build_records()[0], log=log)
        except Exception as e:  # photos are a nicety; never block the rebuild
            res["photos"] = {"error": str(e)}
        res["build"] = do_build()
        try:  # optional: push price/grade/ended changes to Discogs (approved records only)
            from mec import discogs
            if discogs.load_settings().get("auto_sync_listings") and discogs.get_token():
                res["discogs"] = discogs.sync_listings(log=log)
        except Exception as e:
            res["discogs"] = {"result": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return res


def do_csv(text: str, dry: bool, mode: str):
    from mec.csv_import import ImportError_, import_csv_text
    complete = {"complete": True, "partial": False}.get(mode)
    try:
        res = import_csv_text(text, complete=complete, dry_run=dry)
    except (ImportError_, store.SyncRefused) as e:
        if not dry:
            store.record_attempt("csv", False, {}, [str(e)])
        return {"result": "failed", "error": str(e), "dry_run": dry}
    if not dry:
        store.record_attempt(f"csv:{res['format']}", True,
                             {k: v for k, v in res.items() if k not in ("errors", "warnings")}, res["errors"])
        res["build"] = do_build()
    res["result"] = "ok"
    return res


def status_payload():
    from mec.util import REVIEW_REPORT, load_json
    st = store.read_state()
    items = store.load_items()
    counts = {}
    for r in items.values():
        counts[r.get("status", "unknown")] = counts.get(r.get("status", "unknown"), 0) + 1
    rep = load_json(REVIEW_REPORT, {}) or {}
    live = ROOT / "public" / "live" / "index.html"
    return {
        "sync": st, "counts": counts, "total_items": len(items),
        "review_flag_counts": rep.get("flag_counts", {}),
        "api_credentials_present": bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET")),
        "discogs_token_present": bool(os.environ.get("DISCOGS_TOKEN")),
        "last_build": datetime.fromtimestamp(live.stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if live.exists() else None,
        "job": {k: v for k, v in JOB.items()},
        "schedule_utc": _schedule_label(),
    }


def sync_due(now: datetime, sync_cfg: dict, last_start: datetime | None, last_day) -> bool:
    """config sync.interval_minutes (e.g. 60) runs the sync every N minutes; when
    it is null/absent, sync.daily_at_utc ("10:15") runs it once a day."""
    interval = sync_cfg.get("interval_minutes")
    if interval:
        return last_start is None or (now - last_start).total_seconds() >= float(interval) * 60
    hh, mm = (int(x) for x in (sync_cfg.get("daily_at_utc") or "10:15").split(":"))
    return (now.hour, now.minute) >= (hh, mm) and last_day != now.date()


def _last_sync_start() -> datetime | None:
    """The last eBay sync attempt from sync-state.json, so a restart does not
    trigger an extra sync when one ran less than an interval ago."""
    st = store.read_state()
    if not str(st.get("last_attempt_source") or "").startswith("browse"):
        return None
    try:
        return datetime.fromisoformat(st["last_attempt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _schedule_label() -> str | None:
    sc = json.loads((ROOT / "config/site.json").read_text()).get("sync") or {}
    if sc.get("interval_minutes"):
        return f"every {sc['interval_minutes']} min"
    return sc.get("daily_at_utc")


def scheduler(clock=None, sleep=time.sleep, max_loops: int | None = None):
    """Scheduled eBay sync (hourly by default, see sync_due). Inert (records a
    skip) without API credentials. A busy job lock retries a minute later."""
    from mec.util import config
    clock = clock or (lambda: datetime.now(timezone.utc))
    last_day, last_start = None, _last_sync_start()
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            now = clock()
            if sync_due(now, config().get("sync") or {}, last_start, last_day):
                log("scheduled sync starting")
                code, res = run_job("scheduled-sync", do_refresh)
                if code != 409:   # busy (e.g. a long Discogs match): try again next minute
                    last_day, last_start = now.date(), now
                log(f"scheduled sync: {code} {res.get('result', res.get('error'))}")
        except Exception as e:
            log(f"scheduler error: {e}")
        sleep(60)


def discogs_scheduler(interval: float | None = None, once: bool = False):
    """Discogs sales poll. READ-ONLY on Discogs (orders + inventory); it never
    creates, edits or deletes a listing. Inert without DISCOGS_TOKEN."""
    from mec import discogs
    interval = interval or DISCOGS_POLL_SECONDS
    time.sleep(0 if once else 90)  # let startup (and any boot-time sync) settle first
    while True:
        wait = interval
        try:
            code, res = run_job("discogs-poll", lambda: discogs.poll(log=log))
            if code == 409:
                wait = 60   # another job holds the lock: try again in a minute
            else:
                r = res.get("result", res.get("error"))
                if r != "skipped" or not getattr(discogs_scheduler, "_said_skip", False):
                    log(f"discogs poll: {r} {res.get('reason') or ''}{' new_sales=' + str(res['new_sales']) if res.get('new_sales') else ''}")
                discogs_scheduler._said_skip = r == "skipped"
        except Exception as e:
            log(f"discogs scheduler error: {e}")
        if once:
            return
        time.sleep(wait)


def records_cached(ttl: float = 20.0) -> list:
    """merge.build_records() for the verify page, reused for a few seconds so a
    search-as-you-type doesn't rebuild every record on each keystroke."""
    from mec import merge
    now = time.time()
    if _REC_CACHE["records"] is None or now - _REC_CACHE["at"] > ttl:
        _REC_CACHE["records"], _REC_CACHE["at"] = merge.build_records()[0], now
    return _REC_CACHE["records"]


def _bg(name: str, fn):
    """Start fn under the job lock in a thread; returns (status, payload) for the reply."""
    if LOCK.locked():
        return 409, {"error": f"busy: {JOB['running']} is running"}
    threading.Thread(target=lambda: run_job(name, fn), daemon=True).start()
    return 202, {"started": f"{name} (runs in the background; check status)"}


class H(BaseHTTPRequestHandler):
    server_version = "mec-admin"
    sys_version = ""

    def log_message(self, fmt, *args):  # path only, never bodies or tokens
        log(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    def send(self, code: int, payload, ctype="application/json", extra: dict | None = None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def authed(self) -> bool:
        if not TOKEN:
            self.send(503, {"error": "admin disabled: ADMIN_TOKEN is not set"})
            return False
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.startswith("Bearer ") else ""
        if not hmac.compare_digest(got.encode(), TOKEN.encode()):
            time.sleep(1.0)
            self.send(401, {"error": "unauthorized"})
            return False
        return True

    def body(self) -> bytes | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.send(413, {"error": "too large (10 MB limit)"})
            return None
        return self.rfile.read(n) if n else b""

    def jbody(self) -> dict | None:
        raw = self.body()
        if raw is None:
            return None
        try:
            v = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self.send(400, {"error": "body must be JSON"})
            return None
        if not isinstance(v, dict):
            self.send(400, {"error": "body must be a JSON object"})
            return None
        return v

    def discogs_get(self, u):
        from mec import discogs
        qs = parse_qs(u.query)
        if u.path == "/admin/api/discogs":
            return self.send(200, dict(discogs.overview(), job=dict(JOB)))
        if u.path == "/admin/api/discogs/records":
            return self.send(200, {"rows": discogs.review_rows(status=(qs.get("status") or [None])[0])})
        if u.path == "/admin/api/discogs/alerts":
            return self.send(200, {"alerts": discogs.load_alerts()})
        if u.path == "/admin/api/discogs/enable-preview":
            return self.send(200, discogs.enable_preview(records_cached(0)))
        if u.path == "/admin/api/discogs/verify/queue":
            return self.send(200, discogs.verify_queue(records_cached(), q=(qs.get("q") or [""])[0],
                                                       show=(qs.get("show") or ["unverified"])[0]))
        if u.path == "/admin/api/discogs/verify/record":
            try:
                return self.send(200, discogs.verify_record((qs.get("id") or [""])[0], records_cached()))
            except ValueError as e:
                return self.send(404, {"error": str(e)})
        if u.path == "/admin/api/discogs/verify/release":
            iid, rid = (qs.get("item") or [""])[0], (qs.get("rid") or [""])[0]
            m = discogs.load_matches().get(iid) or {}
            ids = {str(c.get("release_id")) for c in m.get("candidates") or []} | {str(m.get("chosen_release_id"))}
            if not rid.isdigit() or rid not in ids:
                return self.send(400, {"error": "rid must be one of this record's candidate releases"})
            try:
                rel = discogs.get_release(discogs.make_client(read_only=True), int(rid))
            except discogs.DiscogsError as e:
                return self.send(502, {"error": str(e)[:300]})
            if rel is None:
                return self.send(200, {"release_id": int(rid), "fetched": False, "reason": discogs.SKIP_REASON})
            return self.send(200, {"release_id": int(rid), "fetched": True,
                                   "identifiers": discogs.runout_identifiers(rel), "year": rel.get("year"),
                                   "released": rel.get("released"), "country": rel.get("country"),
                                   "labels": rel.get("labels"), "notes": rel.get("notes"),
                                   "have": (rel.get("community") or {}).get("have")})
        if u.path == "/admin/api/discogs/plan":
            code, res = run_job("discogs-plan", lambda: discogs.sync_listings(
                client=discogs.make_client(read_only=True), dry_run=True, log=log))
            return self.send(code, res)
        return self.send(404, {"error": "not found"})

    def discogs_post(self, u):
        from mec import discogs
        b = self.jbody()
        if b is None:
            return
        by = str(b.get("by") or "admin")[:60]
        try:
            if u.path == "/admin/api/discogs/match":
                return self.send(*_bg("discogs-match", lambda: discogs.match_records(
                    ids=set(b["ids"]) if b.get("ids") else None, rematch=bool(b.get("rematch")), log=log)))
            if u.path == "/admin/api/discogs/sync-listings":
                return self.send(*_bg("discogs-sync-listings", lambda: discogs.sync_listings(
                    force=bool(b.get("force")), log=log)))
            if u.path == "/admin/api/discogs/check-now":
                return self.send(*run_job("discogs-poll", lambda: discogs.poll(log=log)))
            if u.path == "/admin/api/discogs/decide":
                cl = discogs.make_client(read_only=True) if b.get("action") == "manual" else None
                return self.send(200, {"match": discogs.decide(str(b.get("id") or ""), str(b.get("action") or ""),
                                                               release_id=b.get("release_id"), by=by,
                                                               sleeve_condition=b.get("sleeve_condition"), client=cl)})
            if u.path == "/admin/api/discogs/approve-confident":
                if b.get("expected") is None:
                    return self.send(400, {"error": "expected (the confirmed count) is required"})
                return self.send(200, discogs.approve_all_confident(int(b["expected"]), by=by))
            if u.path == "/admin/api/discogs/toggle":
                return self.send(200, {"match": discogs.set_listing_toggle(str(b.get("id") or ""), bool(b.get("on")), by=by)})
            if u.path == "/admin/api/discogs/list-all-approved":
                if b.get("expected") is None:
                    return self.send(400, {"error": "expected (the confirmed count) is required"})
                return self.send(200, discogs.list_all_approved(int(b["expected"]), by=by))
            if u.path == "/admin/api/discogs/quickpick":
                if b.get("apply") and b.get("expected") is None:
                    return self.send(400, {"error": "expected (the confirmed count) is required to apply"})
                return self.send(200, discogs.quickpick(apply=bool(b.get("apply")), expected=b.get("expected"),
                                                        exclude_title_mismatch=bool(b.get("exclude_title_mismatch")),
                                                        log=log))
            if u.path == "/admin/api/discogs/enable-quick-confident":
                if b.get("expected") is None:
                    return self.send(400, {"error": "expected (the confirmed count) is required"})
                return self.send(200, discogs.enable_quick_confident(records_cached(0), int(b["expected"]), by=by))
            if u.path == "/admin/api/discogs/verify/match":
                m = discogs.load_matches().get(str(b.get("id") or ""))
                if not m:
                    return self.send(404, {"error": "no match entry for this record"})
                rids = list(dict.fromkeys([m.get("chosen_release_id")] + [c["release_id"] for c in m.get("candidates") or []]))
                return self.send(200, discogs.auto_match(b, [r for r in rids if r]))
            if u.path == "/admin/api/discogs/verify/confirm":
                res = discogs.confirm_pressing(str(b.get("id") or ""), b.get("release_id"), b, by=by,
                                               score=b.get("score"))
                out = {"match": res["match"], "listing_live": res["listing_live"]}
                if res["listing_live"]:   # push the new release + comment to the existing Discogs listing
                    if discogs.get_token():
                        iid = str(b.get("id"))
                        code, j = _bg("discogs-verify-push", lambda: discogs.sync_listings(only_ids={iid}, log=log))
                        out["push"] = j if code == 202 else {"queued": False, "note": "another job is running; "
                                                             "the next listing sync pushes it"}
                    else:
                        out["push"] = {"queued": False, "note": discogs.SKIP_REASON}
                return self.send(200, out)
            if u.path == "/admin/api/discogs/verify/skip":
                return self.send(200, {"match": discogs.skip_pressing(str(b.get("id") or ""), by=by)})
            if u.path == "/admin/api/discogs/settings":
                s, errs = discogs.save_settings({k: v for k, v in b.items() if k != "by"})
                return self.send(400 if errs else 200, {"settings": s, "errors": errs})
            if u.path == "/admin/api/discogs/alerts/ack":
                return self.send(200, {"acked": discogs.ack_alerts(b.get("ids"))})
            if u.path == "/admin/api/discogs/ebay-ended":
                return self.send(200, {"discogs_sale": discogs.mark_ebay_ended(str(b.get("id") or ""), by=by)})
            if u.path == "/admin/api/discogs/relist":
                if b.get("confirm") is not True:
                    return self.send(400, {"error": "confirm: true is required"})
                res = discogs.confirm_relist(str(b.get("id") or ""), by=by)
                code, built = run_job("rebuild", do_build)
                return self.send(200, dict(res, build=built if code == 200 else {"error": built.get("error")}))
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/admin/api/discogs" or u.path.startswith("/admin/api/discogs/"):
            if self.authed():
                self.discogs_get(u)
            return
        if u.path in ("/admin", "/admin/"):
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if u.path in ("/admin/verify", "/admin/verify/"):
            return self.send(200, VERIFY_PAGE, "text/html; charset=utf-8")
        if u.path == "/ebay/account-deletion":
            return self.ebay_challenge(u)
        if u.path == "/admin/api/status":
            if self.authed():
                self.send(200, status_payload())
            return
        if u.path == "/admin/api/review":
            if self.authed():
                from mec.util import REVIEW_REPORT, load_json
                self.send(200, load_json(REVIEW_REPORT, {}) or {})
            return
        if u.path == "/admin/api/merch":
            if self.authed():
                rows = merch.read_all()
                self.send(200, {"reservations": rows, "totals": merch.totals(rows), "count": len(rows),
                                "qty": sum(int(r.get("qty") or 0) for r in rows)})
            return
        if u.path == "/admin/api/merch.csv":
            if self.authed():
                body = merch.to_csv(merch.read_all()).encode("utf-8-sig")
                self.send(200, body, "text/csv; charset=utf-8",
                          extra={"Content-Disposition": 'attachment; filename="merch-reservations.csv"'})
            return
        if u.path == "/admin/api/sell":
            if self.authed():
                rows = sell.read_all()
                for r in rows:
                    try:
                        r["review"] = sell_offer.summary(r)
                    except Exception as e:   # one bad review file never hides the list
                        r["review"] = {"status": "?", "error": type(e).__name__}
                self.send(200, {"submissions": rows, "count": len(rows)})
            return
        if u.path.startswith("/admin/api/sell/"):
            if self.authed():
                self.sell_admin_get(u)
            return
        if u.path == "/admin/api/sell.csv":
            if self.authed():
                body = sell.to_csv(sell.read_all()).encode("utf-8-sig")
                self.send(200, body, "text/csv; charset=utf-8",
                          extra={"Content-Disposition": 'attachment; filename="sell-submissions.csv"'})
            return
        if u.path == "/merch/reserve":
            return self.merch_result_page(parse_qs(u.query))
        if u.path == "/sell/submit":
            return self.sell_result_page(parse_qs(u.query))
        if u.path == "/healthz":
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/ebay/account-deletion":
            self.body()
            # we keep no eBay user/buyer data, so there is nothing to delete
            self.send_response(204)
            self.end_headers()
            return
        if u.path == "/merch/reserve":
            return self.merch_reserve()
        if u.path == "/sell/submit":
            return self.sell_submit()
        if not u.path.startswith("/admin/api/"):
            return self.send(404, {"error": "not found"})
        if not self.authed():
            return
        qs = parse_qs(u.query)
        if u.path.startswith("/admin/api/discogs/"):
            return self.discogs_post(u)
        if u.path == "/admin/api/refresh":
            code, res = run_job("refresh", do_refresh)
            return self.send(code, res)
        if u.path == "/admin/api/rebuild":
            code, res = run_job("rebuild", do_build)
            return self.send(code, res)
        if u.path == "/admin/api/csv":
            raw = self.body()
            if raw is None:
                return
            text = raw.decode("utf-8-sig", "replace")
            dry = qs.get("dry", ["1"])[0] == "1"
            mode = qs.get("mode", ["auto"])[0]
            code, res = run_job("csv-import", lambda: do_csv(text, dry, mode))
            return self.send(code, res)
        if u.path == "/admin/api/merch/delete":
            rid = (qs.get("id") or [""])[0]
            return self.send(200 if merch.delete(rid) else 404, {"deleted": rid} if rid else {"error": "id required"})
        if u.path == "/admin/api/sell/delete":
            rid = (qs.get("id") or [""])[0]
            return self.send(200 if sell.delete(rid) else 404, {"deleted": rid} if rid else {"error": "id required"})
        if u.path.startswith("/admin/api/sell/"):
            return self.sell_admin_post(u)
        if u.path == "/admin/api/enrich":
            def bg():
                def work():
                    from mec.public_pages import enrich
                    ids = [i for i in sorted(store.load_items())]
                    res = enrich(ids, log=log)
                    do_build()
                    return res
                run_job("enrich", work)
            if LOCK.locked():
                return self.send(409, {"error": f"busy: {JOB['running']} is running"})
            threading.Thread(target=bg, daemon=True).start()
            return self.send(202, {"started": "enrich (runs in the background; check status)"})
        self.send(404, {"error": "not found"})

    # ------------------------------------------------------------ merch reservations
    def client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[-1].strip() if xff else "") or self.client_address[0]

    def wants_json(self) -> bool:
        return "application/json" in (self.headers.get("Accept") or "")

    def merch_reply(self, code: int, payload: dict, html_state: str, html_data=None):
        if self.wants_json():
            return self.send(code, payload)
        if code == 200 and payload.get("ok"):
            rid = (payload.get("reservation") or {}).get("id", "")
            self.send_response(303)
            self.send_header("Location", "/merch/reserve" + (f"?r={rid}" if rid else "?r=ok"))
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        return self.send(code, render_merch_page(html_state, html_data or payload), "text/html; charset=utf-8")

    def merch_reserve(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MERCH_MAX_BODY:
            return self.merch_reply(413, {"ok": False, "message": "That form was too large. Please shorten the note and try again."},
                                    "error")
        origin = self.headers.get("Origin")
        if origin and origin not in MERCH_ORIGINS and origin != "null":
            return self.merch_reply(403, {"ok": False, "message": "Please reserve from the musicedcafe.com merch page."}, "error")
        raw = self.rfile.read(n) if n else b""
        if "application/x-www-form-urlencoded" not in (self.headers.get("Content-Type") or ""):
            return self.merch_reply(415, {"ok": False, "message": "Please use the reservation form on the merch page."}, "error")
        form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=30)
        if merch.is_spam(form):
            log("merch reserve: honeypot hit, dropped")
            return self.merch_reply(200, {"ok": True, "reservation": {}}, "thanks")
        now, ip = time.time(), self.client_ip()
        if not (MERCH_RL_SHORT.allow(ip, now) and MERCH_RL_DAY.allow(ip, now) and MERCH_RL_ALL.allow("*", now)):
            return self.merch_reply(429, {"ok": False, "message": "That's a lot of reservations in a short time. Please wait a few minutes and try again."},
                                    "error")
        try:
            cat = merch.load_catalog()
        except (OSError, ValueError) as e:
            log(f"merch reserve: catalog unreadable: {type(e).__name__}")
            return self.merch_reply(503, {"ok": False, "message": "Reservations are paused for a moment. Please try again soon."}, "error")
        clean, errors = merch.validate(form, cat)
        if errors:
            slug = merch._one(form, "item")
            return self.merch_reply(400, {"ok": False, "errors": errors}, "invalid",
                                    {"errors": errors, "slug": slug if slug in merch.items_by_slug(cat) else ""})
        rec = merch.append(clean)
        log("merch reserve: stored 1 reservation")
        pub = {k: rec[k] for k in ("id", "item", "item_name", "options", "qty", "name", "note")}
        return self.merch_reply(200, {"ok": True, "reservation": pub}, "thanks")

    def merch_result_page(self, qs):
        rid = (qs.get("r") or [""])[0]
        rec = merch.find(rid) if rid and rid != "ok" else None
        if rid:
            return self.send(200, render_merch_page("thanks", {"reservation": rec}), "text/html; charset=utf-8")
        self.send_response(303)
        self.send_header("Location", "/merch/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------ sell your records
    def sell_reply(self, code: int, payload: dict, html_state: str, html_data=None):
        if self.wants_json():
            return self.send(code, payload)
        if code == 200 and payload.get("ok"):
            rid = (payload.get("submission") or {}).get("id", "")
            self.send_response(303)
            self.send_header("Location", "/sell/submit" + (f"?r={rid}" if rid else "?r=ok"))
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        return self.send(code, render_sell_page(html_state, html_data or payload), "text/html; charset=utf-8")

    def sell_submit(self):
        ctype = self.headers.get("Content-Type") or ""
        multipart = ctype.lower().startswith("multipart/form-data")
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if n < 0:
            return self.sell_reply(411, {"ok": False, "message": "Please use the form on the sell page."}, "error")
        if n > (SELL_MAX_UPLOAD if multipart else SELL_MAX_BODY):
            self.close_connection = True
            return self.sell_reply(413, {"ok": False, "message": (
                "That upload is over 120 MB. Please send fewer or smaller photos (or a few overview photos) "
                "and try again." if multipart else "That form was too large. Please shorten the note and try again.")},
                "error")
        origin = self.headers.get("Origin")
        if origin and origin not in MERCH_ORIGINS and origin != "null":
            return self.sell_reply(403, {"ok": False, "message": "Please send your details from the musicedcafe.com sell page."}, "error")
        if not multipart and "application/x-www-form-urlencoded" not in ctype:
            return self.sell_reply(415, {"ok": False, "message": "Please use the form on the sell page."}, "error")
        now, ip = time.time(), self.client_ip()
        if not (SELL_RL_SHORT.allow(ip, now) and SELL_RL_DAY.allow(ip, now) and SELL_RL_ALL.allow("*", now)):
            return self.sell_reply(429, {"ok": False, "message": "That's a lot of submissions in a short time. Please wait a few minutes and try again."}, "error")
        try:
            cfg = sell.load_config()
        except (OSError, ValueError) as e:
            log(f"sell submit: config unreadable: {type(e).__name__}")
            return self.sell_reply(503, {"ok": False, "message": "The form is paused for a moment. Please try again soon."}, "error")
        if not multipart:
            raw = self.rfile.read(n) if n else b""
            form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=300)
            return self.sell_reply(*self.sell_finish(form, [], cfg))
        if not SELL_UPLOADS.acquire(blocking=False):
            return self.sell_reply(503, {"ok": False, "message": "We're receiving a lot of photos right now. Please try again in a minute."}, "error")
        spool, result = None, None
        try:
            import shutil
            sell.uploads_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
            if shutil.disk_usage(sell.uploads_dir()).free < SELL_MIN_FREE_DISK + n:
                log("sell submit: refused, low disk space")
                result = (503, {"ok": False, "message": "The form is paused for a moment. Please try again later."}, "error")
            else:
                spool = sell_upload.new_spool(sell.uploads_dir())
                try:
                    form, files = sell_upload.read_multipart(self.rfile, n, sell_upload.boundary_of(ctype), spool,
                                                             file_field=sell.is_file_field)
                    result = self.sell_finish(form, files, cfg)
                except sell_upload.UploadError as e:
                    self.close_connection = True
                    result = (e.status, {"ok": False, "message": e.message}, "error")
        finally:
            sell_upload.remove_tree(spool)   # raw uploads (with their EXIF) never outlive the request
            SELL_UPLOADS.release()
        return self.sell_reply(*result)

    def sell_finish(self, form: dict, files: list, cfg: dict) -> tuple:
        """Validate, process the photos and store. Returns sell_reply(...) arguments."""
        if sell.is_spam(form):
            log("sell submit: honeypot hit, dropped")
            return 200, {"ok": True, "submission": {}}, "thanks"
        clean, errors = sell.validate(form, cfg, files)
        if errors:
            return 400, {"ok": False, "errors": errors}, "invalid", {"errors": errors}
        sid = sell.new_id()
        dest = sell.photo_dir(sid)
        rows = {f"r{r['n']}": r.get("row", r["n"]) for r in clean["records"]}
        try:
            for stem, fp in clean["_files"].items():
                try:
                    sell_upload.process_image(fp.path, fp.kind(), dest, stem)
                except sell_upload.ImageRejected as e:
                    if stem.startswith("ov"):
                        errors["overview"] = f"Overview photo {stem[2:]}: {e}"
                    else:
                        rk, slot = stem.split("-")
                        row = rows.get(rk, rk[1:])
                        errors.setdefault(f"r{row}", f"Record {row}, photo of {sell.SLOT_LABEL[slot]}: {e}")
                finally:
                    fp.path.unlink(missing_ok=True)
            if errors:
                sell_upload.remove_tree(dest)
                return 400, {"ok": False, "errors": errors}, "invalid", {"errors": errors}
            rec = sell.append(clean, rid=sid)
        except BaseException:
            sell_upload.remove_tree(dest)
            raise
        log(f"sell submit: stored 1 submission ({len(clean['records'])} records, {len(clean['_files'])} photos)")
        return 200, {"ok": True, "submission": {"id": rec["id"], "name": rec["name"]}}, "thanks"

    # ------------------------------------------------------------ sell offer calculator (admin)
    def _sub(self, sid: str) -> dict | None:
        sub = sell.get(sid)
        if sub is None:
            self.send(404, {"error": "no such submission"})
        return sub

    def sell_admin_get(self, u):
        qs = parse_qs(u.query)
        sid = (qs.get("id") or [""])[0]
        if u.path == "/admin/api/sell/rules":
            return self.send(200, {"rules": sell_offer.load_rules(), "defaults": sell_offer.DEFAULT_RULES,
                                   "sleeve_grades": sell_offer.SLEEVE_GRADES})
        if u.path == "/admin/api/sell/detail":
            sub = self._sub(sid)
            return sub and self.send(200, sell_offer.detail(sub))
        if u.path == "/admin/api/sell/valuation.csv":
            sub = self._sub(sid)
            if sub:
                self.send(200, sell_offer.to_csv(sub).encode("utf-8-sig"), "text/csv; charset=utf-8",
                          extra={"Content-Disposition": f'attachment; filename="sell-valuation-{sid}.csv"'})
            return
        if u.path == "/admin/api/sell/photo":
            p, size = (qs.get("p") or [""])[0], (qs.get("size") or ["thumb"])[0]
            if not sell.PHOTO_STEM_RE.match(p) or size not in ("thumb", "full", "original") or sell.get(sid) is None:
                return self.send(404, {"error": "not found"})
            d = sell.photo_dir(sid)
            name, ctype = {"thumb": (f"{p}.thumb.webp", "image/webp"), "full": (f"{p}.webp", "image/webp"),
                           "original": (f"{p}.heic", "image/heic")}[size]
            f = d / name
            if f.parent != d or not f.is_file():
                return self.send(404, {"error": "not found"})
            return self.send(200, f.read_bytes(), ctype, extra={"Content-Disposition": f'inline; filename="{name}"'})
        return self.send(404, {"error": "not found"})

    def sell_admin_post(self, u):
        from mec import discogs
        b = self.jbody()
        if b is None:
            return
        try:
            if u.path == "/admin/api/sell/rules":
                rules, errs = sell_offer.save_rules(b.get("rules") or {})
                return self.send(400 if errs else 200, {"rules": rules, "errors": errs})
            sub = self._sub(str(b.get("id") or ""))
            if sub is None:
                return
            sid = sub["id"]
            if u.path == "/admin/api/sell/review":
                sell_offer.update_review(sid, sub, b)
                return self.send(200, sell_offer.detail(sub))
            if u.path == "/admin/api/sell/search":
                q = {k: b.get(k) for k in ("catno", "artist", "title") if b.get(k)}
                res = sell_offer.search(sid, sub, str(b.get("key") or ""), q or None)
                return self.send(200, dict(res, detail=sell_offer.detail(sub)))
            if u.path == "/admin/api/sell/pick":
                res = sell_offer.pick(sid, sub, str(b.get("key") or ""), b.get("release_id"),
                                      refresh=bool(b.get("refresh")))
                return self.send(200, dict(res, detail=sell_offer.detail(sub)))
        except discogs.DiscogsError as e:
            return self.send(502, {"error": str(e)[:300]})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": str(e)[:300]})
        return self.send(404, {"error": "not found"})

    def sell_result_page(self, qs):
        rid = (qs.get("r") or [""])[0]
        rec = sell.find(rid) if rid and rid != "ok" else None
        if rid:
            return self.send(200, render_sell_page("thanks", {"submission": rec}), "text/html; charset=utf-8")
        self.send_response(303)
        self.send_header("Location", "/sell/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def ebay_challenge(self, u):
        code = (parse_qs(u.query).get("challenge_code") or [""])[0]
        vt = os.environ.get("EBAY_DELETION_VERIFICATION_TOKEN", "")
        endpoint = os.environ.get("EBAY_DELETION_ENDPOINT_URL", "")
        if not (code and vt and endpoint):
            return self.send(404, {"error": "not configured"})
        h = hashlib.sha256((code + vt + endpoint).encode()).hexdigest()
        return self.send(200, {"challengeResponse": h})


def _e(v) -> str:
    import html
    return html.escape("" if v is None else str(v), quote=True)


def render_merch_page(state: str, data: dict) -> bytes:
    """Fill the built /merch/thanks/ template (site header/footer) with the result."""
    import re
    tpl_path = ROOT / "public" / "live" / "merch" / "thanks" / "index.html"
    try:
        tpl = tpl_path.read_text(encoding="utf-8")
    except OSError:
        tpl = "<!doctype html><meta charset=utf-8><title>Music Ed Cafe</title><!--MEC:RESULT--><!--/MEC:RESULT-->"
    if state == "thanks":
        rec = data.get("reservation") or {}
        rows = ""
        if rec:
            pairs = [("Item", rec.get("item_name"))] + [(k.capitalize(), v) for k, v in (rec.get("options") or {}).items()]
            pairs += [("Quantity", rec.get("qty")), ("Name", rec.get("name"))]
            if rec.get("note"):
                pairs.append(("Note", rec["note"]))
            rows = '<dl class="specs rd-specs">' + "".join(f"<div><dt>{_e(a)}</dt><dd>{_e(b)}</dd></div>" for a, b in pairs) + "</dl>"
        first = (rec.get("name") or "").split(" ")[0]
        inner = (f'<div class="reserve-done"><p class="rd-kicker">Reserved</p>'
                 f'<h1>Thanks{", " + _e(first) if first else ""}! Your reservation is in.</h1>{rows}'
                 "<p>No payment now. We'll email you when your item is ready to pay for and ship. "
                 "Reservations aren't binding.</p></div>")
    elif state == "invalid":
        errs = data.get("errors") or {}
        back = f"/merch/{data['slug']}/#reserve" if data.get("slug") else "/merch/"
        inner = ('<div class="reserve-done is-error"><p class="rd-kicker">Not reserved yet</p>'
                 "<h1>Almost there</h1><p>A couple of details need fixing before we can save your reservation:</p><ul>"
                 + "".join(f"<li>{_e(m)}</li>" for m in errs.values())
                 + f'</ul><p><a class="btn btn-buy" href="{_e(back)}">Go back and fix it</a></p>'
                 "<p>Tip: your browser's back button keeps what you already typed.</p></div>")
    else:
        inner = ('<div class="reserve-done is-error"><p class="rd-kicker">Not reserved</p><h1>Sorry, that didn\'t go through</h1>'
                 f"<p>{_e(data.get('message') or 'Please try again in a minute.')}</p></div>")
    out = re.sub(r"<!--MEC:RESULT-->.*?<!--/MEC:RESULT-->", lambda m: inner, tpl, count=1, flags=re.S)
    return out.encode("utf-8")


def render_sell_page(state: str, data: dict) -> bytes:
    """Fill the built /sell/thanks/ template (site header/footer) with the result."""
    import re
    tpl_path = ROOT / "public" / "live" / "sell" / "thanks" / "index.html"
    try:
        tpl = tpl_path.read_text(encoding="utf-8")
    except OSError:
        tpl = "<!doctype html><meta charset=utf-8><title>Music Ed Cafe</title><!--MEC:RESULT--><!--/MEC:RESULT-->"
    if state == "thanks":
        first = ((data.get("submission") or {}).get("name") or "").split(" ")[0]
        inner = ('<div class="reserve-done"><p class="rd-kicker">Received</p>'
                 f'<h1>Thanks{", " + _e(first) if first else ""}! We got your details.</h1>'
                 "<p>Every record gets looked at individually, so we'll review what you sent and email you with an offer "
                 "or a few follow-up questions. Offers are not final until we've seen photos or the records.</p></div>")
    elif state == "invalid":
        errs = data.get("errors") or {}
        inner = ('<div class="reserve-done is-error"><p class="rd-kicker">Not sent yet</p>'
                 "<h1>Almost there</h1><p>A couple of details need fixing before we can save your submission:</p><ul>"
                 + "".join(f"<li>{_e(m)}</li>" for m in errs.values())
                 + '</ul><p><a class="btn btn-buy" href="/sell/#sell-form">Go back and fix it</a></p>'
                 "<p>Tip: your browser's back button keeps what you already typed.</p></div>")
    else:
        inner = ('<div class="reserve-done is-error"><p class="rd-kicker">Not sent</p><h1>Sorry, that didn\'t go through</h1>'
                 f"<p>{_e(data.get('message') or 'Please try again in a minute.')}</p></div>")
    out = re.sub(r"<!--MEC:RESULT-->.*?<!--/MEC:RESULT-->", lambda m: inner, tpl, count=1, flags=re.S)
    return out.encode("utf-8")


def main():
    port = int(os.environ.get("PORT", "8081"))
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=discogs_scheduler, daemon=True).start()
    log(f"mec-admin on :{port} (token {'set' if TOKEN else 'NOT SET: admin disabled'})")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()

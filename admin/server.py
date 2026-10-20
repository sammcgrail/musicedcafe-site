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
  POST /merch/reserve          public merch reservation form (urlencoded; JSON reply when Accept: application/json,
                               otherwise a 303 redirect to GET /merch/reserve?r=<id>, the thank-you page)
  GET  /ebay/account-deletion  eBay Marketplace Account Deletion challenge (needed for a production keyset)
  POST /ebay/account-deletion  acknowledges notifications (this site stores no eBay buyer data)

Also runs the daily scheduled sync (config sync.daily_at_utc). With no API keys
the scheduled run records "skipped: no credentials" and changes nothing.
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

from mec import merch, store  # noqa: E402
from mec.cli import _load_env  # noqa: E402

_load_env()
TOKEN = os.environ.get("ADMIN_TOKEN", "")
PAGE = (Path(__file__).parent / "index.html").read_bytes()
LOCK = threading.Lock()
JOB = {"running": None, "started": None, "last": None}
MAX_BODY = 10 * 1024 * 1024
MERCH_MAX_BODY = 8 * 1024
MERCH_RL_SHORT = merch.RateLimiter(5, 600)      # per IP: 5 reservations / 10 minutes
MERCH_RL_DAY = merch.RateLimiter(20, 86400)     # per IP: 20 / day
MERCH_RL_ALL = merch.RateLimiter(500, 86400)    # whole site: 500 / day (stops a flood filling the disk)
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


def do_refresh():
    from mec.browse import sync
    res = sync()
    res.pop("state", None)
    try:  # Instagram blog posts: independent of eBay, never blocks the sync
        from mec import instagram
        res["instagram"] = instagram.sync(log=log)
    except Exception as e:
        res["instagram"] = {"result": "failed", "error": str(e)[:200]}
    if res.get("result") == "ok":
        from mec import merge, photos
        try:
            res["photos"] = photos.run(merge.build_records()[0], log=log)
        except Exception as e:  # photos are a nicety; never block the rebuild
            res["photos"] = {"error": str(e)}
        res["build"] = do_build()
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
        "last_build": datetime.fromtimestamp(live.stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if live.exists() else None,
        "job": {k: v for k, v in JOB.items()},
        "schedule_utc": (json.loads((ROOT / "config/site.json").read_text()).get("sync") or {}).get("daily_at_utc"),
    }


def scheduler():
    """Daily sync. Inert (records a skip) without API credentials."""
    from mec.util import config
    last_day = None
    while True:
        try:
            at = (config().get("sync") or {}).get("daily_at_utc", "10:15")
            hh, mm = (int(x) for x in at.split(":"))
            now = datetime.now(timezone.utc)
            if (now.hour, now.minute) >= (hh, mm) and last_day != now.date():
                last_day = now.date()
                log("scheduled sync starting")
                code, res = run_job("scheduled-sync", do_refresh)
                log(f"scheduled sync: {code} {res.get('result', res.get('error'))}")
        except Exception as e:
            log(f"scheduler error: {e}")
        time.sleep(60)


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

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/admin", "/admin/"):
            return self.send(200, PAGE, "text/html; charset=utf-8")
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
        if u.path == "/merch/reserve":
            return self.merch_result_page(parse_qs(u.query))
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
        if not u.path.startswith("/admin/api/"):
            return self.send(404, {"error": "not found"})
        if not self.authed():
            return
        qs = parse_qs(u.query)
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


def main():
    port = int(os.environ.get("PORT", "8081"))
    threading.Thread(target=scheduler, daemon=True).start()
    log(f"mec-admin on :{port} (token {'set' if TOKEN else 'NOT SET: admin disabled'})")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()

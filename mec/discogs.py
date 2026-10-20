"""Discogs marketplace integration: match records to Discogs releases, keep
approved records listed on Discogs, and catch Discogs sales so the record is
marked sold here and Edwin is told to end it on eBay.

eBay stays the source of truth for what exists, its price and its grades.
Discogs is a second shop window. See docs/DISCOGS-INTEGRATION.md.

Inert without DISCOGS_TOKEN (a personal access token from
https://www.discogs.com/settings/developers, read from the environment that
mec/cli.py `_load_env` fills from .env). Every entry point then returns
{"result": "skipped", "reason": "skipped: no DISCOGS_TOKEN"} and makes no call.

Files (all under data/discogs/, written atomically):
  matches.json   eBay item id -> candidates, auto status, chosen release, the
                 human decision (approved_by / approved_at), list_on_discogs
  listings.json  eBay item id -> Discogs listing_id, status, the fields last pushed
  settings.json  price rule, allow_offers, comment template, new-listing status
  state.json     last match / listing sync / poll, cached username, order cursor
  alerts.json    the admin "Alerts" feed (Discogs sales, double sales, errors)

Safety rules (covered by tests/test_marketplace_discogs.py):
  * matching never lists anything; it only writes matches.json
  * a listing is created only for a record whose match a human approved
    (status "approved" with approved_by + approved_at stored in matches.json)
    AND whose "list on Discogs" toggle is on AND that is active on eBay with a
    usable price and grades it can map. Nothing else can reach create_listing
  * external_id = the eBay item id, and the owner's inventory is read before
    any write, so a re-run adopts an existing listing instead of duplicating it
  * listings are deleted only when the record is ended/sold on eBay AND the
    latest eBay sync succeeded with a COMPLETE snapshot; if the latest eBay sync
    failed, the listing sync does nothing at all
  * the scheduled poll uses a read-only client (GET only: orders + inventory),
    and maps a sale to a record only through a listing we created (listings.json)
  * a Discogs sale marks the record sold with status_source "discogs"; the eBay
    sync never flips it back (store.apply_active_snapshot), only Edwin's
    explicit relist confirmation while eBay still shows quantity > 0
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import normalize as N
from . import store
from .util import DATA, config, load_json, now_iso, write_json

API = "https://api.discogs.com"
SITE = "https://www.discogs.com"
USER_AGENT = "MusicEdCafe/1.0 +https://musicedcafe.com"
SKIP_REASON = "skipped: no DISCOGS_TOKEN"
MIN_INTERVAL = 1.0          # seconds between requests: <= 60/min (the authenticated limit)
LOW_WATER = 3               # X-Discogs-Ratelimit-Remaining at or below this -> pause
LOW_WATER_PAUSE = 10.0      # the window is a 60 s moving average, so a short pause refills it
MAX_PAGES = 60
SELLER_HUB_ACTIVE = "https://www.ebay.com/sh/lst/active"

DIR = DATA / "discogs"
MATCHES = DIR / "matches.json"
LISTINGS = DIR / "listings.json"
SETTINGS = DIR / "settings.json"
STATE = DIR / "state.json"
ALERTS = DIR / "alerts.json"

_urlopen = urllib.request.urlopen   # tests replace this; nothing else should
_MLOCK = threading.RLock()          # serialises read-modify-write of matches.json across admin threads

# ----------------------------------------------------------------------------- vocab
# Verified against https://www.discogs.com/developers (Marketplace > New Listing),
# snapshot 2026-10-13. Values must be sent exactly as written.
MEDIA_CONDITIONS = ["Mint (M)", "Near Mint (NM or M-)", "Very Good Plus (VG+)", "Very Good (VG)",
                    "Good Plus (G+)", "Good (G)", "Fair (F)", "Poor (P)"]
SLEEVE_ONLY = ["Generic", "Not Graded", "No Cover"]
SLEEVE_CONDITIONS = MEDIA_CONDITIONS + SLEEVE_ONLY
LISTING_STATUSES = ["Draft", "For Sale"]

M, NM, VGP, VG, GP, G, F, P = MEDIA_CONDITIONS
# Edwin's grades (normalize.GRADE_ORDER) -> Discogs. Discogs has no EX / VG++ /
# VG-, so those round DOWN (never overgrade on a marketplace that rates sellers).
GRADE_MAP = {"M": M, "NM+": NM, "NM": NM, "NM-": NM, "M-": NM, "EX+": VGP, "EX": VGP, "VG++": VGP,
             "VG+": VGP, "VG": VG, "VG-": GP, "G+": GP, "G": G, "F": F, "P": P}
ROUNDED_DOWN = {"EX+", "EX", "VG++", "VG-"}

DEFAULT_TEMPLATE = ("Vinyl: {vinyl}. Sleeve: {sleeve}.{notes}\n"
                    "Ships from Lynn, MA. Photos and full details: {record_url}")
DEFAULT_SETTINGS = {
    "price_mode": "same",          # "same" (eBay price) or "percent"
    "price_percent": 0,            # e.g. -10 = 10% below eBay; only with price_mode "percent"
    "price_rounding": ".99",       # ".99", ".00" or "none"; only with price_mode "percent"
    "allow_offers": False,
    "listing_status": "Draft",     # status for NEW listings: "Draft" (default) or "For Sale"
    "comment_template": DEFAULT_TEMPLATE,
    "location": "",                # Discogs' PRIVATE storage-location field (not "ships from")
    "auto_sync_listings": False,   # push listing changes after each successful eBay sync
    "currency": "USD",
}
COMMENT_MAX = 1000                 # our own cap; Discogs does not document one


class DiscogsError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ReadOnlyViolation(DiscogsError):
    pass


# ----------------------------------------------------------------------------- client

def get_token() -> str | None:
    return (os.environ.get("DISCOGS_TOKEN") or "").strip() or None


def make_client(read_only: bool = False) -> "Client | None":
    tok = get_token()
    return Client(tok, read_only=read_only) if tok else None


class Client:
    """Official Discogs API over urllib. Throttled to <= 60 requests/min,
    honours X-Discogs-Ratelimit-Remaining, retries 429 with backoff.
    The token is only ever put in the Authorization header, never logged."""

    def __init__(self, token: str, *, read_only: bool = False, urlopen=None, sleep=time.sleep,
                 clock=time.monotonic, min_interval: float = MIN_INTERVAL, retries: int = 5):
        if not token:
            raise DiscogsError(SKIP_REASON)
        self._token = token
        self.read_only = read_only
        self._urlopen = urlopen
        self.sleep, self.clock = sleep, clock
        self.min_interval, self.retries = min_interval, retries
        self._last = None
        self.remaining = None
        self.calls = 0

    def __repr__(self):
        return f"<discogs.Client read_only={self.read_only}>"

    def _throttle(self):
        if self._last is not None:
            wait = self._last + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _note_rate(self, headers):
        if headers is None:
            return
        rem = headers.get("X-Discogs-Ratelimit-Remaining")
        if rem is None:
            return
        try:
            self.remaining = int(rem)
        except ValueError:
            return
        if self.remaining <= LOW_WATER:
            self.sleep(LOW_WATER_PAUSE)

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        if self.read_only and method != "GET":
            raise ReadOnlyViolation(f"read-only client refused {method} {path}")
        q = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Discogs token={self._token}", "User-Agent": USER_AGENT,
                   "Accept": "application/vnd.discogs.v2.discogs+json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        opener = self._urlopen or _urlopen
        last = None
        for attempt in range(self.retries):
            self._throttle()
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with opener(req, timeout=30) as r:
                    raw = r.read()
                    self._note_rate(r.headers)
                return json.loads(raw) if raw and raw.strip() else {}
            except urllib.error.HTTPError as e:
                self._note_rate(e.headers)
                try:
                    msg = json.loads(e.read() or b"{}").get("message", "")
                except Exception:
                    msg = ""
                last = DiscogsError(f"HTTP {e.code} {method} {path}: {str(msg)[:200]}", e.code)
                # 429 = rejected before processing, safe to retry for any method.
                # 5xx: retry reads only; a write may have happened (the next run's
                # inventory read + external_id adopts it instead of duplicating).
                if e.code == 429 or (e.code >= 500 and method == "GET"):
                    ra = (e.headers or {}).get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else min(60.0, 5.0 * 2 ** attempt)
                    except ValueError:
                        wait = min(60.0, 5.0 * 2 ** attempt)
                    self.sleep(wait)
                    continue
                raise last
            except urllib.error.URLError as e:
                last = DiscogsError(f"network error {method} {path}: {e.reason}")
                if method != "GET":
                    raise last
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
        raise last or DiscogsError(f"{method} {path} failed")

    def paged(self, path: str, key: str, params: dict | None = None, per_page: int = 100) -> list:
        out, page = [], 1
        while True:
            res = self.request("GET", path, dict(params or {}, page=page, per_page=per_page))
            out += res.get(key) or []
            pages = int((res.get("pagination") or {}).get("pages") or 1)
            if page >= pages:
                return out
            page += 1
            if page > MAX_PAGES:
                raise DiscogsError(f"{path}: more than {MAX_PAGES} pages, refusing a partial read")

    # endpoints (paths and parameter names from the Discogs API docs)
    def identity(self) -> dict:
        return self.request("GET", "/oauth/identity")

    def search(self, **params) -> dict:
        return self.request("GET", "/database/search", dict(params, per_page=params.get("per_page", 50)))

    def release(self, release_id) -> dict:
        return self.request("GET", f"/releases/{int(release_id)}")

    def create_listing(self, fields: dict) -> dict:
        return self.request("POST", "/marketplace/listings", body=fields)

    def edit_listing(self, listing_id, fields: dict) -> dict:
        return self.request("POST", f"/marketplace/listings/{int(listing_id)}", body=fields)

    def delete_listing(self, listing_id) -> dict:
        return self.request("DELETE", f"/marketplace/listings/{int(listing_id)}")

    def inventory(self, username: str, status: str | None = None) -> list:
        return self.paged(f"/users/{urllib.parse.quote(username)}/inventory", "listings",
                          {"status": status})

    def orders(self, status: str | None = None, created_after: str | None = None) -> list:
        return self.paged("/marketplace/orders", "orders",
                          {"status": status, "created_after": created_after,
                           "sort": "created", "sort_order": "desc"})


# ----------------------------------------------------------------------------- files

def _load(path, default):
    v = load_json(path, None)
    return default if v is None else v


def load_matches() -> dict:
    return _load(MATCHES, {})


def save_matches(m: dict):
    write_json(MATCHES, dict(sorted(m.items())))


def _merge_match_results(changed: dict, flags: dict):
    """Write a match run's results without clobbering decisions made meanwhile."""
    with _MLOCK:
        cur = load_matches()
        for k, v in changed.items():
            c = cur.get(k) or {}
            if c.get("status") in ("approved", "skipped"):
                continue          # a human decided while we were searching
            for keep in ("list_on_discogs", "sleeve_condition", "history"):
                if keep in c:
                    v[keep] = c[keep]
            cur[k] = v
        for k, ts in flags.items():
            if k in cur and not cur[k].get("record_changed_since_decision"):
                cur[k]["record_changed_since_decision"] = ts
        save_matches(cur)
        return cur


def load_listings() -> dict:
    return _load(LISTINGS, {})


def save_listings(x: dict):
    write_json(LISTINGS, dict(sorted(x.items())))


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in (_load(SETTINGS, {}) or {}).items() if k in DEFAULT_SETTINGS})
    return s


def validate_settings(new: dict) -> tuple[dict, list[str]]:
    s, errs = load_settings(), []
    for k, v in (new or {}).items():
        if k not in DEFAULT_SETTINGS:
            errs.append(f"unknown setting {k}")
            continue
        s[k] = v
    if s["price_mode"] not in ("same", "percent"):
        errs.append("price_mode must be 'same' or 'percent'")
    try:
        s["price_percent"] = float(s["price_percent"])
        if not -90 <= s["price_percent"] <= 200:
            errs.append("price_percent must be between -90 and 200")
    except (TypeError, ValueError):
        errs.append("price_percent must be a number")
    if s["price_rounding"] not in (".99", ".00", "none"):
        errs.append("price_rounding must be '.99', '.00' or 'none'")
    if s["listing_status"] not in LISTING_STATUSES:
        errs.append("listing_status must be 'Draft' or 'For Sale'")
    for b in ("allow_offers", "auto_sync_listings"):
        if not isinstance(s[b], bool):
            errs.append(f"{b} must be true or false")
    if not isinstance(s["comment_template"], str) or len(s["comment_template"]) > 2000:
        errs.append("comment_template must be text (max 2000 chars)")
    else:
        try:
            s["comment_template"].format_map(_Blank())
        except (ValueError, IndexError, AttributeError) as e:
            errs.append(f"comment_template is not a valid template: {e}")
    if not isinstance(s["location"], str) or len(s["location"]) > 200:
        errs.append("location must be short text")
    return s, errs


def save_settings(new: dict) -> tuple[dict, list[str]]:
    s, errs = validate_settings(new)
    if not errs:
        write_json(SETTINGS, s)
    return s, errs


def load_state() -> dict:
    return _load(STATE, {})


def _update_state(**kw):
    st = load_state()
    st.update(kw)
    write_json(STATE, st)
    return st


# ----------------------------------------------------------------------------- alerts

ALERT_HOOKS: list = []   # callables(alert: dict); register one to forward alerts (email, etc.)


def register_alert_hook(fn):
    ALERT_HOOKS.append(fn)
    return fn


def load_alerts() -> list:
    return _load(ALERTS, [])


def add_alert(kind: str, message: str, *, severity: str = "info", item_id: str | None = None,
              links: dict | None = None, data: dict | None = None) -> dict:
    alerts = load_alerts()
    ts = now_iso()
    a = {"id": f"{ts}-{kind}-{item_id or len(alerts)}", "at": ts, "type": kind, "severity": severity,
         "item_id": item_id, "message": message, "links": links or {}, "data": data or {},
         "acked_at": None}
    write_json(ALERTS, ([a] + alerts)[:500])
    for h in list(ALERT_HOOKS):
        try:
            h(a)
        except Exception:
            pass   # a broken hook never blocks the feed
    return a


def ack_alerts(ids: list[str] | None = None) -> int:
    alerts, n, ts = load_alerts(), 0, now_iso()
    for a in alerts:
        if not a.get("acked_at") and (ids is None or a["id"] in ids):
            a["acked_at"], n = ts, n + 1
    write_json(ALERTS, alerts)
    return n


# ----------------------------------------------------------------------------- normalising

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return s.lower().replace("&", " and ")


def norm_catno(s) -> str:
    """'SP-4658' == 'sp 4658' == 'SP4658'. Spaces, dashes, dots, slashes dropped."""
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", str(s or "")).upper())


def catno_set(s) -> set[str]:
    out = set()
    for part in re.split(r"\s*[,;]\s*", str(s or "")):
        n = norm_catno(part)
        if n and n not in ("NONE", "NA", "NOTONLABEL"):
            out.add(n)
    return out


LABEL_NOISE = {"records", "record", "recording", "recordings", "inc", "ltd", "co", "company", "corp",
               "corporation", "the", "music", "label", "labels", "llc", "of", "america", "usa", "us"}


def norm_label(s) -> str:
    words = re.findall(r"[a-z0-9]+", _fold(re.sub(r"\(\d+\)$", "", str(s or "").strip())))
    return " ".join(w for w in words if w not in LABEL_NOISE)


COUNTRY_ALIASES = {"united states": "us", "usa": "us", "u s a": "us", "us": "us", "u s": "us",
                   "united states of america": "us", "america": "us", "united kingdom": "uk",
                   "uk": "uk", "england": "uk", "great britain": "uk", "germany": "germany",
                   "west germany": "germany", "holland": "netherlands"}


def norm_country(s) -> str | None:
    if not s:
        return None
    k = " ".join(re.findall(r"[a-z]+", _fold(s)))
    return COUNTRY_ALIASES.get(k, k) or None


def _toks(s) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _fold(s)) if w not in ("the", "and", "a", "of")}


def record_format(rec: dict) -> str | None:
    t = (rec.get("format_type") or "").lower()
    size = (rec.get("record_size") or "").replace("”", '"').replace("''", '"')
    speed = (rec.get("speed") or "")
    if "lp" in t or t == "album":
        return "LP"
    if size.startswith("7"):
        return '7"'
    if size.startswith("10"):
        return '10"'
    if size.startswith("12"):
        if t in ("single", "ep", "45", "maxi-single") or speed.startswith("45"):
            return '12" single'
        if speed.startswith("33"):
            return "LP"
        return '12"'
    if t in ("single", "45") or speed.startswith("45"):
        return "single (size unknown)"
    return None


def candidate_format(fmts) -> str | None:
    f = [str(x).lower() for x in (fmts or [])]
    if not f:
        return None
    if "vinyl" not in f and any(x in f for x in ("cd", "cassette", "cdr", "file", "dvd", "8-track cartridge",
                                                    "reel-to-reel", "minidisc", "sacd", "blu-ray")):
        return "not vinyl"
    if '7"' in f:
        return '7"'
    if '10"' in f:
        return '10"'
    if "lp" in f:
        return "LP"
    if '12"' in f:
        return '12" single' if any(x in f for x in ("single", "maxi-single", "ep", "45 rpm")) else '12"'
    return "vinyl (size unknown)" if "vinyl" in f else None


def format_compat(rf: str | None, cf: str | None) -> str:
    if cf == "not vinyl":
        return "inconsistent"
    if not rf or not cf or cf == "vinyl (size unknown)":
        return "unknown"
    if rf == cf:
        return "consistent"
    ok = {("single (size unknown)", '7"'), ("single (size unknown)", '12" single'), ("LP", '12"'),
          ('12" single', '12"'), ('12"', '12" single'), ('12"', "LP")}
    return "consistent" if (rf, cf) in ok else "inconsistent"


def record_year(rec: dict) -> str | None:
    """Pressing year, else release year. A year that only appears in the eBay
    title (listing_year) is never used: it could be either."""
    for f in ("pressing_year", "release_year"):
        m = re.search(r"\b(19|20)\d\d\b", str(rec.get(f) or ""))
        if m:
            return m.group(0)
    return None


# ----------------------------------------------------------------------------- matching

def score_candidate(rec: dict, c: dict) -> dict:
    """c is a /database/search result. Returns the stored candidate with signals."""
    sig = {}
    score = 0
    rc = catno_set(rec.get("catalog_number"))
    cc = catno_set(c.get("catno"))
    sig["catno"] = bool(rc and cc and rc & cc)
    score += 40 if sig["catno"] else 0
    rl = norm_label(rec.get("label"))
    cls = [norm_label(x) for x in (c.get("label") or [])]
    if rl and rl in cls:
        sig["label"] = "exact"
        score += 20
    elif rl and any(x and (set(rl.split()) <= set(x.split()) or set(x.split()) <= set(rl.split())) for x in cls):
        sig["label"] = "partial"
        score += 8
    else:
        sig["label"] = "none" if rl else "unknown"
    sig["format"] = format_compat(record_format(rec), candidate_format(c.get("format")))
    score += {"consistent": 15, "unknown": 0, "inconsistent": -40}[sig["format"]]
    t = str(c.get("title") or "")
    c_artist, _, c_title = t.partition(" - ")
    a_ok = bool(_toks(rec.get("artist")) & _toks(c_artist)) if rec.get("artist") else False
    at, ct = _toks(rec.get("album")), _toks(c_title or t)
    t_ok = bool(at and ct and len(at & ct) / len(at | ct) >= 0.5)
    sig["artist"], sig["title"] = a_ok, t_ok
    score += (7 if a_ok else 0) + (8 if t_ok else 0)
    ry, cy = record_year(rec), str(c.get("year") or "").strip() or None
    if ry and cy:
        d = abs(int(ry) - int(cy)) if cy.isdigit() else 99
        sig["year"] = "exact" if d == 0 else ("close" if d == 1 else "different")
        score += {"exact": 10, "close": 3, "different": -5}[sig["year"]]
    else:
        sig["year"] = "unknown"
    rco, cco = norm_country(rec.get("country")), norm_country(c.get("country"))
    if rco and cco:
        sig["country"] = "match" if rco == cco else "different"
        score += 10 if rco == cco else -10
    else:
        sig["country"] = "unknown"
    rid = c.get("id")
    uri = c.get("uri") or ""
    return {"release_id": rid, "title": t, "label": c.get("label") or [], "catno": c.get("catno"),
            "format": c.get("format") or [], "year": cy, "country": c.get("country"),
            "thumb": c.get("thumb") or c.get("cover_image") or "",
            "url": (SITE + uri) if uri.startswith("/") else f"{SITE}/release/{rid}",
            "score": score, "signals": sig}


def classify(rec: dict, cands: list[dict], truncated: bool = False) -> tuple[str, int | None, str]:
    """-> (status, chosen release_id or None, reason). Never looks at artwork."""
    if not cands:
        return "no_match", None, "Discogs search returned no vinyl candidates"
    cands = sorted(cands, key=lambda c: -c["score"])
    # every pressing that shares the catalog number (and could be this format)
    group = [c for c in cands if c["signals"]["catno"] and c["signals"]["format"] != "inconsistent"]
    strong = [c for c in group if c["signals"]["label"] == "exact" and c["signals"]["format"] == "consistent"]
    if not strong:
        if not group and cands[0]["score"] < 30:
            return "no_match", None, "no candidate shares the catalog number and the best score is low"
        why = ("catalog number matches but the label or format does not confirm it" if group
               else "no candidate shares the catalog number")
        return "review", cands[0]["release_id"], why
    if truncated:
        return "review", strong[0]["release_id"], "search results were truncated; other pressings may exist"
    if len(group) == 1:
        return "confident", strong[0]["release_id"], "single release with this catno + label, format consistent"
    rco, ry = norm_country(rec.get("country")), record_year(rec)
    if not (rco or ry):
        return "review", strong[0]["release_id"], (f"{len(group)} pressings share catno; the record has "
                                                   "no country or year to tell them apart")
    survivors = [c for c in group
                 if (not rco or c["signals"]["country"] == "match") and (not ry or c["signals"]["year"] == "exact")]
    if len(survivors) == 1 and survivors[0] in strong:
        used = " and ".join(x for x, v in (("country", rco), ("year", ry)) if v)
        return "confident", survivors[0]["release_id"], (f"{len(group)} pressings share catno; the record's "
                                                         f"{used} pick exactly one")
    return "review", strong[0]["release_id"], (f"{len(group)} pressings share catno and the record's country/"
                                               f"year leave {len(survivors)}")


def fingerprint(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("artist", "album", "label", "catalog_number", "format_type",
                                    "record_size", "speed", "release_year", "pressing_year", "country")}


def _search(client: Client, params: dict) -> tuple[list, bool]:
    res = client.search(type="release", **params)
    results = res.get("results") or []
    items = int((res.get("pagination") or {}).get("items") or len(results))
    return results, items > len(results)


def find_candidates(rec: dict, client: Client) -> tuple[list[dict], bool, list[dict]]:
    """Label + catno first; artist + title + format + year only when that finds
    no catno match. Returns (scored candidates, truncated, queries run)."""
    raw, queries, truncated = {}, [], False
    cat, label = rec.get("catalog_number"), rec.get("label")
    if cat:
        q = {"catno": cat, "label": label} if label else {"catno": cat}
        res, tr = _search(client, q)
        queries.append(q)
        if not res and label:   # label spelled differently on Discogs
            q = {"catno": cat, "artist": rec.get("artist")} if rec.get("artist") else {"catno": cat}
            res, tr = _search(client, q)
            queries.append(q)
        truncated |= tr
        for r in res:
            raw[r.get("id")] = r
    scored = [score_candidate(rec, r) for r in raw.values()]
    if not any(c["signals"]["catno"] for c in scored) and rec.get("artist") and rec.get("album"):
        q = {"artist": rec["artist"], "release_title": rec["album"], "format": "Vinyl"}
        y = record_year(rec)
        res, tr = _search(client, dict(q, year=y) if y else q)
        queries.append(dict(q, year=y) if y else q)
        if not res and y:
            res, tr = _search(client, q)
            queries.append(q)
        for r in res:
            if r.get("id") not in raw:
                raw[r.get("id")] = r
                scored.append(score_candidate(rec, r))
        # truncation only matters for the catno group (pressings that share it)
        truncated |= tr and any(c["signals"]["catno"] for c in scored)
    scored.sort(key=lambda c: -c["score"])
    return scored, truncated, queries


def match_records(records: list[dict] | None = None, client: Client | None = None, ids=None,
                  rematch: bool = False, limit: int | None = None, log=print) -> dict:
    """Dry run: fills data/discogs/matches.json. Never lists, edits or deletes.
    Human decisions (approved / skipped) are never overwritten."""
    client = client or make_client(read_only=True)   # search only: a write is impossible here
    if not client:
        log(SKIP_REASON)
        _update_state(last_match={"at": now_iso(), "result": "skipped", "reason": SKIP_REASON})
        return {"result": "skipped", "reason": SKIP_REASON}
    if records is None:
        from . import merge
        records = merge.build_records()[0]
    matches = load_matches()
    todo = [r for r in records if r.get("status") == "active" and r.get("kind", "record") == "record"
            and (not ids or r["id"] in ids)]
    counts, errors, done = {}, [], 0
    changed, flags = {}, {}
    for rec in todo:
        iid = rec["id"]
        cur = matches.get(iid) or {}
        fp = fingerprint(rec)
        if cur.get("status") in ("approved", "skipped"):
            if cur.get("fingerprint") != fp and not cur.get("record_changed_since_decision"):
                flags[iid] = now_iso()
            continue
        if cur and cur.get("fingerprint") == fp and not rematch:
            continue
        if limit is not None and done >= limit:
            break
        try:
            cands, truncated, queries = find_candidates(rec, client)
        except DiscogsError as e:
            errors.append(f"{iid}: {e}")
            if e.status in (401, 403):
                break     # bad token: stop, keep what we have
            continue
        status, chosen, reason = classify(rec, cands, truncated)
        matches[iid] = changed[iid] = {
            **{k: v for k, v in cur.items() if k in ("list_on_discogs", "sleeve_condition", "history")},
            "status": status, "auto_status": status, "reason": reason, "chosen_release_id": chosen,
            "candidates": cands[:5], "candidate_count": len(cands), "truncated": truncated,
            "queries": queries, "matched_at": now_iso(), "fingerprint": fp,
            "ebay_title": rec.get("ebay_title"),
            "approved_release_id": None, "approved_by": None, "approved_at": None,
        }
        counts[status] = counts.get(status, 0) + 1
        done += 1
        if done % 25 == 0:
            _merge_match_results(changed, flags)
            log(f"discogs match: {done} records matched so far")
    matches = _merge_match_results(changed, flags)
    summary = {"result": "ok" if not errors else "partial", "matched": done, "new_status_counts": counts,
               "errors": errors[:50], "status_counts": status_counts(matches)}
    _update_state(last_match={"at": now_iso(), "result": summary["result"], "matched": done,
                              "errors": errors[:20]})
    return summary


def status_counts(matches: dict | None = None) -> dict:
    out = {}
    for m in (load_matches() if matches is None else matches).values():
        out[m.get("status")] = out.get(m.get("status"), 0) + 1
    return out


# ----------------------------------------------------------------------------- decisions

def _hist(m: dict, action: str, by: str, **kw):
    m.setdefault("history", []).append({"at": now_iso(), "action": action, "by": by, **kw})
    m["history"] = m["history"][-20:]


def _decide(item_id: str, action: str, *, release_id=None, by: str = "admin", sleeve_condition=None,
           client: Client | None = None) -> dict:
    """approve | pick | manual | skip | reset | set_sleeve. Raises ValueError on a bad request."""
    matches = load_matches()
    m = matches.get(item_id)
    if m is None:
        if action not in ("manual", "skip"):
            raise ValueError("no match entry for this record yet: run the match first")
        m = {"status": "no_match", "auto_status": "no_match", "candidates": [], "chosen_release_id": None}
    by = (str(by or "admin").strip() or "admin")[:60]
    ts = now_iso()
    if action in ("approve", "pick", "manual"):
        if action == "approve":
            rid = m.get("chosen_release_id")
            if not rid:
                raise ValueError("nothing to approve: there is no suggested release")
        else:
            try:
                rid = int(str(release_id).strip().lstrip("rR[").rstrip("]"))
            except (TypeError, ValueError):
                raise ValueError("release_id must be a number") from None
            if rid <= 0:
                raise ValueError("release_id must be positive")
            if action == "pick" and rid not in [c.get("release_id") for c in m.get("candidates") or []]:
                raise ValueError("that release is not one of the candidates (use manual)")
        info = None
        if action == "manual":
            info = {"release_id": rid, "url": f"{SITE}/release/{rid}", "verified": False}
            if client:
                try:
                    rel = client.release(rid)
                    info.update(title=f"{', '.join(a.get('name', '') for a in rel.get('artists') or [])} - "
                                      f"{rel.get('title', '')}",
                                verified=True, year=rel.get("year"), country=rel.get("country"),
                                thumb=rel.get("thumb") or "", url=rel.get("uri") or info["url"])
                except DiscogsError as e:
                    if e.status == 404:
                        raise ValueError(f"Discogs has no release {rid}") from None
                    info["verify_error"] = str(e)[:200]
            m["manual_release"] = info
        m.update(status="approved", chosen_release_id=rid, approved_release_id=rid, approved_by=by,
                 approved_at=ts, approved_via=action, record_changed_since_decision=None)
        _hist(m, action, by, release_id=rid)
    elif action == "skip":
        m.update(status="skipped", approved_release_id=None, approved_by=None, approved_at=None,
                 list_on_discogs=False, skipped_by=by, skipped_at=ts)
        _hist(m, "skip", by)
    elif action == "reset":
        m.update(status=m.get("auto_status") or "review", approved_release_id=None, approved_by=None,
                 approved_at=None, list_on_discogs=False)
        _hist(m, "reset", by)
    elif action == "set_sleeve":
        if sleeve_condition not in (None, "", *SLEEVE_CONDITIONS):
            raise ValueError("sleeve_condition must be one of the Discogs sleeve values")
        m["sleeve_condition"] = sleeve_condition or None
        _hist(m, "set_sleeve", by, sleeve_condition=sleeve_condition or None)
    else:
        raise ValueError(f"unknown action {action}")
    matches[item_id] = m
    save_matches(matches)
    return m


def _approve_all_confident(expected: int, by: str = "admin") -> dict:
    """Bulk approval needs the count the human saw, so a stale page can't approve more."""
    matches = load_matches()
    todo = [k for k, m in matches.items() if m.get("status") == "confident" and m.get("chosen_release_id")]
    if int(expected) != len(todo):
        raise ValueError(f"the confident count changed ({len(todo)} now, you confirmed {expected}); reload")
    ts = now_iso()
    for k in todo:
        m = matches[k]
        m.update(status="approved", approved_release_id=m["chosen_release_id"], approved_at=ts,
                 approved_by=f"{by} (approve all confident)", approved_via="approve_all_confident")
        _hist(m, "approve_all_confident", by, release_id=m["chosen_release_id"])
    save_matches(matches)
    return {"approved": len(todo)}


def _set_listing_toggle(item_id: str, on: bool, by: str = "admin") -> dict:
    matches = load_matches()
    m = matches.get(item_id)
    if not m:
        raise ValueError("no match entry for this record")
    if on and m.get("status") != "approved":
        raise ValueError("approve the Discogs match before turning on listing")
    m["list_on_discogs"] = bool(on)
    _hist(m, "list_on" if on else "list_off", by)
    save_matches(matches)
    if not on:   # off clears a hand-removed / deleted listing so a later "on" creates a fresh one
        listings = load_listings()
        L = listings.get(item_id)
        if L and L.get("status") in ("removed_on_discogs", "deleted"):
            listings[item_id] = {"previous": ((L.get("previous") or []) + [L])[-5:]}
            save_listings(listings)
    return m


def _list_all_approved(expected: int, by: str = "admin") -> dict:
    matches = load_matches()
    todo = [k for k, m in matches.items() if m.get("status") == "approved" and not m.get("list_on_discogs")]
    if int(expected) != len(todo):
        raise ValueError(f"the count changed ({len(todo)} now, you confirmed {expected}); reload")
    for k in todo:
        matches[k]["list_on_discogs"] = True
        _hist(matches[k], "list_on (bulk)", by)
    save_matches(matches)
    return {"turned_on": len(todo)}


def is_approved(m: dict | None) -> bool:
    return bool(m and m.get("status") == "approved" and m.get("approved_release_id")
                and m.get("approved_by") and m.get("approved_at"))


# ----------------------------------------------------------------------------- listing fields

def map_grade(text, sleeve: bool = False) -> tuple[str | None, str | None]:
    """Edwin's grade text -> (Discogs value, note). Unknown -> (None, problem)."""
    if text is None or not str(text).strip():
        return None, "missing"
    t = str(text).strip()
    for v in (SLEEVE_CONDITIONS if sleeve else MEDIA_CONDITIONS):
        if t.lower() == v.lower():
            return v, None
    if sleeve and t.lower() in ("no sleeve", "none"):
        return "No Cover", None
    g = N.norm_grade(t)
    if g and g.upper() in GRADE_MAP:
        g = g.upper()
        return GRADE_MAP[g], (f"{g} has no Discogs equivalent; listed as {GRADE_MAP[g]}" if g in ROUNDED_DOWN else None)
    return None, f"unknown grade {t!r}"


GENERIC_RE = re.compile(r"\b(generic|company|plain|die[- ]cut)\b[^.\n]{0,30}\bsleeve", re.I)


def conditions_for(rec: dict, m: dict | None = None) -> dict:
    out = {"condition": None, "sleeve_condition": None, "problems": [], "notes": []}
    c, note = map_grade(rec.get("media_grade"))
    if c:
        out["condition"] = c
        if note:
            out["notes"].append(f"vinyl: {note}")
    else:
        out["problems"].append(f"vinyl grade {note}: set it on eBay or in the editorial file")
    override = (m or {}).get("sleeve_condition")
    if override:
        out["sleeve_condition"] = override
        out["notes"].append(f"sleeve: set by hand to {override}")
        return out
    s, note = map_grade(rec.get("sleeve_grade"), sleeve=True)
    if s:
        out["sleeve_condition"] = s
        if note:
            out["notes"].append(f"sleeve: {note}")
        return out
    if note == "missing":
        text = " ".join(str(rec.get(k) or "") for k in ("sleeve_grade_note", "condition_notes", "description_text"))
        single = record_format(rec) in ('7"', "single (size unknown)")
        if single and GENERIC_RE.search(text) and not re.search(r"picture sleeve|jacket", text, re.I):
            out["sleeve_condition"] = "Generic"
            out["notes"].append("sleeve: Generic (the description mentions a generic/company sleeve)")
            return out
        out["problems"].append("sleeve grade missing: pick one (or Generic / Not Graded / No Cover)")
    else:
        out["problems"].append(f"sleeve grade {note}")
    return out


def _dec(x) -> Decimal | None:
    try:
        d = Decimal(str(x).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def apply_price_rule(price, settings: dict | None = None) -> Decimal | None:
    s = settings or DEFAULT_SETTINGS
    p = _dec(price)
    if p is None or p <= 0:
        return None
    if s.get("price_mode", "same") == "same":
        return p.quantize(Decimal("0.01"), ROUND_HALF_UP)
    q = p * (Decimal(1) + Decimal(str(s.get("price_percent") or 0)) / Decimal(100))
    r = s.get("price_rounding", ".99")
    if r == ".99":
        out = q.quantize(Decimal(1), ROUND_HALF_UP) - Decimal("0.01")
        return max(out, Decimal("0.99"))
    if r == ".00":
        return max(q.quantize(Decimal(1), ROUND_HALF_UP), Decimal(1)).quantize(Decimal("0.01"))
    return max(q.quantize(Decimal("0.01"), ROUND_HALF_UP), Decimal("0.01"))


class _Blank(dict):
    def __missing__(self, key):
        return ""


def record_url(rec: dict) -> str:
    return (config().get("base_url") or "https://musicedcafe.com").rstrip("/") + (rec.get("path") or "/")


def render_comment(rec: dict, cond: dict, settings: dict) -> str:
    notes = rec.get("condition_notes") or " ".join(
        x for x in (rec.get("media_grade_note"), rec.get("sleeve_grade_note")) if x)
    notes = re.sub(r"\s+", " ", str(notes or "")).strip()
    if len(notes) > 400:
        notes = notes[:397].rsplit(" ", 1)[0] + "..."
    vals = _Blank(vinyl=cond.get("condition") or "", sleeve=cond.get("sleeve_condition") or "",
                  vinyl_grade=rec.get("media_grade") or "", sleeve_grade=rec.get("sleeve_grade") or "",
                  media_note=rec.get("media_grade_note") or "", sleeve_note=rec.get("sleeve_grade_note") or "",
                  notes=(" " + notes) if notes else "", condition_notes=notes,
                  pressing_notes=rec.get("pressing_notes") or "", record_url=record_url(rec),
                  ebay_url=rec.get("ebay_url") or "", artist=rec.get("artist") or "",
                  album=rec.get("album") or "", label=rec.get("label") or "",
                  catalog_number=rec.get("catalog_number") or "", item_id=rec.get("id") or "")
    try:
        text = str(settings.get("comment_template") or DEFAULT_TEMPLATE).format_map(vals)
    except (ValueError, IndexError, AttributeError):
        text = DEFAULT_TEMPLATE.format_map(vals)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text[:COMMENT_MAX]


def desired_listing(rec: dict, m: dict | None, settings: dict) -> tuple[dict | None, list[str], list[str]]:
    """-> (fields without status, problems, notes). fields is None when it can't be listed."""
    problems = []
    if not is_approved(m):
        problems.append("match not approved")
    elif not m.get("list_on_discogs"):
        problems.append("'list on Discogs' is off")
    if rec.get("status") != "active":
        problems.append(f"not active on eBay ({rec.get('status')})")
    if rec.get("kind", "record") != "record":
        problems.append("not a record")
    if (rec.get("currency") or "USD") != settings.get("currency", "USD"):
        problems.append(f"price is in {rec.get('currency')}, Discogs prices are {settings.get('currency')}")
    price = apply_price_rule(rec.get("price"), settings)
    if price is None:
        problems.append("no usable eBay price")
    cond = conditions_for(rec, m)
    problems += cond["problems"]
    if problems:
        return None, problems, cond["notes"]
    fields = {"release_id": int(m["approved_release_id"]), "condition": cond["condition"],
              "sleeve_condition": cond["sleeve_condition"], "price": float(price),
              "comments": render_comment(rec, cond, settings),
              "allow_offers": bool(settings.get("allow_offers")), "external_id": str(rec["id"]),
              "location": settings.get("location") or "", "weight": "auto", "format_quantity": "auto"}
    return fields, [], cond["notes"]


COMPARE = ("release_id", "condition", "sleeve_condition", "price", "comments", "allow_offers", "location")
LIVE = ("For Sale", "Draft", "Expired", "unlisted")


def _differs(a: dict | None, b: dict) -> bool:
    a = a or {}
    return any(a.get(k) != b.get(k) for k in COMPARE)


# ----------------------------------------------------------------------------- listing sync

def ebay_gate() -> tuple[bool, bool, str]:
    """(may_run, may_delete, why). Same rule as browse.py: a failed or partial
    eBay sync changes nothing; only a complete snapshot can end things."""
    st = store.read_state()
    if st.get("last_result") != "ok":
        return False, False, (f"the latest eBay sync ({st.get('last_attempt_source')}, "
                              f"{st.get('last_attempt')}) did not succeed ({st.get('last_result')}); nothing changed")
    complete = bool((st.get("last_success_summary") or {}).get("complete_snapshot"))
    return True, complete, "ok" if complete else "latest eBay snapshot was partial: deletes held back"


def plan_listings(records: list[dict], matches: dict, listings: dict, settings: dict, inventory: list | None,
                  may_delete: bool) -> list[dict]:
    recs = {r["id"]: r for r in records}
    inv = {}
    for it in inventory or []:
        ext = str(it.get("external_id") or "").strip()
        if ext:
            inv.setdefault(ext, []).append(it)
    inv_by_id = {str(it.get("id")): it for it in inventory or []}
    ids = sorted(set(listings) | {k for k, m in matches.items() if m.get("list_on_discogs") or is_approved(m)})
    plan = []
    for iid in ids:
        rec, m, L = recs.get(iid), matches.get(iid), dict(listings.get(iid) or {})
        if rec is None:
            continue
        if L.get("status") == "sold" and not rec.get("discogs_sale"):
            # sold on Discogs, sale not recorded here yet: never re-list it
            plan.append({"op": "blocked", "id": iid, "problems": ["sold on Discogs; waiting for the sales poll"]})
            continue
        if L.get("status") == "removed_on_discogs":
            if is_approved(m) and m.get("list_on_discogs") and rec.get("status") == "active":
                plan.append({"op": "blocked", "id": iid, "problems": [
                    "the listing was removed on Discogs by hand; turn 'list on Discogs' off and on to re-list"]})
            continue
        live_L = L if L.get("listing_id") and L.get("status") in LIVE else None
        remote = inv_by_id.get(str(L.get("listing_id"))) if live_L else None
        if live_L and inventory is not None and remote is None:
            plan.append({"op": "gone", "id": iid, "listing_id": L["listing_id"],
                         "reason": "listing no longer in the Discogs inventory (removed by hand)"})
            continue
        if live_L and remote and remote.get("status") == "Sold":
            plan.append({"op": "mark_sold", "id": iid, "listing_id": L["listing_id"],
                         "reason": "Discogs shows this listing as Sold (the poll records the sale)"})
            continue
        fields, problems, notes = desired_listing(rec, m, settings)
        if fields:
            if not live_L:
                adopt = [x for x in inv.get(iid, []) if x.get("status") in ("For Sale", "Draft", "Expired")]
                if adopt:
                    x = adopt[0]
                    plan.append({"op": "adopt", "id": iid, "listing_id": x["id"], "fields": fields,
                                 "status": x.get("status") if x.get("status") != "Expired" else settings["listing_status"],
                                 "update": True, "reason": "found on Discogs by external_id", "notes": notes})
                    continue
                plan.append({"op": "create", "id": iid, "fields": fields, "status": settings["listing_status"],
                             "reason": "approved, list toggle on, active on eBay", "notes": notes})
                continue
            cur_status = (remote or {}).get("status") or L.get("status")
            if L.get("status") == "unlisted" or cur_status in ("unlisted", "Expired"):
                plan.append({"op": "update", "id": iid, "listing_id": L["listing_id"], "fields": fields,
                             "status": settings["listing_status"], "reason": "re-listing", "notes": notes})
            elif _differs(L.get("pushed"), fields):
                changed = [k for k in COMPARE if (L.get("pushed") or {}).get(k) != fields.get(k)]
                plan.append({"op": "update", "id": iid, "listing_id": L["listing_id"], "fields": fields,
                             "status": cur_status, "reason": "changed: " + ", ".join(changed), "notes": notes})
            continue
        # not listable now
        if not live_L:
            if is_approved(m) and m.get("list_on_discogs") and rec.get("status") == "active":
                plan.append({"op": "blocked", "id": iid, "problems": problems})
            continue
        if rec.get("discogs_sale") or (rec.get("status") == "sold" and rec.get("status_source") == "discogs"):
            continue
        if rec.get("status") in ("ended", "sold"):
            if may_delete:
                plan.append({"op": "delete", "id": iid, "listing_id": L["listing_id"],
                             "reason": f"{rec['status']} on eBay ({rec.get('status_reason') or ''})".strip()})
            else:
                plan.append({"op": "deferred_delete", "id": iid, "listing_id": L["listing_id"],
                             "reason": f"{rec['status']} on eBay, but the latest eBay snapshot was partial"})
            continue
        if L.get("status") != "unlisted" and L.get("pushed"):
            plan.append({"op": "unlist", "id": iid, "listing_id": L["listing_id"],
                         "fields": dict(L["pushed"]), "reason": "; ".join(problems)})
    return plan


def _summ(plan):
    out = {}
    for a in plan:
        out[a["op"]] = out.get(a["op"], 0) + 1
    return out


def _username(client: Client) -> str:
    st = load_state()
    if st.get("username"):
        return st["username"]
    u = client.identity().get("username")
    if not u:
        raise DiscogsError("identity returned no username")
    _update_state(username=u)
    return u


def sync_listings(records: list[dict] | None = None, client: Client | None = None, *, dry_run: bool = False,
                  force: bool = False, log=print) -> dict:
    client = client if client is not None else make_client(read_only=dry_run)
    if not client:
        log(SKIP_REASON)
        _update_state(last_listing_sync={"at": now_iso(), "result": "skipped", "reason": SKIP_REASON})
        return {"result": "skipped", "reason": SKIP_REASON}
    may_run, may_delete, why = ebay_gate()
    if not may_run:
        _update_state(last_listing_sync={"at": now_iso(), "result": "refused", "reason": why})
        return {"result": "refused", "reason": why}
    if records is None:
        from . import merge
        records = merge.build_records()[0]
    settings, matches, listings = load_settings(), load_matches(), load_listings()
    try:   # read everything first: a failure here changes nothing
        username = _username(client)
        inventory = client.inventory(username)
    except DiscogsError as e:
        _update_state(last_listing_sync={"at": now_iso(), "result": "failed", "errors": [str(e)]})
        return {"result": "failed", "errors": [str(e)], "note": "nothing changed"}
    plan = plan_listings(records, matches, listings, settings, inventory, may_delete)
    live = sum(1 for L in listings.values() if L.get("status") in LIVE)
    n_del = sum(1 for a in plan if a["op"] == "delete")
    if n_del > 3 and n_del > 0.5 * max(live, 1) and not force:
        why = f"refusing to delete {n_del} of {live} Discogs listings in one run (limit 50%); use force"
        _update_state(last_listing_sync={"at": now_iso(), "result": "refused", "reason": why})
        return {"result": "refused", "reason": why, "plan": _summ(plan)}
    if dry_run:
        return {"result": "dry_run", "summary": _summ(plan), "plan": plan, "gate": why}
    done, errors, ts = {}, [], now_iso()
    try:
        for a in plan:
            op, iid = a["op"], a["id"]
            if op in ("blocked", "deferred_delete"):
                continue
            L = dict(listings.get(iid) or {})
            try:
                if op == "create":
                    m = matches.get(iid)
                    if not (is_approved(m) and m.get("list_on_discogs")):   # belt and braces
                        raise DiscogsError("refused: no stored human approval")
                    res = client.create_listing(dict(a["fields"], status=a["status"]))
                    lid = res.get("listing_id")
                    if not lid:
                        raise DiscogsError("create returned no listing_id")
                    L = {"listing_id": lid, "release_id": a["fields"]["release_id"], "status": a["status"],
                         "pushed": a["fields"], "created_at": ts, "updated_at": ts,
                         "url": f"{SITE}/sell/item/{lid}", "previous": L.get("previous") or []}
                    if listings.get(iid, {}).get("listing_id"):
                        L["previous"] = (L["previous"] + [listings[iid]])[-5:]
                elif op in ("adopt", "update"):
                    client.edit_listing(a["listing_id"], dict(a["fields"], status=a["status"]))
                    if op == "adopt":
                        L = {"listing_id": a["listing_id"], "created_at": ts, "adopted": True,
                             "url": f"{SITE}/sell/item/{a['listing_id']}"}
                    L.update(release_id=a["fields"]["release_id"], status=a["status"], pushed=a["fields"],
                             updated_at=ts)
                elif op == "unlist":
                    client.edit_listing(a["listing_id"], dict(a["fields"], status="Draft"))
                    L.update(status="unlisted", updated_at=ts, unlisted_reason=a["reason"])
                elif op == "delete":
                    try:
                        client.delete_listing(a["listing_id"])
                    except DiscogsError as e:
                        if e.status != 404:
                            raise
                    L.update(status="deleted", updated_at=ts, deleted_reason=a["reason"])
                elif op == "mark_sold":
                    L.update(status="sold", updated_at=ts)
                elif op == "gone":
                    L.update(status="removed_on_discogs", updated_at=ts)
                L.pop("last_error", None)
                listings[iid] = L
                done[op] = done.get(op, 0) + 1
            except DiscogsError as e:
                errors.append(f"{iid} {op}: {e}")
                L["last_error"] = {"at": ts, "op": op, "error": str(e)[:300]}
                listings[iid] = L
                if e.status in (401, 403):
                    break
    finally:
        save_listings(listings)   # record what really happened on Discogs
    res = {"result": "ok" if not errors else "partial", "done": done, "planned": _summ(plan),
           "errors": errors[:50], "gate": why}
    _update_state(last_listing_sync={"at": ts, **{k: v for k, v in res.items()}})
    if errors:
        add_alert("listing_sync_errors", f"{len(errors)} Discogs listing change(s) failed", severity="warning",
                  data={"errors": errors[:10]})
    return res


# ----------------------------------------------------------------------------- sales

def order_url(order_id) -> str:
    return f"{SITE}/sell/order/{order_id}"


def _sale_from(order: dict | None, item: dict | None, listing: dict | None) -> dict:
    price = (item or {}).get("price") or (listing or {}).get("price") or {}
    created = (order or {}).get("created") or (listing or {}).get("posted") or now_iso()
    return {"order_id": (order or {}).get("id"), "order_status": (order or {}).get("status"),
            "listing_id": (item or listing or {}).get("id"),
            "release_id": ((item or listing or {}).get("release") or {}).get("id"),
            "price": price.get("value"), "currency": price.get("currency") or "USD",
            "date": str(created)[:10] if order else now_iso()[:10]}


def mark_discogs_sold(cs: "store.ChangeSet", rec: dict, sale: dict) -> dict | None:
    """Mark a source record sold because it sold on Discogs. Returns the alert
    payload, or None when this sale was already recorded."""
    ds = rec.get("discogs_sale")
    if ds and str(ds.get("listing_id")) == str(sale.get("listing_id")):
        if sale.get("order_id") and not ds.get("order_id"):
            ds["order_id"], ds["order_url"] = sale["order_id"], order_url(sale["order_id"])
            cs.touch(rec["item_id"])
        return None
    if any(str(h.get("listing_id")) == str(sale.get("listing_id")) for h in rec.get("discogs_sale_history") or []):
        return None   # a sale Edwin already reviewed and relisted after
    prior_status, prior_qty = rec.get("status"), rec.get("quantity_available")
    ts = cs.ts
    key = (sale["date"], str(sale.get("price")), 1)
    if key not in {(x.get("date"), str(x.get("price")), x.get("quantity")) for x in rec.get("sales") or []}:
        rec.setdefault("sales", []).append({"date": sale["date"], "price": None if sale.get("price") is None
                                            else f"{Decimal(str(sale['price'])):.2f}",
                                            "currency": sale["currency"], "quantity": 1, "source": "discogs"})
        rec["sales"].sort(key=lambda x: x.get("date") or "")
    rec["discogs_sale"] = {
        "order_id": sale.get("order_id"), "order_url": order_url(sale["order_id"]) if sale.get("order_id") else None,
        "listing_id": sale.get("listing_id"), "release_id": sale.get("release_id"),
        "sold_at": sale["date"], "detected_at": ts, "price": sale.get("price"), "currency": sale["currency"],
        "status_before": prior_status, "ebay_quantity_at_sale": prior_qty,
        "ebay_seen_active_at": None, "ebay_quantity": None, "ebay_listing_gone_at": None,
        "ebay_marked_ended_at": None, "relist_confirmed_at": None,
    }
    rec["sold_at"] = sale["date"]
    if sale.get("price") is not None:
        rec["price"] = {"value": f"{Decimal(str(sale['price'])):.2f}", "currency": sale["currency"],
                        "source": f"discogs: sale price (order {sale.get('order_id') or 'unknown'})"}
    rec["quantity_available"] = 0
    cs.set_status(rec, "sold", "discogs",
                  f"sold on Discogs (order {sale.get('order_id') or 'unknown'}, listing {sale.get('listing_id')}) "
                  f"on {sale['date']}")
    return {"double_sale": prior_status == "sold"}


def poll(client: Client | None = None, *, rebuild: bool = True, log=print) -> dict:
    """Scheduled every ~15 min. READ-ONLY on Discogs (orders + inventory)."""
    client = client if client is not None else make_client(read_only=True)
    if not client:
        _update_state(last_poll={"at": now_iso(), "result": "skipped", "reason": SKIP_REASON})
        return {"result": "skipped", "reason": SKIP_REASON}
    if not client.read_only:
        raise ReadOnlyViolation("poll needs a read-only client")
    started = now_iso()
    listings = load_listings()
    # current listings only: an old listing that sold before a confirmed relist
    # stays "Sold" in the inventory forever and must not re-mark the record
    by_lid = {str(L["listing_id"]): iid for iid, L in listings.items() if L.get("listing_id")}
    st = load_state()
    try:
        username = _username(client)
        if not by_lid:
            _update_state(last_poll={"at": started, "result": "ok", "note": "no Discogs listings made yet",
                                     "username": username})
            return {"result": "ok", "new_sales": 0, "note": "no Discogs listings made yet"}
        created = [L.get("created_at") for L in listings.values() if L.get("created_at")]
        cursor = st.get("orders_cursor") or (min(created) if created else started)
        orders = client.orders(created_after=_minus_hours(cursor, 24))
        sold_inv = client.inventory(username, status="Sold")
    except DiscogsError as e:
        _update_state(last_poll={"at": started, "result": "failed", "errors": [str(e)]})
        return {"result": "failed", "errors": [str(e)], "note": "nothing changed"}
    events, cancelled = {}, []
    for o in orders:
        status = str(o.get("status") or "")
        for it in o.get("items") or []:
            iid = by_lid.get(str(it.get("id")))
            if not iid:
                continue
            if status.startswith("Cancelled"):
                cancelled.append((iid, o))
                continue
            events.setdefault(iid, _sale_from(o, it, None))
    for x in sold_inv:
        iid = by_lid.get(str(x.get("id")))
        if iid and str(x.get("external_id") or iid) == iid:
            events.setdefault(iid, _sale_from(None, None, x))
    cs = store.ChangeSet()
    new, alerts = [], []
    for iid, sale in events.items():
        rec = cs.get(iid, create=False)
        if rec is None:
            continue
        r = mark_discogs_sold(cs, rec, sale)
        if r is None:
            continue
        new.append(iid)
        if iid in listings:
            listings[iid].update(status="sold", sold_at=sale["date"], order_id=sale.get("order_id"))
        alerts.append((iid, rec, sale, r))
    for iid, o in cancelled:
        rec = cs.items.get(iid) or {}
        ds = rec.get("discogs_sale") or {}
        if ds.get("order_id") == o.get("id") and not ds.get("cancel_alerted"):
            ds["cancel_alerted"] = cs.ts
            cs.touch(iid)
            add_alert("discogs_order_cancelled",
                      f"Discogs order {o.get('id')} for \"{rec.get('title')}\" was cancelled. The record stays "
                      "marked sold here until you confirm it is still for sale.",
                      severity="warning", item_id=iid, links={"order": order_url(o.get("id"))})
    cs.commit()
    save_listings(listings)
    for iid, rec, sale, r in alerts:
        title = rec.get("title") or iid
        links = {"discogs_order": order_url(sale["order_id"]) if sale.get("order_id") else None,
                 "ebay_listing": store.listing_url(iid), "end_on_ebay": SELLER_HUB_ACTIVE}
        if r["double_sale"]:
            add_alert("double_sale", f"\"{title}\" sold on Discogs but was already sold on eBay. "
                      "One buyer needs to be refunded.", severity="critical", item_id=iid, links=links, data=sale)
        else:
            add_alert("discogs_sale", f"Sold on Discogs: \"{title}\" for {sale.get('price')} {sale['currency']}. "
                      f"End eBay listing {iid} now.", severity="action", item_id=iid, links=links, data=sale)
    out = {"result": "ok", "orders_seen": len(orders), "sold_inventory_seen": len(sold_inv),
           "new_sales": len(new), "new_sale_ids": new}
    if new and rebuild:
        try:
            from .site import build
            out["build"] = build()
        except Exception as e:  # the sale is recorded either way
            out["build_error"] = f"{type(e).__name__}: {e}"
    _update_state(orders_cursor=started, last_poll={"at": started, **{k: v for k, v in out.items() if k != "build"}})
    return out


def _minus_hours(ts: str, hours: int) -> str:
    from datetime import datetime, timedelta
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ts
    return (d - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def end_on_ebay_list() -> list[dict]:
    out = []
    for iid, rec in store.load_items().items():
        ds = rec.get("discogs_sale")
        if not ds or ds.get("ebay_listing_gone_at") or ds.get("ebay_marked_ended_at") or rec.get("status") != "sold":
            continue
        out.append({"id": iid, "title": rec.get("title"), "sold_at": ds.get("sold_at"),
                    "order_url": ds.get("order_url"), "ebay_url": store.listing_url(iid),
                    "seller_hub_url": SELLER_HUB_ACTIVE, "ebay_seen_active_at": ds.get("ebay_seen_active_at"),
                    "ebay_quantity": ds.get("ebay_quantity")})
    return sorted(out, key=lambda x: x.get("sold_at") or "", reverse=True)


def mark_ebay_ended(item_id: str, by: str = "admin") -> dict:
    cs = store.ChangeSet()
    rec = cs.get(item_id, create=False)
    if not rec or not rec.get("discogs_sale"):
        raise ValueError("no Discogs sale recorded for this record")
    rec["discogs_sale"]["ebay_marked_ended_at"] = cs.ts
    rec["discogs_sale"]["ebay_marked_ended_by"] = by
    cs.touch(item_id)
    cs.commit()
    return rec["discogs_sale"]


def confirm_relist(item_id: str, by: str = "admin") -> dict:
    """Edwin says the record is still for sale (e.g. a cancelled Discogs order).
    Only allowed when an eBay snapshot AFTER the sale still showed quantity > 0."""
    cs = store.ChangeSet()
    rec = cs.get(item_id, create=False)
    ds = (rec or {}).get("discogs_sale")
    if not ds:
        raise ValueError("no Discogs sale recorded for this record")
    qty = ds.get("ebay_quantity")
    if not (ds.get("ebay_seen_active_at") and ds["ebay_seen_active_at"] >= ds.get("detected_at", "")
            and qty is not None and int(qty) > 0):
        raise ValueError("eBay has not shown this listing active with quantity > 0 since the Discogs sale; "
                         "relist it on eBay first, then refresh from eBay")
    ds["relist_confirmed_at"], ds["relist_confirmed_by"] = cs.ts, by
    rec.setdefault("discogs_sale_history", []).append(ds)
    rec.pop("discogs_sale")
    api_price = ((rec.get("raw") or {}).get("browse_api") or {}).get("price") or {}
    if api_price.get("value"):
        rec["price"] = {"value": api_price["value"], "currency": api_price.get("currency") or "USD",
                        "source": "browse_api: current price"}
    rec["quantity_available"] = int(qty)
    cs.set_status(rec, "active", "admin", f"relist confirmed by {by}; eBay showed quantity {qty} on "
                                          f"{ds['ebay_seen_active_at']}")
    cs.commit()
    listings = load_listings()
    if item_id in listings and listings[item_id].get("status") == "sold":
        old = listings.pop(item_id)
        listings[item_id] = {"previous": (old.get("previous") or []) + [old]}
        save_listings(listings)
    return {"status": "active", "item_id": item_id}


# ----------------------------------------------------------------------------- admin views

def overview() -> dict:
    st = load_state()
    m = load_matches()
    L = load_listings()
    lc = {}
    for x in L.values():
        lc[x.get("status")] = lc.get(x.get("status"), 0) + 1
    alerts = load_alerts()
    return {"token_present": bool(get_token()), "username": st.get("username"),
            "match_counts": status_counts(m),
            "confident_unapproved": sum(1 for x in m.values() if x.get("status") == "confident"),
            "approved_not_listed": sum(1 for x in m.values() if x.get("status") == "approved" and not x.get("list_on_discogs")),
            "list_on": sum(1 for x in m.values() if x.get("list_on_discogs")),
            "listing_counts": lc, "settings": load_settings(),
            "end_on_ebay": end_on_ebay_list(),
            "double_sales": [{"id": k, "title": r.get("title"), "order_url": r["discogs_sale"].get("order_url"),
                              "ebay_sale_reported_at": r["discogs_sale"]["ebay_sale_reported_at"]}
                             for k, r in store.load_items().items()
                             if (r.get("discogs_sale") or {}).get("ebay_sale_reported_at")],
            "alerts_unacked": sum(1 for a in alerts if not a.get("acked_at")),
            "last_match": st.get("last_match"), "last_listing_sync": st.get("last_listing_sync"),
            "last_poll": st.get("last_poll"), "condition_values": MEDIA_CONDITIONS,
            "sleeve_values": SLEEVE_CONDITIONS}


def review_rows(records: list[dict] | None = None, status: str | None = None) -> list[dict]:
    if records is None:
        from . import merge
        records = merge.build_records()[0]
    matches, listings, settings = load_matches(), load_listings(), load_settings()
    rows = []
    for r in records:
        m = matches.get(r["id"])
        if not m and r.get("status") != "active":
            continue
        if status and (m or {}).get("status", "unmatched") != status:
            continue
        lp = r.get("local_photo")
        photo = f"/img/photo/{lp['thumb']}" if lp and r.get("images") else ((r.get("images") or [None])[0])
        fields, problems, notes = desired_listing(r, m, settings) if m else (None, ["not matched yet"], [])
        cond = conditions_for(r, m)
        rows.append({"id": r["id"], "ebay_title": r.get("ebay_title"), "ebay_url": r.get("ebay_url"),
                     "record_url": record_url(r), "photo": photo, "status": r.get("status"),
                     "price": r.get("price"), "discogs_price": str(apply_price_rule(r.get("price"), settings) or ""),
                     "artist": r.get("artist"), "album": r.get("album"), "label": r.get("label"),
                     "catalog_number": r.get("catalog_number"), "year": record_year(r), "country": r.get("country"),
                     "format": record_format(r), "vinyl_grade": r.get("media_grade"),
                     "sleeve_grade": r.get("sleeve_grade"), "discogs_condition": cond["condition"],
                     "discogs_sleeve": cond["sleeve_condition"], "match": m, "listing": listings.get(r["id"]),
                     "listable": bool(fields), "problems": problems, "notes": notes,
                     "grade_conflicts": [c for c in r.get("conflicts") or [] if "grade" in c]})
    order = {"review": 0, "confident": 1, "no_match": 2, "approved": 3, "skipped": 4}
    rows.sort(key=lambda x: (order.get((x["match"] or {}).get("status"), 5), x["id"]))
    return rows


def decide(*a, **kw):
    with _MLOCK:
        return _decide(*a, **kw)


def approve_all_confident(*a, **kw):
    with _MLOCK:
        return _approve_all_confident(*a, **kw)


def set_listing_toggle(*a, **kw):
    with _MLOCK:
        return _set_listing_toggle(*a, **kw)


def list_all_approved(*a, **kw):
    with _MLOCK:
        return _list_all_approved(*a, **kw)

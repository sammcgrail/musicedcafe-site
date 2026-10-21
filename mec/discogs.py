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
RELEASES_DIR = DIR / "releases"   # cached GET /releases/{id} (identifiers, community counts)
# statuses a human (or a rule a human approved) decided: a match re-run never overwrites them
DECIDED = ("approved", "skipped", "quick")
QUICK_RULE_BY = "edwin-quick-rule"
TITLE_WARNING = "no pressing with this catalog number has the eBay title (check the catno or the title)"
UNVERIFIED_NOTE = "Pressing variant not verified; see photos on the listing page."

_urlopen = urllib.request.urlopen   # tests replace this; nothing else should
_MLOCK = threading.RLock()          # serialises read-modify-write of matches.json across admin threads
_PACE_LOCK = threading.Lock()       # one request budget for every make_client() client in this process
_PACE = {"last": None}

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
    return Client(tok, read_only=read_only, shared_pace=True) if tok else None


class Client:
    """Official Discogs API over urllib. Throttled to <= 60 requests/min,
    honours X-Discogs-Ratelimit-Remaining, retries 429 with backoff.
    The token is only ever put in the Authorization header, never logged."""

    def __init__(self, token: str, *, read_only: bool = False, urlopen=None, sleep=time.sleep,
                 clock=time.monotonic, min_interval: float = MIN_INTERVAL, retries: int = 5,
                 shared_pace: bool = False):
        if not token:
            raise DiscogsError(SKIP_REASON)
        self._token = token
        self.read_only = read_only
        self._urlopen = urlopen
        self.sleep, self.clock = sleep, clock
        self.min_interval, self.retries = min_interval, retries
        self._last = None
        self.shared_pace = shared_pace   # True: share one 1 req/s budget with other clients (admin threads)
        self.remaining = None
        self.calls = 0

    def __repr__(self):
        return f"<discogs.Client read_only={self.read_only}>"

    def _throttle(self):
        if self.shared_pace:   # e.g. a verify-page release fetch while a match run is going
            with _PACE_LOCK:
                if _PACE["last"] is not None:
                    wait = _PACE["last"] + self.min_interval - self.clock()
                    if wait > 0:
                        self.sleep(wait)
                _PACE["last"] = self.clock()
            return
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

    def price_suggestions(self, release_id) -> dict:
        """Suggested price per condition ({} when Discogs has none); needs seller settings filled in."""
        return self.request("GET", f"/marketplace/price_suggestions/{int(release_id)}")

    def marketplace_stats(self, release_id, curr: str | None = "USD") -> dict:
        """lowest_price {currency, value} | null, num_for_sale, blocked_from_sale."""
        return self.request("GET", f"/marketplace/stats/{int(release_id)}", {"curr_abbr": curr})

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


KEEP_ON_REMATCH = ("list_on_discogs", "sleeve_condition", "history", "verify_skipped_at", "verify_skipped_by")


def _merge_match_results(changed: dict, flags: dict):
    """Write a match run's results without clobbering decisions made meanwhile."""
    with _MLOCK:
        cur = load_matches()
        for k, v in changed.items():
            c = cur.get(k) or {}
            if c.get("status") in DECIDED:
                continue          # a human decided while we were searching
            for keep in KEEP_ON_REMATCH:
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
            "community": {k: (c.get("community") or {}).get(k) for k in ("have", "want")
                          if (c.get("community") or {}).get(k) is not None},
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


STORE_TOP, STORE_GROUP = 5, 40


def stored_candidates(cands: list[dict]) -> list[dict]:
    """The top 5 by score, plus every pressing that shares the catalog number (up
    to 40), so the quick rule and the verify page see all the pressings, not just
    the 5 best-scoring ones."""
    out, seen = [], set()
    group = [c for c in cands if c["signals"]["catno"] and c["signals"]["format"] != "inconsistent"]
    for c in cands[:STORE_TOP] + group[:STORE_GROUP]:
        if c["release_id"] not in seen:
            seen.add(c["release_id"])
            out.append(c)
    return sorted(out, key=lambda c: -c["score"])


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
        if cur.get("status") in DECIDED or cur.get("pressing_verified"):
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
            **{k: v for k, v in cur.items() if k in KEEP_ON_REMATCH},
            "status": status, "auto_status": status, "reason": reason, "chosen_release_id": chosen,
            "candidates": stored_candidates(cands), "candidate_count": len(cands), "truncated": truncated,
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
        if m.get("pressing_verified") and rid != m.get("verified_release_id"):
            m["pressing_verified"] = False   # the runout was matched to a different release
        _hist(m, action, by, release_id=rid)
    elif action == "skip":
        m.update(status="skipped", approved_release_id=None, approved_by=None, approved_at=None,
                 list_on_discogs=False, skipped_by=by, skipped_at=ts)
        _hist(m, "skip", by)
    elif action == "reset":
        m.update(status=m.get("auto_status") or "review", approved_release_id=None, approved_by=None,
                 approved_at=None, list_on_discogs=False, pressing_verified=False)
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
    if on and not is_approved(m):
        raise ValueError("approve the Discogs match (or quick-pick it) before turning on listing")
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
    """A stored decision: Edwin approved this release, or the quick rule he approved
    picked it (status "quick", approved_by "edwin-quick-rule"). Either way the
    per-record "list on Discogs" toggle is still needed before anything is listed."""
    return bool(m and m.get("status") in ("approved", "quick") and m.get("approved_release_id")
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


def matrix_text(m: dict | None) -> str:
    """'A: <x> / B: <y>' from a verified record's runout, '' when none was read."""
    mx = (m or {}).get("matrix") or {}
    parts = [f"{side}: {mx[k].strip()}" for side, k in (("A", "side_a"), ("B", "side_b"))
             if str(mx.get(k) or "").strip()]
    return " / ".join(parts)


def pressing_line(m: dict | None) -> str:
    """The line appended to every Discogs comment: the unverified note, or the runout."""
    if not (m or {}).get("pressing_verified"):
        return UNVERIFIED_NOTE
    mt = matrix_text(m)
    return f"Matrix/Runout: {mt}" if mt else ""


def render_comment(rec: dict, cond: dict, settings: dict, m: dict | None = None) -> str:
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
    tail = pressing_line(m)
    if tail:   # the template is trimmed first, so the pressing line always survives the cap
        text = text[:COMMENT_MAX - len(tail) - 1].rstrip() + "\n" + tail
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
              "comments": render_comment(rec, cond, settings, m),
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
                                 "old_release_id": (x.get("release") or {}).get("id"),
                                 "update": True, "reason": "found on Discogs by external_id", "notes": notes})
                    continue
                if L.get("status") == "recreate_pending":   # a release change half-done: finish it as a Draft
                    plan.append({"op": "create", "id": iid, "fields": fields, "status": "Draft",
                                 "reason": "re-creating for the verified release", "notes": notes})
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
                             "status": cur_status, "reason": "changed: " + ", ".join(changed), "notes": notes,
                             "old_release_id": (L.get("pushed") or {}).get("release_id")})
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


# Edit Listing takes release_id (docs: "release_id number (required)"), so a
# release change is sent as an edit. If Discogs refuses that edit with one of
# these, the listing is deleted and re-created as a Draft instead.
RECREATE_ON = (400, 409, 422)


def _recreate(client: Client, a: dict, L: dict, listings: dict, iid: str, ts: str) -> dict:
    """Delete + create as Draft, idempotently: the 'recreate_pending' mark is saved
    first, so a crash between the two steps is finished (or adopted by
    external_id) by the next sync instead of leaving the record unlisted."""
    old = a["listing_id"]
    L = dict(L, listing_id=old, status="recreate_pending", updated_at=ts,
             recreate={"from_listing": old, "from_release": a.get("old_release_id"),
                       "to_release": a["fields"]["release_id"], "at": ts})
    listings[iid] = L
    save_listings(listings)
    try:
        client.delete_listing(old)
    except DiscogsError as e:
        if e.status != 404:
            raise
    res = client.create_listing(dict(a["fields"], status="Draft"))
    lid = res.get("listing_id")
    if not lid:
        raise DiscogsError("create returned no listing_id")
    return {"listing_id": lid, "release_id": a["fields"]["release_id"], "status": "Draft",
            "pushed": a["fields"], "created_at": ts, "updated_at": ts, "url": f"{SITE}/sell/item/{lid}",
            "recreated_from": old, "previous": ((L.get("previous") or []) + [dict(L, status="deleted")])[-5:]}


def sync_listings(records: list[dict] | None = None, client: Client | None = None, *, dry_run: bool = False,
                  force: bool = False, only_ids=None, log=print) -> dict:
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
    if only_ids:   # e.g. push one verified record right after the verify page confirms it
        plan = [a for a in plan if a["id"] in set(only_ids)]
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
                    try:
                        client.edit_listing(a["listing_id"], dict(a["fields"], status=a["status"]))
                    except DiscogsError as e:
                        old_rel, new_rel = a.get("old_release_id"), a["fields"]["release_id"]
                        if e.status in RECREATE_ON and old_rel and int(old_rel) != int(new_rel):
                            try:
                                listings[iid] = _recreate(client, a, L, listings, iid, ts)
                            except DiscogsError:
                                L = listings[iid]   # keep the recreate_pending mark for the next run
                                raise
                            done["recreate"] = done.get("recreate", 0) + 1
                            continue
                        raise
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


# ----------------------------------------------------------------------------- release cache

_REL_LOCK = threading.Lock()
SKIP_IDENT_TYPES = {"barcode", "asin", "price code", "rights society", "label code", "isrc", "other"}


def release_path(release_id):
    return RELEASES_DIR / f"{int(release_id)}.json"


def cached_release(release_id) -> dict | None:
    try:
        return load_json(release_path(release_id), None)
    except (TypeError, ValueError):
        return None


def trim_release(rel: dict) -> dict:
    """The parts of GET /releases/{id} we use. Identifiers are kept verbatim
    (type, description, value): that is what Edwin compares the runout with."""
    return {"id": rel.get("id"), "title": rel.get("title"),
            "artists": [a.get("name") for a in rel.get("artists") or [] if a.get("name")],
            "year": rel.get("year"), "released": rel.get("released"), "country": rel.get("country"),
            "labels": [{"name": x.get("name"), "catno": x.get("catno")} for x in rel.get("labels") or []],
            "formats": [{"name": f.get("name"), "qty": f.get("qty"), "descriptions": f.get("descriptions") or [],
                         "text": f.get("text") or ""} for f in rel.get("formats") or []],
            "identifiers": [{"type": i.get("type") or "", "description": i.get("description") or "",
                             "value": i.get("value") or ""} for i in rel.get("identifiers") or []],
            "notes": str(rel.get("notes") or "")[:3000],
            "community": {k: (rel.get("community") or {}).get(k) for k in ("have", "want")},
            "thumb": rel.get("thumb") or "", "uri": rel.get("uri") or f"{SITE}/release/{rel.get('id')}",
            "fetched_at": now_iso()}


def get_release(client: "Client | None", release_id, *, refresh: bool = False) -> dict | None:
    """Cached in data/discogs/releases/<id>.json. Without a client only the cache is read."""
    rid = int(release_id)
    if not refresh:
        c = cached_release(rid)
        if c:
            return c
    if client is None:
        return None
    with _REL_LOCK:   # one fetch at a time; the client is paced as well
        if not refresh:
            c = cached_release(rid)
            if c:
                return c
        rel = trim_release(client.release(rid))
        write_json(release_path(rid), rel)
        return rel


def candidate_have(c: dict) -> int | None:
    """community.have: from the search result if it was stored, else the release cache."""
    v = (c.get("community") or {}).get("have")
    if v is None:
        v = ((cached_release(c["release_id"]) or {}).get("community") or {}).get("have")
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- quick pick
# Edwin's rule (2026-10-20): list every record on its most likely release now,
# say "pressing variant not verified" in the comment, and verify the runout later.

def _year(v) -> int | None:
    m = re.search(r"\b(19|20)\d\d\b", str(v or ""))
    return int(m.group(0)) if m else None


def _speed(s) -> str | None:
    m = re.search(r"\b(33|45|78|16)\b", str(s or ""))
    return m.group(1) if m else None


def format_tier(rec: dict, c: dict) -> int:
    """0 = exact (same LP / 7" / 10" / 12" class and no speed contradiction),
    1 = consistent, 2 = unknown. Inconsistent candidates never get here."""
    rf, cf = record_format(rec), candidate_format(c.get("format"))
    if format_compat(rf, cf) == "unknown":
        return 2
    if rf and rf == cf:
        rs = _speed(rec.get("speed"))
        cs = next((_speed(x) for x in c.get("format") or [] if "rpm" in str(x).lower()), None)
        return 0 if not (rs and cs and rs != cs) else 1
    return 1


def quick_group(cands: list[dict]) -> list[dict]:
    """The pressings that share the catalog number (and could be this format),
    narrowed to those whose label also matches when any do."""
    g = [c for c in cands or [] if c["signals"]["catno"] and c["signals"]["format"] != "inconsistent"]
    lab = [c for c in g if c["signals"]["label"] in ("exact", "partial")]
    return lab or g


def title_consistent(rec: dict, c: dict) -> bool:
    """Is this the same album/single? Looser than the match score's title signal:
    the artist's name inside the album field is ignored ('Lou Rawls Live!' vs
    'Live!'), and a single matches when its title holds the A or B side."""
    t = str(c.get("title") or "")
    c_artist, _, c_title = t.partition(" - ")
    ct = _toks(c_title or t)
    album = str(rec.get("album") or "")
    for part in [album] + (album.split("/") if "/" in album else []):   # "A side / B side"
        at = _toks(part)
        at = (at - _toks(rec.get("artist")) - _toks(c_artist)) or at
        if not at or not ct:
            return True   # nothing to compare: don't count it against the candidate
        if len(at & ct) / len(at) >= 0.6 or len(at & ct) / len(at | ct) >= 0.5:
            return True
    return False


def quick_order(rec: dict, cands: list[dict]) -> dict | None:
    """Apply the rule to cached candidates. -> {ranked, chosen, steps, tie_break,
    tied, warnings} or None when no candidate shares the catalog number.
      a) the record's country, then its year (each only if it leaves something)
      b) the closest format (exact LP / 7" / 12" class and speed first)
      c) the original era: the earliest year at or after the record's release
         year (earliest overall when that year is unknown or nothing is later)
      d) the most Discogs "have"s; then the match score, then the lowest id
    """
    group = quick_group(cands)
    if not group:
        return None
    steps, warnings = [], []
    if rec.get("album"):
        f = [c for c in group if title_consistent(rec, c)]
        if f and len(f) < len(group):
            steps.append(f"title matches: {len(f)} of {len(group)}")
            group = f
        elif not f:
            steps.append("no pressing with this catno has the eBay title")
            warnings.append(TITLE_WARNING)
    rco = norm_country(rec.get("country"))
    if rco:
        f = [c for c in group if norm_country(c.get("country")) == rco]
        if f:
            steps.append(f"country {rec.get('country')}: {len(f)} of {len(group)}")
            group = f
        else:
            steps.append(f"no pressing from {rec.get('country')}; country not used")
            warnings.append("no pressing from the record's country")
    ry = record_year(rec)
    if ry:
        f = [c for c in group if _year(c.get("year")) == int(ry)]
        if f:
            steps.append(f"year {ry}: {len(f)} of {len(group)}")
            group = f
        else:
            steps.append(f"no pressing dated {ry}")
    best = min(format_tier(rec, c) for c in group)
    f = [c for c in group if format_tier(rec, c) == best]
    steps.append(f"format {['exact', 'consistent', 'unknown'][best]}: {len(f)} of {len(group)}")
    group = f
    base = _year(rec.get("release_year")) or _year(rec.get("pressing_year"))
    dated = [c for c in group if _year(c.get("year"))]
    if dated:
        later = [c for c in dated if base and _year(c["year"]) >= base]
        pool = later or dated
        ey = min(_year(c["year"]) for c in pool)
        f = [c for c in pool if _year(c["year"]) == ey]
        if base and later:
            steps.append(f"earliest at or after {base}: {ey} ({len(f)})")
        elif base:
            steps.append(f"nothing dated {base} or later; earliest is {ey} ({len(f)})")
        else:
            steps.append(f"release year unknown; earliest is {ey} ({len(f)})")
        group = f
    else:
        steps.append("no pressing has a year")
    haves = {c["release_id"]: candidate_have(c) for c in group}
    ranked = sorted(group, key=lambda c: (-(haves[c["release_id"]] if haves[c["release_id"]] is not None else -1),
                                          -c["score"], c["release_id"]))
    known = sum(1 for v in haves.values() if v is not None)
    if len(ranked) == 1:
        tie_break = "none needed"
    elif known == len(ranked):
        tie_break = "have"
    elif known:
        tie_break = f"have (known for {known} of {len(ranked)} tied)"
    else:
        tie_break = "have missing: match score, then lowest release id"
    if len(ranked) > 1:
        steps.append(f"{len(ranked)} tied; " + (f"most haves ({haves[ranked[0]['release_id']]})"
                                                 if tie_break.startswith("have (") or tie_break == "have"
                                                 else tie_break))
    chosen = ranked[0]
    return {"chosen": chosen["release_id"], "ranked": [c["release_id"] for c in ranked], "steps": steps,
            "tie_break": tie_break, "tied": len(ranked),
            "missing_have": [rid for rid, v in haves.items() if v is None] if len(ranked) > 1 else [],
            "warnings": warnings}


def _rec_for(m: dict, recs: dict | None, iid: str) -> dict:
    """The record as matched (the stored fingerprint), or the live record when given."""
    r = (recs or {}).get(iid)
    return r if r is not None else dict(m.get("fingerprint") or {}, id=iid)


def exact_confident(m: dict) -> bool:
    """A confident match whose chosen release matched catno AND country AND year exactly."""
    c = next((c for c in m.get("candidates") or [] if c.get("release_id") == m.get("chosen_release_id")), None)
    s = (c or {}).get("signals") or {}
    return bool(c and s.get("catno") and s.get("country") == "match" and s.get("year") == "exact")


def quickpick_scope(m: dict) -> bool:
    return m.get("status") == "review" or (m.get("status") == "quick" and not m.get("pressing_verified"))


def _quickpick(records: list[dict] | None = None, *, apply: bool = False, expected: int | None = None,
               exclude_title_mismatch: bool = False, log=print) -> dict:
    """Pick the most likely release for every 'review' record (cached data only,
    no API call). With apply: store status 'quick', pressing_verified false and
    approved_by 'edwin-quick-rule'. Never lists anything: the per-record
    'list on Discogs' toggle is still needed."""
    recs = {r["id"]: r for r in records} if records else None
    matches = load_matches()
    picks, excluded, ties, warns = {}, {}, {}, {}
    missing_have = set()
    need_enrich = {}
    for iid, m in sorted(matches.items()):
        if not quickpick_scope(m):
            continue
        q = quick_order(_rec_for(m, recs, iid), m.get("candidates") or [])
        if q is None:
            excluded[iid] = "no candidate shares the catalog number"
            continue
        if exclude_title_mismatch and TITLE_WARNING in q["warnings"]:
            excluded[iid] = "title mismatch (excluded on request)"
            continue
        picks[iid] = q
        ties[q["tie_break"]] = ties.get(q["tie_break"], 0) + 1
        for w in q["warnings"]:
            warns[w] = warns.get(w, 0) + 1
        if q["missing_have"]:
            missing_have.update(q["missing_have"])
            need_enrich[iid] = q["ranked"][:3]
    confident_exact = [k for k, m in matches.items() if m.get("status") == "confident" and exact_confident(m)]
    summary = {"result": "dry_run", "would_pick": len(picks), "excluded": len(excluded),
               "excluded_reasons": _count(excluded.values()), "tie_breaks": ties, "warnings": warns,
               "candidates_missing_community_have": len(missing_have),
               "records_needing_enrichment": len(need_enrich),
               "enrichment_calls_top3": len({r for v in need_enrich.values() for r in v
                                             if not cached_release(r)}),
               "confident": sum(1 for m in matches.values() if m.get("status") == "confident"),
               "confident_exact_country_year_catno": len(confident_exact),
               "status_counts": status_counts(matches)}
    if not apply:
        summary["sample"] = {k: picks[k] for k in list(picks)[:3]}
        summary["title_mismatch_ids"] = [k for k, q in picks.items() if TITLE_WARNING in q["warnings"]]
        return summary
    if expected is not None and int(expected) != len(picks):
        raise ValueError(f"the quick-pick count changed ({len(picks)} now, you confirmed {expected}); reload")
    ts = now_iso()
    for iid, q in picks.items():
        m = matches[iid]
        m.update(status="quick", chosen_release_id=q["chosen"], approved_release_id=q["chosen"],
                 approved_by=QUICK_RULE_BY, approved_at=ts, approved_via="quick_rule", pressing_verified=False,
                 quick_reason="; ".join(q["steps"]), quick_pick=q, record_changed_since_decision=None)
        _hist(m, "quick_pick", QUICK_RULE_BY, release_id=q["chosen"])
    for iid in confident_exact:
        if not matches[iid].get("pressing_verified"):
            matches[iid].update(pressing_verified=True, pressing_verified_via="exact country+year+catno",
                                pressing_verified_at=ts)
    for m in matches.values():
        if m.get("status") == "confident" and "pressing_verified" not in m:
            m["pressing_verified"] = False
    save_matches(matches)
    summary.update(result="ok", picked=len(picks), status_counts=status_counts(matches))
    log(f"discogs quickpick: {len(picks)} records set to 'quick' ({ties})")
    return summary


def _count(vals) -> dict:
    out = {}
    for v in vals:
        out[v] = out.get(v, 0) + 1
    return out


def enrich_releases(client: "Client | None" = None, *, top: int = 3, limit: int | None = None, log=print) -> dict:
    """Optional: GET /releases/{id} for the top 3 tied pressings of each quick-pick
    record whose tie can't be broken from cached data (community.have missing).
    Read-only; results land in data/discogs/releases/. Re-run quickpick after."""
    client = client or make_client(read_only=True)
    if not client:
        log(SKIP_REASON)
        return {"result": "skipped", "reason": SKIP_REASON}
    matches = load_matches()
    todo = []
    for iid, m in sorted(matches.items()):
        if not quickpick_scope(m):
            continue
        q = quick_order(dict(m.get("fingerprint") or {}, id=iid), m.get("candidates") or [])
        if q and q["tied"] > 1:
            todo += [r for r in q["ranked"][:top] if r not in todo]
    todo = [r for r in todo if not cached_release(r)]
    if limit is not None:
        todo = todo[:limit]
    done, errors = 0, []
    log(f"discogs enrich: {len(todo)} releases to fetch (about {len(todo)} s)")
    for rid in todo:
        try:
            get_release(client, rid)
            done += 1
        except DiscogsError as e:
            errors.append(f"{rid}: {e}")
            if e.status in (401, 403):
                break
        if done and done % 50 == 0:
            log(f"discogs enrich: {done} fetched")
    return {"result": "ok" if not errors else "partial", "fetched": done, "planned": len(todo),
            "errors": errors[:20]}


# ----------------------------------------------------------------------------- bulk enable

def enable_preview(records: list[dict]) -> dict:
    """Records the bulk 'enable Discogs' action would turn on: quick or confident,
    toggle off, active on eBay, and both grades map to Discogs values. The rest of
    the quick + confident records are listed in the 'needs grade' queue."""
    matches = load_matches()
    recs = {r["id"]: r for r in records}
    eligible, needs_grade, other = [], [], []
    for iid, m in sorted(matches.items()):
        if m.get("status") not in ("quick", "confident") or not m.get("chosen_release_id"):
            continue
        if m.get("list_on_discogs"):
            continue
        r = recs.get(iid)
        if not r or r.get("status") != "active":
            other.append({"id": iid, "why": f"not active on eBay ({(r or {}).get('status')})"})
            continue
        cond = conditions_for(r, m)
        if cond["problems"]:
            needs_grade.append({"id": iid, "title": r.get("ebay_title"), "ebay_url": r.get("ebay_url"),
                                "vinyl_grade": r.get("media_grade"), "sleeve_grade": r.get("sleeve_grade"),
                                "problems": cond["problems"]})
            continue
        eligible.append(iid)
    return {"eligible": eligible, "count": len(eligible), "needs_grade": needs_grade, "other": other}


def _enable_quick_confident(records: list[dict], expected: int, by: str = "admin") -> dict:
    """Bulk: turn 'list on Discogs' on for every eligible quick + confident record.
    A confident record is approved at the same time (the human confirm is this
    action). Needs the count the human confirmed, so a stale page can't do more."""
    prev = enable_preview(records)
    if int(expected) != prev["count"]:
        raise ValueError(f"the count changed ({prev['count']} now, you confirmed {expected}); reload")
    matches, ts = load_matches(), now_iso()
    for iid in prev["eligible"]:
        m = matches[iid]
        if m.get("status") == "confident":
            m.update(status="approved", approved_release_id=m["chosen_release_id"], approved_at=ts,
                     approved_by=f"{by} (enable quick + confident)", approved_via="enable_quick_confident")
            m.setdefault("pressing_verified", exact_confident(m))
            _hist(m, "approve (bulk enable)", by, release_id=m["chosen_release_id"])
        m["list_on_discogs"] = True
        _hist(m, "list_on (bulk enable quick + confident)", by)
    save_matches(matches)
    return {"turned_on": prev["count"], "needs_grade": len(prev["needs_grade"])}


# ----------------------------------------------------------------------------- verify pressing

TRIANGLES = "△▽▲▼Δδ∆⊿▵▿"   # △ ▽ ▲ ▼ Δ δ ∆ ⊿ ▵ ▿


def matrix_tokens(s) -> list[str]:
    """Runout text -> comparable tokens. Case, spaces, dashes, dots and slashes
    don't matter; every triangle glyph (and the words 'delta' / 'triangle')
    becomes TRI; other symbols are dropped."""
    s = str(s or "")
    for ch in TRIANGLES:
        s = s.replace(ch, " TRI ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\b(DELTA|TRIANGLE)\b", " TRI ", s)
    return re.findall(r"[A-Z0-9]+", s)


def norm_matrix(s) -> str:
    return "".join(matrix_tokens(s))


def _lev1(a: str, b: str) -> bool:
    """True when a and b differ by exactly one edit."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def runout_similarity(typed, value) -> float:
    """0..1. Exact after normalising = 1; the typed text inside the identifier =
    0.85..1; the identifier inside the typed text = 0.6..1 (by length ratio); a
    single typo on a long code = 0.75; else 0.8 x token overlap."""
    ci, cv = norm_matrix(typed), norm_matrix(value)
    if not ci or not cv:
        return 0.0
    if ci == cv:
        return 1.0
    best = 0.0
    if len(ci) >= 3 and ci in cv:     # all of what he typed is in the identifier (which says more)
        best = 0.85 + 0.15 * len(ci) / len(cv)
    elif len(cv) >= 3 and cv in ci:   # he typed more than Discogs lists
        best = 0.6 + 0.4 * len(cv) / len(ci)
    if min(len(ci), len(cv)) >= 6 and _lev1(ci, cv):
        best = max(best, 0.75)
    ti, tv = set(matrix_tokens(typed)), set(matrix_tokens(value))
    if ti and tv:
        best = max(best, 0.8 * len(ti & tv) / len(ti | tv))
    return round(best, 3)


def runout_identifiers(rel: dict | None) -> list[dict]:
    return [i for i in (rel or {}).get("identifiers") or []
            if str(i.get("type") or "").strip().lower() not in SKIP_IDENT_TYPES and i.get("value")]


VERIFY_FIELDS = ("side_a", "side_b", "lacquer", "stamper")


def score_runout(inputs: dict, rel: dict | None) -> dict:
    """Score one release against what Edwin typed. -> {score 0..100, hits}."""
    idents = runout_identifiers(rel)
    typed = {k: str(inputs.get(k) or "").strip() for k in VERIFY_FIELDS if str(inputs.get(k) or "").strip()}
    if not typed:
        return {"score": 0, "hits": {}, "note": "nothing typed"}
    if not idents:
        return {"score": 0, "hits": {}, "note": "Discogs lists no runout / matrix for this release"}
    hits, total = {}, 0.0
    for k, v in typed.items():
        best, which = 0.0, None
        for i in idents:
            sim = runout_similarity(v, i["value"])
            if sim > best:
                best, which = sim, i
        total += best
        if which:
            hits[k] = {"similarity": best, "type": which["type"], "description": which["description"],
                       "value": which["value"]}
    return {"score": round(100 * total / len(typed)), "hits": hits}


def auto_match(inputs: dict, release_ids: list, client: "Client | None" = None) -> dict:
    """Rank the candidate releases by how well their identifiers match the typed runout."""
    rows = []
    for rid in release_ids:
        rel = get_release(client, rid) if client else cached_release(rid)
        rows.append(dict(score_runout(inputs, rel), release_id=rid, fetched=bool(rel)))
    rows.sort(key=lambda r: (-r["score"], str(r["release_id"])))
    best = rows[0] if rows and rows[0]["score"] >= 40 else None
    clear = bool(best and (len(rows) == 1 or rows[0]["score"] - rows[1]["score"] >= 10))
    return {"ranked": rows, "best": best["release_id"] if best else None, "best_score": best["score"] if best else 0,
            "clear": clear}


def verify_universe(m: dict | None) -> bool:
    return bool(m and m.get("status") in ("review", "quick", "confident", "approved") and m.get("candidates"))


def _price(r) -> float:
    try:
        return float(_dec(r.get("price")) or 0)
    except (TypeError, ValueError):
        return 0.0


def _photo(r: dict) -> str | None:
    lp = r.get("local_photo")
    return f"/img/photo/{lp['thumb']}" if lp and r.get("images") else ((r.get("images") or [None])[0])


def verify_row(r: dict, m: dict, listing: dict | None) -> dict:
    chosen = m.get("approved_release_id") or m.get("chosen_release_id")
    c = next((c for c in m.get("candidates") or [] if c.get("release_id") == chosen), {}) or {}
    return {"id": r["id"], "ebay_title": r.get("ebay_title"), "ebay_url": r.get("ebay_url"), "photo": _photo(r),
            "artist": r.get("artist"), "album": r.get("album"), "price": r.get("price"),
            "catalog_number": r.get("catalog_number"), "label": r.get("label"), "country": r.get("country"),
            "year": record_year(r), "format": record_format(r), "status": m.get("status"),
            "chosen_release_id": chosen, "chosen_title": c.get("title"), "chosen_year": c.get("year"),
            "chosen_country": c.get("country"), "pressing_verified": bool(m.get("pressing_verified")),
            "matrix": m.get("matrix"), "verify_skipped_at": m.get("verify_skipped_at"),
            "listing": {k: (listing or {}).get(k) for k in ("listing_id", "status", "url")} if listing else None}


def verify_queue(records: list[dict], q: str | None = None, show: str = "unverified", limit: int = 60) -> dict:
    """Default: unverified records, highest price first, 'can't read' ones last."""
    matches, listings = load_matches(), load_listings()
    rows = []
    for r in records:
        m = matches.get(r["id"])
        if r.get("status") != "active" or not verify_universe(m):
            continue
        rows.append(verify_row(r, m, listings.get(r["id"])))
    total, verified = len(rows), sum(1 for x in rows if x["pressing_verified"])
    q = (q or "").strip()
    if q:
        toks = re.findall(r"[a-z0-9]+", _fold(q))
        digits = re.sub(r"\D", "", q)
        def hit(x):
            if digits and len(digits) >= 4 and digits in x["id"]:
                return True
            hay = _fold(" ".join(str(x.get(k) or "") for k in ("artist", "album", "ebay_title", "catalog_number")))
            return bool(toks) and all(t in hay for t in toks)
        rows = [x for x in rows if hit(x)]
    elif show != "all":
        rows = [x for x in rows if not x["pressing_verified"]]
    rows.sort(key=lambda x: (bool(x["pressing_verified"]), bool(x["verify_skipped_at"]), -_price(x), x["id"]))
    return {"rows": rows[:limit], "matching": len(rows), "verified": verified, "total": total}


def verify_record(item_id: str, records: list[dict], client: "Client | None" = None) -> dict:
    """One record with every candidate release's identifiers. Releases are fetched
    lazily (cached, paced) when a client is given; otherwise only the cache is used."""
    r = next((x for x in records if x["id"] == item_id), None)
    m = load_matches().get(item_id)
    if not r or not m:
        raise ValueError("no such record, or it hasn't been matched yet")
    row = verify_row(r, m, load_listings().get(item_id))
    order = list(dict.fromkeys([row["chosen_release_id"]] + list((m.get("quick_pick") or {}).get("ranked") or [])
                               + [c["release_id"] for c in quick_group(m.get("candidates") or [])]
                               + [c["release_id"] for c in m.get("candidates") or []]))
    by_id = {c["release_id"]: c for c in m.get("candidates") or []}
    cands, errors = [], []
    for rid in [x for x in order if x]:
        c = by_id.get(rid, {"release_id": rid})
        rel = None
        try:
            rel = get_release(client, rid) if client else cached_release(rid)
        except DiscogsError as e:
            errors.append(f"{rid}: {e}")
            if e.status in (401, 403):
                client = None   # stop calling with a bad token; show what is cached
        cands.append({"release_id": rid, "title": c.get("title") or (rel or {}).get("title"),
                      "label": c.get("label") or [x["name"] for x in (rel or {}).get("labels") or []],
                      "catno": c.get("catno"), "format": c.get("format") or [], "thumb": c.get("thumb"),
                      "url": c.get("url") or f"{SITE}/release/{rid}",
                      "year": (rel or {}).get("year") or c.get("year"), "released": (rel or {}).get("released"),
                      "country": (rel or {}).get("country") or c.get("country"),
                      "identifiers": runout_identifiers(rel), "notes": (rel or {}).get("notes") or "",
                      "have": ((rel or {}).get("community") or {}).get("have") or (c.get("community") or {}).get("have"),
                      "fetched": bool(rel), "chosen": rid == row["chosen_release_id"]})
    return {"record": row, "candidates": cands, "errors": errors,
            "token_present": bool(get_token()), "quick_reason": m.get("quick_reason"),
            "warnings": (m.get("quick_pick") or {}).get("warnings") or []}


def _confirm_pressing(item_id: str, release_id, inputs: dict, by: str = "admin", score=None) -> dict:
    matches = load_matches()
    m = matches.get(item_id)
    if not m:
        raise ValueError("no match entry for this record")
    try:
        rid = int(release_id)
    except (TypeError, ValueError):
        raise ValueError("release_id must be a number") from None
    if rid not in [c.get("release_id") for c in m.get("candidates") or []] + [m.get("chosen_release_id")]:
        raise ValueError("that release is not one of this record's candidates")
    vals = {k: str(inputs.get(k) or "").strip()[:200] for k in VERIFY_FIELDS}
    if not (vals["side_a"] or vals["side_b"]):
        raise ValueError("type at least one side's runout (or use \"can't read\")")
    by, ts = (str(by or "admin").strip() or "admin")[:60], now_iso()
    m["matrix"] = dict(vals, verified_at=ts, verified_by=by, match_score=score, release_id=rid)
    m.update(chosen_release_id=rid, approved_release_id=rid, pressing_verified=True, verified_release_id=rid,
             pressing_verified_via="runout", pressing_verified_at=ts, verify_skipped_at=None, verify_skipped_by=None,
             status="approved", approved_by=by, approved_at=ts, approved_via="verify_pressing",
             record_changed_since_decision=None)
    _hist(m, "verify_pressing", by, release_id=rid)
    matches[item_id] = m
    save_matches(matches)
    L = load_listings().get(item_id) or {}
    return {"match": m, "listing_live": bool(L.get("listing_id") and L.get("status") in LIVE)}


def _skip_pressing(item_id: str, by: str = "admin") -> dict:
    matches = load_matches()
    m = matches.get(item_id)
    if not m:
        raise ValueError("no match entry for this record")
    m.update(verify_skipped_at=now_iso(), verify_skipped_by=(str(by or "admin").strip() or "admin")[:60])
    _hist(m, "verify_cant_read", m["verify_skipped_by"])
    save_matches(matches)
    return m


def quickpick(*a, **kw):
    with _MLOCK:
        return _quickpick(*a, **kw)


def enable_quick_confident(*a, **kw):
    with _MLOCK:
        return _enable_quick_confident(*a, **kw)


def confirm_pressing(*a, **kw):
    with _MLOCK:
        return _confirm_pressing(*a, **kw)


def skip_pressing(*a, **kw):
    with _MLOCK:
        return _skip_pressing(*a, **kw)


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
            "pressing_verified": sum(1 for x in m.values() if x.get("pressing_verified")),
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
    order = {"review": 0, "quick": 1, "confident": 1, "no_match": 2, "approved": 3, "skipped": 4}
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

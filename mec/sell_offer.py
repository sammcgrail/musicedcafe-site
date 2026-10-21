"""Internal offer calculator for Sell Your Records submissions (admin only).

PRIVATE. Nothing here is ever rendered into the public site or public JSON:
  data/sell/offer_rules.json        Edwin's offer rules (gitignored; DEFAULT_RULES until saved)
  data/sell/review/<id>.json        per-submission review: status, Discogs match, grades,
                                    overrides, final offer (gitignored, mode 600)
  data/sell/valuations/<rid>.json   cached Discogs value data per release (gitignored)

Discogs endpoints (docs: Wayback copy 2026-10-13 of discogs.com/developers, see
docs/DISCOGS-INTEGRATION.md), all GET through mec/discogs.py's paced client:
  GET /database/search                          candidates by catno, or artist + release_title
  GET /marketplace/price_suggestions/{id}       {"Very Good (VG)": {"currency": "USD", "value": 6.78}, ...};
                                                {} when there are none; needs a token whose user
                                                has filled in seller settings
  GET /marketplace/stats/{id}?curr_abbr=USD     {"lowest_price": {"currency","value"} | null,
                                                 "num_for_sale": int | null, "blocked_from_sale": bool}

Resale estimate per record: the suggested price at the chosen vinyl grade x the
sleeve factor; else the lowest listed price x lowest_price_factor x the sleeve
factor; else Edwin's manual value (a manual value always wins when typed).
"""
from __future__ import annotations

import csv
import io
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import discogs, sell
from .util import atomic_write, dump_json, load_json

RULES_FILE = sell.STORE_DIR / "offer_rules.json"
REVIEW_DIR = sell.STORE_DIR / "review"
VAL_DIR = sell.STORE_DIR / "valuations"
_LOCK = threading.RLock()

STATUSES = ["new", "reviewing", "offered", "accepted", "declined", "purchased"]
SHIP_METHODS = ["ship", "pickup"]
VINYL_GRADES = list(discogs.MEDIA_CONDITIONS)
SLEEVE_GRADES = list(discogs.SLEEVE_CONDITIONS)
DEFAULT_GRADE = "Very Good (VG)"
EXTRA_RE = re.compile(r"^x([1-9]|[1-9][0-9])$")
MAX_EXTRA = 99
MAX_MONEY = Decimal("100000")
CENT = Decimal("0.01")

DEFAULT_RULES = {
    # resale under tiers[i][0] -> offer tiers[i][1] (checked in order)
    "tiers": [[5, 0], [15, 1], [30, 2], [50, 3]],
    # at or above the last tier: min(cap, max(floor, round(resale x percent / 100)))
    "top": {"percent": 8, "floor": 3, "cap": 7},
    "ship_per_record": 0.55,          # Media Mail estimate per record we buy, only when shipped
    "lowest_price_factor": 1.0,       # fallback: lowest listed price x this
    "sleeve_factors": {"Mint (M)": 1.0, "Near Mint (NM or M-)": 1.0, "Very Good Plus (VG+)": 1.0,
                       "Very Good (VG)": 1.0, "Good Plus (G+)": 0.85, "Good (G)": 0.7, "Fair (F)": 0.5,
                       "Poor (P)": 0.4, "Generic": 0.9, "Not Graded": 1.0, "No Cover": 0.6},
    "valuation_ttl_days": 7,
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def D(x) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        d = Decimal(str(x).strip().lstrip("$"))
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a number: {str(x)[:20]}")
    if not d.is_finite() or d < 0 or d > MAX_MONEY:
        raise ValueError(f"out of range: {str(x)[:20]}")
    return d


def money(d: Decimal | None) -> float | None:
    return None if d is None else float(d.quantize(CENT, ROUND_HALF_UP))


# ----------------------------------------------------------------------------- rules

def load_rules() -> dict:
    saved = load_json(RULES_FILE, None) or {}
    r = dict(DEFAULT_RULES, **{k: v for k, v in saved.items() if k in DEFAULT_RULES})
    r["sleeve_factors"] = dict(DEFAULT_RULES["sleeve_factors"], **(saved.get("sleeve_factors") or {}))
    r["top"] = dict(DEFAULT_RULES["top"], **(saved.get("top") or {}))
    return r


def validate_rules(new: dict) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    out = load_rules()
    try:
        if "tiers" in new:
            tiers = [[float(D(b)), float(D(o))] for b, o in new["tiers"]]
            if not tiers or len(tiers) > 10:
                errs.append("between 1 and 10 tiers")
            if any(b2 <= b1 for (b1, _), (b2, _) in zip(tiers, tiers[1:])):
                errs.append("tier limits must go up")
            out["tiers"] = tiers
        if "top" in new:
            t = new["top"]
            out["top"] = {"percent": float(D(t.get("percent"))), "floor": float(D(t.get("floor"))),
                          "cap": float(D(t.get("cap")))}
            if out["top"]["cap"] < out["top"]["floor"]:
                errs.append("the top-tier cap must be at least the floor")
        for k in ("ship_per_record", "lowest_price_factor", "valuation_ttl_days"):
            if k in new:
                out[k] = float(D(new[k]))
        if "sleeve_factors" in new:
            sf = {}
            for g, f in (new["sleeve_factors"] or {}).items():
                if g not in SLEEVE_GRADES:
                    errs.append(f"unknown sleeve grade {str(g)[:30]}")
                    continue
                v = float(D(f))
                if v > 2:
                    errs.append(f"sleeve factor for {g} looks wrong (over 2)")
                sf[g] = v
            out["sleeve_factors"] = dict(out["sleeve_factors"], **sf)
    except (TypeError, ValueError, AttributeError) as e:
        errs.append(str(e) or "bad value")
    return (None, errs) if errs else (out, [])


def save_rules(new: dict) -> tuple[dict, list[str]]:
    out, errs = validate_rules(new)
    if errs:
        return load_rules(), errs
    with _LOCK:
        atomic_write(RULES_FILE, dump_json(out))   # mkstemp: mode 600
    return out, []


def rule_offer(resale: Decimal | None, rules: dict) -> Decimal | None:
    """The offer for one record from its estimated resale value."""
    if resale is None:
        return None
    for bound, offer in rules["tiers"]:
        if resale < Decimal(str(bound)):
            return Decimal(str(offer))
    t = rules["top"]
    pct = (resale * Decimal(str(t["percent"])) / 100).quantize(Decimal("1"), ROUND_HALF_UP)
    return min(Decimal(str(t["cap"])), max(Decimal(str(t["floor"])), pct))


# ----------------------------------------------------------------------------- review state

def review_path(sid: str):
    if not sell.ID_RE.match(sid or ""):
        raise ValueError("bad submission id")
    return REVIEW_DIR / f"{sid}.json"


def load_review(sid: str) -> dict:
    rv = load_json(review_path(sid), None) or {}
    rv.setdefault("status", "new")
    rv.setdefault("records", {})
    rv.setdefault("extra", [])
    return rv


def save_review(sid: str, rv: dict):
    rv["updated_at"] = _now()
    atomic_write(review_path(sid), dump_json(rv))


def record_rows(sub: dict, rv: dict) -> list[dict]:
    """The submission's records plus any Edwin added by hand (big lots)."""
    rows = [{"key": str(r["n"]), "artist": r.get("artist", ""), "title": r.get("title", ""),
             "catno": r.get("catno", ""), "photos": r.get("photos") or {}} for r in sub.get("records") or []]
    rows += [{"key": x["key"], "artist": x.get("artist", ""), "title": x.get("title", ""),
              "catno": x.get("catno", ""), "photos": {}, "added": True} for x in rv.get("extra") or []]
    return rows


def _key_ok(sub: dict, rv: dict, key: str) -> bool:
    return key in {r["key"] for r in record_rows(sub, rv)}


def update_review(sid: str, sub: dict, body: dict) -> dict:
    with _LOCK:
        rv = load_review(sid)
        if "status" in body:
            if body["status"] not in STATUSES:
                raise ValueError("unknown status")
            rv["status"] = body["status"]
        if "ship_method" in body:
            if body["ship_method"] not in SHIP_METHODS + [None, ""]:
                raise ValueError("ship_method must be ship or pickup")
            rv["ship_method"] = body["ship_method"] or None
        if "final_offer" in body:
            rv["final_offer"] = money(D(body["final_offer"]))
        if "note" in body:
            rv["note"] = str(body["note"] or "")[:2000]
        if body.get("add_extra"):
            x = body["add_extra"]
            if len(rv["extra"]) >= MAX_EXTRA:
                raise ValueError("too many added records")
            used = {e["key"] for e in rv["extra"]}
            key = next(f"x{i}" for i in range(1, MAX_EXTRA + 1) if f"x{i}" not in used)
            artist, title = str(x.get("artist") or "").strip()[:120], str(x.get("title") or "").strip()[:160]
            if not (artist or title):
                raise ValueError("an added record needs an artist or a title")
            rv["extra"].append({"key": key, "artist": artist, "title": title,
                                "catno": str(x.get("catno") or "").strip()[:40]})
        if body.get("remove_extra"):
            k = str(body["remove_extra"])
            rv["extra"] = [e for e in rv["extra"] if e["key"] != k]
            rv["records"].pop(k, None)
        for key, ch in (body.get("records") or {}).items():
            key = str(key)
            if not _key_ok(sub, rv, key) or not isinstance(ch, dict):
                raise ValueError(f"unknown record {key[:10]}")
            r = rv["records"].setdefault(key, {})
            if "vinyl_grade" in ch:
                if ch["vinyl_grade"] not in VINYL_GRADES:
                    raise ValueError("unknown vinyl grade")
                r["vinyl_grade"] = ch["vinyl_grade"]
            if "sleeve_grade" in ch:
                if ch["sleeve_grade"] not in SLEEVE_GRADES:
                    raise ValueError("unknown sleeve grade")
                r["sleeve_grade"] = ch["sleeve_grade"]
            for k in ("manual_resale", "override_offer"):
                if k in ch:
                    r[k] = money(D(ch[k]))
            if "release_id" in ch:
                r["release_id"] = _rid(ch["release_id"])
        save_review(sid, rv)
        return rv


def _rid(v) -> int | None:
    if v in (None, ""):
        return None
    m = re.search(r"(\d{1,10})\s*$", str(v)) or re.search(r"release/(\d{1,10})", str(v))
    if not m or int(m.group(1)) <= 0:
        raise ValueError("release id must be a number or a Discogs release URL")
    return int(m.group(1))


# ----------------------------------------------------------------------------- Discogs

def _cand(r: dict) -> dict:
    return {"release_id": r.get("id"), "title": r.get("title"), "label": r.get("label") or [],
            "catno": r.get("catno"), "format": r.get("format") or [], "year": r.get("year"),
            "country": r.get("country"), "thumb": r.get("thumb") or "",
            "url": discogs.SITE + (r.get("uri") or f"/release/{r.get('id')}"),
            "have": (r.get("community") or {}).get("have"), "want": (r.get("community") or {}).get("want")}


def search(sid: str, sub: dict, key: str, query: dict | None = None, client=None) -> dict:
    """Discogs candidates for one record: by catalog number first (with the artist
    when given), then artist + title (vinyl). Stores up to 8 in the review."""
    rv = load_review(sid)
    row = next((r for r in record_rows(sub, rv) if r["key"] == key), None)
    if row is None:
        raise ValueError("unknown record")
    q = {k: str((query or {}).get(k) or row.get(k) or "").strip()[:160] for k in ("catno", "artist", "title")}
    client = client or discogs.make_client(read_only=True)
    if client is None:
        return {"result": "skipped", "reason": discogs.SKIP_REASON, "candidates": []}
    tried, res = [], []
    if q["catno"]:
        p = {"catno": q["catno"], "artist": q["artist"]} if q["artist"] else {"catno": q["catno"]}
        tried.append(p)
        res = (client.search(type="release", **p).get("results") or [])
        if not res and q["artist"]:
            tried.append({"catno": q["catno"]})
            res = client.search(type="release", catno=q["catno"]).get("results") or []
    if not res and (q["artist"] or q["title"]):
        p = {"artist": q["artist"], "release_title": q["title"], "format": "Vinyl"}
        tried.append(p)
        res = client.search(type="release", **p).get("results") or []
    cands = [_cand(r) for r in res[:8]]
    with _LOCK:
        rv = load_review(sid)
        r = rv["records"].setdefault(key, {})
        r["candidates"], r["searched_at"], r["query"] = cands, _now(), tried
        if len(cands) == 1 and not r.get("release_id"):
            r["release_id"] = cands[0]["release_id"]
        save_review(sid, rv)
    return {"result": "ok", "candidates": cands, "queries": tried}


def valuation_path(rid: int):
    return VAL_DIR / f"{int(rid)}.json"


def cached_valuation(rid) -> dict | None:
    try:
        return load_json(valuation_path(rid), None)
    except (TypeError, ValueError):
        return None


def get_valuation(rid: int, *, refresh: bool = False, client=None, rules: dict | None = None) -> dict | None:
    """Discogs price suggestions + marketplace stats for one release, cached in
    data/sell/valuations/<rid>.json for valuation_ttl_days. Without a token only
    the cache is read. A failed call is recorded and the other one still kept."""
    rid = int(rid)
    rules = rules or load_rules()
    c = cached_valuation(rid)
    if c and not refresh:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(c["fetched_at"].replace("Z", "+00:00"))
            if age < timedelta(days=float(rules.get("valuation_ttl_days") or 7)):
                return c
        except (KeyError, ValueError, AttributeError):
            pass
    client = client or discogs.make_client(read_only=True)
    if client is None:
        return c
    out = {"release_id": rid, "fetched_at": _now(), "price_suggestions": None, "stats": None, "errors": {}}
    try:
        ps = client.price_suggestions(rid)
        out["price_suggestions"] = {k: v for k, v in (ps or {}).items() if k in VINYL_GRADES and isinstance(v, dict)}
    except discogs.DiscogsError as e:
        out["errors"]["price_suggestions"] = str(e)[:200]
    try:
        st = client.marketplace_stats(rid, curr="USD") or {}
        out["stats"] = {"lowest_price": st.get("lowest_price"), "num_for_sale": st.get("num_for_sale"),
                        "blocked_from_sale": st.get("blocked_from_sale")}
    except discogs.DiscogsError as e:
        out["errors"]["stats"] = str(e)[:200]
    if out["price_suggestions"] is None and out["stats"] is None and c:
        c = dict(c, last_error=out["errors"])     # both failed: keep the old numbers
        atomic_write(valuation_path(rid), dump_json(c))
        return c
    atomic_write(valuation_path(rid), dump_json(out))
    return out


def pick(sid: str, sub: dict, key: str, release_id, *, refresh: bool = False, client=None) -> dict:
    rid = _rid(release_id)
    with _LOCK:
        rv = load_review(sid)
        if not _key_ok(sub, rv, key):
            raise ValueError("unknown record")
        rv["records"].setdefault(key, {})["release_id"] = rid
        save_review(sid, rv)
    val = get_valuation(rid, refresh=refresh, client=client) if rid else None
    return {"release_id": rid, "valuation": val,
            "fetched": bool(val), "reason": None if val else discogs.SKIP_REASON}


# ----------------------------------------------------------------------------- the numbers

def _val_money(v) -> Decimal | None:
    try:
        return Decimal(str(v["value"])) if v and v.get("value") is not None else None
    except (InvalidOperation, TypeError, KeyError, AttributeError):
        return None


def default_ship_method(sub: dict) -> str:
    return "pickup" if sub.get("handoff") == "dropoff" else "ship"


def compute(sub: dict, rv: dict | None = None, rules: dict | None = None, valuations: dict | None = None) -> dict:
    """Per-record resale estimate and offer, plus lot totals. valuations: {rid: valuation}
    (defaults to the on-disk cache; this never calls Discogs)."""
    rules = rules or load_rules()
    rv = rv if rv is not None else load_review(sub["id"])
    ship = rv.get("ship_method") or default_ship_method(sub)
    rows, total_offer, total_resale, n_buy, n_valued = [], Decimal(0), Decimal(0), 0, 0
    for row in record_rows(sub, rv):
        st = rv["records"].get(row["key"]) or {}
        vg, sg = st.get("vinyl_grade") or DEFAULT_GRADE, st.get("sleeve_grade") or DEFAULT_GRADE
        sf = Decimal(str(rules["sleeve_factors"].get(sg, 1.0)))
        rid = st.get("release_id")
        val = (valuations or {}).get(rid) if valuations is not None else (cached_valuation(rid) if rid else None)
        sugg = _val_money(((val or {}).get("price_suggestions") or {}).get(vg))
        stats = (val or {}).get("stats") or {}
        lowest = _val_money(stats.get("lowest_price"))
        manual = D(st.get("manual_resale"))
        if manual is not None:
            resale, src = manual, "manual"
        elif sugg is not None:
            resale, src = sugg * sf, f"Discogs suggestion at {vg}" + (f" x sleeve {sf}" if sf != 1 else "")
        elif lowest is not None:
            f = Decimal(str(rules["lowest_price_factor"]))
            resale, src = lowest * f * sf, f"lowest listed x {f}" + (f" x sleeve {sf}" if sf != 1 else "")
        else:
            resale, src = None, None
        ro = rule_offer(resale.quantize(CENT, ROUND_HALF_UP) if resale is not None else None, rules)
        override = D(st.get("override_offer"))
        offer = override if override is not None else ro
        buying = offer is not None and offer > 0
        if resale is not None:
            n_valued += 1
        if buying:
            n_buy += 1
            total_offer += offer
            total_resale += resale or Decimal(0)
        rows.append({"key": row["key"], "artist": row["artist"], "title": row["title"], "catno": row["catno"],
                     "photos": row["photos"], "added": bool(row.get("added")),
                     "release_id": rid, "candidates": st.get("candidates") or [], "searched_at": st.get("searched_at"),
                     "vinyl_grade": vg, "sleeve_grade": sg, "sleeve_factor": float(sf),
                     "suggested_at_grade": money(sugg), "price_suggestions": (val or {}).get("price_suggestions"),
                     "lowest_price": money(lowest), "num_for_sale": stats.get("num_for_sale"),
                     "blocked_from_sale": stats.get("blocked_from_sale"),
                     "valuation_fetched_at": (val or {}).get("fetched_at"),
                     "valuation_errors": (val or {}).get("errors") or {},
                     "manual_resale": money(manual), "resale": money(resale), "resale_source": src,
                     "rule_offer": money(ro), "override_offer": money(override), "offer": money(offer),
                     "buying": buying})
    shipping = Decimal(str(rules["ship_per_record"])) * n_buy if ship == "ship" else Decimal(0)
    cost = total_offer + shipping
    margin = total_resale - cost
    totals = {"records": len(rows), "valued": n_valued, "buying": n_buy, "offer_total": money(total_offer),
              "ship_method": ship, "shipping": money(shipping), "total_cost": money(cost),
              "resale_total": money(total_resale), "margin": money(margin),
              "margin_pct": (round(float(margin / total_resale * 100), 1) if total_resale > 0 else None)}
    note = (f"Suggested offer: ${money(total_offer):.2f} for {n_buy} of {len(rows)} record"
            f"{'' if len(rows) == 1 else 's'}" +
            (f", plus about ${money(shipping):.2f} for the prepaid shipping label we cover" if ship == "ship" and n_buy else "") +
            (f". {len(rows) - n_valued} still need a value." if len(rows) - n_valued else "."))
    final = rv.get("final_offer")
    return {"rows": rows, "totals": totals, "suggested_note": note,
            "final_offer": final if final is not None else money(total_offer), "final_offer_set": final is not None}


def detail(sub: dict) -> dict:
    rv = load_review(sub["id"])
    rules = load_rules()
    return {"submission": sub, "review": {k: rv.get(k) for k in ("status", "ship_method", "final_offer", "note",
                                                                   "updated_at")},
            "calc": compute(sub, rv, rules), "rules": rules,
            "photo_meta": photo_meta(sub["id"]), "statuses": STATUSES, "ship_methods": SHIP_METHODS,
            "vinyl_grades": VINYL_GRADES, "sleeve_grades": SLEEVE_GRADES,
            "default_ship_method": default_ship_method(sub), "token_present": bool(discogs.get_token())}


def photo_meta(sid: str) -> dict:
    """Which stored files exist per photo stem (no file contents)."""
    out = {}
    try:
        d = sell.photo_dir(sid)
    except ValueError:
        return out
    if not d.is_dir():
        return out
    for p in d.iterdir():
        name = p.name
        if name.endswith(".thumb.webp"):
            out.setdefault(name[:-11], {})["thumb"] = True
        elif name.endswith(".webp"):
            out.setdefault(name[:-5], {})["full"] = True
        elif name.endswith(".heic"):
            out.setdefault(name[:-5], {})["original"] = True
    return out


def summary(sub: dict) -> dict:
    rv = load_review(sub["id"])
    c = compute(sub, rv)
    return {"status": rv.get("status", "new"), "final_offer": c["final_offer"], "buying": c["totals"]["buying"],
            "records": c["totals"]["records"]}


def to_csv(sub: dict) -> str:
    c = compute(sub)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["record", "artist", "title", "catno", "discogs_release_id", "discogs_url", "vinyl_grade",
                "sleeve_grade", "suggested_at_grade", "lowest_listed", "num_for_sale", "resale_estimate",
                "resale_source", "rule_offer", "override_offer", "offer"])
    for r in c["rows"]:
        w.writerow([sell._cell(x) for x in (
            r["key"], r["artist"], r["title"], r["catno"], r["release_id"],
            f"{discogs.SITE}/release/{r['release_id']}" if r["release_id"] else "", r["vinyl_grade"],
            r["sleeve_grade"], r["suggested_at_grade"], r["lowest_price"], r["num_for_sale"], r["resale"],
            r["resale_source"], r["rule_offer"], r["override_offer"], r["offer"])])
    t = c["totals"]
    w.writerow([])
    for k, v in (("records", t["records"]), ("valued", t["valued"]), ("buying", t["buying"]),
                 ("offer_total", t["offer_total"]), ("ship_method", t["ship_method"]),
                 ("shipping_estimate", t["shipping"]), ("total_cost", t["total_cost"]),
                 ("resale_total", t["resale_total"]), ("margin", t["margin"]), ("margin_pct", t["margin_pct"]),
                 ("final_offer", c["final_offer"])):
        w.writerow([k, v])
    return buf.getvalue()

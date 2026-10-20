"""Merch preorder reservations: catalog loading, validation and storage (stdlib only).

The catalog is content/merch/merch.json. Reservations are appended to
data/merch/reservations.jsonl (gitignored: it holds names and emails). Nothing
in this module logs or prints a reservation's contents.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from .util import ROOT

CATALOG = ROOT / "content" / "merch" / "merch.json"
STORE_DIR = ROOT / "data" / "merch"
STORE = STORE_DIR / "reservations.jsonl"
_LOCK = threading.Lock()

MAX_QTY = 10
MAX_NAME = 80
MAX_EMAIL = 254
MAX_NOTE = 500
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HONEYPOT = "website"


def load_catalog(path=None) -> dict:
    with open(path or CATALOG, encoding="utf-8") as f:
        return json.load(f)


def items_by_slug(cat: dict) -> dict:
    return {it["slug"]: it for it in cat.get("items", [])}


def _one(form: dict, key: str) -> str:
    v = form.get(key, "")
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v)


def validate(form: dict, cat: dict) -> tuple[dict | None, dict]:
    """form: a flat dict (or parse_qs-style dict of lists). Returns (clean, errors).
    errors maps field name -> friendly message; clean is None when there are errors."""
    errors: dict[str, str] = {}
    items = items_by_slug(cat)
    slug = _one(form, "item").strip()
    item = items.get(slug)
    if not item:
        errors["item"] = "Please pick an item from the list."
    elif item.get("status") != "preorder":
        errors["item"] = "Sorry, that item isn't taking reservations right now."
    options = {}
    if item:
        for opt in item.get("options") or []:
            vals = opt.get("values") or []
            got = _one(form, f"opt_{opt['name']}").strip()
            if not got and len(vals) == 1:
                got = vals[0]
            if got not in vals:
                errors[f"opt_{opt['name']}"] = f"Please choose a {opt.get('label', opt['name']).lower()}."
            else:
                options[opt["name"]] = got
    q = _one(form, "qty").strip()
    try:
        qty = int(q)
        if not 1 <= qty <= MAX_QTY:
            raise ValueError
    except ValueError:
        qty = None
        errors["qty"] = f"Quantity must be a whole number from 1 to {MAX_QTY}."
    name = " ".join(CTRL_RE.sub("", _one(form, "name")).split())
    if not name:
        errors["name"] = "Please tell us your name."
    elif len(name) > MAX_NAME:
        errors["name"] = f"Please keep your name under {MAX_NAME} characters."
    email = _one(form, "email").strip()
    if not email or len(email) > MAX_EMAIL or not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address, like you@example.com."
    note = CTRL_RE.sub("", _one(form, "note").replace("\r\n", "\n")).strip()
    if len(note) > MAX_NOTE:
        errors["note"] = f"Please keep the note under {MAX_NOTE} characters."
    if _one(form, "consent") not in ("yes", "on", "1", "true"):
        errors["consent"] = "Please tick the box so we can email you about your reservation."
    if errors:
        return None, errors
    return {"item": slug, "item_name": item["name"], "options": options, "qty": qty,
            "name": name, "email": email, "note": note}, {}


def is_spam(form: dict) -> bool:
    """Honeypot: a hidden field real visitors never fill in."""
    return bool(_one(form, HONEYPOT).strip())


def append(clean: dict) -> dict:
    rec = {"id": secrets.token_urlsafe(12),
           "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           **clean}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _LOCK:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(STORE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    return rec


def read_all() -> list[dict]:
    if not STORE.exists():
        return []
    out = []
    with open(STORE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn line never breaks the admin view
    return out


def find(rid: str, max_age_hours: float = 24) -> dict | None:
    """Reservation by its unguessable id, only while fresh (for the thank-you page)."""
    if not rid or len(rid) > 40:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for r in read_all():
        if secrets.compare_digest(str(r.get("id", "")), rid):
            try:
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                return None
            return r if ts >= cutoff else None
    return None


def delete(rid: str) -> bool:
    with _LOCK:
        rows = read_all()
        keep = [r for r in rows if r.get("id") != rid]
        if len(keep) == len(rows):
            return False
        tmp = STORE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, STORE)
        return True


def options_label(opts: dict) -> str:
    return ", ".join(f"{v}" for v in (opts or {}).values())


def totals(rows: list[dict]) -> list[dict]:
    """Quantity per item and per item+options combination."""
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("item"), options_label(r.get("options")))
        a = agg.setdefault(key, {"item": r.get("item"), "item_name": r.get("item_name") or r.get("item"),
                                 "options": key[1], "qty": 0, "reservations": 0})
        a["qty"] += int(r.get("qty") or 0)
        a["reservations"] += 1
    return sorted(agg.values(), key=lambda a: (a["item_name"], a["options"]))


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # no spreadsheet formulas


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "item", "options", "qty", "name", "email", "note", "id"])
    for r in rows:
        w.writerow([_cell(x) for x in (r.get("ts"), r.get("item_name") or r.get("item"),
                                       options_label(r.get("options")), r.get("qty"), r.get("name"),
                                       r.get("email"), r.get("note"), r.get("id"))])
    return buf.getvalue()


class RateLimiter:
    """Sliding-window limit per key (client IP), in memory."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self.lock:
            h = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(h) >= self.limit:
                self.hits[key] = h
                return False
            h.append(now)
            self.hits[key] = h
            if len(self.hits) > 5000:  # forget idle clients
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= self.window]:
                    self.hits.pop(k, None)
            return True

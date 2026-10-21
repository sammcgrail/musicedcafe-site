"""Sell Your Records submissions: validation and storage (stdlib only).

Config is content/sell/sell.json. Submissions are appended to
data/sell/submissions.jsonl (gitignored, mode 600: it holds names, emails and
phone numbers). Photos live in data/sell/uploads/<id>/ (see mec/sell_upload.py)
and Edwin's private review / offer state in data/sell/review/ (mec/sell_offer.py).
Nothing in this module logs or prints a submission's contents.
Mirrors mec/merch.py.

Form v2 (multipart/form-data): the contact fields, how the records get to us
(handoff), up to 40 record rows (r<N>_artist, r<N>_title, r<N>_catno and the
photos r<N>_front, r<N>_label, r<N>_back) and up to 10 "overview" photos.
Rows are required unless the count is 25+ AND at least one overview photo is sent.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone

from .merch import CTRL_RE, EMAIL_RE, HONEYPOT, RateLimiter, _cell, _one  # noqa: F401  (re-exported)
from .util import ROOT

CONFIG = ROOT / "content" / "sell" / "sell.json"
STORE_DIR = ROOT / "data" / "sell"
STORE = STORE_DIR / "submissions.jsonl"
_LOCK = threading.Lock()

MAX_NAME = 80
MAX_EMAIL = 254
MAX_PHONE = 30
MAX_LOCATION = 80
MAX_GENRES = 300
MAX_PHOTOS = 1000
MAX_NOTE = 1000
MAX_ARTIST = 120
MAX_TITLE = 160
MAX_CATNO = 40
MAX_RECORDS = 40
MAX_OVERVIEW = 10
FORM_ROWS = 5                     # empty rows rendered server-side (works without JS)
PHONE_RE = re.compile(r"^[0-9+()\-. xX]+$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
FILE_FIELD_RE = re.compile(r"^(?:r([1-9]|[1-3][0-9]|40)_(front|label|back)|overview)$")
PHOTO_STEM_RE = re.compile(r"^(?:r([1-9]|[1-3][0-9]|40)-(front|label|back)|ov([1-9]|10))$")
PHOTO_SLOTS = ("front", "label", "back")
SLOT_LABEL = {"front": "the jacket front", "label": "the vinyl label", "back": "the jacket back"}
HANDOFF = {"ship": "I'll ship them (you send a prepaid label)",
           "dropoff": "Drop-off or pickup around Lynn, by appointment",
           "unsure": "Not sure yet"}


def uploads_dir():
    return STORE_DIR / "uploads"


def is_file_field(name: str) -> bool:
    return bool(FILE_FIELD_RE.match(name or ""))


def handoff_options(cfg: dict) -> dict:
    return {k: v for k, v in HANDOFF.items() if k != "dropoff" or cfg.get("local_pickup") is True}


def load_config(path=None) -> dict:
    with open(path or CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _text(form: dict, key: str, multiline=False) -> str:
    s = CTRL_RE.sub("", _one(form, key).replace("\r\n", "\n"))
    return s.strip() if multiline else " ".join(s.split())


def _many(form: dict, key: str) -> list[str]:
    v = form.get(key, [])
    if not isinstance(v, list):
        v = [v]
    return [str(x).strip() for x in v]


def _file_problem(fp) -> str | None:
    if fp.too_big:
        return "is over 8 MB"
    if fp.kind() is None:
        return "isn't a photo we can use (JPEG, PNG, WebP or HEIC)"
    return None


def validate(form: dict, cfg: dict, files: list | None = None) -> tuple[dict | None, dict]:
    """form: a flat dict (or parse_qs-style dict of lists); files: sell_upload.FilePart list.
    Returns (clean, errors). clean["_files"] maps a photo stem ("r1-front", "ov1") to its
    FilePart for the caller to process; everything else in clean is JSON-safe."""
    errors: dict[str, str] = {}
    files = files or []
    name = _text(form, "name")
    if not name:
        errors["name"] = "Please tell us your name."
    elif len(name) > MAX_NAME:
        errors["name"] = f"Please keep your name under {MAX_NAME} characters."
    email = _one(form, "email").strip()
    if not email or len(email) > MAX_EMAIL or not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address, like you@example.com."
    phone = _text(form, "phone")
    if phone and (len(phone) > MAX_PHONE or not PHONE_RE.match(phone) or sum(c.isdigit() for c in phone) < 7):
        errors["phone"] = "That phone number doesn't look right. You can leave it blank."
    location = _text(form, "location")
    if not location:
        errors["location"] = "Please tell us your city or ZIP code."
    elif len(location) > MAX_LOCATION:
        errors["location"] = f"Please keep the city or ZIP under {MAX_LOCATION} characters."
    count = _one(form, "count").strip()
    if count not in cfg.get("count_options", []):
        errors["count"] = "Please choose roughly how many records you have."
    allowed = cfg.get("format_options", [])
    formats = [f for f in allowed if f in _many(form, "formats")]
    if not formats:
        errors["formats"] = "Please tick at least one format."
    genres = _text(form, "genres")
    if len(genres) > MAX_GENRES:
        errors["genres"] = f"Please keep the genres and artists under {MAX_GENRES} characters."
    condition = _one(form, "condition").strip()
    if condition not in cfg.get("condition_options", []):
        errors["condition"] = "Please choose the overall condition."
    handoff = _one(form, "handoff").strip()
    if handoff not in handoff_options(cfg):
        errors["handoff"] = "Please tell us how the records would get to us."
    photos = _text(form, "photos", multiline=True)
    if len(photos) > MAX_PHOTOS:
        errors["photos"] = f"Please keep the photo links under {MAX_PHOTOS} characters."
    note = _text(form, "note", multiline=True)
    if len(note) > MAX_NOTE:
        errors["note"] = f"Please keep the note under {MAX_NOTE} characters."
    if _one(form, "consent") not in ("yes", "on", "1", "true"):
        errors["consent"] = "Please tick the box so we can contact you about your records."

    by_field: dict[str, list] = {}
    for fp in files:
        by_field.setdefault(fp.name, []).append(fp)
    out_files, records = {}, []
    for i in range(1, MAX_RECORDS + 1):
        artist, title, catno = (_text(form, f"r{i}_{k}") for k in ("artist", "title", "catno"))
        slots = {k: (by_field.get(f"r{i}_{k}") or [None])[0] for k in PHOTO_SLOTS}
        if not (artist or title or catno or any(slots.values())):
            continue
        probs = []
        if not artist:
            probs.append("add the artist")
        elif len(artist) > MAX_ARTIST:
            probs.append(f"keep the artist under {MAX_ARTIST} characters")
        if not title:
            probs.append("add the album or song title")
        elif len(title) > MAX_TITLE:
            probs.append(f"keep the title under {MAX_TITLE} characters")
        if len(catno) > MAX_CATNO:
            probs.append(f"keep the catalog number under {MAX_CATNO} characters")
        for k in PHOTO_SLOTS:
            fp = slots[k]
            if fp is None:
                if k != "back":
                    probs.append(f"add a photo of {SLOT_LABEL[k]}")
                continue
            bad = _file_problem(fp)
            if bad:
                probs.append(f"the photo of {SLOT_LABEL[k]} {bad}")
        if probs:
            errors[f"r{i}"] = f"Record {i}: please " + "; ".join(probs) + "."
            continue
        n = len(records) + 1
        rec = {"n": n, "row": i, "artist": artist, "title": title, "catno": catno, "photos": {}}
        for k in PHOTO_SLOTS:
            if slots[k] is not None:
                stem = f"r{n}-{k}"
                rec["photos"][k] = stem
                out_files[stem] = slots[k]
        records.append(rec)
    overview = by_field.get("overview") or []
    ov_stems = []
    if len(overview) > MAX_OVERVIEW:
        errors["overview"] = f"Please send at most {MAX_OVERVIEW} overview photos."
    else:
        bad = [_file_problem(fp) for fp in overview]
        if any(bad):
            errors["overview"] = "One of the overview photos " + next(b for b in bad if b) + "."
        else:
            for j, fp in enumerate(overview, 1):
                ov_stems.append(f"ov{j}")
                out_files[f"ov{j}"] = fp
    big_lot = count in cfg.get("count_options", []) and count != cfg.get("count_options", [""])[0]
    if not records and not any(k.startswith("r") and k[1:].isdigit() for k in errors):
        if big_lot and not ov_stems and "overview" not in errors:
            errors["records"] = ("Please add at least one record with its photos, or a few overview photos "
                                 "of the crates or spines.")
        elif not big_lot:
            errors["records"] = "Please add at least one record with a photo of the jacket and of the vinyl."
    if errors:
        return None, errors
    return {"v": 2, "name": name, "email": email, "phone": phone, "location": location, "count": count,
            "formats": formats, "genres": genres, "condition": condition, "handoff": handoff,
            "photos": photos, "note": note, "records": records, "overview": ov_stems, "_files": out_files}, {}


def is_spam(form: dict) -> bool:
    """Honeypot: a hidden field real visitors never fill in."""
    return bool(_one(form, HONEYPOT).strip())


def new_id() -> str:
    return secrets.token_urlsafe(12)


def append(clean: dict, rid: str | None = None) -> dict:
    clean = {k: v for k, v in clean.items() if not k.startswith("_")}
    rec = {"id": rid or new_id(),
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
    """Submission by its unguessable id, only while fresh (for the thank-you page)."""
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


def get(rid: str) -> dict | None:
    """Submission by id (admin), any age."""
    if not rid or not ID_RE.match(rid):
        return None
    for r in read_all():
        if r.get("id") == rid:
            return r
    return None


def photo_dir(rid: str):
    if not ID_RE.match(rid or ""):
        raise ValueError("bad submission id")
    return uploads_dir() / rid


def delete(rid: str) -> bool:
    """Deletes the submission, its photos and its private review state."""
    if rid and ID_RE.match(rid):
        shutil.rmtree(photo_dir(rid), ignore_errors=True)
        try:
            os.unlink(STORE_DIR / "review" / f"{rid}.json")
        except OSError:
            pass
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


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "name", "email", "phone", "city_zip", "count", "formats", "genres_artists",
                "condition", "handoff", "records_listed", "overview_photos", "photo_links", "note", "id"])
    for r in rows:
        w.writerow([_cell(x) for x in (r.get("ts"), r.get("name"), r.get("email"), r.get("phone"), r.get("location"),
                                       r.get("count"), ", ".join(r.get("formats") or []), r.get("genres"),
                                       r.get("condition"), r.get("handoff", ""), len(r.get("records") or []),
                                       len(r.get("overview") or []), r.get("photos"), r.get("note"), r.get("id"))])
    return buf.getvalue()

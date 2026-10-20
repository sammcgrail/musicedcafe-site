"""Small shared helpers: paths, atomic JSON IO, slugs, timestamps."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("MEC_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
SOURCE_DIR = DATA / "source"
EDITORIAL_DIR = DATA / "editorial"
SAMPLES_FILE = DATA / "samples" / "sample-records.json"
SYNC_STATE = DATA / "sync-state.json"
SYNC_LOG = DATA / "sync-log.jsonl"
REVIEW_REPORT = DATA / "review-report.json"
SLUGS_FILE = DATA / "slugs.json"
CONFIG = ROOT / "config" / "site.json"
CSV_MAPPINGS = ROOT / "config" / "csv-mappings.json"
PUBLIC = ROOT / "public"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def dump_json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, obj) -> None:
    atomic_write(path, dump_json(obj))


def append_log(entry: dict) -> None:
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SYNC_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def slugify(text: str, max_len: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0]
    return text or "item"


def config() -> dict:
    cfg = load_json(CONFIG, {})
    # environment overrides (so a deploy can set the seller without editing JSON)
    if os.environ.get("EBAY_SELLER_USERNAME"):
        cfg["ebay_seller_username"] = os.environ["EBAY_SELLER_USERNAME"].strip()
    return cfg

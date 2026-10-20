"""Import eBay CSV exports (sales/transaction reports and active-listing downloads).

Pipeline: read -> find header row (skips report preamble) -> detect format ->
keep ONLY mapped columns (all buyer / ship-to / payout / fee / tax columns are
dropped on read) -> validate -> de-duplicate by item ID -> stage into the source
store -> commit all-or-nothing.

Nothing from a dropped column is ever stored, logged or echoed back.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from . import store
from .util import CSV_MAPPINGS, load_json

MAX_BYTES = 10 * 1024 * 1024


class ImportError_(Exception):
    pass


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "")).strip().lower()


def parse_date(v: str | None) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b-%d-%y",
                "%d-%b-%y", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y %H:%M:%S"):
        for cand in (v, v[:10], " ".join(v.split()[:3])):
            try:
                return datetime.strptime(cand.strip(), fmt).date().isoformat()
            except ValueError:
                continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", v)
    if m:
        return m.group(1)
    m = re.match(r"([A-Z][a-z]{2})[- ](\d{1,2})[-, ]+(\d{2,4})", v)
    if m:
        y = m.group(3)
        y = "20" + y if len(y) == 2 else y
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {y}", "%b %d %Y").date().isoformat()
        except ValueError:
            pass
    return None


def parse_price(v: str | None) -> str | None:
    v = (v or "").strip()
    if not v or v in ("--", "-"):
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", v)
    if not m:
        return None
    num = m.group(0).replace(",", "")
    try:
        f = float(num)
    except ValueError:
        return None
    if f < 0:
        return None
    return f"{f:.2f}"


def parse_currency(v: str | None, price_raw: str | None, default: str) -> tuple[str, bool]:
    v = (v or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", v):
        return v, False
    pr = (price_raw or "").upper()
    if "US $" in pr or pr.startswith("$"):
        return "USD", False
    return default, True


def read_rows(text: str, mappings: dict):
    """Return (format_name, spec, header, rows) where rows are dicts of mapped
    columns only. Skips any preamble lines before the header row."""
    reader = list(csv.reader(io.StringIO(text)))
    for idx, row in enumerate(reader[:40]):
        normed = [_norm(c) for c in row]
        for name, spec in mappings["formats"].items():
            if all(_norm(d) in normed for d in spec["detect"]):
                return name, spec, row, reader[idx + 1:], idx
    raise ImportError_("could not recognise this CSV: no known header row in the first 40 lines. "
                       "Supported: " + ", ".join(s["label"] for s in mappings["formats"].values()))


def import_csv_text(text: str, *, complete: bool | None = None, dry_run: bool = False,
                    force: bool = False, source_name: str | None = None) -> dict:
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise ImportError_("file too large (10 MB limit)")
    mappings = load_json(CSV_MAPPINGS)
    default_cur = mappings.get("default_currency", "USD")
    fmt, spec, header, body, header_line = read_rows(text, mappings)
    hidx = {_norm(h): i for i, h in enumerate(header)}

    def col(row, field):
        for name in spec["columns"].get(field, []):
            i = hidx.get(_norm(name))
            if i is not None and i < len(row) and row[i].strip() not in ("", "--"):
                return row[i].strip()
        return None

    aspect_cols = []
    if spec.get("aspect_prefix"):
        p = spec["aspect_prefix"].lower()
        aspect_cols = [(h[len(spec["aspect_prefix"]):].strip(), i) for i, h in enumerate(header)
                       if _norm(h).startswith(p)]
    present = {f: any(_norm(n) in hidx for n in names) for f, names in spec["columns"].items()}
    filt = {(_norm(k)): [x.lower() for x in v] for k, v in (spec.get("row_filter") or {}).items()}

    errors, warnings, valid = [], [], []
    skipped_filtered = 0
    skipped = []
    allow = set((mappings.get("category_allow") or {}).get("ids") or [])
    for n, row in enumerate(body, start=header_line + 2):
        if not any(c.strip() for c in row):
            continue
        if filt:
            ok = True
            for k, allowed in filt.items():
                i = hidx.get(k)
                if i is None or i >= len(row) or row[i].strip().lower() not in allowed:
                    ok = False
            if not ok:
                skipped_filtered += 1
                continue
        iid = (col(row, "item_id") or "").replace(" ", "")
        if not re.fullmatch(r"\d{9,15}", iid):
            errors.append(f"line {n}: missing or invalid item ID")
            continue
        title = col(row, "title")
        price_raw = col(row, "price")
        price = parse_price(price_raw)
        currency, cur_defaulted = parse_currency(col(row, "currency"), price_raw, default_cur)
        qraw = col(row, "quantity")
        try:
            qty = int(float(qraw)) if qraw is not None else None
        except ValueError:
            errors.append(f"line {n} (item {iid}): quantity {qraw!r} is not a number")
            continue
        if not title:
            warnings.append(f"line {n} (item {iid}): no title")
        if price is None:
            warnings.append(f"line {n} (item {iid}): no usable price")
        if cur_defaulted:
            warnings.append(f"line {n} (item {iid}): no currency column, assumed {default_cur}")
        mapped = {f: col(row, f) for f in spec["columns"]}
        mapped = {k: v for k, v in mapped.items() if v is not None}
        if spec["kind"] == "active_snapshot" and allow and mapped.get("category_id") \
                and mapped["category_id"] not in allow:
            skipped.append({"item_id": iid, "title": title, "category": mapped.get("category"),
                            "reason": f"category {mapped.get('category')} ({mapped['category_id']}) is not a vinyl category"})
            continue
        listing = None
        if spec["kind"] == "active_snapshot":
            fmt_raw = (mapped.get("listing_format") or "").strip().upper()
            ltype = "AUCTION" if "AUCTION" in fmt_raw or fmt_raw == "CHINESE" else (
                "FIXED_PRICE" if fmt_raw else None)
            try:
                bids = int(float(mapped["bids"])) if mapped.get("bids") else 0
            except ValueError:
                bids = 0
            listing = {"type": ltype, "start_date": parse_date(mapped.get("start_date")),
                       "end_date": parse_date(mapped.get("end_date")), "end_raw": mapped.get("end_date"),
                       "bids": bids, "buy_it_now": parse_price(mapped.get("buy_it_now")),
                       "start_price": parse_price(mapped.get("start_price"))}
        aspects = {}
        for name, i in aspect_cols:
            if i < len(row) and row[i].strip():
                aspects[name] = row[i].strip()
        if aspects:
            mapped["aspects"] = aspects
        valid.append({"item_id": iid, "title": title, "price": price, "currency": currency,
                      "quantity": qty, "date": parse_date(col(row, "date")), "raw": mapped, "line": n,
                      "listing": listing})

    # de-duplicate by item ID
    by_id: dict[str, list] = {}
    for v in valid:
        by_id.setdefault(v["item_id"], []).append(v)
    dupes = {k: len(v) for k, v in by_id.items() if len(v) > 1}

    source = source_name or f"csv:{fmt}"
    summary = {"format": fmt, "format_label": spec["label"], "kind": spec["kind"],
               "rows_read": len(valid) + len(errors) + len(skipped), "rows_valid": len(valid),
               "rows_invalid": len(errors), "rows_filtered_out": skipped_filtered,
               "unique_items": len(by_id), "duplicate_item_ids": len(dupes),
               "skipped_non_vinyl": skipped,
               "columns_present": sorted(f for f, ok in present.items() if ok),
               "columns_missing": sorted(f for f, ok in present.items() if not ok),
               "dropped_columns": len(header) - sum(1 for h in header if any(
                   _norm(h) == _norm(n) for names in spec["columns"].values() for n in names))
               - len(aspect_cols)}

    cs = store.ChangeSet()
    if spec["kind"] == "sold":
        # every row is one sale; repeated IDs are multiple sales of one listing
        sales = [{k: v[k] for k in ("item_id", "title", "price", "currency", "quantity", "date", "raw")}
                 for v in valid]
        result = store.apply_sales(cs, sales, source)
        if dupes:
            warnings.append(f"{len(dupes)} item IDs appear on several rows; kept as separate sales of one listing")
    else:
        items = []
        for iid, rows in by_id.items():
            first = rows[0]
            if len(rows) > 1:
                # variations / repeated rows: one listing, summed quantity
                q = sum((r["quantity"] or 0) for r in rows)
                first = dict(first, quantity=q)
                warnings.append(f"item {iid}: {len(rows)} rows (variations or duplicates) merged, quantity summed")
            items.append(first)
        if complete is None:
            complete = not errors  # an export with unreadable rows is never treated as complete
        elif complete and errors:
            raise ImportError_("refusing to treat this file as a complete snapshot: "
                               f"{len(errors)} rows could not be read")
        result = store.apply_active_snapshot(cs, items, source, complete=complete, force=force)
    summary.update(result)
    summary["errors"], summary["warnings"] = errors[:100], warnings[:100]
    summary["dry_run"] = dry_run
    if not dry_run:
        summary["files_written"] = cs.commit()
    return summary

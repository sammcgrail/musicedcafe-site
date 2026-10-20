"""Command line: python3 -m mec <command>

  import-csv FILE [--dry-run] [--partial|--complete] [--force] [--no-build]
  sync [--force] [--no-build]       Browse API sync (skips cleanly without keys)
  enrich [--refresh] [--ids ...]    fetch public description/item pages for known items
  scrape-store                      best-effort read of the public store page (never ends items)
  ended-photos [--retry]            Browse lookup of sold/ended listings for Edwin's photos (cached)
  build                             regenerate public/live/
  status                            print sync state + review summary
  discogs match [--ids ...] [--rematch] [--limit N]   match records to Discogs releases (dry run, lists nothing)
  discogs status                    Discogs match / listing / poll summary (no API calls)
  discogs plan                      what a listing sync would create / update / unlist / delete (reads only)
  discogs sync-listings [--force]   push approved records to Discogs (needs per-record approval)
  discogs poll [--no-build]         check Discogs orders + sold inventory, mark sales (reads only)
  Every discogs command that calls the API logs "skipped: no DISCOGS_TOKEN" without a token.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_env():
    """Read KEY=VALUE lines from .env into os.environ (never printed)."""
    import os
    from .util import ROOT
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None):
    _load_env()
    ap = argparse.ArgumentParser(prog="mec")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-csv")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--complete", dest="complete", action="store_const", const=True)
    g.add_argument("--partial", dest="complete", action="store_const", const=False)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-build", action="store_true")
    p = sub.add_parser("sync")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-build", action="store_true")
    p = sub.add_parser("enrich")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--ids", nargs="*")
    p.add_argument("--spacing", type=float, default=6.0)
    p = sub.add_parser("coverart")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--retry-misses", action="store_true")
    p.add_argument("--prefer-group", action="store_true")
    p.add_argument("--revinyl", action="store_true")
    p.add_argument("--vet-group", action="store_true")
    sub.add_parser("photos")
    p = sub.add_parser("ended-photos")
    p.add_argument("--retry", action="store_true", help="re-check not_found / error entries")
    p.add_argument("--spacing", type=float, default=1.0)
    sub.add_parser("scrape-store")
    sub.add_parser("build")
    sub.add_parser("instagram")
    sub.add_parser("status")
    p = sub.add_parser("discogs")
    dsub = p.add_subparsers(dest="dcmd", required=True)
    q = dsub.add_parser("match")
    q.add_argument("--ids", nargs="*")
    q.add_argument("--rematch", action="store_true", help="re-run records already matched (never approved/skipped ones)")
    q.add_argument("--limit", type=int)
    dsub.add_parser("status")
    dsub.add_parser("plan")
    q = dsub.add_parser("sync-listings")
    q.add_argument("--force", action="store_true", help="allow deleting more than 50%% of listings in one run")
    q = dsub.add_parser("poll")
    q.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)

    from . import store
    if a.cmd == "import-csv":
        from .csv_import import ImportError_, import_csv_text
        text = Path(a.file).read_text(encoding="utf-8-sig", errors="replace")
        try:
            res = import_csv_text(text, complete=a.complete, dry_run=a.dry_run, force=a.force)
        except (ImportError_, store.SyncRefused) as e:
            if not a.dry_run:
                store.record_attempt("csv", False, {}, [str(e)])
            print(f"import failed, nothing changed: {e}", file=sys.stderr)
            return 2
        if not a.dry_run:
            store.record_attempt(f"csv:{res['format']}", True,
                                 {k: v for k, v in res.items() if k not in ("errors", "warnings")},
                                 res["errors"])
        print(json.dumps(res, indent=2))
        if not a.dry_run and not a.no_build:
            from .site import build
            print(json.dumps(build()))
        return 0
    if a.cmd == "sync":
        from .browse import sync
        res = sync(force=a.force, dry_run=a.dry_run)
        print(json.dumps({k: v for k, v in res.items() if k != "state"}, indent=2))
        if res.get("result") == "ok" and not a.no_build:
            from . import merge, photos
            from .site import build
            print(json.dumps(photos.run(merge.build_records()[0])))
            print(json.dumps(build()))
        return 0 if res.get("result") in ("ok", "skipped", "dry_run") else 1
    if a.cmd == "instagram":
        from . import instagram
        res = instagram.sync()
        print(json.dumps(res))
        if res.get("result") == "ok" and not getattr(a, "no_build", False):
            from .site import build
            print(json.dumps(build()))
        return 0 if res.get("result") in ("ok", "skipped") else 1
    if a.cmd == "enrich":
        from .public_pages import enrich
        ids = a.ids or sorted(store.load_items())
        print(json.dumps(enrich(ids, spacing=a.spacing, refresh=a.refresh)))
        return 0
    if a.cmd == "coverart":
        from . import coverart, merge
        if a.vet_group:
            print(json.dumps(coverart.vet_group_fronts()))
            return 0
        if a.prefer_group:
            print(json.dumps(coverart.prefer_group_covers()))
            return 0
        recs, _ = merge.build_records()
        recs.sort(key=lambda r: (r["status"] != "active", r["id"]))  # active first
        print(json.dumps(coverart.run(recs, refresh=a.refresh, limit=a.limit, retry_misses=a.retry_misses, revinyl=a.revinyl)))
        return 0
    if a.cmd == "photos":
        from . import merge, photos
        recs, _ = merge.build_records()
        print(json.dumps(photos.run(recs)))
        return 0
    if a.cmd == "ended-photos":
        from . import ended
        from .util import config
        ids = sorted(k for k, r in store.load_items().items() if r.get("status") in ("sold", "ended"))
        print(json.dumps(ended.run(ids, config()["ebay_seller_username"], retry=a.retry, spacing=a.spacing)))
        return 0
    if a.cmd == "scrape-store":
        from .scrape_store import scrape_store
        print(json.dumps(scrape_store(), indent=2))
        return 0
    if a.cmd == "build":
        from .site import build
        print(json.dumps(build()))
        return 0
    if a.cmd == "discogs":
        return _discogs(a)
    if a.cmd == "status":
        from .util import REVIEW_REPORT, load_json
        st = store.read_state()
        rep = load_json(REVIEW_REPORT, {}) or {}
        print(json.dumps({"sync_state": {k: v for k, v in st.items() if k != "history"},
                          "review_flag_counts": rep.get("flag_counts")}, indent=2))
        return 0


def _discogs(a) -> int:
    from . import discogs
    if a.dcmd == "status":
        ov = discogs.overview()
        ov.pop("condition_values", None)
        ov.pop("sleeve_values", None)
        print(json.dumps(ov, indent=2))
        return 0
    if a.dcmd == "match":
        res = discogs.match_records(ids=set(a.ids) if a.ids else None, rematch=a.rematch, limit=a.limit)
    elif a.dcmd == "plan":
        res = discogs.sync_listings(client=discogs.make_client(read_only=True), dry_run=True)
    elif a.dcmd == "sync-listings":
        res = discogs.sync_listings(force=a.force)
    else:
        res = discogs.poll(rebuild=not a.no_build)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("result") in ("ok", "skipped", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())

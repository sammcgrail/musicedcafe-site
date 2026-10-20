# Music Ed Cafe

The catalog website for **Music Ed Cafe**, an independent vinyl record seller in
Lynn, MA. The site helps people find records, then sends them to the exact eBay
listing to buy. There is no checkout on the site. eBay handles payment, shipping
and returns.

Live: https://musicedcafe.com/ (old preview host musicedcafe.sebland.com redirects; /ebay/* still served there) (status: see HANDOFF.md)

## How it works

```
eBay (source of truth)                 data/source/items/<id>.json   raw eBay data + derived status (never hand-edited)
  ├─ CSV exports (Seller Hub) ──────▶  data/source/public/<id>.json  public description / item-page captures
  ├─ Browse API (when keys exist)      data/editorial/<id>.json      YOUR overrides (a sync never touches these)
  └─ public description pages                     │
                                                  ▼
                                       mec/merge.py  → site records + data/review-report.json
                                                  ▼
                                       mec/site.py   → public/live/  (static HTML, sitemap, JSON search index)
```

- **No database, no runtime app server.** The public site is static files served by
  nginx (`musicedcafe-web`, port 20085).
- A tiny **admin sidecar** (`musicedcafe-admin`, port 20086; Python stdlib only)
  serves `/admin/`. It has a "Refresh from eBay" button, CSV upload, sync status
  and the review report. It also runs the daily scheduled sync and answers eBay's
  account-deletion challenge at `/ebay/account-deletion`.
- Builds are atomic. They are written to `public/.build-*` and swapped into
  `public/live`, so a failed build never leaves a half-built site.

## Commands

```sh
scripts/mec import-csv <file.csv> [--dry-run] [--partial|--complete] [--force]
scripts/mec sync              # Browse API; logs "skipped" and changes nothing without keys
scripts/mec enrich            # fetch public descriptions (and item pages if CAMOFOX_URL is set)
scripts/mec scrape-store      # best-effort public store-page read (never ends or sells anything)
scripts/mec build             # rebuild public/live
scripts/mec status            # sync state + review flag counts
scripts/mec discogs match     # match records to Discogs releases (dry run; needs DISCOGS_TOKEN)
scripts/mec discogs plan      # preview Discogs listing changes (read-only)
scripts/mec discogs sync-listings  # list approved records on Discogs (per-record approval required)
scripts/mec discogs poll      # check Discogs sales (read-only on Discogs; also runs every 15 min)
scripts/import-ebay.sh [csv]  # all of the above in order, then build
python3 tests/test_import.py  # import and sync-safety tests
docker compose up -d --build  # run web + admin
```

`scripts/make-brand-assets.py` regenerates the hero, OG image and icons from
`assets/src/poster.jpg`. It needs Pillow; nothing else does.

## Data schema

### `data/source/items/<item id>.json` (written only by imports and syncs)
| key | meaning |
|---|---|
| `item_id` | eBay item number (the dedupe key) |
| `listing_url` | `https://www.ebay.com/itm/<item_id>` |
| `status` | `active`, `unavailable` (0 qty), `ended` (gone from a complete snapshot, not known sold) or `sold` (eBay-confirmed sale) |
| `status_source`, `status_reason`, `status_changed_at` | why and when the status was set |
| `price` | `{value, currency, source}`: the current price, or the sale price for sold items |
| `quantity_available` | from the latest active snapshot |
| `sales[]` | `{date, price, currency, quantity, source}` from transaction or orders reports |
| `first_seen_at`, `last_seen_active_at`, `sold_at` | timestamps |
| `raw.<source>` | the mapped CSV row or API JSON, exactly as received (buyer, address, payout and fee columns are never read) |

### `data/source/public/<item id>.json`
Raw public captures: `description_html` (the listing description) and
`item_page` (title, photos, item specifics, condition), with fetch timestamps.

### Site record fields (derived in `mec/merge.py`)
Artist, album, format type, record size, speed, genre or style, label, catalog #,
release year, **listing year** (a year that appears only in the title, so it could
be a release or a pressing year), pressing year, country, vinyl grade (+ note),
sleeve grade (+ note), condition, edition or pressing notes, images and
description. Each one carries **provenance** (`aspect:<Item specific>`,
`description:<Label>`, `title`, or `editorial`). Unknown fields stay blank and
are listed in `data/review-report.json`.

Priority: API or item-page specifics > Edwin's description lines > title
parsing. For grades, the description wins over the item-specifics dropdown.
Every disagreement is flagged as a conflict. Pressing details are never inferred
from artwork or from what an album "usually" is.

### `data/editorial/<item id>.json`
Your overrides. See `data/editorial/README.md`.

### `data/sync-state.json`
`last_success`, `last_success_source`, `last_attempt`, `last_result`
(`ok`, `failed` or `skipped`), `errors[]` and `history[]`. The site footer shows
"Inventory last synced …" from `last_success`.

### `config/site.json`
Site URL, store URL, seller username, contact placeholders, EPN campaign ID,
Browse categories, sync schedule (`sync.daily_at_utc`) and the end-guard
(`sync.max_end_fraction`).

### `config/csv-mappings.json`
Column mappings and detection rules for each supported eBay CSV format. Only the
mapped columns are ever read.

## Secrets
`.env` (not committed) holds `ADMIN_TOKEN`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`,
`EBAY_DELETION_VERIFICATION_TOKEN`, `EBAY_DELETION_ENDPOINT_URL` and `DISCOGS_TOKEN`
(see `docs/DISCOGS-INTEGRATION.md`). Raw CSV
exports are never committed (`*.csv` is gitignored, apart from the synthetic test
fixtures).

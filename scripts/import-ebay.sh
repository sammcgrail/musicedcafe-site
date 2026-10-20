#!/bin/sh
# Re-pull everything eBay will give us without a login, then rebuild the site.
#   1. Browse API sync (only if EBAY_CLIENT_ID/SECRET are in .env; otherwise a logged skip)
#   2. public store page read (best effort, never ends or sells anything)
#   3. public descriptions / item pages for known items that are missing them
#   4. rebuild public/live
# Optional: scripts/import-ebay.sh /path/to/ebay-export.csv  (imports the CSV first)
# Set CAMOFOX_URL=http://localhost:9377 to let step 3 try item pages for photos.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
MEC="python3 -I $HERE/mec"
if [ "${1:-}" != "" ]; then
  $MEC import-csv "$1" --no-build
fi
$MEC sync --no-build || echo "sync failed (see data/sync-state.json); catalog unchanged"
$MEC scrape-store || true
$MEC enrich || true
$MEC build

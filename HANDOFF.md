# Music Ed Cafe website: handoff

**Live:** https://musicedcafe.com/ (musicedcafe.net, www.* and musicedcafe.sebland.com 301 here; eBay deletion endpoint still at musicedcafe.sebland.com/ebay/account-deletion until re-registered)
**Admin:** https://musicedcafe.com/admin/ (needs the admin token in `.env`)
**Repo:** github.com/sammcgrail/musicedcafe (private)

## Where the catalog stands (2026-10-20, after the first eBay API sync)

| | count | source |
|---|---|---|
| **Active records for sale** | **339** | eBay Browse API (production keys), seller `music_ed_cafe`, 03:03 UTC sync |
| API vs Seller Hub export | 339 = 339 | same item ids both ways: 0 API-only, 0 export-only, 0 auctions |
| Active with Edwin's own photos | **339 of 339** | eBay listing images (1,109 photos). The lead photo is self-hosted as WebP (300/720px); extra gallery photos link to i.ebayimg.com |
| Sold records (archive) | 88 | Transaction report: 1 with Edwin's photo, 44 stock covers, 43 placeholders |
| Ended / newly sold / added by the sync | 0 / 0 / 0 | |

Item specifics coverage for the 339 active records (eBay item specifics first,
then Edwin's description, then the title):

| Field | Records with it |
|---|---|
| Artist | 339 |
| Album | 339 |
| Format | 338 |
| Speed | 336 |
| Record size | 334 |
| Label | 329 |
| Vinyl grade | 317 |
| Catalog number | 313 |
| Country | 313 |
| Genre | 306 (303 in a genre collection) |
| Release year | 261 |
| Sleeve grade | 197 |

For grades, Edwin's description wins over the item-specifics dropdown, and 122
grade disagreements are flagged (73 vinyl, 49 sleeve). See `/admin/` →
Needs review.

**Sync behaviour learned on the first run.** Paging through Browse search
without a sort order returned only 333 unique ids out of a reported total of
339. The client now pages with `sort=newlyListed`, falling back to other sort
orders and then direct `get_item_by_legacy_id` lookups, until the union
reconciles with the total. `config/site.json → browse.trusted_complete` is
`true` because the first reconciled API snapshot matched the export exactly.
From now on, a listing that disappears from a successful, reconciled sync is
marked "ended". A sync that would end more than 50% of stock is still refused.

### Stock cover art (temporary, until Edwin's own photos come through)
Records with no eBay photo show **stock cover art** from the Cover Art Archive /
MusicBrainz (iTunes Search as a fallback). It is labelled "Stock cover" on
cards, with a line under the image on record pages ("Stock cover art for
reference, not a photo of this copy…"). It is kept OUT of the Product JSON-LD
and the OG image, and credited in the footer and on How buying works.
- Separate layer: `data/enrichment/coverart.json` (mbid, release group, source
  URL, confidence, fetched_at, or the rejected candidate and reason), with
  self-hosted WebP files in `data/assets/coverart/` (300px for cards, 500px for
  record pages). It is never mixed into eBay source data or editorial files.
- Precedence: Edwin's eBay photos > editorial `images` > stock art > placeholder.
- Matching is strict. Artist and title must both match after normalising.
  Singles only match Single/EP release groups, and LPs never match Singles. A
  catalog-number match picks that exact release's art. Otherwise a vinyl
  release's front is used, then the release-group front, but only if that
  image comes from a vinyl release (CD, SHM-CD and digital-edition art is
  dropped). Low-confidence candidates are rejected and listed in
  the admin review report.
- Refresh: `scripts/mec coverart`. It's cached, so only new or changed records
  are fetched, at about 1 request per second to MusicBrainz.
  `--retry-misses` retries rejected and no-match records.
- To veto a cover, set its entry's `status` to `"vetoed"` in
  `data/enrichment/coverart.json` and rebuild. Re-runs never refetch a vetoed
  entry. One cover (Pointer Sisters –
  Fire, 128015289745) was vetoed by hand: CAA holds the wrong image for that
  release.

## 1. Fully working now
- Pre-rendered static site (about 460 pages): home, Shop All Records,
  Recently Sold archive, 427 record pages, genre and format collections,
  6 guides, About, Contact and How buying works.
- **Sticky search bar** on Shop and Recently Sold. Search, Filters & sort and
  the live result count stay pinned under the header while scrolling, and a
  floating **Back to top** button appears after about one screen of scrolling
  on long pages.
- **Search, filters and sort** (client-side, no server). Text search covers
  artist, title, label, catalog # and year. Filters are genre, format, price and
  vinyl grade. State lives in the URL (`?q=`, `?genre=`…), so links are shareable.
  On mobile, filters open in a bottom-sheet drawer.
- **Record pages**: photos (when available), details with provenance, Edwin's
  original description, JSON-LD Product/Offer, canonical, OG tags and "more
  from this genre". Active records get a big **Buy on eBay** button (plus a
  sticky bar on mobile) linking to `https://www.ebay.com/itm/<id>`, with
  "Payment, shipping and returns are handled securely on eBay". Sold pages get a
  sold banner and **available alternatives** (same artist, then genre, then
  format), and are left out of Shop, search and featured.
  Auctions (none listed today) show "Auction · current bid $X · ends <date>"
  with a **Bid on eBay** button. Fixed-price listings show the price and
  **Buy on eBay**.
- **SEO**: `sitemap.xml`, `robots.txt`, OnlineStore JSON-LD (Lynn, MA, **online
  only: no street address, hours or storefront**), BreadcrumbList, Article,
  CollectionPage, semantic HTML and a skip link. The hero image is AVIF/WebP at 3
  sizes and is the LCP element; everything else is lazy-loaded.
- **Inventory pipeline**: raw eBay data in `data/source/`, your edits in
  `data/editorial/`, merged at build time. Field-level provenance, a review
  report, and dedupe by eBay item ID.
- **CSV importer** for the Transaction report, Orders report, Seller Hub "All
  active listings" and File Exchange active listings. It handles column mapping,
  report preambles and validation. Buyer, ship-to, payout, fee and tax columns
  are dropped on read. It has dry-run preview, safe ended handling and a
  missing-columns report. Tests: `python3 tests/test_import.py` (14 passing).
- **Sync safety**: a failed or partial sync changes nothing. Listings are ended
  only after a complete snapshot, with a 50% mass-ending guard. "Sold" is set
  only on an eBay-confirmed sale. `data/sync-state.json` records the last
  success, last attempt and errors, and the footer shows "Inventory last synced …".
- **Admin page** (`/admin/`, token login): Refresh from eBay, Rebuild, CSV upload
  with preview then apply, "fetch missing photos & descriptions", sync status
  and errors, and the per-item review list.
- **Daily scheduled sync** (in the admin sidecar, 10:15 UTC). It is live now
  that API keys are in `.env`. It runs Browse sync, then photos, then rebuild,
  and records the result in `data/sync-state.json`, the footer and `/admin/`.
- **eBay account-deletion endpoint** at `/ebay/account-deletion`, which a
  production keyset requires.
- Six original guides: cleaning, storage, the Goldmine grading scale, first
  turntable setup, spotting a first pressing, and buying used vinyl online.

## 2. Placeholders waiting on Edwin
- **About page story**: a "Coming soon" box marks the spot (edit `content/about.html`).
- **Email and social links**: `config/site.json → contact` (email, instagram,
  facebook, tiktok, youtube). Empty values stay hidden, and the contact page says
  "coming soon".
- **YouTube channel link** (his store page mentions the channel).
- **Photos**: none of the 339 active records and only 1 of 88 sold records have
  photos. The API fills them for active listings. Until then, a photo URL can be
  added per item in `data/editorial/<id>.json` (`"images": [...]`).
- **Any confirmed details**: a storefront, hours, services or memberships are
  NOT shown anywhere. The About page calls a real cafe "a someday dream" and says
  it's online only. Change that copy only once something is confirmed.
- **Item specifics on eBay**: genre, record size, speed, catalog #, country, and
  separate vinyl and sleeve grades. Filling them in on eBay fixes them on the
  site at the next sync. Use `data/editorial/<id>.json` for site-only fixes.
- **Fresh active-listings CSVs** until API keys exist (Seller Hub → Reports →
  Downloads → Listings → All active listings), uploaded on `/admin/` as a
  complete snapshot. Listings missing from a new complete export move to the
  archive as "ended".

## 3. Credentials and hosting to go fully live
1. **Domain** (for example musicedcafe.com, about $10–15/yr). Either point DNS
   here (add a Caddy site block for the domain and change `base_url` in
   `config/site.json`), or move to free static hosting (Cloudflare Pages or
   Netlify). `public/live/` is a plain static folder, and the admin sidecar is
   only needed for the button, the schedule and the deletion endpoint. Without
   the sidecar, run `scripts/import-ebay.sh` from a cron or a GitHub Action
   instead.
2. **Google Search Console**: verify the domain (DNS TXT), then submit
   `/sitemap.xml`.
3. **eBay developer account** (free) and a **production keyset**. Put
   `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` in `.env`. Before eBay activates the
   keyset, register the account-deletion endpoint with
   `EBAY_DELETION_VERIFICATION_TOKEN`. See `docs/EBAY-INTEGRATION.md`. No eBay
   accounts were created.
4. *(Optional)* **Trading API user consent**, if Edwin wants ended and sold
   history synced automatically rather than through CSV exports.
5. *(Optional)* **eBay Partner Network campaign ID**: set `epn_campaign_id` for
   affiliate-tagged Buy links. Check the EPN terms for linking to your own
   listings first.

## 4. Adding or updating records
- **Normal path (after API keys):** list on eBay as usual. The daily sync, or
  the admin page's "Refresh from eBay", adds the listing, updates price and
  quantity, and moves sold or ended listings to the archive.
- **Without keys:** download a CSV from Seller Hub and upload it on `/admin/`
  (raw exports go in `data/imports/`, which git ignores).
  Preview it, then Apply. The CLI equivalent is
  `scripts/mec import-csv file.csv`. Upload the Transaction report too, so sold
  items are marked as sold rather than just "ended".
- **Fix or add details:** create `data/editorial/<item id>.json` (see
  `data/editorial/README.md`), then Rebuild. Editorial edits survive every sync.
  eBay always wins on price, quantity, status and URL.
- **Feature a record:** `{"featured": true}` in its editorial file.
- After any manual edit: `scripts/mec build` (or Rebuild on `/admin/`).

## 5. Discogs (list there too, catch cross-sales)
Built and tested against mocks (2026-10-20). The full detail and API citations
are in `docs/DISCOGS-INTEGRATION.md`.

**Setup for Edwin:**
1. On discogs.com, fill in **Seller Settings**: payment, shipping policies,
   ships from US, and terms. Discogs won't put a listing on sale without them.
2. Settings → Developers → **Generate new token**. Put it in `.env` as
   `DISCOGS_TOKEN=…`, then restart the admin container
   (`docker restart musicedcafe-admin`; the code is bind-mounted). After 10:15
   UTC a restart also runs that day's eBay sync.
3. Run `scripts/mec discogs match`, or press "Match records to Discogs" on
   `/admin/`. This is a dry run that only writes `data/discogs/matches.json`,
   and takes about 6–15 min for 339 records at 1 request a second.
4. Review on `/admin/` → Discogs: approve, pick another candidate, type a
   release ID, set a sleeve value, or skip. "Approve all confident" asks for
   confirmation.
5. Choose the settings (price rule, offers, comment template, new listings as
   **Draft**), turn on **List on Discogs** for the records you want, then press
   **Sync listings now**. Check the drafts on Discogs and publish them there.

**Quick listing (Edwin's decision, 2026-10-20):** list every record on its most
likely release now, and verify each pressing from the runout over time.
1. Optional but better: `scripts/mec discogs match --rematch` (about 6–15 min).
   This refreshes the candidates with every pressing that shares the catno and
   the Discogs "have" counts. The first run kept only the top 5 and no haves.
2. `scripts/mec discogs quickpick` (report, no API call), then
   `scripts/mec discogs quickpick --apply`. Or use "Quick-pick likely releases"
   on `/admin/`. Records become `quick`, with `pressing_verified: false`.
3. If the report says ties lack "have" counts: `scripts/mec discogs enrich`
   (GET /releases for the top 3 tied pressings), then `quickpick --apply` again.
4. `scripts/mec discogs enable` (preview and needs-grade queue), then
   `scripts/mec discogs enable --expected N --by Edwin`. Or use "Enable Discogs
   for all quick + confident records that have a vinyl grade". Records with no
   usable vinyl or sleeve grade stay out, listed under Needs grade.
5. `scripts/mec discogs sync-listings`. Listings arrive as **Drafts** whose
   comment ends "Pressing variant not verified; see photos on the listing page."
6. Edwin works through **`/admin/verify`** on his phone, highest price first.
   He types the side A/B runout, the best-matching Discogs pressing lights up,
   and he taps Confirm (or "Can't read"). A confirm updates the Discogs listing's
   release and comment ("Matrix/Runout: A: … / B: …"), and the record page shows
   Matrix/Runout after the next rebuild.

**Automatic:**
- Every 15 min the admin sidecar checks Discogs orders and sold inventory. This
  is read-only on Discogs.
- A Discogs sale marks the record sold here (`status_source: "discogs"`, which
  the eBay sync never undoes), adds an Alert and an **End on eBay** entry, and
  rebuilds the site so it moves to Sold.
- If "After each eBay sync, push changes" is on (off by default), each daily
  eBay sync pushes price and grade changes to Discogs and removes listings for
  records that ended or sold on eBay. It only removes them after a complete eBay
  snapshot, and it never touches records that weren't approved and toggled on.
  With it off, press "Sync listings now".

**Manual:**
- **Ending the eBay listing after a Discogs sale.** Use the links in End on
  eBay (Seller Hub → Active → find the item number → End listing). The next
  complete eBay sync clears the entry. Automating it needs Edwin's eBay OAuth
  consent for the Trading API `EndFixedPriceItem`; the doc lists the exact
  steps.
- Publishing drafts on Discogs.
- Picking a sleeve value for records without a sleeve grade.
- Verifying pressings (`/admin/verify`). Until then, a listing says the variant isn't verified.
- "Still for sale?" if a Discogs order is cancelled. It works only when eBay
  still shows quantity > 0.

## Ops notes (sebland)
- Containers: `musicedcafe-web` (nginx, 20085) and `musicedcafe-admin`
  (20086), both with `restart: unless-stopped`, from `docker compose up -d --build`.
- Caddy block `musicedcafe.sebland.com` in `/root/box/app/Caddyfile`. It was
  applied live via `docker cp` + `caddy reload`, so the box image wasn't rebuilt
  with other people's uncommitted work. The next normal box rebuild bakes it in.
- `.env` holds the admin token and is never committed. Raw eBay CSVs are never
  committed either.

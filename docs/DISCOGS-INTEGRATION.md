# Discogs integration: list on Discogs, mark cross-sales sold

Edwin asked: *"I want to be able to list my records on discogs ... have my ebay
listings linked ... so if it sells on one website it gets marked as sold on others."*

Code: `mec/discogs.py`, with CLI `scripts/mec discogs …`, endpoints in
`admin/server.py` (`/admin/api/discogs/*`), the "Discogs" section of `/admin/`,
and tests in `tests/test_marketplace_discogs.py` (mocked HTTP only).

eBay stays the source of truth for what exists, its price and its grades.
Discogs is a second shop window that follows eBay.

## Verified against the Discogs API docs

`https://www.discogs.com/developers` sits behind a Cloudflare bot check that
blocks server-side fetches. Everything below was read from the Wayback Machine
copy dated **2026-10-13**:
<https://web.archive.org/web/20261013233149/https://www.discogs.com/developers/>
(anchors: `#page:home,header:home-general-information`, `#page:authentication`,
`#page:database,header:database-search`, `#page:marketplace`,
`#page:user-identity`). Items marked *(inferred)* are not stated outright.
Check them on the first live run.

| What | Docs say | Used in the code |
|---|---|---|
| User-Agent | "Your application must provide a User-Agent string that identifies itself", e.g. `AwesomeDiscogsBrowser/0.1 +http://adb.example.com`. With no UA you get an empty response. | `MusicEdCafe/1.0 +https://musicedcafe.com` |
| Personal token | `Authorization: Discogs token=abcxyz123456`. A personal access token is "authenticated as user … for token holder only", which is what marketplace orders and private inventory fields need. | header `Discogs token=<DISCOGS_TOKEN>` |
| Rate limit | "Authenticated requests are limited to 60 per minute". It's a moving average over 60 s. Headers: `X-Discogs-Ratelimit`, `X-Discogs-Ratelimit-Used`, `X-Discogs-Ratelimit-Remaining`. "Your application should … throttle its requests locally." | 1 request per second at most; pauses 10 s when Remaining ≤ 3; 429 is retried with backoff (5, 10, 20, 40 s, or `Retry-After`) |
| Pagination | `page`, `per_page` (max 100, default 50), plus a `pagination` object with `pages`/`items` | reads every page; refuses a read of more than 60 pages |
| Identity | `GET /oauth/identity` → `{id, username, resource_url, consumer_name}` | gives the inventory username (cached in `data/discogs/state.json`) |
| Search | `GET /database/search`, "Authentication (as any user) is required". Params include `type`, `title`, `release_title`, `artist`, `label`, `country`, `year`, `format`, `catno`, `barcode`. Results have `id`, `title` ("Artist - Title"), `label[]`, `catno`, `format[]`, `year`, `country`, `thumb`, `uri`, and `community {want, have}` | `type=release` with `catno`+`label`, then `artist`+`release_title`+`format=Vinyl`+`year`. `community.have/want` is stored on each candidate since 2026-10-20 (the first match run dropped it) |
| Release | `GET /releases/{release_id}`. The example shows `identifiers[] {type, value}` (plus `description` on real releases *(inferred from the site; the docs example only has a Barcode)*), `community {have, want, …}`, `labels[] {name, catno}`, `formats[]`, `country`, `year`, `released`, `notes` | checks a release ID Edwin types in by hand; the verify page and `discogs enrich` cache it in `data/discogs/releases/<id>.json` |
| New listing | `POST /marketplace/listings` with `release_id` (required), `condition` (required), `sleeve_condition`, `price` (required, seller's currency), `comments`, `allow_offers` (default false), `status` ("For Sale" or "Draft"), `external_id` ("Private Comments", seller only), `location` (private storage location), `weight` and `format_quantity` (either can be `auto`). Request `Content-Type: application/json`. Response 201 `{listing_id, resource_url}` | JSON body; `external_id` = eBay item id; `weight`/`format_quantity` = `auto` |
| Edit listing | `POST /marketplace/listings/{listing_id}`, same fields; `release_id number (required)` "The ID of the release you are posting" is one of them. "If the listing's status is not For Sale, Draft, or Expired, it cannot be modified – only deleted. To re-list a Sold listing, a new listing must be created." Response 204 | updates resend all fields, keeping the listing's current status. A verified pressing changes `release_id` through this edit. *(inferred)* that Discogs accepts a *different* release id there: the docs list the field but don't say. If the edit is refused (400 / 409 / 422) the listing is deleted and re-created as a Draft, idempotently (below) |
| Delete listing | `DELETE /marketplace/listings/{listing_id}`: "Permanently remove a listing". Response 204 | used when the record ended or sold on eBay |
| Inventory | `GET /users/{username}/inventory{?status,sort,sort_order}`. As the inventory owner you "get additional weight, format_quantity, external_id, location, and quantity keys". Status filter, e.g. `for sale` | read in full before any write (idempotency); `status=Sold` in the poll |
| Price suggestions | `GET /marketplace/price_suggestions/{release_id}`: "Retrieve price suggestions for the provided Release ID. If no suggestions are available, an empty object will be returned. Authentication is required, and the user needs to have filled out their seller settings. Suggested prices will be denominated in the user's selling currency." Body: one key per condition, e.g. `"Very Good (VG)": {"currency": "USD", "value": 6.78}` | Sell Your Records offer calculator only (`mec/sell_offer.py`, admin): the value at the grade Edwin picks; cached 7 days in `data/sell/valuations/<id>.json` |
| Marketplace stats | `GET /marketplace/stats/{release_id}{?curr_abbr}`: number for sale, lowest listed price, blocked flag. "Releases that have no items for sale ... will return a body with null data in the lowest_price and num_for_sale keys." Body `{"lowest_price": {"currency", "value"}, "num_for_sale": 26, "blocked_from_sale": false}` | same; `curr_abbr=USD`; the lowest price is the fallback when there's no suggestion |
| Orders | `GET /marketplace/orders{?status,created_after,created_before,archived,sort,sort_order}`. Status keys: All, New Order, Buyer Contacted, Invoice Sent, Payment Pending, Payment Received, In Progress, Shipped, Merged, Order Changed, Refund Sent, Cancelled, Cancelled (Non-Paying Buyer), Cancelled (Item Unavailable), Cancelled (Per Buyer's Request), Cancelled (Refund Received). Each order has `id` ("1-1"), `status`, `created`, `items[]` `{id, release{id}, price{value,currency}}`, and also buyer and shipping address | `created_after`, `sort=created`. **Only** order id, status, date, item listing id, release id and price are kept. Buyer and address are never stored |

**Condition vocabulary** (New Listing and Edit Listing, verbatim):
`condition` must be one of `Mint (M)`, `Near Mint (NM or M-)`,
`Very Good Plus (VG+)`, `Very Good (VG)`, `Good Plus (G+)`, `Good (G)`,
`Fair (F)`, `Poor (P)`. `sleeve_condition` takes the same list plus `Generic`,
`Not Graded`, `No Cover`.

*(inferred)* An order item's `id` is the **listing id**. The docs' example
shows `items[].id` but doesn't label it. That's why the poll also reads
`inventory?status=Sold`, which carries `external_id` = the eBay item id. Either
path alone is enough to catch a sale.

*(inferred)* The inventory `status` filter takes the listing status text
(`For Sale`, `Draft`, `Sold`). The docs only show `for sale` as an example.

## Setup Edwin does once (on discogs.com)

1. **Seller settings** (Dashboard → Seller Settings): PayPal / Discogs Payments,
   shipping policies (`weight=auto` is sent so Discogs can estimate shipping),
   "Ships from" = United States, plus terms. Discogs won't let a listing go
   For Sale until these are filled in. A Draft works either way.
2. **Token**: Settings → Developers → "Generate new token". Put it in `.env` as
   `DISCOGS_TOKEN=…` (never committed). Restart the admin container so the poll
   picks it up.
3. **Match**: `scripts/mec discogs match` (or "Match records to Discogs" on
   `/admin/`). This is a dry run that only fills `data/discogs/matches.json`.
   At 1 request a second, 339 records take about 6–15 minutes (1–3 searches each).
4. **Review** on `/admin/` → Discogs → Review matches (details below).
5. **Settings**: price rule, offers, comment template, and new-listing status
   (Draft by default).
6. Turn on **List on Discogs** for the records you want, then press **Sync
   listings now**. They arrive as Drafts. Look them over on Discogs and publish
   them there (Discogs' inventory page has a bulk "Edit → Status" action).

## Matching: record → release_id

- **Search order**: `catno` + `label`. If that finds nothing, `catno` + `artist`.
  `artist` + `release_title` + `format=Vinyl` + `year` (then without year) runs
  only when no candidate shares the catalog number.
- **Scoring**: catalog number exact after normalising (+40; upper-case, with
  spaces, dashes, dots and slashes dropped, and each comma-separated Discogs
  catno compared), label (exact +20, partial +8; "Records/Inc/Ltd/Music" and
  similar ignored), format (+15 consistent, −40 inconsistent). Formats are
  compared as LP, 7", 10", 12" single, from record size + speed + format type.
  CD and cassette candidates are always inconsistent. Then artist (+7) and title
  (+8) words, year (+10 exact, +3 if a year apart, −5 otherwise) and country
  (+10 / −10).
- **Year used**: the pressing year, else the release year. A year that only
  appears in the eBay title is never used, because it could be either.
- **confident**: catno + label exact and format consistent, AND it's the only
  pressing that shares that catno. If several pressings share the catno (same
  catno, other country or year, or a label we can't confirm), it stays
  **review** unless the record's own country and/or year narrows them to
  exactly one. Truncated search results are always **review**.
- **review**: up to 5 candidates with thumbnails and Discogs links.
  **no_match**: nothing shares the catno and the best score is under 30.
- **Never from artwork.** Thumbnails are only shown to Edwin. No image is
  compared, and no pressing is guessed from a cover or from what an album
  "usually" is.
- A re-run only matches new or changed records. It never touches an approved or
  skipped one. If eBay details change after a decision, the record is flagged
  "re-check".

## Quick listing: Edwin's rule (2026-10-20)

Most records are **review**: several pressings share the catalog number. Edwin
chose "quick": list every record on its most likely release now, say so in the
comment, and verify the pressing later from the runout.

`scripts/mec discogs quickpick` (report only) / `--apply`, or **Quick-pick
likely releases** on `/admin/` (shows the count, then asks for confirmation).
It makes **no API call**: it only reads the cached candidates in matches.json.

- **Scope**: every `review` record (and earlier quick picks that aren't
  verified) with at least one candidate that shares the catalog number and
  could be the same format. If any of them also match the label, only those
  are used. `no_match` and records with no shared catno are left out.
  `confident` records keep their status.
- **Order** (each filter is only applied if it leaves something):
  0. the eBay title (pressings whose title matches; the artist's name inside
     the album field and A/B sides of singles are allowed for). If none match,
     the record is still picked but flagged "no pressing with this catalog
     number has the eBay title (check the catno or the title)". Use
     `--exclude-title-mismatch` to leave those out.
  1. (a) the record's country, then its year (pressing year, else release year);
  2. (b) the closest format: the same LP / 7" / 10" / 12" class with no speed
     contradiction, then consistent, then unknown;
  3. (c) the original era: the earliest year at or after the record's release
     year. If that year is unknown, or every pressing is older, the earliest
     overall. Undated pressings come last;
  4. (d) the most Discogs `community.have`; then the match score; then the
     lowest release id.
- **Stored**: `status: "quick"`, `chosen_release_id` = `approved_release_id`,
  `pressing_verified: false`, `approved_by: "edwin-quick-rule"`,
  `approved_at`, `approved_via: "quick_rule"`, `quick_reason` (the steps in
  words) and `quick_pick` (`ranked`, `tie_break`, `warnings`).
- **Confident** records get `pressing_verified: true` only when their match
  was exact on catno AND country AND year (`pressing_verified_via: "exact
  country+year+catno"`), otherwise `false`.
- A match re-run never touches `quick` (or verified) records.
- **Missing data**: the first match run did not store `community.have`, so a
  tie at step (d) falls back to the match score. `scripts/mec discogs enrich`
  fetches `GET /releases/{id}` for the top 3 tied pressings of each such record
  (read-only, 1 request/second, cached in `data/discogs/releases/`). Re-run
  quickpick afterwards. The first run also stored only the **top 5 candidates**
  per record; new match runs store every pressing that shares the catno (up to
  40). Re-running `scripts/mec discogs match --rematch` before the quick pick
  refreshes both (it never touches quick, approved or skipped records).

Quick-picked records are **not listed by themselves**. Listing still needs the
per-record toggle, or the bulk action **Enable Discogs for all quick +
confident records that have a vinyl grade** (`scripts/mec discogs enable`,
preview first, then `--expected N`). It confirms the count, approves the
confident ones at the same time, and leaves out records whose vinyl or sleeve
grade doesn't map. Those are shown in the **Needs grade** queue. New listings
start as **Draft** (the default setting). Edwin flips them to For Sale on
Discogs.

**Comment**: every listing's comment ends with one line:
- `pressing_verified` false: "Pressing variant not verified; see photos on the listing page."
- verified with a runout: "Matrix/Runout: A: <side A> / B: <side B>"
- verified by exact country+year+catno, no runout typed: no extra line.

The template is trimmed first, so the line always survives the 1,000-character cap.

## Verify pressing (`/admin/verify`, phone page)

The same token sign-in as `/admin/` (the page is a static shell holding no
data; every `/admin/api/discogs/verify/*` call needs `Authorization: Bearer
<ADMIN_TOKEN>`). It's built for 390 px, with 44 px+ tap targets.

- **Queue**: unverified records (review, quick, confident, approved with
  candidates; active on eBay), highest price first, "can't read" ones last,
  with "N of M verified". Search by eBay item #, artist, title or catno.
- **Record**: eBay photo and title, catno, label, country/year, the release
  it's listed as now, the quick rule's reasons and warnings, and every
  candidate. For each candidate: country, released date, labels, catno,
  format, haves, release notes, and the `identifiers` from `GET
  /releases/{id}` (Matrix / Runout, Lacquer Cut Code, Stamper, Mastering SID
  and similar, with their descriptions. Barcode, ASIN, price/label/rights
  codes are left out). Identifiers load lazily, one release at a time, when
  the record opens: `GET /admin/api/discogs/verify/release?item=&rid=` (only
  the record's own candidates; read-only client; one 1 req/s budget shared with
  any other Discogs client in the admin process; cached in
  `data/discogs/releases/<id>.json`).
- **Inputs**: Side A runout, Side B runout, optional lacquer / stamper codes,
  and **Can't read** (stores `verify_skipped_at` and moves to the next record).
- **Auto-match** (`POST /admin/api/discogs/verify/match`): both sides are
  normalised (case, spaces, dashes, dots and slashes ignored; △ ▽ ▲ ▼ Δ ∆ and
  the words "delta" / "triangle" all become one token). Each typed value is
  compared with every identifier: exact = 1; typed text found inside the
  identifier = 0.85–1; the identifier inside the typed text = 0.6–1 (by length
  ratio); one typo on a code of 6+ characters = 0.75; otherwise 0.8 × token
  overlap. A release's score is the mean over the fields typed, × 100. The best
  one (score ≥ 40) is highlighted with its score and a one-tap **Confirm**. A
  "compare them" note appears when the runner-up is within 10 points. Any
  candidate can be picked instead with "This is my pressing".
- **Confirm** (`POST /admin/api/discogs/verify/confirm`): `chosen_release_id`
  = `approved_release_id` = the release, `status: "approved"`,
  `pressing_verified: true`, `matrix {side_a, side_b, lacquer, stamper,
  verified_at, verified_by, match_score, release_id}`. If a Discogs listing
  exists, a background listing sync for just that record is started. It edits
  the listing (`POST /marketplace/listings/{id}`) with the new `release_id` and
  the Matrix/Runout comment, keeping its status. If Discogs refuses the release
  change (400 / 409 / 422), the listing is marked `recreate_pending` (saved
  first), deleted, and re-created as a **Draft**. A crash between the two steps
  is finished by the next sync: it adopts by `external_id`, or creates the
  Draft.
- **Site**: a verified record's page shows **Matrix/Runout** in its details
  table after the next rebuild. Unverified records show nothing.

## Review and approval (admin page)

The admin page shows counts per status and one card per record: the eBay photo,
title and link, the Discogs candidates (thumbnail, link, label, catno, format,
year, country and score signals), the price on eBay and on Discogs, and the
grade mapping. Grade conflicts from the review report are shown too.
Actions:

- **Approve**: approve the suggested release.
- **Use this**: pick a different candidate.
- **Use release ID**: type an ID or paste a release URL. It's checked with
  `GET /releases/{id}` when a token is present.
- **Skip** and **Undo**.
- **Sleeve override**: set Generic, Not Graded, No Cover, or a grade.
- **Approve all confident (N)**: asks for confirmation, and the server refuses
  if the count changed since the page loaded.
- **List on Discogs**: a per-record toggle, available once approved. **Turn on
  listing for all approved (N)**: also confirmed and count-checked.

Each approval stores `approved_by` (the name typed into "Your name"),
`approved_at` and a short history in `data/discogs/matches.json`.

## Listing fields

- **condition**: Edwin's vinyl grade mapped as M → Mint, NM/NM-/M- → Near Mint,
  VG+ → VG+, VG → VG, G+ → G+, G → G, F → Fair, P → Poor. "Strong VG+" counts as
  VG+. Discogs has no **EX / EX+ / VG++**, so those are listed as **VG+**, and
  **VG-** as **G+**. They're rounded down so Discogs never overgrades a record,
  and a note is shown. A missing or unreadable vinyl grade leaves the record out
  and flags it.
- **sleeve_condition**: the same mapping. If there is no sleeve grade, a 7"
  whose description says it's in a generic/company/plain/die-cut sleeve, and
  never mentions a picture sleeve or jacket, gets `Generic`. Otherwise the record
  is left out and flagged until Edwin picks a sleeve value on the admin page or
  adds a grade on eBay.
- **price**: "same as eBay" (exact), or eBay adjusted by a percent and rounded
  to .99 (nearest whole dollar minus 1¢, at least 0.99), .00 or the cent. Only
  USD records are listed.
- **comments**: a template with fields `{vinyl} {sleeve} {vinyl_grade}
  {sleeve_grade} {notes} {pressing_notes} {record_url} {ebay_url} {artist}
  {album} {label} {catalog_number}`. The default says the grades, Edwin's
  condition notes, "Ships from Lynn, MA", and links the record page on
  musicedcafe.com. It's capped at 1,000 characters (our own limit; Discogs
  doesn't document one).
- **external_id** = the eBay item id. **location** = an optional private shelf
  note. `allow_offers` comes from settings. `status` for new listings comes from
  settings (Draft by default). An edit keeps whatever status the listing has on
  Discogs, so if Edwin publishes a draft there, it stays published.

## Listing sync (`scripts/mec discogs sync-listings`, "Sync listings now")

1. Refuses outright, with no calls, if the **latest eBay sync did not succeed**.
2. Reads `GET /oauth/identity` and the **whole owner inventory**. If any read
   fails, nothing changes.
3. Plans one action per record:
   - **create**: approved, toggle on, active on eBay, grades and price usable.
   - **adopt**: a listing with `external_id` = the eBay id already exists, for
     example after a crash mid-run. It's linked, not duplicated.
   - **update**: the price, grades, comment, offers or release changed on eBay.
   - **unlist** (set to Draft): toggle turned off, approval undone, 0 quantity on
     eBay, or a grade became unmappable.
   - **delete**: the record is **ended or sold on eBay** AND the latest eBay
     snapshot was **complete**. After a partial snapshot it is held back as
     `deferred_delete`. More than 50% of listings deleted in one run is refused
     without `--force`.
   - **blocked**: approved and toggled on, but not listable. The problems are
     shown on the card.
   - A listing that is **Sold** on Discogs is never edited or re-created. A
     listing someone removed by hand on Discogs is not re-created until the
     toggle is turned off and on again.
4. `data/discogs/listings.json` records what happened on Discogs, even when a
   later item in the same run fails.

`scripts/mec discogs plan` (or "Preview listing changes") prints the plan using
a read-only client.

Automatic: with **"After each eBay sync, push changes" = Yes** (off by
default), the daily eBay sync runs the listing sync after a successful refresh.
This is how price and grade changes on eBay reach Discogs without anyone
pressing a button. It still only touches approved, toggled records.

## Cross-sale detection (every 15 min, plus "Check Discogs sales now")

- The admin scheduler runs `discogs.poll()` every 15 minutes
  (`DISCOGS_POLL_MINUTES`). It retries a minute later if another job holds the
  lock.
- The poll gets a **read-only client**: any POST or DELETE raises before a
  request is built. It reads `GET /marketplace/orders?created_after=…` (from
  the last poll, minus a 24 h overlap) and `GET /users/{u}/inventory?status=Sold`.
- A sale is mapped to a record **only through a listing in listings.json**,
  so Edwin's other Discogs stock is never touched. With no listings yet, the
  poll only calls `/oauth/identity`.
- On a sale, the source record (`data/source/items/<id>.json`) gets
  `status: "sold"`, `status_source: "discogs"`, the sale date and price, and a
  `discogs_sale` block (order id and link, listing id, and eBay tracking
  fields). Then:
  - an **Alerts** entry is added ("Sold on Discogs: … End eBay listing N now"),
    or a critical "sold twice" alert if eBay had already sold it;
  - the record appears in **End on eBay** on `/admin/`, with links to the
    Discogs order, the eBay listing and Seller Hub → Active listings;
  - the site is rebuilt, so the record moves to Recently Sold.
- **The eBay sync can't undo it.** `store.apply_active_snapshot` skips the
  status and price for a record with an open `discogs_sale`. It only records
  `ebay_seen_active_at` / `ebay_quantity`, and once a complete snapshot no
  longer has the listing, `ebay_listing_gone_at`. That clears End on eBay
  automatically. An eBay sale report for the same record is kept as a
  double-sale flag and doesn't replace the Discogs source.
- **Back to active** is only possible through "Still for sale?" (or
  `discogs.confirm_relist`). It requires that Edwin confirms, AND that an eBay
  snapshot taken after the sale shows quantity > 0. A cancelled Discogs order
  raises an alert but never reverts anything by itself.
- **Alert hook**: `discogs.register_alert_hook(fn)` calls `fn(alert_dict)` for
  every new alert (the place to plug in email later). A failing hook never
  blocks the feed. The feed itself is `data/discogs/alerts.json`.

## Ending on eBay automatically: not built, and what it would need

Edwin's listings are made in Seller Hub, so the **Sell Inventory API can't end
them**. It only sees listings created through the Inventory API (eBay KB 5276;
see `docs/EBAY-INTEGRATION.md`). The route that works is the **Trading API
`EndFixedPriceItem`** (`ItemID` + `EndingReason`, where `NotAvailable` means
"the item is no longer available for sale"), or `EndItem` for auctions. Only
the item's seller can call it.
[EndFixedPriceItem](https://developer.ebay.com/Devzone/XML/docs/Reference/eBay/EndFixedPriceItem.html),
[EndReasonCodeType](https://developer.ebay.com/DevZone/XML/docs/Reference/eBay/types/EndReasonCodeType.html).
In the last 12 hours of a listing that has bids, it can't be ended early.

The consent it needs:
1. In the eBay developer portal, on the production keyset: User Tokens →
   create a **RuName** (redirect URL name), with an accept URL on our domain
   (for example `https://musicedcafe.com/admin/ebay/consent`).
2. Edwin signs in to eBay **as music_ed_cafe** once through the OAuth
   **authorization code grant** and grants the app access. We exchange the code
   for a user access token (about 2 h) and a **refresh token** (about 18 months).
   Store both in `.env` or `data/` outside git.
3. Send Trading API calls with the user token in the `X-EBAY-API-IAF-TOKEN`
   header *(inferred, check the Trading OAuth page)*. The scope is the
   Trading-API-compatible user scope, `https://api.ebay.com/oauth/api_scope`
   *(inferred)*. The older alternative is an Auth'n'Auth token
   (`RequesterCredentials/eBayAuthToken`, valid 18 months).
4. Then `discogs.poll` could call EndFixedPriceItem after a Discogs sale. It
   should still keep End on eBay as the fallback when the call fails.

Until then, ending on eBay is **manual**. The Seller Hub link opens Active
listings: search the item number, then Actions → End listing. The next complete
eBay sync then clears the entry.

## Files

| file | what |
|---|---|
| `data/discogs/matches.json` | per eBay id: `status` (confident / review / no_match / quick / approved / skipped), `auto_status`, `candidates` (top 5 + every catno-sharing pressing up to 40, with `community` on new runs), `chosen_release_id`, `approved_release_id`, `approved_by`, `approved_at`, `list_on_discogs`, `sleeve_condition`, `quick_reason`, `quick_pick`, `pressing_verified`, `pressing_verified_via`, `matrix`, `verify_skipped_at`, `history` |
| `data/discogs/releases/<id>.json` | cached `GET /releases/{id}`: identifiers, community have/want, labels, formats, country, year, notes |
| `data/discogs/listings.json` | per eBay id: `listing_id`, `status` (For Sale / Draft / unlisted / deleted / sold / removed_on_discogs / recreate_pending), `pushed` fields, timestamps, `last_error`, `recreated_from`, `previous` |
| `data/discogs/settings.json` | listing settings. It's in `data/` because the admin container mounts the repo read-only, apart from `data/` and `public/` |
| `data/discogs/state.json` | `username`, `orders_cursor`, `last_match`, `last_listing_sync`, `last_poll` |
| `data/discogs/alerts.json` | Alerts feed (newest first, last 500) |

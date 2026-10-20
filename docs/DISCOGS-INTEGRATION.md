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
| Search | `GET /database/search`, "Authentication (as any user) is required". Params include `type`, `title`, `release_title`, `artist`, `label`, `country`, `year`, `format`, `catno`, `barcode`. Results have `id`, `title` ("Artist - Title"), `label[]`, `catno`, `format[]`, `year`, `country`, `thumb`, `uri` | `type=release` with `catno`+`label`, then `artist`+`release_title`+`format=Vinyl`+`year` |
| Release | `GET /releases/{release_id}` | checks a release ID Edwin types in by hand |
| New listing | `POST /marketplace/listings` with `release_id` (required), `condition` (required), `sleeve_condition`, `price` (required, seller's currency), `comments`, `allow_offers` (default false), `status` ("For Sale" or "Draft"), `external_id` ("Private Comments", seller only), `location` (private storage location), `weight` and `format_quantity` (either can be `auto`). Request `Content-Type: application/json`. Response 201 `{listing_id, resource_url}` | JSON body; `external_id` = eBay item id; `weight`/`format_quantity` = `auto` |
| Edit listing | `POST /marketplace/listings/{listing_id}`, same fields. "If the listing's status is not For Sale, Draft, or Expired, it cannot be modified – only deleted. To re-list a Sold listing, a new listing must be created." Response 204 | updates resend all fields, keeping the listing's current status |
| Delete listing | `DELETE /marketplace/listings/{listing_id}`: "Permanently remove a listing". Response 204 | used when the record ended or sold on eBay |
| Inventory | `GET /users/{username}/inventory{?status,sort,sort_order}`. As the inventory owner you "get additional weight, format_quantity, external_id, location, and quantity keys". Status filter, e.g. `for sale` | read in full before any write (idempotency); `status=Sold` in the poll |
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
| `data/discogs/matches.json` | per eBay id: `status` (confident / review / no_match / approved / skipped), `auto_status`, `candidates` (top 5), `chosen_release_id`, `approved_release_id`, `approved_by`, `approved_at`, `list_on_discogs`, `sleeve_condition`, `history` |
| `data/discogs/listings.json` | per eBay id: `listing_id`, `status` (For Sale / Draft / unlisted / deleted / sold / removed_on_discogs), `pushed` fields, timestamps, `last_error`, `previous` |
| `data/discogs/settings.json` | listing settings. It's in `data/` because the admin container mounts the repo read-only, apart from `data/` and `public/` |
| `data/discogs/state.json` | `username`, `orders_cursor`, `last_match`, `last_listing_sync`, `last_poll` |
| `data/discogs/alerts.json` | Alerts feed (newest first, last 500) |

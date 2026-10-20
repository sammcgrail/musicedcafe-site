# eBay integration: what each official route can and can't do

Checked against eBay's developer documentation and OpenAPI specs on 2026-10-18.
Items marked *(inferred)* were not stated outright in the docs. Test them once,
with a live call, before relying on them.

Seller: **`music_ed_cafe`** (store slug `musicedcafe`,
https://www.ebay.com/str/musicedcafe). Listings are created in Seller Hub, not
through the Inventory API.

## TL;DR recommendation

1. **Now (no keys needed):** import CSV exports from Seller Hub. Use the
   *Transaction report* for sold history (already loaded: 88 sales) and
   *Reports → Downloads → Listings → All active listings* for current stock.
   The site also reads each listing's public description page for the specifics
   Edwin types there.
2. **Next (free developer account, application keys only):** the **Browse API**.
   Use `item_summary/search` with `filter=sellers:{music_ed_cafe}` plus the Vinyl
   Records category, then `getItem` per item to get item specifics, the
   description, photos, condition and availability. The client is built
   (`mec/browse.py`). It becomes active when `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`
   are added to `.env`, and then the daily scheduled sync uses it.
3. **Later, only if needed (needs Edwin's consent via OAuth):** the **Trading
   API** `GetSellerList` / `GetMyeBaySelling`. This is the only official route that
   lists his ended and unsold listings, with exact quantities, in one place.

Don't use the **Sell Inventory API** for this. It only shows listings that were
created through the Inventory API, so Edwin's Seller Hub listings won't appear
(details below).

## Verified in production (2026-10-20)
- The client-credentials token and Browse search with `category_ids=176985` +
  `filter=sellers:{music_ed_cafe},buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}`
  work with no `q`. The search reported 339 items, matching the Seller Hub export
  exactly.
- **Paging gotcha:** without `sort`, offset paging returned only 333 unique ids
  for a total of 339, with ordering shifting between pages. With
  `sort=newlyListed`, all 339 came back. The client unions several sort orders,
  then does direct legacy-id lookups for any known-active id still missing, and
  only calls a snapshot complete when the union equals the reported total.
- `getItem` returned images, item specifics (`localizedAspects`), description
  and `conditionDescription` for all 339 items.

## The options

### Browse API (recommended for automatic sync)
- **Auth:** an application token from the client-credentials grant.
  `POST https://api.ebay.com/identity/v1/oauth2/token`, Basic auth with
  `client_id:client_secret`, `grant_type=client_credentials`,
  `scope=https://api.ebay.com/oauth/api_scope`. The token lasts 2 hours. No seller
  login or consent is needed. [oauth-client-credentials-grant]
- **Listing a seller's items:** `GET /buy/browse/v1/item_summary/search` with
  `filter=sellers:{music_ed_cafe}` (verified, up to 250 sellers per filter)
  [ref-buy-browse-filters]. You also need a `q`, `category_ids`, `gtin` or `epid`.
  A seller filter alone is expected to be rejected *(inferred)*. `category_ids`
  takes one ID per call. 176985 (Vinyl Records) is a leaf category, so it works
  without `q` *(inferred)*. Audio gear (for example the Gemini DJ mixer he sold)
  sits in other categories, so add their IDs to `config/site.json → browse.category_ids`.
- **Paging:** `limit` 1–200, `offset` < 10,000 and a multiple of `limit`. Results
  are capped at 10,000 items per query (verified). The client marks a snapshot
  incomplete if it ever reaches that cap.
- **Buying options:** by default only FIXED_PRICE listings are returned. The
  client asks for `buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}` explicitly
  (verified).
- **getItem** (`/buy/browse/v1/item/{v1|id|0}`, or `get_item_by_legacy_id`) returns
  `localizedAspects` (item specifics), `description`, `image` and
  `additionalImages`, `price`, `estimatedAvailabilities`, `itemWebUrl`,
  `condition`, `conditionDescription`, `itemEndDate`, `legacyItemId` and
  `categoryPath` (verified).
- **Limits:** the quantity is an *estimate*. The docs say an exact quantity is
  "impossible to return". **Active listings only:** search never returns sold or
  ended items, and getItem on ended items is undocumented. Browse defaults to
  5,000 calls/day, and the token endpoint to 1,000/day [api-call-limits]. That's
  plenty for a daily sync of a few hundred listings.
- **Header:** `X-EBAY-C-MARKETPLACE-ID: EBAY_US`.
- **Licence:** the call-limits page notes that Buy APIs "require an additional
  license" for some uses. Read the current terms when you create the keyset.
  Browsing your own active listings to show on your own site is the standard
  use case. *(partly verified)*

### Sell Inventory API (not suitable)
From eBay's knowledge base: *"The Inventory API is ONLY compatible with the
Inventory API. Meaning if an item was listed using any of the tools available via
the UI on eBay.com or was listed using the Trading API, they will not surface
using the Inventory API."* [kb-5276]

`bulkMigrateListing` can convert listings, but only fixed-price ones with a SKU
and business policies. Auctions can't be converted, and migrated listings then
**can't be edited in Seller Hub** [inventory overview]. That's a one-way trap for
a read-only sync, so don't do it.

### Trading API: GetSellerList / GetMyeBaySelling (full history; needs seller OAuth)
- Both calls are still live. The portal's deprecation metadata shows nothing for
  them (checked 2026-10-18). A community post mentions an email about
  GetMyeBaySelling being deprecated, but this isn't confirmed.
- **Auth:** a user token. Edwin signs in to eBay once through the authorization
  code grant, then the app keeps a refresh token.
- **GetSellerList:** needs a Start or End time window of **under 120 days** per
  call, `EntriesPerPage` ≤ 200, and returns description and item specifics at
  `DetailLevel=ReturnAll`. It covers active and ended listings.
  [GetSellerList reference]
- **GetMyeBaySelling:** returns ActiveList, SoldList (with DurationInDays) and
  UnsoldList.
- Trading defaults to 5,000 calls/day.

### Sell Feed API (price and quantity report)
- `LMS_ACTIVE_INVENTORY_REPORT` gives price and quantity for all of a seller's
  active listings, through a seller OAuth token with the `sell.inventory` scope.
  It probably covers Seller Hub listings because it is the legacy LMS report
  *(inferred)*. It has no specifics, descriptions or images.

### Production keyset requirement
To activate a **production** keyset, eBay requires you to either subscribe to
**Marketplace Account Deletion** notifications or apply for an exemption
[marketplace-account-deletion].
- **Subscribing:** you need an HTTPS endpoint and a verification token. eBay sends
  `GET ?challenge_code=…`, and you reply
  `{"challengeResponse": sha256(challengeCode + verificationToken + endpointUrl)}`
  in hex. It also POSTs notifications, which must be acknowledged.
- **This repo already includes the endpoint:**
  `https://musicedcafe.sebland.com/ebay/account-deletion` (in `admin/server.py`).
  Put the same token in `.env` (`EBAY_DELETION_VERIFICATION_TOKEN`) and in the
  developer portal, and set `EBAY_DELETION_ENDPOINT_URL` to the exact URL you
  register. If it moves to Edwin's own domain, update both.
- The site stores no eBay *user* data, only Edwin's own listings, so there is
  nothing to delete. The endpoint acknowledges notifications with HTTP 204.
- The exemption ("not persisting eBay data") doesn't honestly fit, because the
  catalog does persist listing data.

### Affiliate links (optional, eBay Partner Network)
The format is `https://www.ebay.com/itm/<id>?mkevt=1&mkcid=1&mkrid=711-53200-19255-0&campid=<CAMPAIGN_ID>&toolid=10001&customid=<sub-id>`
[ref-epn-link]. Set `epn_campaign_id` in `config/site.json` and every Buy on eBay
button switches to tracked links. Check the EPN terms for linking to your own
listings first.

## Field coverage: CSV exports vs API

| Field | Transaction report (have) | Seller Hub "All active listings" CSV | Browse API getItem | Trading GetSellerList |
|---|---|---|---|---|
| eBay item ID | yes | yes | yes (`legacyItemId`) | yes |
| Exact listing URL | derived from ID | derived from ID | yes (`itemWebUrl`) | yes |
| Title | yes | yes | yes | yes |
| Original description | **no** | **no** | yes | yes |
| Images | **no** | **no** | yes | yes |
| Price + currency | sale price | current/start price | yes | yes |
| Availability / quantity | sold qty | available + sold qty | estimate only | exact |
| Listing status | sold only | active only | active only | active + ended + sold |
| Artist / album | **no** (title only) | **no** (title only) | yes, if set as item specifics | yes, if set |
| Format, record size, speed | **no** (title only) | listing format only, no record format | yes, if set | yes, if set |
| Genre / style | **no** | category name only | yes, if set | yes, if set |
| Label, catalog # | **no** (title only) | **no** | yes, if set | yes, if set |
| Release year | **no** (title only) | **no** | yes, if set | yes, if set |
| Pressing year, country | **no** | **no** | only if Edwin adds them as specifics | same |
| Vinyl grade / sleeve grade | **no** (title only) | **no** | yes (`Record Grading`, `Sleeve Grading`) | yes |
| Condition + condition notes | **no** | condition only | yes (+ `conditionDescription`) | yes |
| Category | **no** | yes | yes | yes |

Column lists for the Seller Hub active-listings download come from eBay's
"Downloadable reports" guide (v1.0.0, April 2021): Item number, Title,
Variation, Custom label, Available quantity, Format, Currency, Start price, Buy It
Now price, Reserve price, Current price, Sold quantity, Views, Watchers, Bids,
Start date, End date, eBay category 1/2 name and number, Condition, eBay Product
ID (ePID), Listing site. Check one real download, because the live headers may
differ slightly. The importer matches common variants (see
`config/csv-mappings.json`).

**What the site does about the gaps today:** it fetches each item's **public
description page** (`itm.ebaydesc.com/itmdesc/<id>`, no login). Edwin's
descriptions usually spell out `Artist:`, `Title:`, `Label:`, `Catalog Number:`,
`Year:`, `Vinyl:` and `Cover:`, and the importer reads those lines with provenance
`description:<label>`. Of the 88 sold items, 57 descriptions had readable text.
The other 31 returned an empty client-rendered shell. Photos and item specifics
are only on the item page itself. From this server, eBay returned an error page or
security check for item pages after the first request, so photos are missing for
87 of 88 sold records, and a placeholder sleeve is shown. With API keys, `getItem`
fills all of this in for active listings.

## Sync safety (implemented)
- A failed, partial or refused sync changes nothing. Only `data/sync-state.json`
  records the error (`last_attempt`, `last_result`, `errors`).
- Listings are marked **ended** only when a *complete, successful* snapshot (an
  API sync, or an active-listings CSV with every row valid) no longer contains
  them. A sync that would end more than 50% of active stock at once is refused as
  a probable feed failure (override with `--force`).
- A listing is marked **sold** only when eBay says so explicitly: a sale in a
  transaction or orders report. If a later active snapshot still lists the item
  (multi-quantity), it stays active.
- Ended and sold pages stay up for SEO, with a banner and available
  alternatives (same artist, then genre, then format). They are excluded from
  Shop, search and featured.

## Sources
- [oauth-client-credentials-grant] https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html
- [ref-buy-browse-filters] https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html
- Browse OpenAPI spec: https://developer.ebay.com/api-docs/master/buy/browse/openapi/3/buy_browse_v1_oas3.json
- [api-call-limits] https://developer.ebay.com/develop/get-started/api-call-limits
- [kb-5276] https://developer.ebay.com/support/knowledge-base/5276
- [inventory overview] https://developer.ebay.com/api-docs/sell/inventory/overview.html
- [GetSellerList reference] https://developer.ebay.com/Devzone/XML/docs/Reference/eBay/GetSellerList.html
- GetMyeBaySelling: https://developer.ebay.com/Devzone/XML/docs/Reference/eBay/GetMyeBaySelling.html
- Trading OAuth: https://developer.ebay.com/api-docs/static/oauth-trad-apis.html
- Sell Feed spec: https://developer.ebay.com/api-docs/master/sell/feed/openapi/3/sell_feed_v1_oas3.json
- [marketplace-account-deletion] https://developer.ebay.com/marketplace-account-deletion
- Downloadable reports guide: https://ir.ebaystatic.com/cr/v/c1/rsc/feeds/v1/guide-downloadable-reports.pdf
- [ref-epn-link] https://developer.ebay.com/api-docs/buy/static/ref-epn-link.html

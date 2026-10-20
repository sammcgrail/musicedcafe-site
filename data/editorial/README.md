# Editorial overrides (yours; a sync never touches these)

One file per eBay item ID, named `<item id>.json`. Every key is optional:

```json
{
  "artist": "Joan Armatrading",
  "album": "Show Some Emotion",
  "display_title": "Joan Armatrading – Show Some Emotion",
  "genres": ["Rock", "Singer-Songwriter"],
  "blurb": "A short note in your own words, shown above the details.",
  "featured": true,
  "hidden": false,
  "images": ["https://i.ebayimg.com/images/g/.../s-l1600.jpg"],
  "label": "A&M", "catalog_number": "SP-4663", "release_year": "1977",
  "pressing_year": null, "country": "US",
  "media_grade": "VG+", "sleeve_grade": "VG",
  "pressing_notes": "Original inner sleeve",
  "notes": "Private note, never shown on the site"
}
```

Rules:
- Editorial wins on presentation: artist, album, title, genres, blurb, photos,
  and the record details above.
- eBay always wins on **price, currency, quantity, status (active, sold or ended)
  and the listing URL**. If you put those keys here, they are ignored, and the
  item is flagged in the review report.
- Changing `slug` changes the page URL. Avoid it once a page is live.

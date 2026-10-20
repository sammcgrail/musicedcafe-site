"""Merge source data + public captures + editorial overrides into site records.

Ownership:
  source (eBay) ALWAYS wins on: item_id, listing_url, status, price, currency,
                                 quantity, sold_at
  editorial wins on presentation: artist, album, display_title, genres, blurb,
                                 featured, hidden, extra notes, image order
Editorial files are never written by a sync; a sync never reads them.
"""
from __future__ import annotations

import re

from . import normalize as N
from .ended import SOURCE_LABEL as ENDED_LABEL, images_for as ended_images, load as load_ended
from .public_pages import PUBLIC_DIR, is_blocked
from .store import load_items
from .util import EDITORIAL_DIR, REVIEW_REPORT, SLUGS_FILE, config, load_json, slugify, write_json

PROTECTED = {"item_id", "listing_url", "status", "price", "currency", "quantity_available",
             "sold_at", "ebay_url"}
EDITORIAL_FIELDS = {"artist", "album", "display_title", "genres", "blurb", "featured", "hidden",
                    "notes", "images", "format_type", "record_size", "speed", "label",
                    "catalog_number", "release_year", "pressing_year", "country", "media_grade",
                    "sleeve_grade", "pressing_notes", "slug"}

GENRE_COLLECTIONS = [
    # (slug, label, keywords). A genre or style matches a collection when it
    # contains one of the keywords as a word ("Psychedelic Rock" -> rock).
    ("rock", "Rock", ["rock", "punk", "psych", "psychedelic", "new wave", "grunge", "metal", "surf"]),
    ("soul-rnb", "Soul & R&B", ["soul", "r&b", "rhythm & blues", "rhythm and blues", "motown", "doo wop"]),
    ("funk-disco", "Funk & Disco", ["funk", "disco", "boogie", "dance", "electronic", "house"]),
    ("jazz", "Jazz & Blues", ["jazz", "blues", "swing", "bebop", "stride", "big band"]),
    ("pop", "Pop", ["pop", "easy listening", "vocal", "lounge"]),
    ("country", "Country & Folk", ["country", "folk", "bluegrass", "americana", "rockabilly", "western"]),
    ("hip-hop", "Hip-Hop", ["hip hop", "hip-hop", "rap"]),
    ("gospel", "Gospel", ["gospel", "religious", "devotional", "christian"]),
    ("classical", "Classical", ["classical", "opera", "orchestral", "baroque"]),
    ("soundtracks", "Soundtracks & Musicals", ["soundtrack", "musical", "stage & screen", "broadway"]),
    ("world-latin", "World & Latin", ["latin", "world", "reggae", "afro", "calypso", "salsa", "polka"]),
    ("childrens", "Children's", ["children", "kids", "storybook"]),
    ("comedy", "Comedy & Novelty", ["comedy", "novelty", "spoken word"]),
]
FORMAT_COLLECTIONS = [
    ("lp", "LPs & Albums"), ("double-lp", "Double LPs & Sets"), ("7-inch", '7" Singles'),
    ("12-inch-single", '12" Singles'), ("audio-gear", "Audio Gear"),
]


def short_title(title: str) -> str:
    """'Blue Oyster Cult Some Enchanted Evening LP 1978 Columbia ...' -> text before the
    first format/year/label token. Display only; the full eBay title is kept."""
    m = N.ALBUM_STOP.search(" " + title)
    cut = (" " + title)[: m.start()].strip(" -–—,") if m else title
    return cut if len(cut) >= 6 else title


def genre_slugs(genres: list[str]) -> list[str]:
    out = []
    for g in genres:
        low = " " + g.lower().replace("-", " ") + " "
        for slug, _label, words in GENRE_COLLECTIONS:
            if slug not in out and any(re.search(r"(?<![a-z])" + re.escape(w.replace("-", " ")) + r"(?![a-z])", low)
                                       for w in words):
                out.append(slug)
    return out


def format_slugs(r: dict) -> list[str]:
    if r.get("kind") == "gear":
        return ["audio-gear"]
    t = (r.get("format_type") or "").lower()
    size = (r.get("record_size") or "").replace("”", '"')
    out = []
    if re.match(r"^[2-9]lp|^[2-9]x", t) or "box" in t:
        out += ["double-lp", "lp"]
    elif t in ("lp", "album") or (t == "" and size == '12"' and r.get("speed") == "33 RPM"):
        out.append("lp")
    elif "single" in t or t in ("45", "ep"):
        if size.startswith("12"):
            out.append("12-inch-single")
        elif size.startswith("7") or r.get("speed") == "45 RPM":
            out.append("7-inch")
    return out


def _relabel(layer: dict) -> dict:
    """Provenance for eBay's structured item specifics reads 'eBay item specifics:<Name>'."""
    return {k: (v, p.replace("aspect:", "eBay item specifics:", 1)) for k, (v, p) in layer.items()}


def _page_layers(pub: dict, title: str = "") -> tuple[dict, dict, list[str], str, bool]:
    """From a public capture: (aspect layer, description layer, images, desc text, page_says_sold)."""
    aspects, images, sold = {}, [], False
    page = pub.get("item_page")
    if page and not is_blocked(page):
        aspects = page.get("specifics") or {}
        images = page.get("images") or []
        sold = bool((page.get("body_flags") or {}).get("sold"))
    text = N.html_to_text(pub.get("description_html"))
    return _relabel(N.from_aspects(aspects)), N.from_description(text, title), images, text, sold


def _api_layers(raw: dict) -> tuple[dict, dict, list[str], str, str | None]:
    if not raw:
        return {}, {}, [], "", None
    aspects = {a.get("name"): a.get("value") for a in raw.get("localizedAspects", []) or []}
    imgs = [x.get("imageUrl") for x in [raw.get("image") or {}] + (raw.get("additionalImages") or [])
            if x.get("imageUrl")]
    text = N.html_to_text(raw.get("description"))
    cond_notes = (raw.get("conditionDescription") or "").strip()
    d = N.from_description(text + ("\n" + cond_notes.replace(". ", ".\n") if cond_notes else ""), raw.get("title"))
    if cond_notes:
        d.setdefault("condition_notes", (cond_notes, "eBay condition description"))
    return _relabel(N.from_aspects(aspects)), d, imgs, text, raw.get("condition")


def build_records() -> tuple[list[dict], dict]:
    cfg = config()
    from .util import DATA
    covers = load_json(DATA / "enrichment" / "coverart.json", {}) or {}
    photos = load_json(DATA / "enrichment" / "photos.json", {}) or {}
    ended_cache = load_ended()
    slugs = load_json(SLUGS_FILE, {}) or {}
    slugs_changed = False
    records, review = [], {}
    for iid, src in load_items().items():
        pub = load_json(PUBLIC_DIR / f"{iid}.json", {}) or {}
        ed = load_json(EDITORIAL_DIR / f"{iid}.json", {}) or {}
        api_raw = (src.get("raw") or {}).get("browse_api") or {}
        a_api, d_api, img_api, text_api, cond_api = _api_layers(api_raw)
        a_pg, d_pg, img_pg, text_pg, page_sold = _page_layers(pub, src.get("title") or "")
        title = src.get("title") or api_raw.get("title") or ""
        t_layer = N.from_title(title)
        desc_text = text_api or text_pg
        layers = [("api", a_api), ("page", a_pg), ("desc", d_api or d_pg), ("title", t_layer)]
        vals, prov, conflicts = N.combine(layers)
        mpn = vals.get("mpn")
        if mpn and mpn.strip().lower() in ("does not apply", "n/a", "na", "none", "unknown"):
            mpn = None
        if not vals.get("catalog_number") and mpn:
            vals["catalog_number"], prov["catalog_number"] = mpn, prov["mpn"]
        elif mpn and prov.get("catalog_number") == "title":
            same = re.sub(r"[^a-z0-9]", "", mpn.lower()) == re.sub(r"[^a-z0-9]", "", vals["catalog_number"].lower())
            if same:  # the title's formatting, confirmed by the MPN item specific
                prov["catalog_number"] = prov["mpn"] + " (formatted as in the title)"
            else:
                conflicts.append(f"catalog_number: {vals['catalog_number']} (title) vs {mpn} ({prov['mpn']})")
        # description "Format: LP, Stereo" -> fill format/audio only if still unknown
        fd = vals.get("format_desc")
        if fd:
            if "format_type" not in vals or prov.get("format_type") == "title":
                m = re.search(r"\b(\d?x?LP|EP|Single|45)\b", fd, re.I)
                if m:
                    vals["format_type"], prov["format_type"] = m.group(1).upper().replace("45", "Single"), prov["format_desc"]
            if "audio" not in vals:
                m = re.search(r"\b(Mono|Stereo|Quadraphonic)\b", fd, re.I)
                if m:
                    vals["audio"], prov["audio"] = m.group(1).title(), prov["format_desc"]
        kind = "gear" if t_layer.get("kind") else "record"
        # genres: explicit genre/style first, then words the seller put in the title
        genres = []
        for f in ("genre", "style"):
            if vals.get(f):
                for g in re.split(r"\s*[,/;|]\s*", vals[f]):
                    if g.strip() and g.strip() not in genres:
                        genres.append(g.strip())
        genres += [g for g in (vals.get("title_genres") or []) if g not in genres]
        page = pub.get("item_page") or {}
        condition = cond_api or (page.get("condition") if page and not is_blocked(page) else None)
        # Edwin's photos, best source first: the live listing (Browse sync), the
        # public item page, then the cached Browse lookup of the ENDED listing
        # (sold records; see mec/ended.py). Stock art / placeholder only after all.
        img_end = ended_images(ended_cache, iid) if not (img_api or img_pg) else []
        images = img_api or img_pg or img_end
        photo_source = ("eBay listing photo (Browse API)" if img_api else
                        "eBay listing photo (public item page)" if img_pg else
                        ENDED_LABEL if img_end else None)
        ebay_images = list(images)
        # ---------------- editorial overlay (presentation only)
        overrides = {k: v for k, v in ed.items() if k in EDITORIAL_FIELDS}
        ignored = sorted(k for k in ed if k in PROTECTED)
        for k, v in overrides.items():
            if k in ("genres", "images", "blurb", "featured", "hidden", "notes", "display_title", "slug"):
                continue
            vals[k], prov[k] = v, "editorial"
        if ed.get("genres"):
            genres = list(ed["genres"])
        if ed.get("images"):
            images = list(ed["images"])
            photo_source = "editorial"
        # ---------------- identity / status (source only)
        status = src.get("status") or "unknown"
        artist, album = vals.get("artist"), vals.get("album")
        display = ed.get("display_title") or (f"{artist} – {album}" if artist and album else short_title(title))
        if iid not in slugs:
            base = slugify(f"{artist} {album}" if artist and album else title)
            slugs[iid] = f"{base}-{iid}"
            slugs_changed = True
        slug = ed.get("slug") or slugs[iid]
        price = src.get("price") or {}
        listing = src.get("listing") or {}
        rec = {
            "id": iid,
            "slug": slug,
            "path": f"/records/{slug}/",
            "ebay_url": src.get("listing_url") or f"https://www.ebay.com/itm/{iid}",
            "ebay_title": title,
            "display_title": display,
            "status": status,
            "status_reason": src.get("status_reason"),
            "sold_at": src.get("sold_at"),
            "first_seen_at": src.get("first_seen_at"),
            "price": price.get("value"),
            "currency": price.get("currency") or "USD",
            "price_source": price.get("source"),
            "quantity_available": src.get("quantity_available"),
            "listing_type": listing.get("type") or "FIXED_PRICE",
            "start_date": listing.get("start_date") or (src.get("first_seen_at") or "")[:10],
            "end_date": listing.get("end_date"),
            "bids": listing.get("bids") or 0,
            "buy_it_now": listing.get("buy_it_now"),
            "kind": kind,
            "images": images,
            "condition": condition,
            "genres": genres,
            "genre_slugs": genre_slugs(genres),
            "description_text": desc_text,
            "description_paragraphs": N.description_paragraphs(desc_text),
            "blurb": ed.get("blurb"),
            "featured": ed["featured"] if isinstance(ed.get("featured"), bool) else None,  # True/False = editorial override, None = rule decides
            "hidden": bool(ed.get("hidden")),
            "editorial_notes": ed.get("notes"),
            "provenance": prov,
            "conflicts": conflicts,
            "editorial_ignored": ignored,
        }
        for f in N.FIELDS + ["media_grade_note", "sleeve_grade_note", "title_grade", "title_notes"]:
            rec.setdefault(f, vals.get(f))
        rec["format_slugs"] = format_slugs(rec)
        rec["ebay_images"] = ebay_images
        rec["photo_source"] = photo_source if images else None
        ph = photos.get(iid) or {}
        rec["local_photo"] = ph.get("files") if images and ph.get("url") == images[0] else None
        # stock cover art: a separate enrichment layer, used only when there is no
        # photo from eBay or the editorial file, and only for the same artist/album
        cov = covers.get(iid) or {}
        rec["stock_cover"] = None
        if not images and cov.get("status") == "accepted" and cov.get("artist") == rec.get("artist") \
                and cov.get("album") == rec.get("album"):
            f = cov["files"]
            rec["stock_cover"] = {"thumb": f"/img/cover/{f['thumb']}", "large": f"/img/cover/{f['large']}",
                                  "thumb_w": f["thumb_w"], "thumb_h": f["thumb_h"],
                                  "large_w": f["large_w"], "large_h": f["large_h"],
                                  "source": cov.get("source"), "mbid": cov.get("mbid"),
                                  "release_group": cov.get("release_group"), "confidence": cov.get("confidence")}
        # ---------------- review flags
        flags = []
        if kind == "record":
            for f in N.REVIEW_FIELDS:
                if not rec.get(f):
                    if f == "genre" and genres:
                        flags.append("genre: only from words in the title")
                    elif f == "release_year" and rec.get("listing_year"):
                        flags.append("release_year: only a year in the title (could be release or pressing)")
                    else:
                        flags.append(f"{f}: missing")
            for f in ("artist", "album", "catalog_number", "media_grade", "sleeve_grade", "label"):
                if prov.get(f) == "title":
                    flags.append(f"{f}: parsed from the eBay title, please confirm")
            if rec.get("title_grade") and not (rec.get("media_grade") and rec.get("sleeve_grade")):
                flags.append(f"grade '{rec['title_grade']}' in title does not say vinyl or sleeve")
        if not images:
            if rec["stock_cover"]:
                flags.append(f"images: none from eBay (stock cover shown, {rec['stock_cover']['source']} "
                             f"confidence {rec['stock_cover']['confidence']})")
            else:
                flags.append("images: none (placeholder sleeve shown)")
            if cov.get("status") in ("rejected", "vetoed"):
                flags.append(f"cover art: rejected candidate {cov.get('candidate')} ({cov.get('reason')})")
        if not desc_text:
            flags.append("description: not retrieved")
        flags += [f"conflict: {c}" for c in conflicts]
        if page_sold and status == "active":
            flags.append("item page says sold but the last sync listed it active")
        if ignored:
            flags.append(f"editorial tried to override source-owned fields (ignored): {', '.join(ignored)}")
        rec["review_flags"] = flags
        review[iid] = {"title": title, "status": status, "flags": flags,
                       "provenance": prov}
        records.append(rec)
    if slugs_changed:
        write_json(SLUGS_FILE, dict(sorted(slugs.items())))
    records.sort(key=lambda r: (r.get("sold_at") or r.get("start_date") or ""), reverse=True)
    counts = {}
    for it in review.values():
        for f in it["flags"]:
            if f.startswith("conflict: "):
                key = "conflict: " + f.split(": ")[1]
            elif ": " in f and not f.startswith(("grade ", "editorial", "item page")):
                field, what = f.split(": ", 1)
                key = f"{what.split(' (')[0]}: {field}"
            else:
                key = re.sub(r"'[^']*'", "'…'", f)
            counts[key] = counts.get(key, 0) + 1
    report = {"generated_for": len(records), "flag_counts": dict(sorted(counts.items(), key=lambda x: -x[1])),
              "items": review}
    write_json(REVIEW_REPORT, report)
    return records, report

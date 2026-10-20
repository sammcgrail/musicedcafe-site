"""Stock cover art: MusicBrainz + Cover Art Archive, iTunes Search as a fallback.

This is a SEPARATE enrichment layer (data/enrichment/coverart.json + images in
data/assets/coverart/). It never touches eBay source data or Edwin's editorial
files. Stock art is reference art, not a photo of the copy for sale, so the site
labels it as such and keeps it out of structured data and OG images.

Precedence on the site: Edwin's eBay photos > editorial images > stock art > placeholder.

Matching is deliberately strict - a wrong cover is worse than no cover:
  * artist AND title must match after normalisation (case, punctuation, "the",
    "&"/"and", accents, trailing "(Remastered)"-style notes)
  * the release-group type must fit the format: a 7"/12" single only matches a
    Single/EP release-group, an LP never matches a Single
  * catalog number (+label), vinyl format and year are bonuses that pick WHICH
    release's cover to use, never a substitute for the artist/title match
Rejected best candidates are recorded so they show up in the review report.

Etiquette: MusicBrainz 1 request / 1.1 s with a contact User-Agent; iTunes
roughly 1 request / 3.5 s. Results are cached; re-runs skip finished items.
"""
from __future__ import annotations

import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import DATA, load_json, now_iso, write_json

UA = "MusicEdCafe/1.0 ( sammcgrail@gmail.com )"
CACHE = DATA / "enrichment" / "coverart.json"
ASSETS = DATA / "assets" / "coverart"
SIZES = {"thumb": 300, "large": 500}
ACCEPT = 0.8
_last = {"mb": 0.0, "itunes": 0.0}


def _wait(kind: str, gap: float):
    dt = time.time() - _last[kind]
    if dt < gap:
        time.sleep(gap - dt)
    _last[kind] = time.time()


def _get(url: str, kind: str | None = None, gap: float = 0.0, binary=False, timeout=30):
    if kind:
        _wait(kind, gap)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*" if binary else "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(8_000_000)
                return data if binary else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    return None


def norm(s: str | None) -> str:
    s = re.sub(r"['’‘`´]", "", s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"\((?:remaster|reissue|mono|stereo|original|deluxe|expanded)[^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^\s*the\s+", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def norm_cat(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_single(rec: dict) -> bool:
    t = (rec.get("format_type") or "").lower()
    return "single" in t or t in ("ep",) or "7-inch" in rec.get("format_slugs", []) or \
        "12-inch-single" in rec.get("format_slugs", [])


def title_variants(rec: dict) -> list[str]:
    album = rec.get("album") or ""
    out = [album]
    if " / " in album:
        out.append(album.split(" / ")[0])
    base = re.sub(r"\s*\([^)]*\)\s*$", "", album).strip()
    if base and base not in out:
        out.append(base)
    return [x for x in out if x]


def credit_name(ac: list) -> str:
    return "".join((a.get("name") or "") + (a.get("joinphrase") or "") for a in ac or [])


GENERIC = re.compile(r"^(?:(?:the )?(?:very )?(?:best|greatest|golden)(?: of)?(?: hits)?|greatest hits|hits|gold|"
                     r"live|in concert|anthology|collection|the collection|(?:a )?christmas album|"
                     r"(?:20|16|12) (?:greatest|golden) hits)(?: vol(?:ume)? \\d+| \\d+| [ivx]+)?$")


def is_generic(title: str) -> bool:
    return bool(GENERIC.match(norm(title)))


def score_release(rec: dict, rel: dict) -> tuple[float, list[str]]:
    why = []
    want_a = norm(rec.get("artist"))
    got_a = norm(credit_name(rel.get("artist-credit")))
    if not want_a or want_a != got_a:
        return 0.0, [f"artist '{credit_name(rel.get('artist-credit'))}' != '{rec.get('artist')}'"]
    titles = {norm(t) for t in title_variants(rec)}
    got_t = norm(rel.get("title"))
    if got_t not in titles:
        return 0.4, [f"title '{rel.get('title')}' != '{rec.get('album')}'"]
    score = 0.8
    why.append("artist+title")
    ptype = ((rel.get("release-group") or {}).get("primary-type") or "").lower()
    if is_single(rec):
        if ptype and ptype not in ("single", "ep"):
            return 0.5, [f"release-group type {ptype} for a single"]
    elif ptype in ("single",):
        return 0.5, ["single release-group for an LP"]
    cats = [norm_cat(li.get("catalog-number")) for li in rel.get("label-info") or []]
    if rec.get("catalog_number") and norm_cat(rec["catalog_number"]) in cats:
        score += 0.15
        why.append("catalog #")
    fmts = " ".join((m.get("format") or "") for m in rel.get("media") or []).lower()
    if "vinyl" in fmts:
        score += 0.03
        size = (rec.get("record_size") or "").replace('"', "")
        if (is_single(rec) and '7"' in fmts) or (not is_single(rec) and '12"' in fmts) or (size and size in fmts):
            score += 0.02
            why.append("format")
    year = rec.get("release_year") or rec.get("listing_year")
    if year and (rel.get("date") or "").startswith(year):
        score += 0.03
        why.append("year")
    if is_generic(rec.get("album") or "") and "catalog #" not in why:
        return 0.6, [f"generic title '{rec.get('album')}' needs a catalog-number match"]
    return min(score, 1.0), why


def mb_search(rec: dict) -> list[dict]:
    a = rec["artist"].replace('"', "")
    results = []
    for t in title_variants(rec)[:2]:
        q = f'release:"{t.replace(chr(34), "")}" AND artist:"{a}"'
        if is_single(rec):
            q += " AND (primarytype:single OR primarytype:ep)"
        url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 25})
        res = _get(url, "mb", 1.1) or {}
        results += res.get("releases") or []
        if results:
            break
    return results


def caa_image(mbid: str, rgid: str | None, prefer_group: bool = False) -> tuple[bytes | None, str | None]:
    urls = [f"https://coverartarchive.org/release/{mbid}/front-500"]
    if rgid:
        g = f"https://coverartarchive.org/release-group/{rgid}/front-500"
        urls = [g] + urls if prefer_group else urls + [g]
    for url in urls:
        try:
            data = _get(url, None, binary=True, timeout=40)
        except Exception:
            data = None
        if data and len(data) > 2000:
            return data, url
        time.sleep(0.5)
    return None, None


def itunes_match(rec: dict) -> dict | None:
    term = f"{rec['artist']} {title_variants(rec)[-1]}"
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "album", "limit": 15, "country": "US"})
    res = _get(url, "itunes", 3.5) or {}
    titles = {norm(t) for t in title_variants(rec)}
    best = None
    for it in res.get("results") or []:
        name = re.sub(r"\s+-\s+(single|ep)$", "", it.get("collectionName") or "", flags=re.I)
        is_sing = bool(re.search(r"\s-\s(single|ep)$", it.get("collectionName") or "", re.I))
        if norm(it.get("artistName")) != norm(rec["artist"]) or norm(name) not in titles:
            continue
        if is_single(rec) != is_sing and is_single(rec):
            continue  # a single must match an iTunes single, not the album
        if not is_single(rec) and is_sing:
            continue
        best = it
        break
    return best


def _group_front_is_vinyl(rgid: str) -> bool:
    try:
        meta = _get(f"https://coverartarchive.org/release-group/{rgid}", None) or {}
        src_rel = (meta.get("release") or "").rstrip("/").split("/")[-1]
        rel = _get(f"https://musicbrainz.org/ws/2/release/{src_rel}?inc=media&fmt=json", "mb", 1.1) or {}
    except Exception:
        return False
    return "vinyl" in " ".join((m.get("format") or "") for m in rel.get("media") or []).lower()


def to_webp(data: bytes, key: str) -> dict:
    from PIL import Image
    ASSETS.mkdir(parents=True, exist_ok=True)
    im = Image.open(io.BytesIO(data)).convert("RGB")
    out = {}
    for name, px in SIZES.items():
        r = im.copy()
        r.thumbnail((px, px), Image.LANCZOS)
        fn = f"{key}-{px}.webp"
        r.save(ASSETS / fn, "WEBP", quality=72 if px < 400 else 76, method=6)
        out[name] = fn
        out[f"{name}_w"], out[f"{name}_h"] = r.size
    return out


def run(records: list[dict], log=print, refresh=False, limit: int | None = None, retry_misses=False,
        revinyl=False) -> dict:
    cache = load_json(CACHE, {}) or {}
    stats = {"accepted_caa": 0, "accepted_itunes": 0, "rejected": 0, "no_match": 0, "skipped_no_artist_album": 0,
             "cached": 0}
    todo = [r for r in records if not r["images"]]
    if limit:
        todo = todo[:limit]
    for n, rec in enumerate(todo, 1):
        iid = rec["id"]
        prev = cache.get(iid)
        if prev and not refresh and prev.get("artist") == rec.get("artist") and prev.get("album") == rec.get("album") \
                and not (retry_misses and prev.get("status") in ("rejected", "no_match")) \
                and not (revinyl and prev.get("status") == "accepted" and prev.get("source") == "caa"
                         and (prev.get("confidence") or 0) < 0.95):
            stats["cached"] += 1
            continue
        entry = {"artist": rec.get("artist"), "album": rec.get("album"), "fetched_at": now_iso()}
        if not rec.get("artist") or not rec.get("album") or rec.get("kind") != "record":
            entry["status"] = "skipped"
            entry["reason"] = "artist or album unknown"
            stats["skipped_no_artist_album"] += 1
            cache[iid] = entry
            continue
        try:
            rels = mb_search(rec)
        except Exception as e:
            log(f"[{n}] {iid}: MusicBrainz error {e}; will retry next run")
            continue
        scored = sorted(((score_release(rec, r), r) for r in rels), key=lambda x: -x[0][0])
        good = [(s, r) for (s, why), r in [((x[0][0], x[0][1]), x[1]) for x in scored] if s >= ACCEPT]
        got = None
        # Which cover? A catalog-number match is Edwin's exact release; otherwise prefer
        # a VINYL release's own front (its art is the LP/45 sleeve), then the
        # release-group's canonical front. CD-only editions' covers are never used
        # directly, because obi strips and CD reissue art imply the wrong edition.
        def is_vinyl(r):
            return "vinyl" in " ".join((m.get("format") or "") for m in r.get("media") or []).lower()
        tried = set()
        for s, r in [x for x in good if is_vinyl(x[1])][:4]:
            data, src = caa_image(r["id"], None)
            tried.add(r["id"])
            if data:
                got = ("caa", s, r, data, src)
                break
        if not got and good:
            s, r = good[0]
            rgid = (r.get("release-group") or {}).get("id")
            if rgid:
                try:
                    data = _get(f"https://coverartarchive.org/release-group/{rgid}/front-500", None, binary=True, timeout=40)
                except Exception:
                    data = None
                if data and len(data) > 2000 and _group_front_is_vinyl(rgid):
                    got = ("caa", s, r, data, f"https://coverartarchive.org/release-group/{rgid}/front-500")
        if not got:
            try:
                it = itunes_match(rec)
            except Exception as e:
                it = None
                log(f"[{n}] {iid}: iTunes error {e}")
            if it and it.get("artworkUrl100"):
                src = it["artworkUrl100"].replace("100x100bb", "600x600bb")
                try:
                    data = _get(src, None, binary=True)
                except Exception:
                    data = None
                if data:
                    got = ("itunes", 0.85, {"id": None, "title": it.get("collectionName"),
                                            "artist": it.get("artistName"), "itunes_id": it.get("collectionId")}, data, src)
        if got:
            source, s, r, data, src = got
            key = (r.get("id") or f"itunes-{r.get('itunes_id')}")
            files = to_webp(data, key)
            entry.update({"status": "accepted", "source": source, "confidence": round(s, 2), "source_url": src,
                          "mbid": r.get("id"), "release_group": (r.get("release-group") or {}).get("id"),
                          "matched_title": r.get("title"), "matched_artist": credit_name(r.get("artist-credit")) or r.get("artist"),
                          "matched_date": r.get("date"), "files": files})
            stats["accepted_" + source] += 1
        else:
            best = scored[0] if scored else None
            if best and best[0][0] >= ACCEPT:
                entry.update({"status": "no_image", "reason": "matched on MusicBrainz but no cover in CAA or iTunes",
                              "candidate": f"{credit_name(best[1].get('artist-credit'))} – {best[1].get('title')} ({best[1]['id']})"})
                stats["no_match"] += 1
            elif best:
                entry.update({"status": "rejected", "confidence": round(best[0][0], 2),
                              "reason": "; ".join(best[0][1]),
                              "candidate": f"{credit_name(best[1].get('artist-credit'))} – {best[1].get('title')} "
                                           f"({(best[1].get('release-group') or {}).get('primary-type') or '?'}, {best[1]['id']})"})
                stats["rejected"] += 1
            else:
                entry.update({"status": "no_match", "reason": "no MusicBrainz or iTunes result"})
                stats["no_match"] += 1
        cache[iid] = entry
        if n % 10 == 0:
            write_json(CACHE, cache)
        log(f"[{n}/{len(todo)}] {iid} {rec.get('artist')} – {rec.get('album')}: {entry['status']} "
            f"{entry.get('source', '')} {entry.get('confidence', '')}")
    write_json(CACHE, cache)
    return stats


def prefer_group_covers(log=print) -> dict:
    """Re-fetch accepted CAA covers from the release-group's canonical front when
    the match had no catalog-number confirmation (so the edition is unknown)."""
    cache = load_json(CACHE, {}) or {}
    n = changed = 0
    for iid, e in cache.items():
        if e.get("status") != "accepted" or e.get("source") != "caa" or not e.get("release_group"):
            continue
        if (e.get("confidence") or 0) >= 0.95 or "/release-group/" in (e.get("source_url") or ""):
            continue
        n += 1
        url = f"https://coverartarchive.org/release-group/{e['release_group']}/front-500"
        try:
            data = _get(url, None, binary=True, timeout=40)
        except Exception:
            data = None
        if data and len(data) > 2000:
            e["files"] = to_webp(data, f"rg-{e['release_group']}")
            e["source_url"], e["cover_from"] = url, "release-group front (edition unconfirmed)"
            changed += 1
        time.sleep(0.5)
    write_json(CACHE, cache)
    return {"checked": n, "switched_to_release_group_front": changed}


def vet_group_fronts(log=print) -> dict:
    """A release-group front can come from any edition. Look up which release
    the image belongs to; if that release is not vinyl (e.g. a CD reissue with an
    obi strip), drop the cover - it would suggest the wrong edition."""
    cache = load_json(CACHE, {}) or {}
    checked = dropped = 0
    for iid, e in cache.items():
        if e.get("status") != "accepted" or "/release-group/" not in (e.get("source_url") or ""):
            continue
        checked += 1
        try:
            meta = _get(f"https://coverartarchive.org/release-group/{e['release_group']}", None) or {}
            src_rel = (meta.get("release") or "").rstrip("/").split("/")[-1]
            rel = _get(f"https://musicbrainz.org/ws/2/release/{src_rel}?inc=media&fmt=json", "mb", 1.1) or {}
        except Exception as ex:
            log(f"{iid}: vet error {ex}")
            continue
        fmts = " ".join((m.get("format") or "") for m in rel.get("media") or [])
        e["art_release"], e["art_release_format"] = src_rel, fmts
        if "vinyl" not in fmts.lower():
            e["status"] = "rejected"
            e["reason"] = f"only cover available is from a non-vinyl edition ({fmts or 'unknown format'})"
            e["candidate"] = f"{e.get('matched_artist')} – {e.get('matched_title')} (art from release {src_rel})"
            e.pop("files", None)
            dropped += 1
        log(f"{iid}: art from {src_rel} [{fmts}] -> {e['status']}")
    write_json(CACHE, cache)
    return {"checked_group_fronts": checked, "dropped_non_vinyl": dropped}


def drop_generic(log=print) -> dict:
    cache = load_json(CACHE, {}) or {}
    n = 0
    for iid, e in cache.items():
        if e.get("status") == "accepted" and is_generic(e.get("album") or "") and (e.get("confidence") or 0) < 0.95:
            e["status"], e["reason"] = "rejected", f"generic title '{e.get('album')}' needs a catalog-number match"
            e["candidate"] = f"{e.get('matched_artist')} – {e.get('matched_title')} ({e.get('mbid') or e.get('source')})"
            e.pop("files", None)
            n += 1
            log(f"{iid}: {e.get('artist')} – {e.get('album')} dropped (generic)")
    write_json(CACHE, cache)
    return {"dropped_generic": n}

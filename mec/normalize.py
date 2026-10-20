"""Turn raw eBay data into catalog fields, recording where every value came from.

Sources, highest priority first:
  aspect:<Name>        item specifics from the Browse API (localizedAspects) or the
                       public item page ("Item specifics" table)
  description:<Label>  an explicit "Label: value" line in Edwin's own listing text
  title                parsed from the eBay listing title (heuristic; always flagged)

Rules:
  * never infer anything from artwork or from "what this album usually is"
  * a value we cannot read explicitly stays None and is flagged for review
  * a year in a TITLE is stored as listing_year ("year as listed"), because a
    title cannot tell a release year from a reissue/pressing year
  * a bare grade in a title ("... VG+") is not assigned to vinyl or sleeve
"""
from __future__ import annotations

import html as htmllib
import re

FIELDS = [
    "artist", "album", "format", "format_type", "record_size", "speed", "genre", "style",
    "label", "catalog_number", "release_year", "listing_year", "pressing_year", "country",
    "media_grade", "sleeve_grade", "condition", "condition_notes", "pressing_notes",
    "audio", "vinyl_color",
]

# fields whose absence we report for every record
REVIEW_FIELDS = ["artist", "album", "format_type", "record_size", "speed", "genre", "label",
                 "catalog_number", "release_year", "country", "media_grade", "sleeve_grade"]

ASPECTS = {
    "artist": ["Artist"],
    "album": ["Release Title", "Album Name", "Album"],
    "format": ["Format"],
    "format_type": ["Type"],
    "record_size": ["Record Size"],
    "speed": ["Speed"],
    "genre": ["Genre"],
    "style": ["Style", "Sub-Genre"],
    "label": ["Record Label", "Label"],
    "catalog_number": ["Catalog Number", "Catalogue Number", "Catalog #"],
    "mpn": ["MPN"],
    "release_year": ["Release Year"],
    "pressing_year": ["Pressing Year", "Year of Pressing", "Year Pressed"],
    "country": ["Country/Region of Manufacture", "Country of Manufacture", "Country of Origin"],
    "media_grade": ["Record Grading", "Vinyl Grading", "Media Grading", "Vinyl Grade", "Record Grade"],
    "sleeve_grade": ["Sleeve Grading", "Sleeve Grade", "Cover Grading", "Cover Grade", "Jacket Grade",
                     "Inlay Condition", "Sleeve Condition", "Cover Condition"],
    "condition_notes": ["Seller Notes"],
    "pressing_notes": ["Edition", "Features"],
    "audio": ["Number of Audio Channels"],
    "vinyl_color": ["Color", "Vinyl Color"],
}

DESC_LABELS = {
    "artist": ["artist"],
    "album": ["title", "album", "release title"],
    "label": ["label", "record label"],
    "catalog_number": ["catalog number", "catalog #", "catalog no", "catalog", "cat #", "cat no", "catalogue number"],
    "release_year": ["year", "release year", "released", "original release"],
    "pressing_year": ["pressing year", "year of pressing", "pressed"],
    "country": ["country", "country of manufacture", "country/region of manufacture", "made in"],
    "media_grade": ["vinyl", "media", "record", "disc", "vinyl grade", "record grade", "vinyl condition", "record condition"],
    "sleeve_grade": ["cover", "sleeve", "jacket", "cover grade", "sleeve grade", "jacket condition", "sleeve condition", "picture sleeve"],
    "pressing_notes": ["pressing", "edition"],
    "format_desc": ["format"],
    "speed": ["speed"],
    "genre": ["genre", "genres"],
    "style": ["style", "styles"],
    "record_size": ["record size", "size"],
    "vinyl_color": ["vinyl color", "color"],
}

GRADE_WORDS = [
    (r"\bnear\s*mint\b", "NM"), (r"\bmint\b", "M"), (r"\bexcellent\b", "EX"),
    (r"\bvery\s*good\s*plus\s*plus\b", "VG++"), (r"\bvery\s*good\s*plus\b", "VG+"),
    (r"\bvery\s*good\b", "VG"), (r"\bgood\s*plus\b", "G+"), (r"\bgood\b", "G"),
    (r"\bfair\b", "F"), (r"\bpoor\b", "P"),
]
GRADE_TOKEN = r"(?:M-|M|NM-|NM\+?|EX\+?|VG\+\+|VG\+|VG-|VG|G\+|G|F|P)"
GRADE_ORDER = ["M", "NM+", "NM", "NM-", "M-", "EX+", "EX", "VG++", "VG+", "VG", "VG-", "G+", "G", "F", "P"]

KNOWN_LABELS = [
    "Deutsche Grammophon", "RCA Victor", "Warner Bros", "Philadelphia International", "Sun Records",
    "Grand Award", "Blue Sky", "Blue Note", "T-Neck", "Pan Am", "A&M", "ABC", "Alpine", "Apple",
    "Arista", "Asylum", "ATCO", "Atlantic", "Capitol", "Casablanca", "Chess", "Columbia", "Cotillion",
    "Decca", "Dunhill", "Elektra", "EMI", "Epic", "Gordy", "Impulse", "Island", "Jive", "London",
    "MCA", "Mercury", "Motown", "Polydor", "Prestige", "RCA", "Reprise", "Republic", "Riva", "RSO",
    "Sire", "Specialty", "Starline", "Stax", "Tamla", "Verve",
]

TITLE_GENRES = [
    ("classic rock", "Classic Rock"), ("psych rock", "Psych Rock"), ("hard rock", "Hard Rock"),
    ("rock & roll", "Rock & Roll"), ("rock and roll", "Rock & Roll"), ("afro funk", "Afro Funk"),
    ("classic pop", "Pop"), ("r&b", "R&B"), ("soul", "Soul"), ("funk", "Funk"), ("disco", "Disco"),
    ("boogie", "Boogie"), ("gospel", "Gospel"), ("jazz", "Jazz"), ("country", "Country"),
    ("comedy", "Comedy"), ("rap", "Rap"), ("soundtrack", "Soundtrack"), ("rock", "Rock"),
    ("pop", "Pop"),
]

GEAR_WORDS = r"\b(mixer|turntable|receiver|amplifier|speaker|cartridge|stylus|headphones|cassette deck|tape deck)\b"


# --------------------------------------------------------------------------- helpers

def clean(v):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", str(v)).strip(" \t\n-–—:;,")
    return v or None


def norm_grade(text: str | None) -> str | None:
    """'Very Good Plus (VG+)' -> 'VG+'; 'VG+ (clean, glossy)' -> 'VG+'; prose -> None."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"\((" + GRADE_TOKEN + r")(?:\s+or\s+" + GRADE_TOKEN + r")?\)", t)
    if m:
        return m.group(1)
    m = re.match(r"^\s*(?:(?:strong|solid|high|low|light|clean|nice|easy|true|very nice)\s+)?(" + GRADE_TOKEN + r")(?![A-Za-z])", t, re.I)
    if m:
        return m.group(1).upper()
    low = t.lower()
    for pat, g in GRADE_WORDS:
        if re.match(r"^\s*" + pat, low):
            return g
    return None


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript|head)\b.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|ul|ol|section|table|dd|dt)>", "\n", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            blank = True
            continue
        if blank and out:
            out.append("")
        out.append(ln)
        blank = False
    text = "\n".join(out).strip()
    if text.startswith("eBay\n"):
        text = text[5:].lstrip()
    return text


def description_paragraphs(text: str) -> list[str]:
    paras, cur = [], []
    for ln in (text or "").split("\n"):
        if not ln.strip():
            if cur:
                paras.append(" ".join(cur) if len(cur) == 1 else "\n".join(cur))
                cur = []
        else:
            cur.append(ln.strip())
    if cur:
        paras.append("\n".join(cur))
    return paras


# --------------------------------------------------------------------------- extractors

def from_aspects(aspects: dict) -> dict:
    """aspects: {name: value}. Returns {field: (value, provenance)}."""
    if not aspects:
        return {}
    lower = {k.strip().lower(): (k, v) for k, v in aspects.items() if v not in (None, "")}
    out = {}
    for field, names in ASPECTS.items():
        for name in names:
            hit = lower.get(name.lower())
            if not hit:
                continue
            k, v = hit
            v = clean(v)
            if v is None:
                continue
            if field in ("media_grade", "sleeve_grade"):
                g = norm_grade(v)
                if not g:
                    continue
                v = g
            if field == "condition_notes":
                v = dedupe_truncated(v)
            if field in ("release_year", "pressing_year"):
                m = re.search(r"\b(19\d\d|20\d\d)\b", v)
                if not m:
                    continue
                v = m.group(1)
            out[field] = (v, f"aspect:{k}")
            break
    return out


def dedupe_truncated(v: str) -> str:
    """eBay renders seller notes twice: a truncated '... ' teaser and the full text."""
    v = v.strip("“”\" ")
    if " ... " in v:
        head, tail = v.split(" ... ", 1)
        tail = tail.strip("“”\" ")
        if tail.startswith(head[:30]):
            return tail
    return v


def _loose(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def from_description(text: str, title: str | None = None) -> dict:
    out = {}
    if not text:
        return out
    label_to_field = {}
    for field, labels in DESC_LABELS.items():
        for lab in labels:
            label_to_field[lab] = field
    # Edwin's descriptions open with an "Artist – Title" heading line
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    if first and len(first) < 140 and ":" not in first:
        parts = re.split(r"\s+[–—-]\s+", first, maxsplit=1)
        ok = len(parts) == 2 and all(2 <= len(x.strip()) <= 90 for x in parts) \
            and not re.search(r"\s[–—-]\s", parts[1]) and not parts[1].rstrip().endswith(".") \
            and not re.search(r"\b(pressing|original|vintage|reissue|copy)\b", parts[0], re.I)
        # the heading must agree with the listing title, or it is prose, not a heading
        if ok and title:
            ok = _loose(parts[0]) in _loose(title) and _loose(parts[1]).split(" ")[0] in _loose(title)
        if ok:
            out["artist"] = (clean(parts[0]), "description:heading")
            out["album"] = (clean(re.sub(r"[“”\"]", "", parts[1])), "description:heading")
    # split into "Label: value" candidates on line breaks
    for ln in text.split("\n"):
        m = re.match(r"^\s*([A-Za-z][A-Za-z #/.]{1,28}?)\s*[:\-–]\s+(.{1,200})$", ln)
        if not m:
            continue
        lab, val = m.group(1).strip().lower().rstrip("."), m.group(2).strip()
        field = label_to_field.get(lab)
        if not field or (field in out and not out[field][1].endswith(":heading")):
            continue
        if field in ("media_grade", "sleeve_grade"):
            g = norm_grade(val)
            if not g:
                continue  # e.g. "Sleeve: Generic white paper sleeve" is not a grade
            out[field] = (g, f"description:{m.group(1).strip()}")
            note_field = "media_grade_note" if field == "media_grade" else "sleeve_grade_note"
            rest = re.sub(r"^\s*(?:\w+\s+)?" + re.escape(g) + r"\s*", "", val, flags=re.I).strip(" ()-–.,;:")
            if len(rest) > 2:
                out[note_field] = (rest, f"description:{m.group(1).strip()}")
            continue
        if field in ("release_year", "pressing_year"):
            y = re.fullmatch(r"\s*(19\d\d|20\d\d)\s*", val)
            if not y:
                continue
            val = y.group(1)
        if field == "country" and len(val) > 40:
            continue
        out[field] = (clean(val), f"description:{m.group(1).strip()}")
    return out


SEP = re.compile(r"\s+[-–—]+\s*|\s*[-–—]+\s+")
ALBUM_STOP = re.compile(
    r"""\s(?:\(?\d?\s?LPs?\b|\d?xLP\b|EP\b|\(?(?:7|10|12)\s?["”″]|\d{2}\s?RPM\b|45\b|33\b|Vinyl\b|Record\b|Mono\b|Stereo\b|
        180g\b|(?:19|20)\d\d\b|VG|NM\b|EX\b|Original\b|Sealed\b|Promo\b|PROMO\b|Gatefold\b|w/|\+|Complete\b|"""
    + "|".join(re.escape(lbl) + r"\b" for lbl in KNOWN_LABELS) + r")""", re.X)

CATNO = re.compile(
    r"""(?<![\w/-])(
        (?:[A-Z]{1,5}\d?(?:-\d)?[-\s])\d{2,6}(?:-\d{1,4})?(?:[A-Z]\d)?(?:/\d{3,5})?   # SP-4663, JC 35563, RS-2-4001, SPRO-9330/9331
      | \d{1,2}[A-Z]?-\d{3,6}                                                         # 4-45894, 5E-507, 34-03759
      | [A-Z]{1,3}\d-\d{3,4}[A-Z]?\d?                                                # AL8-8212, M7-912R1, ALB6-8313
    )(?![\w])""", re.X)
NOT_CATNO_PREFIX = re.compile(r"^(?:(?:LP|EP)[-\s](?:19|20)\d\d$|(?:LP|EP)\s|(?:VG|NM|EX|RPM|BT|MXR|VOL|NO)[-\s])", re.I)


def from_title(title: str) -> dict:
    out = {}
    t = (title or "").strip()
    if not t:
        return out
    prov = "title"
    if re.search(GEAR_WORDS, t, re.I):
        out["kind"] = ("gear", prov)
        return out
    parts = [p for p in SEP.split(t) if p.strip()]
    if len(parts) >= 2:
        artist = clean(parts[0])
        album_raw = parts[1]
        m = ALBUM_STOP.search(" " + album_raw)
        album = clean((" " + album_raw)[: m.start()] if m else album_raw)
        if album and album.count("(") > album.count(")"):
            album = clean(album[: album.rfind("(")])
        if album and album.lower() in ("self titled", "self-titled"):
            album = None
        if artist and not re.search(r"\b(LP|Vinyl|Record|45|RPM)\b", artist):
            out["artist"] = (artist, prov)
        if album:
            out["album"] = (album, prov)
    # format
    m = re.search(r"(?<![\w.+])(\d)(?:x)?LPs?\b", t, re.I)
    if m and m.group(1) != "1":
        out["format_type"] = (f"{m.group(1)}LP", prov)
    elif re.search(r"(?:12|10)\s?[\"”″]\s?(?:Vinyl\s)?Single", t, re.I):
        out["format_type"] = ("Single", prov)
    elif re.search(r"\bLP\b", t):
        out["format_type"] = ("LP", prov)
    elif re.search(r"\b7\s?[\"”″]|\b45\s?RPM\b|\b45\b", t, re.I):
        out["format_type"] = ("Single", prov)
    elif re.search(r"\bEP\b", t):
        out["format_type"] = ("EP", prov)
    m = re.search(r"\b(7|10|12)\s?[\"”″]", t)
    if m:
        out["record_size"] = (f'{m.group(1)}"', prov)
    m = re.search(r"\b(33|45|78)\s?RPM\b", t, re.I) or re.search(r"(?:7\s?[\"”″]\s*|\s)(45)(?:\s|$)", t)
    if m:
        out["speed"] = (f"{m.group(1)} RPM", prov)
    if re.search(r"\bMono\b", t, re.I):
        out["audio"] = ("Mono", prov)
    elif re.search(r"\bStereo\b", t, re.I):
        out["audio"] = ("Stereo", prov)
    years = [y for y in re.findall(r"(?<![\d-])((?:19|20)\d\d)(?![\d-])", t)]
    if len(set(years)) == 1:
        out["listing_year"] = (years[0], prov)
    # label: the earliest known label name in the title (longest wins a tie)
    hits = []
    for lbl in KNOWN_LABELS:
        mm = re.search(r"(?<![\w&])" + re.escape(lbl) + r"(?![\w&])", t)
        if mm:
            hits.append((mm.start(), -len(lbl), lbl))
    if hits:
        out["label"] = (sorted(hits)[0][2], prov)
    for m in CATNO.finditer(t):
        c = m.group(1).strip()
        if NOT_CATNO_PREFIX.match(c) or re.fullmatch(r"(19|20)\d\d-(19|20)\d\d", c):
            continue
        for lbl in KNOWN_LABELS:
            if c.upper().startswith(lbl.upper() + " "):
                c = c[len(lbl) + 1:]
        if re.fullmatch(r"[A-Z]{1,5}\s(19|20)\d\d", c):  # "LP 1977"-style word + year
            continue
        out["catalog_number"] = (c, prov)
        break
    # grades: only when the title says what the grade is for
    m = re.search(r"\b(" + GRADE_TOKEN + r")\s*/\s*(" + GRADE_TOKEN + r")(?![\w+])", t)
    if m:
        out["media_grade"], out["sleeve_grade"] = (m.group(1), prov), (m.group(2), prov)
    else:
        mv = re.search(r"(?<![\w])(" + GRADE_TOKEN + r")\s+(?:Vinyl|Record|Disc|Media)\b", t)
        ms = re.search(r"(?<![\w])(" + GRADE_TOKEN + r")\s+(?:Cover|Sleeve|Jacket)\b", t)
        if mv:
            out["media_grade"] = (mv.group(1), prov)
        if ms:
            out["sleeve_grade"] = (ms.group(1), prov)
        if not mv and not ms:
            bare = re.findall(r"(?<![\w])(" + GRADE_TOKEN + r")(?=\s|$)", t)
            bare = [b for b in bare if b not in ("M", "G", "F", "P")]
            if bare:
                out["title_grade"] = (" ".join(dict.fromkeys(bare)), prov)
    genres = []
    low = t.lower()
    for kw, g in TITLE_GENRES:
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", low) and g not in genres:
            if g == "Rock" and any(x in genres for x in ("Classic Rock", "Psych Rock", "Hard Rock", "Rock & Roll")):
                continue
            genres.append(g)
            low = low.replace(kw, " ")
    if genres:
        out["title_genres"] = (genres, prov)
    notes = []
    for kw in ("Original Press", "Original Pressing", "Promo", "PROMO", "Gatefold", "Sealed", "In Shrink",
               "Hype Sticker", "Picture Sleeve", "Numbered", "Limited Edition", "180g", "Colored Vinyl",
               "Yellow Vinyl", "Orange Colored Vinyl", "Poster", "Booklet", "Inner Sleeve", "Generic Sleeve"):
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", t):
            notes.append(kw)
    if notes:
        out["title_notes"] = (", ".join(dict.fromkeys(n.title() if n.isupper() else n for n in notes)), prov)
    return out


# Grades: the seller's own per-copy description beats an item-specifics dropdown
# (dropdowns get copied between listings); disagreements are flagged either way.
FIELD_ORDER = {"media_grade": ["desc", "api", "page", "title"],
               "sleeve_grade": ["desc", "api", "page", "title"]}


def combine(layers: list[tuple[str, dict]]) -> tuple[dict, dict, list[str]]:
    """layers: [(layer_name, {field: (value, provenance)})] highest priority first.
    Returns (values, provenance, conflicts)."""
    values, prov, conflicts = {}, {}, []
    by_name = dict(layers)
    fields = []
    for _n, layer in layers:
        fields += [f for f in layer if f not in fields]
    for field in fields:
        order = FIELD_ORDER.get(field)
        seq = [(n, by_name[n]) for n in order if n in by_name] if order else layers
        for _name, layer in seq:
            if field not in layer:
                continue
            val, p = layer[field]
            if val in (None, "", []):
                continue
            if field not in values:
                values[field], prov[field] = val, p
            elif field in ("media_grade", "sleeve_grade", "catalog_number", "release_year", "label"):
                a = re.sub(r"[\s\-.]", "", str(values[field])).lower()
                b = re.sub(r"[\s\-.]", "", str(val)).lower()
                if a != b and not (field == "label" and (a in b or b in a)) and \
                        not (field == "catalog_number" and (a in b or b in a)):
                    conflicts.append(f"{field}: {values[field]} ({prov[field]}) vs {val} ({p})")
    return values, prov, conflicts

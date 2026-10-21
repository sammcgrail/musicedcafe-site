"""Static site generator: data -> public/live/ (pre-rendered HTML, no runtime server).

Build is atomic: everything is written to public/.build-<ts>/ and then swapped
into public/live/ with renames, so a failed build never leaves a half site.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import merge
from .store import read_state
from .util import PUBLIC, ROOT, config, load_json

E = html.escape
STATIC = ROOT / "assets" / "static"
CONTENT = ROOT / "content"
NOW = time.time()


def esc(v) -> str:
    return E("" if v is None else str(v), quote=True)


def money(value, currency="USD") -> str:
    if value in (None, ""):
        return ""
    try:
        v = float(value)
    except ValueError:
        return esc(value)
    sym = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€"}.get(currency, currency + " ")
    return f"{sym}{v:,.2f}"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return esc(iso)
    return d.strftime("%B %-d, %Y")


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return esc(iso)
    return d.strftime("%B %-d, %Y at %H:%M UTC")


def ebay_img(url: str, size: int) -> str:
    return re.sub(r"/s-l\d+\.(jpg|jpeg|png|webp)$", f"/s-l{size}.webp", url)


def jsonld(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return '<script type="application/ld+json">' + s.replace("</", "<\\/") + "</script>"


ARCHIVE_STATUSES = ("sold", "ended", "unavailable")


def archive_key(r: dict) -> str:
    """Recently Sold order (newest first): the sale date, or for a record with no
    confirmed sale ("Ended" stamp) the date its status changed, then the
    status-change timestamp. Also the cards' data-date, which site.js sorts by."""
    changed = r.get("status_changed_at") or ""
    return f"{(r.get('sold_at') or changed or r.get('start_date') or '')[:10]} {changed}"


class Site:
    def __init__(self, out: Path):
        self.out = out
        self.cfg = config()
        self.base = self.cfg["base_url"].rstrip("/")
        self.records, self.report = merge.build_records()
        self.visible = [r for r in self.records if not r["hidden"]]
        self.active = [r for r in self.visible if r["status"] == "active"]
        self.archive = sorted((r for r in self.visible if r["status"] in ARCHIVE_STATUSES),
                              key=archive_key, reverse=True)
        self.sold = [r for r in self.archive if r["status"] == "sold"]
        self.state = read_state()
        self.urls: list[tuple[str, str | None]] = []
        self.asset_v = self._asset_version()
        self.guides = self._load_guides()
        self.posts = self._load_posts()
        self.merch = self._load_merch()
        self.pressings = self._load_pressings()

    # ------------------------------------------------------------------ utils
    def _load_pressings(self) -> dict:
        """eBay id -> 'A: <x> / B: <y>' for records whose runout Edwin verified
        (data/discogs/matches.json). Unverified records show nothing."""
        from .discogs import load_matches, matrix_text
        try:
            return {k: t for k, m in load_matches().items()
                    if m.get("pressing_verified") and (t := matrix_text(m))}
        except Exception:   # the Discogs data is optional; the site builds without it
            return {}

    def _asset_version(self) -> str:
        h = hashlib.sha1()
        for p in sorted(STATIC.rglob("*")):
            if p.is_file() and p.suffix in (".css", ".js"):
                h.update(p.read_bytes())
        return h.hexdigest()[:10]

    def _load_guides(self):
        guides = []
        for p in sorted((CONTENT / "guides").glob("*.html")):
            raw = p.read_text(encoding="utf-8")
            m = re.match(r"\s*<!--\s*(\{.*?\})\s*-->\s*", raw, re.S)
            meta = json.loads(m.group(1)) if m else {}
            meta.setdefault("slug", p.stem)
            meta["body"] = raw[m.end():] if m else raw
            guides.append(meta)
        guides.sort(key=lambda g: g.get("order", 99))
        return guides

    def write(self, rel: str, text: str, sitemap=True, priority: str | None = None):
        path = self.out / rel.lstrip("/")
        if rel.endswith("/"):
            path = path / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if sitemap:
            self.urls.append((rel, priority))

    def abs(self, path: str) -> str:
        return self.base + path

    # ------------------------------------------------------------------ chrome
    def layout(self, *, title: str, desc: str, path: str, body: str, nav: str = "",
               og_image: str | None = None, ld: list | None = None, noindex=False,
               og_type="website", head_extra: str = "", page_class: str = "") -> str:
        cfg = self.cfg
        full_title = title if "Music Ed Cafe" in title else f"{title} | Music Ed Cafe"
        og = og_image or self.abs("/img/og.jpg")
        v = self.asset_v
        robots = '<meta name="robots" content="noindex,follow">' if noindex else ""
        lds = "".join(jsonld(x) for x in (ld or []))
        return f"""<!doctype html>
<html lang="en" class="no-js">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(self.abs(path))}">
{robots}
<meta name="theme-color" content="#221d18">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta property="og:site_name" content="Music Ed Cafe">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{esc(full_title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(self.abs(path))}">
<meta property="og:image" content="{esc(og)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preload" href="/fonts/alfa-slab-one-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/css/site.css?v={v}">
{head_extra}{lds}
<script>document.documentElement.classList.replace('no-js','js')</script>
</head>
<body class="{page_class}">
<a class="skip-link" href="#main">Skip to content</a>
{self.header(nav)}
<main id="main" tabindex="-1">
{body}
</main>
<button type="button" class="to-top" id="to-top" aria-label="Back to top" hidden><span aria-hidden="true">↑</span></button>
{self.footer()}
<script src="/js/site.js?v={v}" defer></script>
</body>
</html>
"""

    def header(self, nav: str) -> str:
        items = [("shop", "/shop/", "Shop"), ("merch", "/merch/", "Merch"), ("sold", "/sold/", "Recently Sold"),
                 ("sell", "/sell/", "Sell Your Records"),
                 ("genres", "/genre/", "Genres"), ("guides", "/guides/", "Guides"), ("blog", "/blog/", "Blog"),
                 ("about", "/about/", "About"), ("contact", "/contact/", "Contact"),
                 ("ebay", self.cfg["ebay_store_url"], "eBay store")]
        links = "".join(
            f'<li class="nav-{k}"><a href="{esc(h)}"{" aria-current=\"page\"" if k == nav else ""}'
            f'{" rel=\"noopener\"" if k == "ebay" else ""}>{t}</a></li>'
            for k, h, t in items)
        return f"""<header class="site-header">
  <div class="wrap header-row">
    <a class="brand" href="/" aria-label="Music Ed Cafe home">
      <img src="/favicon.png" width="32" height="32" alt="">
      <span>Music Ed Cafe</span>
    </a>
    <a class="header-cta" href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">eBay store</a>
    <button class="menu-btn" type="button" aria-expanded="false" aria-controls="site-nav">
      <span class="menu-icon" aria-hidden="true"></span><span class="menu-label">Menu</span>
    </button>
  </div>
  <nav id="site-nav" class="site-nav" aria-label="Main">
    <ul class="wrap">{links}</ul>
  </nav>
</header>"""

    def footer(self) -> str:
        st = self.state
        if st.get("last_success"):
            src = {"browse_api": "the eBay API"}.get(st.get("last_success_source"), "an eBay export")
            synced = f"Inventory last synced {fmt_ts(st['last_success'])} from {src}."
        else:
            synced = "Inventory has not been synced yet."
        social = self.social_links()
        return f"""<footer class="site-footer">
  <div class="wrap footer-grid">
    <div>
      <p class="footer-brand">Music Ed Cafe</p>
      <p>Independent vinyl records, sold online from Lynn, Massachusetts.</p>
      <p class="slogan">Spin it. Feel it. Live it.</p>
    </div>
    <div>
      <p class="footer-h">Buying</p>
      <p>Every purchase happens on eBay. Payment, shipping and returns are handled securely there.</p>
      <p><a href="/buying-on-ebay/">How buying works</a></p>
    </div>
    <div>
      <p class="footer-h">Explore</p>
      <ul class="footer-links">
        <li><a href="/shop/">Shop all records</a></li>
        <li><a href="/sold/">Recently sold</a></li>
        <li><a href="/merch/">Merch preorders</a></li>
        <li><a href="/sell/">Sell your records</a></li>
        <li><a href="/guides/">Collecting &amp; care guides</a></li>
        <li><a href="/about/">About</a></li>
        <li><a href="/contact/">Contact</a></li>
        <li><a href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">Music Ed Cafe on eBay</a></li>
        {social}
      </ul>
    </div>
  </div>
  <div class="wrap footer-meta">
    <p class="sync-note" id="sync-note">{esc(synced)}</p>
    <p>Prices and availability are set on eBay and can change at any time. Music Ed Cafe is an independent seller and is not affiliated with eBay Inc.</p>
    <p>Where a record has no photo yet, the stock cover art comes from the <a href="https://coverartarchive.org/" rel="noopener">Cover Art Archive</a> / <a href="https://musicbrainz.org/" rel="noopener">MusicBrainz</a> (or Apple iTunes where they have none). It is for reference only and is not a photo of the copy for sale.</p>
    <p><button type="button" class="footer-top" data-to-top>Back to top</button></p>
  </div>
</footer>"""

    def social_links(self) -> str:
        c = self.cfg.get("contact", {})
        out = []
        for key, label in (("instagram", "Instagram"), ("facebook", "Facebook"), ("tiktok", "TikTok"),
                           ("youtube", "YouTube")):
            if c.get(key):
                out.append(f'<li><a href="{esc(c[key])}" rel="noopener me">{label}</a></li>')
        return "".join(out)

    # ------------------------------------------------------------------ pieces
    def sleeve(self, r: dict, size: int = 500, eager=False, sizes="(max-width: 600px) 46vw, 240px") -> str:
        alt = f"{r['display_title']} record sleeve"
        lp = r.get("local_photo")
        if r["images"] and lp:
            big = size >= 900
            src = f'/img/photo/{lp["large"] if big else lp["thumb"]}'
            w, h = (lp["large_w"], lp["large_h"]) if big else (lp["thumb_w"], lp["thumb_h"])
            srcset = f'/img/photo/{lp["thumb"]} {lp["thumb_w"]}w, /img/photo/{lp["large"]} {lp["large_w"]}w'
            load = 'fetchpriority="high"' if eager else 'loading="lazy" decoding="async"'
            return (f'<img class="sleeve-img" src="{src}" srcset="{srcset}" sizes="{sizes}" width="{w}" '
                    f'height="{h}" alt="{esc(alt)}" {load}>')
        if r["images"]:
            u = r["images"][0]
            src = ebay_img(u, size)
            srcset = f"{ebay_img(u, 300)} 300w, {ebay_img(u, 500)} 500w, {ebay_img(u, 960)} 960w"
            load = 'fetchpriority="high"' if eager else 'loading="lazy" decoding="async"'
            return (f'<img class="sleeve-img" src="{esc(src)}" srcset="{esc(srcset)}" sizes="{sizes}" '
                    f'width="500" height="500" alt="{esc(alt)}" {load}>')
        sc = r.get("stock_cover")
        if sc:
            big = size >= 900
            src = sc["large"] if big else sc["thumb"]
            w, h = (sc["large_w"], sc["large_h"]) if big else (sc["thumb_w"], sc["thumb_h"])
            srcset = f'{sc["thumb"]} {sc["thumb_w"]}w, {sc["large"]} {sc["large_w"]}w'
            load = 'fetchpriority="high"' if eager else 'loading="lazy" decoding="async"'
            return (f'<img class="sleeve-img stock" src="{src}" srcset="{srcset}" sizes="{sizes}" width="{w}" '
                    f'height="{h}" alt="Stock cover art for {esc(r["display_title"])} (not a photo of this copy)" {load}>')
        hue = int(r["id"][-3:]) % 5
        artist = r.get("artist") or ""
        album = r.get("album") or r["display_title"]
        return (f'<div class="sleeve-ph ph{hue}" role="img" aria-label="{esc(alt)} (no photo yet)">'
                f'<span class="ph-artist">{esc(artist)}</span><span class="ph-album">{esc(album)}</span>'
                f'<span class="ph-note">Photo on eBay</span></div>')

    def card(self, r: dict, eager=False) -> str:
        status = r["status"]
        sticker = ""
        auction = r.get("listing_type") == "AUCTION"
        if status == "active" and r["price"]:
            sticker = (f'<span class="sticker{" sticker-bid" if auction else ""}" aria-hidden="true">'
                       f'{"<small>bid</small>" if auction else ""}{money(r["price"], r["currency"])}</span>')
        stamp = '<span class="stamp" aria-hidden="true">Sold</span>' if status == "sold" else (
            '<span class="stamp stamp-ended" aria-hidden="true">Ended</span>' if status in ("ended", "unavailable") else "")
        meta = " · ".join(x for x in [r.get("format_label_short") or self.format_label(r),
                                      r.get("release_year") or r.get("listing_year")] if x)
        if status == "active" and auction:
            price_line = (f'<span class="card-price">Auction · current bid {money(r["price"], r["currency"])}</span>'
                          f'<span class="card-meta">ends {fmt_date(r.get("end_date"))}</span>')
        elif status == "active":
            price_line = f'<span class="card-price">{money(r["price"], r["currency"])}</span>'
        elif status == "sold":
            price_line = f'<span class="card-sold">Sold {fmt_date(r.get("sold_at"))}</span>'
        else:
            price_line = '<span class="card-sold">No longer available</span>'
        data = {
            "id": r["id"], "status": status,
            "genres": " ".join(r["genre_slugs"]), "formats": " ".join(r["format_slugs"]),
            "price": r["price"] or "", "grade": r.get("media_grade") or "",
            "date": archive_key(r) if status in ARCHIVE_STATUSES else (r.get("start_date") or "")[:10],
            "artist": (r.get("artist") or r["display_title"]).lower(),
        }
        attrs = " ".join(f'data-{k}="{esc(v)}"' for k, v in data.items())
        return f"""<li class="card" {attrs}>
  <a class="card-link" href="{r['path']}">
    <span class="card-art">{self.sleeve(r, eager=eager)}<span class="disc" aria-hidden="true"></span>{sticker}{stamp}{'<span class="stock-tag">Stock cover</span>' if r.get("stock_cover") else ''}</span>
    <span class="card-body">
      <span class="card-title">{esc(r['display_title'])}</span>
      <span class="card-meta">{esc(meta)}</span>
      {price_line}
    </span>
  </a>
</li>"""

    def grid(self, recs, eager_first=0, cls="") -> str:
        if not recs:
            return ""
        return f'<ul class="grid {cls}">' + "".join(
            self.card(r, eager=i < eager_first) for i, r in enumerate(recs)) + "</ul>"

    def format_label(self, r: dict) -> str:
        if r["kind"] == "gear":
            return "Audio gear"
        t = r.get("format_type") or ""
        size = (r.get("record_size") or "").replace("”", '"')
        if t.lower() == "single" and size:
            return f"{size} single"
        if t:
            return t if t != "Single" else "Single"
        return size or "Vinyl"

    def buy_button(self, r: dict, big=True, verb="Buy on eBay") -> str:
        url = self.affiliate(r["ebay_url"])
        return (f'<a class="btn btn-buy{" btn-big" if big else ""}" href="{esc(url)}" rel="noopener sponsored" '
                f'data-ebay-item="{esc(r["id"])}">{verb}<span class="visually-hidden">: '
                f'{esc(r["display_title"])}</span></a>')

    def affiliate(self, url: str) -> str:
        camp = self.cfg.get("epn_campaign_id")
        if not camp:
            return url
        return (f"{url}?mkevt=1&mkcid=1&mkrid=711-53200-19255-0&campid={camp}&toolid=10001"
                f"&customid=musicedcafe-site")

    def genre_meta(self):
        out = []
        for slug, label, _words in merge.GENRE_COLLECTIONS:
            recs = [r for r in self.visible if slug in r["genre_slugs"]]
            if recs:
                out.append((slug, label, recs))
        return out

    def format_meta(self):
        out = []
        for slug, label in merge.FORMAT_COLLECTIONS:
            recs = [r for r in self.visible if slug in r["format_slugs"]]
            if recs:
                out.append((slug, label, recs))
        return out

    def org_ld(self):
        c = self.cfg
        same = [c["ebay_store_url"]] + [v for k, v in (c.get("contact") or {}).items()
                                        if v and k != "email" and not k.startswith("_")]
        return {
            "@context": "https://schema.org", "@type": "OnlineStore", "@id": self.abs("/#org"),
            "name": "Music Ed Cafe", "url": self.abs("/"), "logo": self.abs("/apple-touch-icon.png"),
            "image": self.abs("/img/og.jpg"), "slogan": "Good Coffee, Good Music, Good People",
            "description": "Independent online vinyl record seller based in Lynn, Massachusetts. "
                           "All purchases are completed on eBay.",
            "address": {"@type": "PostalAddress", "addressLocality": "Lynn", "addressRegion": "MA",
                        "addressCountry": "US"},
            "areaServed": "US", "sameAs": same,
        }

    def breadcrumb_ld(self, crumbs):
        return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": self.abs(p)}
            for i, (n, p) in enumerate(crumbs)]}

    def crumbs_html(self, crumbs):
        parts = []
        for i, (n, p) in enumerate(crumbs):
            if i == len(crumbs) - 1:
                parts.append(f'<li aria-current="page">{esc(n)}</li>')
            else:
                parts.append(f'<li><a href="{p}">{esc(n)}</a></li>')
        return f'<nav class="crumbs" aria-label="Breadcrumb"><ol>{"".join(parts)}</ol></nav>'

    def empty_shop(self) -> str:
        return f"""<div class="empty-state">
  <p class="empty-h">The shelves here are between restocks.</p>
  <p>New records are listed on eBay first. Everything for sale right now is in the live Music Ed Cafe store, and listings will show up on this page as soon as the store syncs.</p>
  <p class="empty-actions"><a class="btn btn-buy" href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">Browse the live eBay store</a>
  <a class="btn btn-ghost" href="/sold/">See what has sold</a></p>
</div>"""

    def filter_ui(self, recs, scope: str) -> str:
        genres = [(s, l, sum(1 for r in recs if s in r["genre_slugs"])) for s, l, _ in merge.GENRE_COLLECTIONS]
        genres = [g for g in genres if g[2]]
        formats = [(s, l, sum(1 for r in recs if s in r["format_slugs"])) for s, l in merge.FORMAT_COLLECTIONS]
        formats = [f for f in formats if f[2]]
        gopts = "".join(f'<option value="{s}">{esc(l)} ({n})</option>' for s, l, n in genres)
        fopts = "".join(f'<option value="{s}">{esc(l)} ({n})</option>' for s, l, n in formats)
        price_block = ""
        sort_price = ""
        if scope in ("shop", "featured"):
            price_block = """<label class="field"><span>Price</span>
          <select name="price" id="f-price">
            <option value="">Any price</option><option value="0-15">Under $15</option>
            <option value="15-30">$15 to $30</option><option value="30-60">$30 to $60</option>
            <option value="60-">$60 and up</option></select></label>"""
            sort_price = ('<option value="price-asc">Price: low to high</option>'
                          '<option value="price-desc">Price: high to low</option>')
        newest = "Newest" if scope in ("shop", "featured") else "Most recently sold"
        return f"""<div class="finder-sentinel" aria-hidden="true"></div>
<div class="finder-bar" id="finder-bar">
<form class="finder" id="finder" role="search" data-scope="{scope}" onsubmit="return false">
  <label class="search-box"><span class="visually-hidden">Search records</span>
    <input type="search" name="q" id="f-q" placeholder="Search artist, title, label, catalog #" autocomplete="off" enterkeyhint="search">
  </label>
  <button class="btn btn-ghost filter-toggle" type="button" aria-expanded="false" aria-controls="filters">Filters &amp; sort <span class="filter-count" id="filter-count"></span></button>
  <div class="filters" id="filters">
    <div class="filters-head"><p class="filters-title">Filters &amp; sort</p><button type="button" class="btn btn-ghost filters-close">Done</button></div>
    <label class="field"><span>Genre</span><select name="genre" id="f-genre"><option value="">All genres</option>{gopts}</select></label>
    <label class="field"><span>Format</span><select name="format" id="f-format"><option value="">All formats</option>{fopts}</select></label>
    {price_block}
    <label class="field"><span>Vinyl grade</span><select name="grade" id="f-grade">
      <option value="">Any grade</option><option value="NM">NM / Mint</option><option value="VG+">VG+ or better</option><option value="VG">VG or better</option></select></label>
    <label class="field"><span>Sort</span><select name="sort" id="f-sort">
      <option value="new">{newest}</option>{sort_price}<option value="artist">Artist A to Z</option></select></label>
    <button type="button" class="btn btn-ghost filters-reset" id="f-reset">Clear all</button>
  </div>
</form>
<p class="result-count" id="result-count" aria-live="polite">{len(recs)} {'record' if len(recs) == 1 else 'records'}</p>
</div>"""

    # ------------------------------------------------------------------ pages
    def page_home(self):
        cfg = self.cfg
        has_active = bool(self.active)
        all_featured = self.featured_records()
        featured = all_featured[:8]
        if not featured:
            pool = [r for r in self.active if r["images"]] or self.active
            featured = sorted(pool, key=lambda r: -float(r["price"] or 0))[:4]
        arrivals = sorted([r for r in self.active if r not in featured],
                          key=lambda r: r.get("start_date") or "", reverse=True)[:8]
        recent_sold = self.sold[:12]
        cta = ('<a class="btn btn-buy btn-big" href="/shop/">Shop all records</a>' if has_active else
               f'<a class="btn btn-buy btn-big" href="{esc(cfg["ebay_store_url"])}" rel="noopener">Shop the live eBay store</a>')
        genre_tiles = "".join(
            f'<li><a class="genre-tile g-{i % 6}" href="/genre/{s}/"><span class="genre-name">{esc(l)}</span>'
            f'<span class="genre-count">{len(rs)} {"record" if len(rs) == 1 else "records"}</span></a></li>'
            for i, (s, l, rs) in enumerate(self.genre_meta()))
        stock = ""
        if has_active:
            stock = f"""<section class="section wrap" aria-labelledby="h-featured">
  <div class="section-head"><h2 id="h-featured">Featured records</h2><a href="{'/featured/' if all_featured else '/shop/'}">{f'See all {len(all_featured)} featured' if all_featured else 'Shop all'}</a></div>
  {self.grid(featured)}
</section>
<section class="section wrap" aria-labelledby="h-new">
  <div class="section-head"><h2 id="h-new">New arrivals</h2><a href="/shop/?sort=new">See all new</a></div>
  {self.grid(arrivals)}
</section>"""
        else:
            stock = f"""<section class="section wrap" aria-labelledby="h-new">
  <div class="section-head"><h2 id="h-new">New arrivals</h2></div>
  {self.empty_shop()}
</section>"""
        guides = "".join(
            f'<li><a class="guide-card" href="/guides/{g["slug"]}/"><span class="guide-kicker">{esc(g.get("kicker", "Guide"))}</span>'
            f'<span class="guide-title">{esc(g["title"])}</span><span class="guide-desc">{esc(g["description"])}</span></a></li>'
            for g in self.guides[:3])
        sold_strip = ""
        if recent_sold:
            sold_strip = f"""<section class="section wrap" aria-labelledby="h-sold">
  <div class="section-head"><h2 id="h-sold">From the crates: recently sold</h2><a href="/sold/">See all {len(self.sold)}</a></div>
  <p class="section-sub">A look at records that have already found new homes, so you can see what turns up here.</p>
  {self.grid(recent_sold, cls="grid-strip")}
</section>"""
        body = f"""{self.merch_strip()}
<section class="hero">
  <div class="wrap hero-grid">
    <picture class="hero-art">
      <source type="image/avif" srcset="/img/poster-480.avif 480w, /img/poster-800.avif 800w, /img/poster-1200.avif 1200w" sizes="(max-width: 760px) 100vw, 520px">
      <source type="image/webp" srcset="/img/poster-480.webp 480w, /img/poster-800.webp 800w, /img/poster-1200.webp 1200w" sizes="(max-width: 760px) 100vw, 520px">
      <img src="/img/poster-1200.jpg" width="1200" height="1200" fetchpriority="high" decoding="async"
        alt="Engraved poster: a smiling man in a beanie holds a steaming Music Ed Cafe mug in front of a giant vinyl record, with the slogans Good Coffee, Good Music, Good People and Support Local, Support Each Other.">
    </picture>
    <div class="hero-copy">
      <p class="eyebrow">Independent vinyl · Lynn, Massachusetts</p>
      <h1>Music Ed Cafe</h1>
      <p class="lede">Hand-picked records, cleaned, graded honestly and photographed as they are. Find something you love here, then buy it securely through the Music Ed Cafe store on eBay.</p>
      <div class="hero-actions">{cta}<a class="btn btn-ghost btn-big" href="/sold/">What we've sold</a></div>
      <p class="hero-note">Payment, shipping and returns are handled by eBay. <a href="/buying-on-ebay/">How it works</a></p>
    </div>
  </div>
</section>
<section class="steps wrap" aria-label="How buying works">
  <ol class="steps-list">
    <li><span class="step-n" aria-hidden="true">1</span><span><strong>Find a record here.</strong> Browse by genre and format, or search.</span></li>
    <li><span class="step-n" aria-hidden="true">2</span><span><strong>Tap Buy on eBay.</strong> It opens the exact listing.</span></li>
    <li><span class="step-n" aria-hidden="true">3</span><span><strong>Check out on eBay.</strong> Payment, shipping and returns are handled there.</span></li>
  </ol>
</section>
{stock}
{sold_strip}
<section class="section wrap" aria-labelledby="h-genres">
  <div class="section-head"><h2 id="h-genres">Dig by genre</h2><a href="/genre/">All genres</a></div>
  <ul class="genre-grid">{genre_tiles}</ul>
</section>
<section class="section wrap" aria-labelledby="h-sell">
  <div class="sell-teaser">
    <div>
      <h2 id="h-sell">Sell us your records</h2>
      <p>Got a collection or a box of records to move? Music Ed Cafe buys records in lots, and every record is looked at individually.</p>
    </div>
    <a class="btn btn-buy btn-big" href="/sell/">Sell Your Records</a>
  </div>
</section>
<section class="section band" aria-labelledby="h-why">
  <div class="wrap why-grid">
    <div>
      <h2 id="h-why">Records with a story</h2>
      <p>Music Ed Cafe is an independent vinyl seller in Lynn, Massachusetts. In the store's own words, every record has a story, and the goal is simple: connect great music with great listeners while helping preserve the history of recorded sound.</p>
      <p><a href="/about/">More about Music Ed Cafe</a></p>
    </div>
    <ul class="why-list">
      <li>Records cleaned before listing, with an ultrasonic cleaner and Spin Doctor</li>
      <li>Clear photos of the exact copy you'll receive</li>
      <li>Honest condition notes and grading, vinyl and sleeve graded separately</li>
      <li>Digitized audio samples available on request before you buy</li>
      <li>Secure packaging and quick shipping</li>
    </ul>
  </div>
</section>
<section class="section wrap" aria-labelledby="h-guides">
  <div class="section-head"><h2 id="h-guides">Collecting &amp; care guides</h2><a href="/guides/">All guides</a></div>
  <ul class="guide-grid">{guides}</ul>
</section>
<section class="slogans" aria-label="Music Ed Cafe slogans">
  <div class="wrap slogan-row">
    <p>Good Coffee, Good Music, Good People</p>
    <p>Support Local, Support Each Other</p>
    <p>Keep the coffee flowin' and the music goin'</p>
  </div>
</section>"""
        ld = [self.org_ld(), {"@context": "https://schema.org", "@type": "WebSite", "name": "Music Ed Cafe",
                              "url": self.abs("/"), "potentialAction": {
                                  "@type": "SearchAction", "target": self.abs("/shop/?q={search_term_string}"),
                                  "query-input": "required name=search_term_string"}}]
        self.write("/", self.layout(
            title="Music Ed Cafe: Independent Vinyl Records from Lynn, MA",
            desc="Independent vinyl records from Lynn, Massachusetts. Hand-cleaned, honestly graded LPs and 45s, "
                 "sold through the Music Ed Cafe store on eBay.",
            path="/", body=body, ld=ld, page_class="home"), priority="1.0")

    def page_shop(self):
        recs = self.active
        listing = self.grid(recs, eager_first=4, cls="results") if recs else ""
        empty = "" if recs else self.empty_shop()
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), ("Shop All Records", "/shop/")])}
  <h1>Shop All Records</h1>
  <p class="lede">Every record available right now. Each one links to its exact eBay listing, where payment, shipping and returns are handled.</p>
</div>
<div class="wrap shop-layout">
  {self.filter_ui(recs, "shop") if recs else ""}
  {listing}
  <div class="no-results" id="no-results" hidden><p>No records match those filters.</p><button class="btn btn-ghost" type="button" id="nr-reset">Clear filters</button></div>
  {empty}
</div>"""
        self.write("/shop/", self.layout(
            title="Shop All Records", path="/shop/", nav="shop", body=body, page_class="shop",
            desc=f"Shop vinyl records from Music Ed Cafe in Lynn, MA: {len(recs)} available now. "
                 "Search by artist, genre, format and price, then buy securely on eBay."),
            priority="0.9")

    def featured_records(self) -> list[dict]:
        """Active records to feature: an editorial featured=True/False always wins;
        otherwise a record qualifies when its price is strictly above featured.min_price."""
        min_p = (self.cfg.get("featured") or {}).get("min_price")
        def qualifies(r):
            if r.get("featured") is True:
                return True
            if r.get("featured") is False or min_p is None:
                return False
            try:
                return float(r.get("price") or 0) > float(min_p)
            except (TypeError, ValueError):
                return False
        return sorted([r for r in self.active if qualifies(r)], key=lambda r: -float(r.get("price") or 0))

    def page_featured(self):
        recs = self.featured_records()
        min_p = (self.cfg.get("featured") or {}).get("min_price")
        lede = (f"Standout records from the crates, all over ${min_p:g}. " if min_p is not None else "Standout records from the crates. ")
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), ("Featured", "/featured/")])}
  <h1>Featured Records</h1>
  <p class="lede">{esc(lede)}Each one links to its exact eBay listing, where payment, shipping and returns are handled.</p>
</div>
<div class="wrap shop-layout">
  {self.filter_ui(recs, "featured") if recs else ""}
  {self.grid(recs, eager_first=4, cls="results") if recs else self.empty_shop()}
  <div class="no-results" id="no-results" hidden><p>No featured records match those filters.</p><button class="btn btn-ghost" type="button" id="nr-reset">Clear filters</button></div>
</div>"""
        self.write("/featured/", self.layout(
            title="Featured Records", path="/featured/", nav="shop", body=body, page_class="shop",
            desc=f"{len(recs)} featured vinyl records from Music Ed Cafe in Lynn, MA. Buy securely on eBay."),
            priority="0.8")

    def page_sold(self):
        recs = self.archive
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), ("Recently Sold", "/sold/")])}
  <h1>Recently Sold</h1>
  <p class="lede">Records that have already gone to new homes. They aren't for sale, but they show what comes through Music Ed Cafe. For what's available today, <a href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">browse the live eBay store</a>.</p>
</div>
<div class="wrap shop-layout">
  {self.filter_ui(recs, "sold")}
  {self.grid(recs, eager_first=4, cls="results")}
  <div class="no-results" id="no-results" hidden><p>No sold records match those filters.</p><button class="btn btn-ghost" type="button" id="nr-reset">Clear filters</button></div>
</div>"""
        self.write("/sold/", self.layout(
            title="Recently Sold Records", path="/sold/", nav="sold", body=body, page_class="shop",
            desc=f"{len(self.sold)} vinyl records recently sold by Music Ed Cafe, an independent record seller in Lynn, MA."),
            priority="0.6")

    def alternatives(self, r: dict, n=8) -> list[dict]:
        pool = [x for x in self.active if x["id"] != r["id"]]
        out = []
        a = (r.get("artist") or "").lower()
        if a:
            out += [x for x in pool if (x.get("artist") or "").lower() == a]
        for g in r["genre_slugs"]:
            out += [x for x in pool if g in x["genre_slugs"] and x not in out]
        for f in r["format_slugs"]:
            out += [x for x in pool if f in x["format_slugs"] and x not in out]
        return out[:n]

    def related_sold(self, r: dict, n=8) -> list[dict]:
        pool = [x for x in self.archive if x["id"] != r["id"]]
        out = [x for x in pool if set(x["genre_slugs"]) & set(r["genre_slugs"])]
        if len(out) < n:
            out += [x for x in pool if set(x["format_slugs"]) & set(r["format_slugs"]) and x not in out]
        return out[:n]

    def detail_rows(self, r: dict) -> str:
        prov = r["provenance"]
        rows = []

        unset = object()

        def add(label, field, value=unset, note=None):
            v = r.get(field) if value is unset else value
            if not v:
                return
            p = prov.get(field, "")
            mark = ' <span class="src" title="Read from the eBay listing title">(from listing title)</span>' if p == "title" else ""
            extra = f'<span class="note">{esc(note)}</span>' if note else ""
            rows.append(f"<div><dt>{label}</dt><dd>{esc(v)}{mark}{extra}</dd></div>")

        add("Artist", "artist")
        add("Title", "album")
        add("Format", "format_type", self.format_label(r) if (r.get("format_type") or r.get("record_size")) else None)
        if r.get("record_size") and r.get("format_type") and "single" not in (r.get("format_type") or "").lower():
            add("Record size", "record_size")
        add("Speed", "speed")
        add("Sound", "audio")
        add("Label", "label")
        add("Catalog #", "catalog_number")
        if r.get("release_year"):
            add("Release year", "release_year")
        elif r.get("listing_year"):
            add("Year (as listed)", "listing_year", note="The listing title doesn't say whether this is the release or the pressing year.")
        add("Pressing year", "pressing_year")
        add("Country", "country")
        add("Matrix/Runout", "matrix_runout", self.pressings.get(r["id"]))
        add("Vinyl grade", "media_grade", note=r.get("media_grade_note"))
        add("Sleeve grade", "sleeve_grade", note=r.get("sleeve_grade_note"))
        if r.get("title_grade") and not (r.get("media_grade") or r.get("sleeve_grade")):
            add("Grade in title", "title_grade", note="The title gives this grade without saying whether it's for the vinyl or the sleeve. Check the photos and listing.")
        add("Condition", "condition")
        add("Vinyl color", "vinyl_color", r.get("vinyl_color") if (r.get("vinyl_color") or "").lower() not in ("black", "") else None)
        add("Edition notes", "pressing_notes")
        add("Listing notes", "title_notes")
        add("Genre", "genre", ", ".join(r["genres"]) if r["genres"] else None)
        add("eBay listing title", "ebay_title")
        return "".join(rows)

    def page_record(self, r: dict):
        status = r["status"]
        title = r["display_title"]
        crumbs = [("Home", "/")]
        if status == "active":
            crumbs.append(("Shop", "/shop/"))
        else:
            crumbs.append(("Recently Sold", "/sold/"))
        crumbs.append((title, r["path"]))
        imgs = r["images"]
        gallery_main = self.sleeve(r, size=960, eager=True, sizes="(max-width: 760px) 92vw, 520px")
        thumbs = ""
        if len(imgs) > 1:
            thumbs = '<ul class="thumbs">' + "".join(
                f'<li><a href="{esc(ebay_img(u, 1600))}" data-full="{esc(ebay_img(u, 960))}" '
                f'aria-label="Photo {i + 1} of {len(imgs)}"><img src="{esc(ebay_img(u, 140))}" width="70" height="70" '
                f'alt="" loading="lazy"></a></li>' for i, u in enumerate(imgs[:12])) + "</ul>"
        if status == "active":
            qty = r.get("quantity_available")
            auction = r.get("listing_type") == "AUCTION"
            verb = "Bid on eBay" if auction else "Buy on eBay"
            if auction:
                bids = r.get("bids") or 0
                price_html = (f'<p class="price-kicker">Auction · {bids} bid{"s" if bids != 1 else ""}</p>'
                              f'<p class="price">Current bid {money(r["price"], r["currency"])}</p>'
                              f'<p class="buy-note">Ends {fmt_date(r.get("end_date"))}. The current bid can change at any moment; eBay shows the live price.'
                              f'{" Buy It Now: " + money(r["buy_it_now"], r["currency"]) + "." if r.get("buy_it_now") else ""}</p>')
            else:
                price_html = f'<p class="price">{money(r["price"], r["currency"])}</p>'
            buy = f"""<div class="buy-box">
  {price_html}
  {self.buy_button(r, verb=verb)}
  <p class="buy-note">Payment, shipping and returns are handled securely on eBay. {f'{qty} available.' if qty and int(qty) > 1 else ''}</p>
</div>"""
            banner = ""
            buy += (f'<div class="buy-bar" aria-hidden="true"><span class="buy-bar-price">{money(r["price"], r["currency"])}</span>'
                    f'<a class="btn btn-buy" tabindex="-1" href="{esc(self.affiliate(r["ebay_url"]))}" rel="noopener sponsored">{verb}</a></div>')
        else:
            if status == "sold":
                msg = f"This record sold{' on ' + fmt_date(r.get('sold_at')) if r.get('sold_at') else ''}."
            else:
                msg = "This eBay listing has ended or is unavailable."
            price_note = ""
            if status == "sold" and r.get("price"):
                price_note = f'<p class="sold-price">Sold for {money(r["price"], r["currency"])}</p>'
            banner = f"""<div class="archive-banner" role="status">
  <p class="archive-h">{esc(msg)}</p>
  <p>It's no longer for sale. Similar records that are available are listed below, and new finds go up on <a href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">the eBay store</a> first.</p>
</div>"""
            buy = f"""<div class="buy-box buy-box-archived">
  {price_note}
  <a class="btn btn-ghost" href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">Browse records for sale on eBay</a>
  <p class="buy-note"><a href="{esc(r['ebay_url'])}" rel="noopener nofollow">View the original eBay listing</a> (sold listings stay visible on eBay for a while).</p>
</div>"""
        desc = ""
        if r["description_paragraphs"]:
            paras = "".join(f"<p>{'<br>'.join(esc(x) for x in p.split(chr(10)))}</p>" for p in r["description_paragraphs"])
            desc = f"""<section class="desc selectable" aria-labelledby="h-desc">
  <h2 id="h-desc">From the eBay listing</h2>
  {paras}
</section>"""
        blurb = f'<div class="blurb selectable"><p>{esc(r["blurb"])}</p></div>' if r.get("blurb") else ""
        alts = self.alternatives(r)
        more = ""
        if status == "active":
            same = [x for x in self.active if x["id"] != r["id"] and set(x["genre_slugs"]) & set(r["genre_slugs"])][:8]
            if same:
                g0 = r["genre_slugs"][0]
                more = f"""<section class="section" aria-labelledby="h-more"><div class="section-head"><h2 id="h-more">More from this genre</h2><a href="/genre/{g0}/">See all</a></div>{self.grid(same)}</section>"""
        else:
            if alts:
                more = f"""<section class="section" aria-labelledby="h-more"><div class="section-head"><h2 id="h-more">Available alternatives</h2></div>{self.grid(alts)}</section>"""
            else:
                more = f"""<section class="section" aria-labelledby="h-more"><div class="section-head"><h2 id="h-more">Available alternatives</h2></div>
<div class="empty-state small"><p>Nothing similar is listed on this site right now. The live eBay store always has the current stock.</p><p><a class="btn btn-buy" href="{esc(self.cfg['ebay_store_url'])}" rel="noopener">Browse the live eBay store</a></p></div></section>"""
            rel = self.related_sold(r)
            if rel:
                more += f"""<section class="section" aria-labelledby="h-rel"><div class="section-head"><h2 id="h-rel">Also sold from the crates</h2><a href="/sold/">See all</a></div>{self.grid(rel, cls="grid-strip")}</section>"""
        rows = self.detail_rows(r)
        sub = " · ".join(x for x in [self.format_label(r), r.get("label"), r.get("release_year") or r.get("listing_year")] if x)
        body = f"""<div class="wrap">
  {self.crumbs_html(crumbs)}
  {banner}
  <article class="record" data-status="{status}">
    <div class="record-media">
      <div class="record-main">{gallery_main}{'<span class="stamp stamp-lg" aria-hidden="true">Sold</span>' if status == 'sold' else ''}</div>
      {thumbs}
      {'<p class="stock-note">Stock cover art for reference, not a photo of this copy. See the <a href="' + esc(r["ebay_url"]) + '" rel="noopener">eBay listing</a> for actual photos and condition.</p>' if r.get("stock_cover") else ''}
    </div>
    <div class="record-info">
      <h1>{esc(title)}</h1>
      <p class="record-sub">{esc(sub)}</p>
      {buy}
      {blurb}
      <dl class="specs selectable">{rows}</dl>
      <p class="fine">Details are copied from the eBay listing. Anything the listing doesn't state is left out rather than guessed. The eBay listing is the final word on condition and price.</p>
    </div>
  </article>
  {desc}
  {more}
</div>"""
        avail = {"active": "https://schema.org/InStock", "sold": "https://schema.org/SoldOut"}.get(
            status, "https://schema.org/Discontinued")
        product = {"@context": "https://schema.org", "@type": "Product", "name": title,
                   "url": self.abs(r["path"]), "sku": r["id"],
                   "category": "Music > Vinyl Records" if r["kind"] == "record" else "Audio Equipment",
                   "description": (r["description_text"][:500] if r["description_text"] else r["ebay_title"])}
        if imgs:
            product["image"] = [ebay_img(u, 1600) for u in imgs[:6]]
        if r.get("artist"):
            product["brand"] = {"@type": "Brand", "name": r.get("label") or r["artist"]}
        if r.get("catalog_number"):
            product["mpn"] = r["catalog_number"]
        cond = (r.get("condition") or "").lower()
        if r["price"]:
            product["offers"] = {"@type": "Offer", "url": r["ebay_url"], "price": r["price"],
                                 "priceCurrency": r["currency"], "availability": avail,
                                 "itemCondition": "https://schema.org/NewCondition" if cond.startswith("new")
                                 else "https://schema.org/UsedCondition",
                                 "seller": {"@type": "Organization", "name": "Music Ed Cafe", "@id": self.abs("/#org")}}
            if r.get("end_date") and status == "active":
                product["offers"]["priceValidUntil"] = r["end_date"]
        g = ", ".join(r["genres"][:2])
        if status == "active":
            desc_meta = (f"{title}: {self.format_label(r)}{', ' + r['label'] if r.get('label') else ''}"
                         f"{', vinyl ' + r['media_grade'] if r.get('media_grade') else ''}. "
                         f"{money(r['price'], r['currency'])} from Music Ed Cafe in Lynn, MA. Buy securely on eBay.")
        else:
            desc_meta = (f"{title} ({self.format_label(r)}{', ' + r['label'] if r.get('label') else ''}"
                         f"{', ' + g if g else ''}). Sold by Music Ed Cafe, an independent record seller in Lynn, MA. "
                         "See similar records available now.")
        noindex = status != "active" and self.cfg.get("archive", {}).get("noindex_sold_pages")
        og_img = ebay_img(imgs[0], 960) if imgs else None
        self.write(r["path"], self.layout(
            title=title, desc=desc_meta, path=r["path"], body=body, nav="shop" if status == "active" else "sold",
            ld=[product, self.breadcrumb_ld(crumbs)], og_image=og_img, og_type="product", noindex=noindex,
            page_class="record-page"), sitemap=not noindex, priority="0.8" if status == "active" else "0.4")

    def collection_page(self, kind: str, slug: str, label: str, recs: list[dict]):
        path = f"/{kind}/{slug}/"
        act = [r for r in recs if r["status"] == "active"]
        arc = [r for r in recs if r["status"] != "active"]
        noun = "genre" if kind == "genre" else "format"
        crumbs = [("Home", "/"), ("Genres" if kind == "genre" else "Formats", f"/{kind}/"), (label, path)]
        avail = (self.grid(act, eager_first=4) if act else
                 f'<div class="empty-state small"><p>No {esc(label)} records are listed on this site right now. New finds go up on eBay first.</p>'
                 f'<p><a class="btn btn-buy" href="{esc(self.cfg["ebay_store_url"])}" rel="noopener">Browse the live eBay store</a></p></div>')
        arch = ""
        if arc:
            arch = f"""<section class="section" aria-labelledby="h-arch"><div class="section-head"><h2 id="h-arch">Recently sold {esc(label)}</h2></div>
<p class="section-sub">Already sold, shown so you can see what turns up.</p>{self.grid(arc)}</section>"""
        others = "".join(f'<li><a href="/{kind}/{s}/"{" aria-current=\"page\"" if s == slug else ""}>{esc(l)}</a></li>'
                         for s, l, _ in (self.genre_meta() if kind == "genre" else self.format_meta()))
        body = f"""<div class="wrap page-head">
  {self.crumbs_html(crumbs)}
  <h1>{esc(label)}</h1>
  <p class="lede">{len(act)} available now{f" · {len(arc)} recently sold" if arc else ""}.</p>
  <ul class="chip-row" aria-label="Other {noun}s">{others}</ul>
</div>
<div class="wrap">
  <section class="section" aria-labelledby="h-avail"><div class="section-head"><h2 id="h-avail">Available now</h2></div>{avail}</section>
  {arch}
</div>"""
        ld = [self.breadcrumb_ld(crumbs), {"@context": "https://schema.org", "@type": "CollectionPage",
                                           "name": f"{label} vinyl records", "url": self.abs(path)}]
        self.write(path, self.layout(
            title=f"{label} Vinyl Records", path=path, body=body, nav="genres", ld=ld,
            desc=f"{label} vinyl records from Music Ed Cafe, an independent record seller in Lynn, MA. "
                 f"{len(act)} available now, {len(arc)} recently sold."), priority="0.7")

    def index_page(self, kind: str):
        meta = self.genre_meta() if kind == "genre" else self.format_meta()
        title = "Genres" if kind == "genre" else "Formats"
        other = ("format", "Browse by format") if kind == "genre" else ("genre", "Browse by genre")
        tiles = "".join(
            f'<li><a class="genre-tile g-{i % 6}" href="/{kind}/{s}/"><span class="genre-name">{esc(l)}</span>'
            f'<span class="genre-count">{sum(1 for r in rs if r["status"] == "active")} available · {sum(1 for r in rs if r["status"] != "active")} sold</span></a></li>'
            for i, (s, l, rs) in enumerate(meta))
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), (title, f"/{kind}/")])}
  <h1>Browse by {title[:-1].lower()}</h1>
  <p class="lede">Collections are built from the genre and format details in each eBay listing. <a href="/{other[0]}/">{other[1]}</a>.</p>
</div>
<div class="wrap"><ul class="genre-grid">{tiles}</ul></div>"""
        self.write(f"/{kind}/", self.layout(title=f"{title}: Vinyl Records by {title[:-1]}", path=f"/{kind}/", body=body,
                                            nav="genres", desc=f"Browse Music Ed Cafe vinyl by {title[:-1].lower()}."),
                   priority="0.6")

    def page_guides(self):
        cards = "".join(
            f'<li><a class="guide-card" href="/guides/{g["slug"]}/"><span class="guide-kicker">{esc(g.get("kicker", "Guide"))}</span>'
            f'<span class="guide-title">{esc(g["title"])}</span><span class="guide-desc">{esc(g["description"])}</span></a></li>'
            for g in self.guides)
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), ("Guides", "/guides/")])}
  <h1>Record collecting &amp; care guides</h1>
  <p class="lede">Practical, no-nonsense guides for looking after records and buying them with confidence.</p>
</div>
<div class="wrap"><ul class="guide-grid">{cards}</ul></div>"""
        self.write("/guides/", self.layout(title="Record Collecting & Care Guides", path="/guides/", nav="guides", body=body,
                                           desc="How to clean and store vinyl, what record grades mean, setting up a first turntable, and spotting first pressings."),
                   priority="0.7")
        for i, g in enumerate(self.guides):
            path = f"/guides/{g['slug']}/"
            crumbs = [("Home", "/"), ("Guides", "/guides/"), (g["title"], path)]
            nxt = self.guides[(i + 1) % len(self.guides)]
            body = f"""<div class="wrap narrow">
  {self.crumbs_html(crumbs)}
  <article class="prose selectable">
    <p class="eyebrow">{esc(g.get("kicker", "Guide"))}</p>
    <h1>{esc(g["title"])}</h1>
    <p class="lede">{esc(g["description"])}</p>
    {g["body"]}
  </article>
  <aside class="next-guide"><p>Next guide</p><a href="/guides/{nxt['slug']}/">{esc(nxt['title'])}</a></aside>
</div>"""
            ld = [{"@context": "https://schema.org", "@type": "Article", "headline": g["title"],
                   "description": g["description"], "url": self.abs(path),
                   "author": {"@type": "Organization", "name": "Music Ed Cafe", "@id": self.abs("/#org")},
                   "publisher": {"@type": "Organization", "name": "Music Ed Cafe", "@id": self.abs("/#org")},
                   "datePublished": g.get("date", "2026-10-18"), "image": self.abs("/img/og.jpg")},
                  self.breadcrumb_ld(crumbs)]
            self.write(path, self.layout(title=g["title"], path=path, nav="guides", body=body, ld=ld,
                                         desc=g["description"], og_type="article", page_class="guide"),
                       priority="0.6")

    # ------------------------------------------------------------------ blog (Instagram posts)
    def _load_posts(self) -> list[dict]:
        f = CONTENT / "blog" / "posts.json"
        if not f.exists():
            return []
        import re as _re
        out = []
        for p in json.loads(f.read_text(encoding="utf-8")).get("posts", []):
            url = (p.get("instagram_url") or "").strip()
            m = _re.match(r"^https://(?:www\.)?instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)/?", url)
            if not m or not p.get("slug") or not p.get("title"):
                continue  # invalid entries are skipped, never rendered half-broken
            p = dict(p, instagram_url=f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/")
            out.append(p)
        # Auto posts from the Instagram API (mec/instagram.py). Manual entries win on the same permalink.
        from . import instagram
        have = {p["instagram_url"] for p in out}
        for ig in instagram.cached_posts():
            m = _re.match(r"^https://(?:www\.)?instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)/?", ig.get("permalink") or "")
            if not m:
                continue
            url = f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/"
            if url in have:
                continue
            cap = (ig.get("caption") or "").strip()
            first = next((l.strip() for l in cap.splitlines() if l.strip() and not l.strip().startswith("#")), "")
            title = (first[:70].rsplit(" ", 1)[0] + "…") if len(first) > 70 else (first or "New from the crates")
            note = "\n\n".join(l.strip() for l in cap.split("\n\n") if l.strip())
            date = (ig.get("timestamp") or "")[:10]
            out.append({"slug": f"ig-{date}-{m.group(2).lower()[:11]}", "date": date, "title": title,
                        "instagram_url": url, "note": note, "records": [], "auto": True,
                        "image": ig.get("image") if ig.get("image") and (ROOT / "data" / "assets" / "instagram" / f"{ig['image']}-480.webp").exists() else None,
                        "media_type": ig.get("media_type")})
        return sorted(out, key=lambda p: p.get("date") or "", reverse=True)

    def post_records(self, p: dict) -> list[dict]:
        want = [str(i) for i in p.get("records") or []]
        by_id = {str(r["id"]): r for r in self.active}
        return [by_id[i] for i in want if i in by_id]

    def ig_photo(self, p: dict) -> str:
        if not p.get("image"):
            return ""
        alt = esc((p.get("title") or "Instagram post")[:120])
        badge = '<span class="ig-badge">Video on Instagram</span>' if p.get("media_type") == "VIDEO" else ""
        return (f'<figure class="ig-photo"><a href="{esc(p["instagram_url"])}" rel="noopener">'
                f'<img src="/img/ig/{esc(p["image"])}-1080.webp" srcset="/img/ig/{esc(p["image"])}-480.webp 480w, /img/ig/{esc(p["image"])}-1080.webp 1080w" '
                f'sizes="(max-width: 600px) 100vw, 540px" alt="{alt}" decoding="async">{badge}</a></figure>')

    def ig_embed(self, p: dict) -> str:
        u = esc(p["instagram_url"])
        return (f'<div class="ig-embed"><blockquote class="instagram-media" data-instgrm-permalink="{u}?utm_source=ig_embed" '
                f'data-instgrm-version="14"><a href="{u}" rel="noopener">View this post on Instagram</a></blockquote></div>')

    def page_blog(self):
        ig = (self.cfg.get("contact") or {}).get("instagram")
        follow = (f'<p class="empty-actions"><a class="btn btn-buy" href="{esc(ig)}" rel="noopener me">Follow @musicedcafe on Instagram</a></p>'
                  if ig else "")
        if self.posts:
            cards = "".join(
                f'<li><a class="guide-card blog-card" href="/blog/{esc(p["slug"])}/">'
                + (f'<img class="blog-thumb" src="/img/ig/{esc(p["image"])}-480.webp" alt="" width="480" height="480" loading="lazy" decoding="async">' if p.get("image") else "")
                + f'<span class="guide-kicker">{esc(p.get("date") or "")}</span>'
                f'<span class="guide-title">{esc(p["title"])}</span>'
                f'<span class="guide-desc">{esc((p.get("note") or "From the Music Ed Cafe Instagram")[:140])}</span></a></li>'
                for p in self.posts)
            listing = f'<ul class="guide-grid">{cards}</ul>{follow}'
        else:
            listing = ('<div class="empty-state"><p>New posts from the crates are on the way. '
                       'In the meantime, see what just came in on Instagram.</p>' + follow + '</div>')
        body = f"""<div class="wrap page-head">
  {self.crumbs_html([("Home", "/"), ("Blog", "/blog/")])}
  <h1>From the Crates</h1>
  <p class="lede">New finds, crate digs and records worth knowing about, straight from the Music Ed Cafe Instagram.</p>
</div>
<div class="wrap">{listing}</div>"""
        self.write("/blog/", self.layout(title="From the Crates: the Music Ed Cafe Blog", path="/blog/", nav="blog", body=body,
                                         desc="New vinyl finds, crate digs and record stories from Music Ed Cafe in Lynn, MA."),
                   priority="0.6")
        embed_js = '<script async src="https://www.instagram.com/embed.js"></script>'
        for p in self.posts:
            path = f"/blog/{p['slug']}/"
            crumbs = [("Home", "/"), ("Blog", "/blog/"), (p["title"], path)]
            recs = self.post_records(p)
            in_post = (f'<section class="section" aria-labelledby="h-inpost"><h2 id="h-inpost">In this post</h2>{self.grid(recs)}</section>'
                       if recs else "")
            note = "".join(f"<p>{esc(x)}</p>" for x in (p.get("note") or "").split("\n\n") if x.strip())
            body = f"""<div class="wrap narrow">
  {self.crumbs_html(crumbs)}
  <article class="prose selectable">
    <p class="eyebrow">{esc(p.get("date") or "")}</p>
    <h1>{esc(p["title"])}</h1>
    {self.ig_photo(p)}
    {note}
    {self.ig_embed(p)}
  </article>
  {in_post}
  {follow}
</div>"""
            ld = [{"@context": "https://schema.org", "@type": "BlogPosting", "headline": p["title"],
                   "url": self.abs(path), "datePublished": p.get("date"),
                   "author": {"@type": "Organization", "name": "Music Ed Cafe", "@id": self.abs("/#org")}},
                  self.breadcrumb_ld(crumbs)]
            self.write(path, self.layout(title=p["title"], path=path, nav="blog", body=body + embed_js, ld=ld,
                                         desc=(p.get("note") or p["title"])[:155], og_type="article", page_class="guide"),
                       priority="0.5")

    # ------------------------------------------------------------------ merch (preorder reservations)
    MERCH_COPY = ("Preorder reservations. No payment now. We'll email you when your item is ready to pay for "
                  "and ship. Reservations aren't binding.")
    MERCH_MOCKUP = "Mockup, final product may vary"

    def _load_merch(self) -> dict:
        f = CONTENT / "merch" / "merch.json"
        if not f.exists():
            return {"collections": [], "items": []}
        cat = json.loads(f.read_text(encoding="utf-8"))
        cols = {c["slug"]: c for c in cat.get("collections", [])}
        for it in cat.get("items", []):
            it["path"] = f"/merch/{it['slug']}/"
            it["col"] = cols.get(it.get("collection"), {"name": "", "slug": ""})
        return cat

    def merch_price(self, it: dict) -> str:
        return money(it["price"], self.merch.get("currency", "USD")) if it.get("price") not in (None, "") else "Price TBA"

    def merch_eta(self, it: dict) -> str:
        return it.get("eta") or "Pressing soon"

    def merch_img(self, it: dict, eager=False, sizes="(max-width: 600px) 46vw, 260px") -> str:
        b = f"/img/merch/{esc(it['image'])}"
        load = 'fetchpriority="high" decoding="async"' if eager else 'loading="lazy" decoding="async"'
        return (f'<img class="merch-img" src="{b}-960.webp" srcset="{b}-480.webp 480w, {b}-960.webp 960w" '
                f'sizes="{sizes}" width="960" height="960" alt="{esc(it.get("image_alt") or it["name"])}" {load}>')

    def merch_card(self, it: dict) -> str:
        return f"""<li class="card merch-card">
  <a class="card-link" href="{it['path']}">
    <span class="merch-art">{self.merch_img(it)}<span class="mock-tag">Mockup</span></span>
    <span class="card-body">
      <span class="card-title">{esc(it['name'])}</span>
      <span class="card-meta">{esc(it.get('type', ''))} · {esc(self.merch_eta(it))}</span>
      <span class="card-price">{esc(self.merch_price(it))}</span>
      <span class="merch-cta">Reserve yours</span>
    </span>
  </a>
</li>"""

    def merch_strip(self) -> str:
        items = self.merch.get("items") or []
        if not items:
            return ""
        thumbs = "".join(f'<span class="ms-thumb">{self.merch_img(it, sizes="72px")}</span>' for it in items[:4])
        return f"""<section class="merch-strip-wrap" aria-label="Merch preorders"><div class="wrap">
  <a class="merch-strip" href="/merch/">
    <span class="ms-thumbs" aria-hidden="true">{thumbs}</span>
    <span class="ms-copy"><span class="ms-kicker">New</span><span class="ms-h">Music Ed Cafe merch, reserve yours</span>
    <span class="ms-sub">Shirts, mugs, dad hats and tote bags. No payment now.</span></span>
    <span class="ms-go" aria-hidden="true">&rarr;</span>
  </a>
</div></section>"""

    def merch_notice(self) -> str:
        return f'<div class="preorder-note" role="note"><p class="pn-h">How preorders work</p><p>{esc(self.MERCH_COPY)}</p></div>'

    def merch_form(self, it: dict) -> str:
        if it.get("status") != "preorder":
            return '<div class="empty-state small"><p>Reservations for this item are closed right now.</p></div>'
        opts = []
        for o in it.get("options") or []:
            vals = o.get("values") or []
            nm = f"opt_{o['name']}"
            if len(vals) == 1:
                opts.append(f'<div class="rf-field"><span class="rf-label">{esc(o["label"])}</span>'
                            f'<span class="rf-static">{esc(vals[0])}</span><input type="hidden" name="{nm}" value="{esc(vals[0])}"></div>')
                continue
            chips = "".join(
                f'<label class="chip-opt"><input type="radio" name="{nm}" value="{esc(v)}" required>'
                f'<span>{esc(v)}</span></label>' for v in vals)
            opts.append(f'<fieldset class="rf-field rf-opts" data-field="{nm}"><legend class="rf-label">{esc(o["label"])}</legend>'
                        f'<div class="chip-opts">{chips}</div><p class="rf-err" id="err-{nm}" hidden></p></fieldset>')
        qty = "".join(f'<option value="{n}">{n}</option>' for n in range(1, 11))
        return f"""<form class="reserve-form" id="reserve-form" method="post" action="/merch/reserve" data-merch-form>
  <h2 id="reserve">Reserve this item</h2>
  <input type="hidden" name="item" value="{esc(it['slug'])}">
  <p class="rf-item">Reserving: <strong>{esc(it['name'])}</strong></p>
  <div class="rf-summary" role="alert" tabindex="-1" hidden></div>
  {''.join(opts)}
  <div class="rf-field" data-field="qty"><label class="rf-label" for="rf-qty">Quantity</label>
    <select id="rf-qty" name="qty">{qty}</select><p class="rf-err" id="err-qty" hidden></p></div>
  <div class="rf-field" data-field="name"><label class="rf-label" for="rf-name">Your name</label>
    <input id="rf-name" name="name" type="text" autocomplete="name" maxlength="80" required><p class="rf-err" id="err-name" hidden></p></div>
  <div class="rf-field" data-field="email"><label class="rf-label" for="rf-email">Email</label>
    <input id="rf-email" name="email" type="email" autocomplete="email" inputmode="email" maxlength="254" required>
    <p class="rf-hint">Only used to contact you about this reservation.</p><p class="rf-err" id="err-email" hidden></p></div>
  <div class="rf-field" data-field="note"><label class="rf-label" for="rf-note">Note <span class="rf-opt">(optional)</span></label>
    <textarea id="rf-note" name="note" rows="3" maxlength="500"></textarea><p class="rf-err" id="err-note" hidden></p></div>
  <div class="rf-hp" aria-hidden="true"><label for="rf-website">Leave this field empty</label>
    <input id="rf-website" name="website" type="text" tabindex="-1" autocomplete="off"></div>
  <div class="rf-field" data-field="consent"><label class="rf-check"><input type="checkbox" name="consent" value="yes" required>
    <span>Email me about my reservation</span></label><p class="rf-err" id="err-consent" hidden></p></div>
  <button class="btn btn-buy btn-big rf-submit" type="submit">Reserve, no payment now</button>
  <p class="rf-fine">{esc(self.MERCH_COPY)}</p>
</form>
<div class="reserve-done" id="reserve-done" tabindex="-1" hidden></div>"""

    def page_merch(self):
        cat = self.merch
        items = cat.get("items") or []
        if not items:
            return
        crumbs = [("Home", "/"), ("Merch", "/merch/")]
        sections = []
        for c in cat.get("collections", []):
            its = [it for it in items if it.get("collection") == c["slug"]]
            if not its:
                continue
            sections.append(f"""<section class="section" id="{esc(c['slug'])}" aria-labelledby="h-{esc(c['slug'])}">
  <div class="section-head"><h2 id="h-{esc(c['slug'])}">{esc(c['name'])}</h2></div>
  <p class="section-sub">{esc(c.get('description', ''))}</p>
  <ul class="grid merch-grid">{''.join(self.merch_card(it) for it in its)}</ul>
</section>""")
        body = f"""<div class="wrap page-head">
  {self.crumbs_html(crumbs)}
  <p class="eyebrow">Preorders open</p>
  <h1>Music Ed Cafe Merch</h1>
  <p class="lede">Shirts, mugs, dad hats and reusable tote bags with the Music Ed Cafe logo, in two collections. We're pressing them now. Reserve yours and we'll get in touch when it's ready.</p>
  {self.merch_notice()}
</div>
<div class="wrap">
  {''.join(sections)}
  <p class="mock-note">Product photos are mockups. Final product may vary.</p>
</div>"""
        ld = [self.breadcrumb_ld(crumbs),
              {"@context": "https://schema.org", "@type": "CollectionPage", "name": "Music Ed Cafe Merch",
               "url": self.abs("/merch/"),
               "mainEntity": {"@type": "ItemList", "itemListElement": [
                   {"@type": "ListItem", "position": i + 1, "url": self.abs(it["path"]), "name": it["name"]}
                   for i, it in enumerate(items)]}}]
        self.write("/merch/", self.layout(
            title="Music Ed Cafe Merch: Shirts, Mugs, Hats & Totes (Preorder)", path="/merch/", nav="merch",
            body=body, ld=ld, page_class="merch",
            og_image=self.abs(f"/img/merch/{items[0]['image']}-800.jpg"),
            desc="Reserve Music Ed Cafe merch: t-shirts, mugs, dad hats and tote bags in the Keep the Coffee Flowin "
                 "and Spin It. Feel It. Live It. collections. No payment now."), priority="0.8")
        for it in items:
            self.page_merch_item(it, items)
        self.page_merch_thanks()

    def page_merch_item(self, it: dict, items: list[dict]):
        path = it["path"]
        crumbs = [("Home", "/"), ("Merch", "/merch/"), (it["name"], path)]
        opts = "".join(f'<div><dt>{esc(o["label"])}</dt><dd>{esc(", ".join(o.get("values") or []))}</dd></div>'
                       for o in it.get("options") or [])
        same = [x for x in items if x["slug"] != it["slug"] and x.get("collection") == it.get("collection")]
        other = [x for x in items if x["slug"] != it["slug"] and x.get("type") == it.get("type")]
        more = "".join(self.merch_card(x) for x in (same + other)[:4])
        body = f"""<div class="wrap">
  {self.crumbs_html(crumbs)}
  <article class="record merch-item">
    <div class="record-media">
      <figure class="merch-main">{self.merch_img(it, eager=True, sizes="(max-width: 760px) 92vw, 520px")}
        <figcaption class="mock-cap">{esc(self.MERCH_MOCKUP)}</figcaption></figure>
    </div>
    <div class="record-info">
      <p class="eyebrow">{esc(it['col'].get('name', ''))}</p>
      <h1>{esc(it['name'])}</h1>
      <p class="record-sub">{esc(it.get('type', ''))} · Preorder</p>
      <div class="buy-box merch-box">
        <p class="price">{esc(self.merch_price(it))}</p>
        <p class="merch-eta"><strong>{esc(self.merch_eta(it))}</strong></p>
        <p class="buy-note">{esc(self.MERCH_COPY)}</p>
      </div>
      <p class="blurb selectable">{esc(it.get('description', ''))}</p>
      {f'<dl class="specs selectable">{opts}</dl>' if opts else ''}
      {self.merch_form(it)}
    </div>
  </article>
  {f'<section class="section" aria-labelledby="h-more"><div class="section-head"><h2 id="h-more">More merch</h2><a href="/merch/">See all</a></div><ul class="grid merch-grid">{more}</ul></section>' if more else ''}
</div>"""
        imgs = [self.abs(f"/img/merch/{it['image']}-960.webp"), self.abs(f"/img/merch/{it['image']}-800.jpg")]
        product = {"@context": "https://schema.org", "@type": "Product", "name": it["name"], "url": self.abs(path),
                   "sku": it["slug"], "image": imgs, "description": it.get("description", ""),
                   "brand": {"@type": "Brand", "name": "Music Ed Cafe"}, "category": it.get("type", "Merchandise")}
        if it.get("price") not in (None, ""):
            product["offers"] = {"@type": "Offer", "url": self.abs(path), "price": str(it["price"]),
                                 "priceCurrency": self.merch.get("currency", "USD"),
                                 "availability": "https://schema.org/PreOrder",
                                 "seller": {"@type": "Organization", "name": "Music Ed Cafe", "@id": self.abs("/#org")}}
        price_txt = self.merch_price(it)
        self.write(path, self.layout(
            title=f"{it['name']} (Preorder)", path=path, nav="merch", body=body, page_class="merch-page",
            ld=[product, self.breadcrumb_ld(crumbs)], og_image=imgs[1], og_type="product",
            desc=f"Reserve the Music Ed Cafe {it['name']}. {price_txt}, {self.merch_eta(it).lower()}. "
                 "No payment now, we'll email you when it's ready."), priority="0.7")

    def page_merch_thanks(self):
        """Template for the reservation result page. The admin sidecar fills the
        <!--MEC:RESULT--> block when it serves GET /merch/reserve (works without JS)."""
        body = f"""<div class="wrap narrow page-head">
  {self.crumbs_html([("Home", "/"), ("Merch", "/merch/"), ("Reservation", "/merch/thanks/")])}
  <!--MEC:RESULT--><div class="reserve-done">
    <h1>Merch reservations</h1>
    <p class="lede">Pick an item on the merch page to reserve it. No payment now.</p>
  </div><!--/MEC:RESULT-->
  <p class="empty-actions"><a class="btn btn-buy" href="/merch/">Back to merch</a> <a class="btn btn-ghost" href="/shop/">Shop records</a></p>
</div>"""
        self.write("/merch/thanks/", self.layout(title="Merch reservation", path="/merch/thanks/", nav="merch",
                                                 body=body, desc="Your Music Ed Cafe merch reservation.",
                                                 noindex=True, page_class="merch"), sitemap=False)

    # ------------------------------------------------------------------ sell your records
    def _load_sell(self) -> dict:
        f = CONTENT / "sell" / "sell.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def sell_row(self, i: int) -> str:
        """One record row of the sell form (rows 1..FORM_ROWS are rendered here; JS clones more)."""
        return f"""<fieldset class="sf-rec" data-row="{i}">
      <legend class="rf-label">Record <span class="sf-n">{i}</span></legend>
      <div class="sf-grid">
        <div class="rf-field"><label class="rf-label" for="sf-r{i}-artist">Artist</label>
          <input id="sf-r{i}-artist" name="r{i}_artist" type="text" maxlength="120" autocomplete="off"></div>
        <div class="rf-field"><label class="rf-label" for="sf-r{i}-title">Album or song title</label>
          <input id="sf-r{i}-title" name="r{i}_title" type="text" maxlength="160" autocomplete="off"></div>
        <div class="rf-field"><label class="rf-label" for="sf-r{i}-catno">Catalog number <span class="rf-opt">(optional, on the spine or label)</span></label>
          <input id="sf-r{i}-catno" name="r{i}_catno" type="text" maxlength="40" autocomplete="off"></div>
      </div>
      <div class="sf-photos">
        <div class="rf-field sf-file"><label class="rf-label" for="sf-r{i}-front">Photo: jacket front</label>
          <input id="sf-r{i}-front" name="r{i}_front" type="file" accept="image/*"></div>
        <div class="rf-field sf-file"><label class="rf-label" for="sf-r{i}-label">Photo: vinyl label and surface</label>
          <input id="sf-r{i}-label" name="r{i}_label" type="file" accept="image/*"></div>
        <div class="rf-field sf-file"><label class="rf-label" for="sf-r{i}-back">Photo: jacket back <span class="rf-opt">(optional)</span></label>
          <input id="sf-r{i}-back" name="r{i}_back" type="file" accept="image/*"></div>
      </div>
      <p class="rf-err" id="err-r{i}" hidden></p>
    </fieldset>"""

    def sell_form(self, cfg: dict) -> str:
        from . import sell as sellmod
        counts = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cfg["count_options"])
        conds = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cfg["condition_options"])
        fmts = "".join(f'<label class="chip-opt"><input type="checkbox" name="formats" value="{esc(f)}">'
                       f'<span>{esc(f)}</span></label>' for f in cfg["format_options"])
        hand = "".join(f'<label class="rf-check sf-radio"><input type="radio" name="handoff" value="{esc(k)}" required>'
                       f'<span>{esc(v)}</span></label>' for k, v in sellmod.handoff_options(cfg).items())
        rows = "\n    ".join(self.sell_row(i) for i in range(1, sellmod.FORM_ROWS + 1))
        return f"""<form class="reserve-form sell-form" id="sell-form" method="post" action="/sell/submit" enctype="multipart/form-data" data-sell-form data-max-rows="{sellmod.MAX_RECORDS}" data-max-overview="{sellmod.MAX_OVERVIEW}">
  <h2>Tell us about your records</h2>
  <p class="rf-fine">Your details, then a photo of each record's jacket and its vinyl so we can grade it. Nothing is final until we've seen the records.</p>
  <div class="rf-summary" role="alert" tabindex="-1" hidden></div>
  <div class="rf-field" data-field="name"><label class="rf-label" for="sf-name">Your name</label>
    <input id="sf-name" name="name" type="text" autocomplete="name" maxlength="80" required><p class="rf-err" id="err-name" hidden></p></div>
  <div class="rf-field" data-field="email"><label class="rf-label" for="sf-email">Email</label>
    <input id="sf-email" name="email" type="email" autocomplete="email" inputmode="email" maxlength="254" required>
    <p class="rf-hint">Only used to reply about your records.</p><p class="rf-err" id="err-email" hidden></p></div>
  <div class="rf-field" data-field="phone"><label class="rf-label" for="sf-phone">Phone <span class="rf-opt">(optional)</span></label>
    <input id="sf-phone" name="phone" type="tel" autocomplete="tel" inputmode="tel" maxlength="30"><p class="rf-err" id="err-phone" hidden></p></div>
  <div class="rf-field" data-field="location"><label class="rf-label" for="sf-location">City or ZIP code</label>
    <input id="sf-location" name="location" type="text" autocomplete="postal-code" maxlength="80" required>
    <p class="rf-hint">Helps us plan the shipping label or a meetup. No street address needed.</p><p class="rf-err" id="err-location" hidden></p></div>
  <div class="rf-field" data-field="count"><label class="rf-label" for="sf-count">About how many records?</label>
    <select id="sf-count" name="count" required><option value="">Choose one</option>{counts}</select><p class="rf-err" id="err-count" hidden></p></div>
  <fieldset class="rf-field rf-opts" data-field="formats"><legend class="rf-label">Formats (tick all that apply)</legend>
    <div class="chip-opts">{fmts}</div><p class="rf-err" id="err-formats" hidden></p></fieldset>
  <div class="rf-field" data-field="genres"><label class="rf-label" for="sf-genres">Genres or artists <span class="rf-opt">(optional)</span></label>
    <input id="sf-genres" name="genres" type="text" maxlength="300" placeholder="Soul, jazz, some Motown..."><p class="rf-err" id="err-genres" hidden></p></div>
  <div class="rf-field" data-field="condition"><label class="rf-label" for="sf-condition">Overall condition</label>
    <select id="sf-condition" name="condition" required><option value="">Choose one</option>{conds}</select><p class="rf-err" id="err-condition" hidden></p></div>
  <fieldset class="rf-field" data-field="handoff"><legend class="rf-label">How would the records get to us?</legend>
    <div class="sf-radios">{hand}</div><p class="rf-err" id="err-handoff" hidden></p></fieldset>
  <section class="sf-records" aria-labelledby="h-sf-records" data-field="records">
    <h3 id="h-sf-records">Your records</h3>
    <p class="rf-hint">One row per record: the artist, the title, a photo of the <strong>jacket front</strong> and a photo of the <strong>vinyl label and playing surface</strong> (out of the sleeve, in good light). A photo of the jacket back helps too. Fill in as many as you like, up to {sellmod.MAX_RECORDS}; empty rows are ignored.</p>
    <p class="rf-err" id="err-records" hidden></p>
    <div class="sf-rows" data-rows>
    {rows}
    </div>
    <p class="sf-add-wrap" hidden><button class="btn btn-ghost sf-add" type="button" data-add-row>Add another record</button> <span class="rf-hint sf-count" aria-live="polite"></span></p>
  </section>
  <div class="rf-field" data-field="overview"><label class="rf-label" for="sf-overview">Overview photos <span class="rf-opt">(optional, up to {sellmod.MAX_OVERVIEW})</span></label>
    <input id="sf-overview" name="overview" type="file" accept="image/*" multiple>
    <p class="rf-hint">For 40+ records, send a few crate/spine photos and we'll ask for details on the ones we're interested in. With 25 or more records, overview photos can stand in for the rows above.</p><p class="rf-err" id="err-overview" hidden></p></div>
  <div class="rf-field" data-field="photos"><label class="rf-label" for="sf-photos">Photo album link <span class="rf-opt">(optional)</span></label>
    <textarea id="sf-photos" name="photos" rows="2" maxlength="1000" placeholder="Google Photos, iCloud or Dropbox link"></textarea>
    <p class="rf-err" id="err-photos" hidden></p></div>
  <div class="rf-field" data-field="note"><label class="rf-label" for="sf-note">Anything else? <span class="rf-opt">(optional)</span></label>
    <textarea id="sf-note" name="note" rows="3" maxlength="1000"></textarea><p class="rf-err" id="err-note" hidden></p></div>
  <div class="rf-hp" aria-hidden="true"><label for="sf-website">Leave this field empty</label>
    <input id="sf-website" name="website" type="text" tabindex="-1" autocomplete="off"></div>
  <div class="rf-field" data-field="consent"><label class="rf-check"><input type="checkbox" name="consent" value="yes" required>
    <span>Music Ed Cafe may contact me about these records</span></label><p class="rf-err" id="err-consent" hidden></p></div>
  <div class="sf-progress" data-progress hidden><div class="sf-bar"><span data-bar></span></div><p class="rf-hint" data-progress-text aria-live="polite"></p></div>
  <button class="btn btn-buy btn-big rf-submit" type="submit">Send my records</button>
  <p class="rf-fine">This form is private: your details and photos go to Music Ed Cafe only and are never shown on the site. Photos up to 8 MB each.</p>
</form>
<div class="reserve-done" id="sell-done" tabindex="-1" hidden></div>"""

    def page_sell(self):
        cfg = self._load_sell()
        postage = cfg.get("postage")
        postage_txt = esc(postage) if postage else "We'll sort out shipping together when we make an offer."
        pickup = ('<p>Local pickup or drop-off around Lynn and the North Shore can be arranged by appointment.</p>'
                  if cfg.get("local_pickup") is True else "")
        crumbs = [("Home", "/"), ("Sell Your Records", "/sell/")]
        body = f"""<div class="wrap narrow page-head">
  {self.crumbs_html(crumbs)}
  <h1>Sell Us Your Records</h1>
  <p class="lede">Music Ed Cafe buys record collections and lots, and every record is looked at individually. If you have records you'd like to see go to a good home, we'd love to hear about them.</p>
  <p><a class="btn btn-buy btn-big" href="#sell-form">Tell us about your records</a></p>
</div>
<div class="wrap narrow sell-page">
  <section class="sell-sec" aria-labelledby="h-offers">
    <h2 id="h-offers">A fair offer for every record</h2>
    <p>We review every record and make a fair offer. Clear photos of each jacket and its vinyl let us grade them properly and get back to you faster.</p>
    <p>The offer is final once we've seen the records and they match the photos.</p>
  </section>
  <section class="sell-sec" aria-labelledby="h-want">
    <h2 id="h-want">What we're looking for</h2>
    <p><strong>Yes, please:</strong> soul, R&amp;B, jazz, rock, funk, hip-hop and classic pop LPs and 45s in good condition.</p>
    <p><strong>Usually not:</strong> water-damaged or moldy records, heavily scratched records, and common easy-listening or orchestral records in bulk. If you're not sure, ask anyway, we're friendly about it.</p>
  </section>
  <section class="sell-sec" aria-labelledby="h-how">
    <h2 id="h-how">How it works</h2>
    <ol class="steps-list sell-steps">
      <li><span class="step-n" aria-hidden="true">1</span><span><strong>Send us the details and photos.</strong> Use the form below: a photo of each jacket and its vinyl.</span></li>
      <li><span class="step-n" aria-hidden="true">2</span><span><strong>Get an offer.</strong> We'll email you after we've looked things over.</span></li>
      <li><span class="step-n" aria-hidden="true">3</span><span><strong>Ship it or meet up.</strong> Whichever works for the lot.</span></li>
    </ol>
    <p>{postage_txt}</p>
    {pickup}
  </section>
  <section class="sell-sec" aria-labelledby="h-ship">
    <h2 id="h-ship">Packing tips</h2>
    <p>If you're shipping, we'll email you a prepaid USPS Media Mail label, so there's nothing for you to pay. Delivery usually takes about 2&ndash;8 business days.</p>
    <ul>
      <li>Weigh the packed box and tell us the weight, so the label is right.</li>
      <li>Use a strong double-wall box with cushioning on every side.</li>
      <li>Ship records standing vertically, never flat in a stack.</li>
      <li>If the fit is tight, take the records out of their outer sleeves.</li>
    </ul>
  </section>
  {self.sell_form(cfg)}
</div>"""
        ld = [self.breadcrumb_ld(crumbs)]
        self.write("/sell/", self.layout(
            title="Sell Your Records", path="/sell/", nav="sell", body=body, ld=ld, page_class="sell",
            desc="Music Ed Cafe buys record collections and lots and makes a fair offer for every record. "
                 "We cover shipping. Send photos of your records."), priority="0.6")
        thanks = f"""<div class="wrap narrow page-head">
  {self.crumbs_html([("Home", "/"), ("Sell Your Records", "/sell/"), ("Thank you", "/sell/thanks/")])}
  <!--MEC:RESULT--><div class="reserve-done">
    <h1>Sell Your Records</h1>
    <p class="lede">Use the form on the sell page to tell us about your records.</p>
  </div><!--/MEC:RESULT-->
  <p class="empty-actions"><a class="btn btn-buy" href="/sell/">Back to the sell page</a> <a class="btn btn-ghost" href="/shop/">Shop records</a></p>
</div>"""
        self.write("/sell/thanks/", self.layout(title="Sell Your Records: thank you", path="/sell/thanks/", nav="sell",
                                                body=thanks, desc="Your Sell Your Records submission to Music Ed Cafe.",
                                                noindex=True, page_class="sell"), sitemap=False)

    def page_static(self, slug: str, nav: str, title: str, desc: str, heading: str):
        raw = (CONTENT / f"{slug}.html").read_text(encoding="utf-8")
        raw = raw.replace("{{EBAY_STORE_URL}}", esc(self.cfg["ebay_store_url"]))
        raw = raw.replace("{{CONTACT_LINKS}}", self.contact_block())
        path = f"/{slug}/"
        crumbs = [("Home", "/"), (heading, path)]
        body = f"""<div class="wrap narrow">
  {self.crumbs_html(crumbs)}
  <article class="prose selectable">
    <h1>{esc(heading)}</h1>
    {raw}
  </article>
</div>"""
        ld = [self.breadcrumb_ld(crumbs)]
        if slug == "about":
            ld.append(self.org_ld())
        self.write(path, self.layout(title=title, path=path, nav=nav, body=body, desc=desc, ld=ld), priority="0.5")

    def contact_block(self) -> str:
        c = self.cfg.get("contact", {})
        rows = []
        if c.get("email"):
            rows.append(f'<li><strong>Email:</strong> <a href="mailto:{esc(c["email"])}">{esc(c["email"])}</a></li>')
        for key, label in (("instagram", "Instagram"), ("facebook", "Facebook"), ("tiktok", "TikTok"), ("youtube", "YouTube")):
            if c.get(key):
                rows.append(f'<li><strong>{label}:</strong> <a href="{esc(c[key])}" rel="noopener me">{esc(c[key])}</a></li>')
        if not rows:
            return ('<p class="placeholder-note"><strong>Coming soon:</strong> an email address and social links. '
                    'Until then, eBay messages are the fastest way to reach Music Ed Cafe.</p>')
        return "<ul>" + "".join(rows) + "</ul>"

    def page_404(self):
        body = f"""<div class="wrap narrow page-head">
  <h1>That page skipped</h1>
  <p class="lede">We couldn't find that page. The record may have been renamed, or the link might be mistyped.</p>
  <p class="empty-actions"><a class="btn btn-buy" href="/shop/">Shop all records</a> <a class="btn btn-ghost" href="/sold/">Recently sold</a> <a class="btn btn-ghost" href="/">Home</a></p>
</div>"""
        self.write("/404.html", self.layout(title="Page not found", path="/404.html", body=body,
                                            desc="Page not found.", noindex=True), sitemap=False)

    def search_index(self):
        idx = []
        for r in self.visible:
            text = " ".join(str(x) for x in [r["display_title"], r["ebay_title"], r.get("artist"), r.get("album"),
                                              r.get("label"), r.get("catalog_number"), " ".join(r["genres"]),
                                              r.get("release_year") or r.get("listing_year"), self.format_label(r)] if x)
            idx.append({"id": r["id"], "path": r["path"], "title": r["display_title"], "artist": r.get("artist"),
                        "status": r["status"], "price": r["price"], "currency": r["currency"],
                        "genres": r["genre_slugs"], "formats": r["format_slugs"],
                        "grade": r.get("media_grade"), "year": r.get("release_year") or r.get("listing_year"),
                        "date": (r.get("sold_at") or r.get("start_date") or "")[:10],
                        "text": text.lower()})
        self.write("/search-index.json", json.dumps({"generated": self.state.get("last_success"), "records": idx},
                                                    ensure_ascii=False, separators=(",", ":")), sitemap=False)

    def sitemap(self):
        today = datetime.utcnow().date().isoformat()
        rows = "".join(f"<url><loc>{esc(self.abs(p))}</loc><lastmod>{today}</lastmod>"
                       f"{f'<priority>{pr}</priority>' if pr else ''}</url>"
                       for p, pr in self.urls if not p.endswith(".json"))
        self.write("/sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
                   f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>\n', sitemap=False)
        self.write("/robots.txt", f"User-agent: *\nAllow: /\nDisallow: /admin/\n\nSitemap: {self.abs('/sitemap.xml')}\n",
                   sitemap=False)

    def copy_static(self):
        for p in STATIC.rglob("*"):
            if p.is_file():
                dest = self.out / p.relative_to(STATIC)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        ph = ROOT / "data" / "assets" / "photos"
        if ph.exists():
            dest = self.out / "img" / "photo"
            dest.mkdir(parents=True, exist_ok=True)
            for p in ph.glob("*.webp"):
                shutil.copy2(p, dest / p.name)
        igd = ROOT / "data" / "assets" / "instagram"
        if igd.exists():
            dest = self.out / "img" / "ig"
            dest.mkdir(parents=True, exist_ok=True)
            for p in igd.glob("*.webp"):
                shutil.copy2(p, dest / p.name)
        cov = ROOT / "data" / "assets" / "coverart"
        if cov.exists():
            dest = self.out / "img" / "cover"
            dest.mkdir(parents=True, exist_ok=True)
            for p in cov.glob("*.webp"):
                shutil.copy2(p, dest / p.name)
        img = STATIC / "img"
        for name, src in (("favicon.png", "favicon.png"), ("favicon.ico", "favicon.png"),
                          ("apple-touch-icon.png", "apple-touch-icon.png"),
                          ("apple-touch-icon-precomposed.png", "apple-touch-icon.png")):
            shutil.copy2(img / src, self.out / name)

    def build(self):
        self.copy_static()
        self.page_home()
        self.page_shop()
        self.page_sold()
        self.page_featured()
        for r in self.visible:
            self.page_record(r)
        for slug, label, recs in self.genre_meta():
            self.collection_page("genre", slug, label, recs)
        for slug, label, recs in self.format_meta():
            self.collection_page("format", slug, label, recs)
        self.index_page("genre")
        self.index_page("format")
        self.page_guides()
        self.page_blog()
        self.page_merch()
        self.page_sell()
        self.page_static("about", "about", "About Music Ed Cafe",
                         "Music Ed Cafe is an independent vinyl record seller in Lynn, Massachusetts. Good coffee, good music, good people.",
                         "About Music Ed Cafe")
        self.page_static("contact", "contact", "Contact Music Ed Cafe",
                         "How to reach Music Ed Cafe: message through eBay for questions about any record or order.",
                         "Contact")
        self.page_static("buying-on-ebay", "", "How Buying Works",
                         "Every Music Ed Cafe purchase is completed on eBay, which handles payment, shipping and returns.",
                         "How buying works")
        self.page_404()
        self.search_index()
        self.sitemap()
        return {"records": len(self.records), "active": len(self.active), "sold": len(self.sold),
                "archived": len(self.archive), "pages": len(self.urls)}


def build() -> dict:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    tmp = PUBLIC / f".build-{stamp}-{os.getpid()}"
    live, old = PUBLIC / "live", PUBLIC / f".old-{stamp}-{os.getpid()}"
    try:
        stats = Site(tmp).build()
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if live.exists():
        os.replace(live, old)
    os.replace(tmp, live)
    shutil.rmtree(old, ignore_errors=True)
    for p in PUBLIC.glob(".build-*"):
        if p.is_dir() and time.time() - p.stat().st_mtime > 3600:
            shutil.rmtree(p, ignore_errors=True)
    return stats

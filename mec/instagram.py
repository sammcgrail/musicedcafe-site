"""Instagram auto-import for the blog ("From the Crates").

Uses the Instagram API with Instagram Login (graph.instagram.com), reading ONLY
Edwin's own account. Credentials: IG_ACCESS_TOKEN (+ optional IG_USER_ID) in .env.
Long-lived tokens last 60 days; we refresh weekly and keep the newest token in
data/instagram/token.json (gitignored, mode 600) so .env never needs rewriting.

Safety: a failed fetch changes nothing; posts.json is only replaced by a
successful response. Tokens are never logged or returned.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import DATA, atomic_write, load_json, now_iso

IG_DIR = DATA / "instagram"
IMG_DIR = DATA / "assets" / "instagram"  # self-hosted copies (IG CDN urls expire); gitignored
POSTS = IG_DIR / "posts.json"
TOKEN = IG_DIR / "token.json"
STATE = IG_DIR / "state.json"
API = "https://graph.instagram.com"
FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
REFRESH_EVERY = 7 * 86400


def _get(path: str, params: dict, timeout=20) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "MusicEdCafe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
        except Exception:
            err = {}
        # message/code only, never the URL (it carries the token)
        raise RuntimeError(f"instagram HTTP {e.code}: {err.get('type', '')} {err.get('code', '')} {err.get('message', '')}".strip()) from None


def _token() -> tuple[str | None, float]:
    saved = load_json(TOKEN, {}) or {}
    if saved.get("access_token"):
        return saved["access_token"], float(saved.get("refreshed_at", 0))
    t = os.environ.get("IG_ACCESS_TOKEN")
    return (t or None), 0.0


def _save_token(tok: str) -> None:
    IG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(TOKEN, json.dumps({"access_token": tok, "refreshed_at": time.time()}))
    os.chmod(TOKEN, 0o600)


def _save_image(p: dict, log=print) -> str | None:
    """Download the post's image (video -> its thumbnail) and save WebP 480/1080.
    Returns the base file name, or None. Never raises."""
    pid = "".join(c for c in str(p.get("id", "")) if c.isalnum())
    if not pid:
        return None
    if (IMG_DIR / f"{pid}-480.webp").exists() and (IMG_DIR / f"{pid}-1080.webp").exists():
        return pid
    url = p.get("thumbnail_url") if p.get("media_type") == "VIDEO" else p.get("media_url")
    url = url or p.get("thumbnail_url")
    if not url:
        return None
    try:
        import io
        from PIL import Image
        req = urllib.request.Request(url, headers={"User-Agent": "MusicEdCafe/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if not (r.headers.get("Content-Type") or "").startswith("image/"):
                return None
            data = r.read(15_000_000)
        im = Image.open(io.BytesIO(data)).convert("RGB")
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        for w in (480, 1080):
            c = im.copy()
            c.thumbnail((w, w * 2))
            c.save(IMG_DIR / f"{pid}-{w}.webp", "WEBP", quality=74, method=5)
        return pid
    except Exception as e:
        log(f"instagram image {pid} failed: {str(e)[:120]}")
        return None


def sync(log=print) -> dict:
    tok, refreshed_at = _token()
    if not tok:
        return {"result": "skipped", "reason": "no IG_ACCESS_TOKEN"}
    st = load_json(STATE, {}) or {}
    st["last_attempt"] = now_iso()
    try:
        if time.time() - refreshed_at > REFRESH_EVERY:
            try:
                r = _get("/refresh_access_token", {"grant_type": "ig_refresh_token", "access_token": tok})
                if r.get("access_token"):
                    tok = r["access_token"]
                    _save_token(tok)
            except RuntimeError as e:  # refresh can fail on a <24h-old token; keep going with the current one
                log(f"instagram token refresh skipped: {e}")
        posts, params = [], {"fields": FIELDS, "limit": 25, "access_token": tok}
        r = _get("/me/media", params)
        posts += r.get("data", [])
        out = [{"id": p["id"], "permalink": p.get("permalink"), "caption": p.get("caption") or "",
                "media_type": p.get("media_type"), "timestamp": p.get("timestamp"),
                "image": _save_image(p, log)}
               for p in posts if p.get("permalink")]
        IG_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(POSTS, json.dumps({"fetched_at": now_iso(), "posts": out}, indent=1))
        st.update(last_success=now_iso(), last_error=None, count=len(out))
        atomic_write(STATE, json.dumps(st, indent=1))
        return {"result": "ok", "posts": len(out)}
    except Exception as e:
        st["last_error"] = str(e)[:300]
        atomic_write(STATE, json.dumps(st, indent=1)) if IG_DIR.exists() else None
        return {"result": "failed", "error": str(e)[:300]}


def cached_posts() -> list[dict]:
    return (load_json(POSTS, {}) or {}).get("posts", [])

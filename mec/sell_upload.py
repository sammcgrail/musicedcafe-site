"""Sell Your Records photo uploads: a streaming multipart/form-data reader and
the image checks + processing (stdlib + Pillow; pillow-heif when installed).

PRIVACY: uploaded photos are private. They are spooled to
data/sell/uploads/.incoming-<random>/ (mode 700 dir, 600 files), then saved as
data/sell/uploads/<submission id>/<name>.webp (+ .thumb.webp), mode 600, with
EXIF/GPS stripped. data/sell/ is gitignored, the nginx container never mounts
data/, and the only way to read a photo back is the token-protected admin
endpoint. Nothing here logs a filename, a field value or a photo.

Memory: the request body is never held in memory. It is read in 64 KB chunks
and file parts go straight to disk, so a 120 MB request costs ~64 KB of RAM.
Images are decoded one at a time (PROCESS_LOCK) with JPEG draft-mode
downscaling and per-format pixel caps (MAX_PIXELS) for formats without it.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import os
import re
import secrets
import shutil
import threading
from pathlib import Path

MAX_FILE = 8 * 1024 * 1024            # per photo
MAX_REQUEST = 120 * 1024 * 1024       # whole request
MAX_PARTS = 300                       # 40 rows x 6 fields + overview + contact fields, with room
MAX_FIELD_TEXT = 4096                 # one text field
MAX_TEXT_TOTAL = 96 * 1024            # all text fields together
MAX_HEADER = 8192                     # one part's headers
CHUNK = 64 * 1024

FULL_PX = 1600
THUMB_PX = 400
MAX_PIXELS_DRAFT = 120_000_000        # JPEG: decoded at 1/2..1/8 scale, so big files stay small in RAM
# Formats without draft decoding are decoded at full size. Caps from measured peak RSS
# (Pillow 12.3, 2026-10-21) so one photo stays under ~100 MB of the admin container:
MAX_PIXELS = {"png": 24_000_000,      # RGB / grey PNG: ~3-4 B/px
              "png_alpha": 10_000_000,  # alpha or palette: converted + premultiplied, ~8 B/px
              "webp": 6_000_000,      # libwebp decode: ~15 B/px (24 MP peaked at 345 MB)
              "heic": 12_500_000}     # pillow-heif: ~8 B/px (12 MP iPhone photo peaked at ~100 MB)
LIGHT_MODES = ("RGB", "L", "CMYK", "YCbCr")

ALLOWED_MIME = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/pjpeg": "jpeg", "image/png": "png",
                "image/webp": "webp", "image/heic": "heic", "image/heif": "heic",
                "image/heic-sequence": "heic", "image/heif-sequence": "heic"}
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
PIL_FORMATS = {"jpeg": {"JPEG", "MPO"}, "png": {"PNG"}, "webp": {"WEBP"}, "heic": {"HEIF"}}
PROCESS_LOCK = threading.Lock()

try:   # optional: HEIC decoding (iPhones). Without it HEIC originals are stored as-is, no preview.
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:  # ImportError, or a broken native lib
    HEIF = False


class UploadError(Exception):
    """The request as a whole can't be read (malformed, too many fields, too big)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class ImageRejected(Exception):
    pass


def boundary_of(content_type: str) -> bytes | None:
    m = re.search(r'boundary=(?:"([^"]{1,70})"|([^;\s]{1,70}))', content_type or "", re.I)
    if not m:
        return None
    return (m.group(1) or m.group(2)).encode("latin-1", "replace")


def sniff(head: bytes) -> str | None:
    """Image type from the first bytes (magic numbers), or None."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return "heic"
    return None


class FilePart:
    __slots__ = ("name", "mime", "path", "size", "head", "too_big")

    def __init__(self, name, mime, path):
        self.name, self.mime, self.path = name, mime, path
        self.size, self.head, self.too_big = 0, b"", False

    def kind(self) -> str | None:
        """'jpeg'/'png'/'webp'/'heic' when BOTH the declared type and the magic bytes are images we take."""
        if ALLOWED_MIME.get((self.mime or "").lower()) is None:
            return None
        return sniff(self.head)


class _Reader:
    def __init__(self, rfile, length: int):
        self.f, self.left, self.buf = rfile, length, b""

    def fill(self) -> bool:
        if self.left <= 0:
            return False
        data = self.f.read(min(CHUNK, self.left))
        if not data:
            raise UploadError(400, "The upload was cut off. Please try again.")
        self.left -= len(data)
        self.buf += data
        return True

    def drain(self):
        while self.left > 0:
            data = self.f.read(min(CHUNK, self.left))
            if not data:
                return
            self.left -= len(data)


def _headers(raw: bytes):
    msg = email.message_from_bytes(raw + b"\r\n\r\n", policy=email.policy.compat32)
    name = msg.get_param("name", header="content-disposition")
    if isinstance(name, tuple):          # RFC 2231 form
        name = email.utils.collapse_rfc2231_value(name)
    filename = msg.get_param("filename", header="content-disposition")
    return (str(name) if name is not None else None), filename is not None, msg.get_content_type()


def read_multipart(rfile, length: int, boundary: bytes, spool_dir: Path, *, file_field=None,
                   max_file: int | None = None, max_parts: int | None = None):
    """Stream a multipart body. Returns (fields: {name: [str]}, files: [FilePart]).
    Text parts are capped in memory; file parts (with a filename) go to spool_dir.
    file_field(name) -> bool limits which names may carry files (others are dropped).
    Empty file parts (no file chosen) are dropped. A file over max_file is kept as a
    FilePart with too_big=True and no data on disk (the rest of it is read and thrown away)."""
    max_file = MAX_FILE if max_file is None else max_file
    max_parts = MAX_PARTS if max_parts is None else max_parts
    if not boundary or len(boundary) > 70:
        raise UploadError(400, "Please use the form on the sell page.")
    r = _Reader(rfile, length)
    delim = b"--" + boundary
    body_delim = b"\r\n" + delim
    keep = len(body_delim) - 1
    fields: dict[str, list[str]] = {}
    files: list[FilePart] = []
    text_total = parts = 0
    while True:                                           # preamble
        i = r.buf.find(delim)
        if i >= 0:
            r.buf = r.buf[i + len(delim):]
            break
        r.buf = r.buf[-keep:]
        if not r.fill():
            raise UploadError(400, "Please use the form on the sell page.")
    while True:
        while len(r.buf) < 2:
            if not r.fill():
                raise UploadError(400, "The upload was cut off. Please try again.")
        if r.buf[:2] == b"--":                            # closing delimiter
            r.drain()
            return fields, files
        if r.buf[:2] != b"\r\n":
            raise UploadError(400, "Please use the form on the sell page.")
        r.buf = r.buf[2:]
        while (j := r.buf.find(b"\r\n\r\n")) < 0:
            if len(r.buf) > MAX_HEADER or not r.fill():
                raise UploadError(400, "Please use the form on the sell page.")
        name, is_file, mime = _headers(r.buf[:j])
        r.buf = r.buf[j + 4:]
        parts += 1
        if parts > max_parts:
            raise UploadError(413, "That form has too many fields.")
        fp, fh, text = None, None, None
        if is_file and name and (file_field is None or file_field(name)):
            spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = spool_dir / f"raw-{parts}-{secrets.token_hex(4)}"
            fh = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
            fp = FilePart(name, mime, path)
        elif not is_file and name:
            text = bytearray()

        def sink(chunk: bytes):
            nonlocal text_total
            if not chunk:
                return
            if fp is not None:
                if len(fp.head) < 32:
                    fp.head += chunk[:32 - len(fp.head)]
                fp.size += len(chunk)
                if fp.size > max_file:
                    fp.too_big = True
                elif not fp.too_big:
                    fh.write(chunk)
            elif text is not None:
                text_total += len(chunk)
                if text_total > MAX_TEXT_TOTAL:
                    raise UploadError(413, "That form was too large. Please shorten the notes and try again.")
                if len(text) <= MAX_FIELD_TEXT:
                    text.extend(chunk[:MAX_FIELD_TEXT + 1 - len(text)])
        try:
            while True:
                k = r.buf.find(body_delim)
                if k >= 0:
                    sink(r.buf[:k])
                    r.buf = r.buf[k + len(body_delim):]
                    break
                if len(r.buf) > keep:
                    sink(r.buf[:-keep])
                    r.buf = r.buf[-keep:]
                if not r.fill():
                    raise UploadError(400, "The upload was cut off. Please try again.")
        finally:
            if fh is not None:
                fh.close()
        if fp is not None:
            if fp.too_big:
                fp.path.unlink(missing_ok=True)
            if fp.size == 0:                               # "no file chosen"
                fp.path.unlink(missing_ok=True)
            else:
                files.append(fp)
        elif text is not None:
            fields.setdefault(name, []).append(bytes(text).decode("utf-8", "replace"))


def new_spool(base: Path) -> Path:
    d = base / f".incoming-{secrets.token_hex(8)}"
    d.mkdir(parents=True, mode=0o700)
    return d


def remove_tree(p: Path | None):
    if p is not None:
        shutil.rmtree(p, ignore_errors=True)


def _open600(path: Path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")


def _to_srgb(img):
    icc = img.info.get("icc_profile")
    if not icc:
        return img
    try:
        import io

        from PIL import ImageCms
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(img, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
    except Exception:
        return img


def process_image(src: Path, kind: str, dest_dir: Path, stem: str) -> dict:
    """Decode, rotate upright, strip all metadata, and write <stem>.webp (<= 1600 px)
    and <stem>.thumb.webp (<= 400 px), mode 600. HEIC without pillow-heif: the
    original is kept as <stem>.heic (no preview possible). Raises ImageRejected."""
    from PIL import Image, ImageOps
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "heic" and not HEIF:
        out = dest_dir / f"{stem}.heic"
        with open(src, "rb") as fi, _open600(out) as fo:
            shutil.copyfileobj(fi, fo)
        return {"kind": kind, "preview": False, "original": out.name, "bytes": out.stat().st_size}
    with PROCESS_LOCK:
        try:
            with Image.open(src) as im:
                if im.format not in PIL_FORMATS[kind]:
                    raise ImageRejected("That file isn't the kind of image it says it is.")
                w, h = im.size
                if kind == "jpeg":
                    if w * h > MAX_PIXELS_DRAFT:
                        raise ImageRejected("That photo is too large (too many pixels).")
                    s = 1
                    while s < 8 and max(w, h) // (s * 2) >= FULL_PX:
                        s *= 2
                    if s > 1:
                        im.draft("RGB", (w // s, h // s))
                elif w * h > MAX_PIXELS["png_alpha" if kind == "png" and im.mode not in LIGHT_MODES else kind]:
                    raise ImageRejected("That photo is too large (too many pixels). Please send a smaller copy.")
                exif = im.getexif()
                orientation = exif.get(0x0112, 1) if exif else 1
                icc = im.info.get("icc_profile")
                im.load()
                src_img = im if im.mode in ("RGB", "L", "RGBA", "LA") else im.convert("RGBA" if im.mode in ("P", "PA", "I;16", "I", "F") and "transparency" in im.info else "RGB")
                f = max(1, max(im.size) // FULL_PX)
                # an integer box-reduce first (one small copy), then free the full-size pixels:
                # peak memory is about the decoded image (x2 with alpha) plus 1/f^2 of it
                img = src_img.reduce(f) if f > 1 else src_img.copy()
                if src_img is not im:
                    src_img.close()
                im.close()
                img.thumbnail((FULL_PX, FULL_PX), Image.Resampling.LANCZOS)
                if icc:
                    img.info["icc_profile"] = icc
                if orientation and orientation != 1:   # rotate the small copy upright
                    img.getexif()[0x0112] = orientation
                    img = ImageOps.exif_transpose(img)
                img = _to_srgb(img)
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    flat = Image.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.split()[3])
                    img = flat
                else:
                    img = img.convert("RGB")
                clean = Image.new("RGB", img.size)     # a fresh image: no EXIF, GPS, XMP or ICC carried over
                clean.paste(img)
        except ImageRejected:
            raise
        except Exception as e:   # UnidentifiedImageError, DecompressionBombError, truncated files...
            raise ImageRejected("We couldn't read that photo. Please try a JPEG or PNG.") from e
        full = dest_dir / f"{stem}.webp"
        with _open600(full) as f:
            clean.save(f, "WEBP", quality=82, method=4)
        thumb = clean.copy()
        thumb.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
        with _open600(dest_dir / f"{stem}.thumb.webp") as f:
            thumb.save(f, "WEBP", quality=75, method=4)
        return {"kind": kind, "preview": True, "w": clean.size[0], "h": clean.size[1],
                "bytes": full.stat().st_size}

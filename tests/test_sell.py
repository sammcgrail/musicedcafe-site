"""Sell Your Records tests: form v2 validation, multipart streaming + limits, image
checks (MIME + magic bytes, EXIF/GPS stripping), the no-JS path, privacy of
uploads and admin photo endpoints, the private offer calculator (rule
boundaries, shipping vs pickup, Discogs valuation caching) and the built page.

Run:  python3 -m unittest discover -s tests   (or python3 tests/test_sell.py)
Everything is written to a temp dir; the real data/sell/ is never touched, and
Discogs is always a mock (no network).
"""
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if "MEC_ROOT" not in os.environ:
    os.environ["MEC_ROOT"] = tempfile.mkdtemp(prefix="mec-test-")
TMP = Path(os.environ["MEC_ROOT"])
os.environ.pop("EBAY_CLIENT_ID", None)
os.environ.pop("EBAY_CLIENT_SECRET", None)
os.environ.pop("DISCOGS_TOKEN", None)
sys.path.insert(0, str(REPO))

from PIL import Image  # noqa: E402

from mec import discogs, sell, sell_offer, sell_upload  # noqa: E402

CFG = sell.load_config(REPO / "content" / "sell" / "sell.json")
CONTACT = {"name": "Test Person", "email": "test@example.com", "phone": "(555) 123-4567", "location": "Lynn, MA 01901",
           "count": "1-24", "formats": ["LP", "45"], "genres": "Soul and jazz", "condition": "Mostly very good or better",
           "handoff": "ship", "photos": "", "note": "hi", "consent": "yes"}
ROW1 = {"r1_artist": "Al Green", "r1_title": "Call Me", "r1_catno": "XSHL 32077"}


def jpeg_bytes(size=(640, 480), color=(200, 30, 30), exif=None) -> bytes:
    b = io.BytesIO()
    kw = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, color).save(b, "JPEG", quality=85, **kw)
    return b.getvalue()


def png_bytes(size=(300, 200)) -> bytes:
    b = io.BytesIO()
    Image.new("RGBA", size, (10, 120, 200, 128)).save(b, "PNG")
    return b.getvalue()


def gps_exif(orientation=6):
    ex = Image.Exif()
    ex[0x0112] = orientation                    # Orientation: rotate 90
    ex[0x010F] = "SecretPhoneMaker"             # Make
    gps = {1: "N", 2: (42.0, 27.0, 59.0), 3: "W", 4: (70.0, 56.0, 58.0)}
    ex[0x8825] = gps                            # GPSInfo IFD
    return ex


def multipart(fields: dict, files: list, boundary="----mecTestBoundary7MA4YWxk") -> tuple[bytes, str]:
    """files: [(field, filename, mime, bytes)]"""
    out = io.BytesIO()
    for k, v in fields.items():
        for x in (v if isinstance(v, list) else [v]):
            out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{x}\r\n'.encode())
    for field, fname, mime, data in files:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
                  f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def row_files(n=1, front=None, label=None, back=None):
    f = [(f"r{n}_front", "front.jpg", "image/jpeg", front or jpeg_bytes()),
         (f"r{n}_label", "label.jpg", "image/jpeg", label or jpeg_bytes(color=(20, 20, 20)))]
    if back is not None:
        f.append((f"r{n}_back", "back.png", "image/png", back))
    return f


def use_temp_store():
    d = TMP / "data" / "sell"
    shutil.rmtree(d, ignore_errors=True)
    sell.STORE_DIR, sell.STORE = d, d / "submissions.jsonl"
    sell_offer.RULES_FILE, sell_offer.REVIEW_DIR, sell_offer.VAL_DIR = d / "offer_rules.json", d / "review", d / "valuations"


def parse(body: bytes, ctype: str, spool: Path, **kw):
    return sell_upload.read_multipart(io.BytesIO(body), len(body), sell_upload.boundary_of(ctype), spool,
                                      file_field=sell.is_file_field, **kw)


# --------------------------------------------------------------------------- validation
class Validation(unittest.TestCase):
    def setUp(self):
        use_temp_store()
        self.spool = sell_upload.new_spool(TMP / "spool")

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)

    def files(self, specs):
        body, ct = multipart({}, specs)
        return parse(body, ct, self.spool)[1]

    def check(self, files=None, **change):
        form = dict(CONTACT, **ROW1)
        form.update(change)
        form = {k: v for k, v in form.items() if v is not None}
        return sell.validate(form, CFG, self.files(row_files() if files is None else files))

    def test_config_shape(self):
        self.assertEqual(CFG["count_options"], ["1-24", "25-99", "100-499", "500+"])
        self.assertEqual(CFG["postage"], "We cover shipping. If you're shipping, we'll send you a prepaid label.")
        self.assertIs(CFG["local_pickup"], True)
        self.assertIn("dropoff", sell.handoff_options(CFG))
        self.assertNotIn("dropoff", sell.handoff_options(dict(CFG, local_pickup=None)))

    def test_good(self):
        clean, err = self.check()
        self.assertEqual(err, {})
        self.assertEqual(clean["formats"], ["LP", "45"])
        self.assertEqual(clean["records"], [{"n": 1, "row": 1, "artist": "Al Green", "title": "Call Me",
                                             "catno": "XSHL 32077", "photos": {"front": "r1-front", "label": "r1-label"}}])
        self.assertEqual(sorted(clean["_files"]), ["r1-front", "r1-label"])

    def test_required_fields(self):
        for k in ("name", "email", "location", "count", "formats", "condition", "consent", "handoff"):
            self.assertIn(k, self.check(**{k: None})[1], k)
        for k in ("phone", "genres", "photos", "note"):
            self.assertEqual(self.check(**{k: None})[1], {}, k)

    def test_enums(self):
        self.assertIn("count", self.check(count="9999")[1])
        self.assertIn("condition", self.check(condition="mint?")[1])
        self.assertIn("formats", self.check(formats=["8-track"])[1])
        self.assertIn("handoff", self.check(handoff="teleport")[1])
        self.assertIn("handoff", sell.validate(dict(CONTACT, **ROW1, handoff="dropoff"), dict(CFG, local_pickup=False),
                                               self.files(row_files()))[1])
        clean, _ = self.check(formats=["LP", "8-track", "LP"])
        self.assertEqual(clean["formats"], ["LP"])

    def test_text_limits_and_cleaning(self):
        self.assertIn("name", self.check(name="x" * 81)[1])
        for bad in ("nope", "a@b", "a b@c.com", "@c.com"):
            self.assertIn("email", self.check(email=bad)[1], bad)
        for bad in ("abc", "12345", "1" * 31):
            self.assertIn("phone", self.check(phone=bad)[1], bad)
        self.assertIn("note", self.check(note="x" * 1001)[1])
        self.assertIn("r1", self.check(r1_artist="x" * 121)[1])
        clean, _ = self.check(name="  Ann \x00\x07 Lee  ", note=None)
        self.assertEqual(clean["name"], "Ann Lee")

    def test_row_needs_both_photos(self):
        e = self.check(files=[("r1_front", "f.jpg", "image/jpeg", jpeg_bytes())])[1]
        self.assertIn("vinyl label", e["r1"])
        e = self.check(files=[("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])[1]
        self.assertIn("jacket front", e["r1"])
        e = self.check(r1_artist="")[1]
        self.assertIn("add the artist", e["r1"])
        clean, e = self.check(files=row_files(back=png_bytes()))
        self.assertEqual(e, {})
        self.assertEqual(clean["records"][0]["photos"]["back"], "r1-back")

    def test_rows_needed_unless_big_lot_with_overview(self):
        empty = {"r1_artist": "", "r1_title": "", "r1_catno": ""}
        ov = [("overview", "crate.jpg", "image/jpeg", jpeg_bytes())]
        self.assertIn("records", self.check(files=[], **empty)[1])                       # 1-24, nothing
        self.assertIn("records", self.check(files=ov, **empty)[1])                       # 1-24 + overview: rows still needed
        self.assertIn("records", self.check(files=[], count="25-99", **empty)[1])        # 25+ with no overview
        clean, e = self.check(files=ov, count="25-99", **empty)                          # 25+ with overview: ok
        self.assertEqual(e, {})
        self.assertEqual((clean["records"], clean["overview"]), ([], ["ov1"]))
        many = [("overview", f"c{i}.jpg", "image/jpeg", jpeg_bytes((50, 50))) for i in range(11)]
        self.assertIn("overview", self.check(files=many, count="500+", **empty)[1])

    def test_rows_renumber_and_max_40(self):
        form = dict(CONTACT, r3_artist="A", r3_title="B", r41_artist="C", r41_title="D")
        files = self.files(row_files(3) + row_files(41))
        self.assertEqual({f.name for f in files}, {"r3_front", "r3_label"})   # r41_* is not a file field
        clean, e = sell.validate(form, CFG, files)
        self.assertEqual(e, {})
        self.assertEqual([(r["n"], r["row"]) for r in clean["records"]], [(1, 3)])

    def test_mime_and_magic_both_required(self):
        txt = b"hello, I am not an image" * 10
        e = self.check(files=[("r1_front", "f.jpg", "image/jpeg", txt), ("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])[1]
        self.assertIn("isn't a photo", e["r1"])                                           # lies about its type
        e = self.check(files=[("r1_front", "f.jpg", "text/plain", jpeg_bytes()), ("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])[1]
        self.assertIn("isn't a photo", e["r1"])                                           # real JPEG, wrong declared type
        gif = b"GIF89a" + b"\x00" * 40
        e = self.check(files=[("r1_front", "f.gif", "image/gif", gif), ("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])[1]
        self.assertIn("isn't a photo", e["r1"])

    def test_honeypot(self):
        self.assertFalse(sell.is_spam(CONTACT))
        self.assertTrue(sell.is_spam(dict(CONTACT, website="http://spam")))


# --------------------------------------------------------------------------- multipart + images
class Multipart(unittest.TestCase):
    def setUp(self):
        self.spool = sell_upload.new_spool(TMP / "spool")

    def tearDown(self):
        shutil.rmtree(self.spool, ignore_errors=True)

    def test_fields_files_and_chunk_boundaries(self):
        big = jpeg_bytes((1800, 1400)) + os.urandom(200_000)      # crosses many 64 KB reads
        body, ct = multipart({"name": "Ann", "formats": ["LP", "45"], "r1_artist": "X"},
                             [("r1_front", "a.jpg", "image/jpeg", big), ("r1_back", "", "application/octet-stream", b""),
                              ("evil_field", "x.jpg", "image/jpeg", jpeg_bytes())])
        fields, files = parse(body, ct, self.spool)
        self.assertEqual(fields["formats"], ["LP", "45"])
        self.assertEqual([f.name for f in files], ["r1_front"])   # empty "no file" part and unknown file field dropped
        self.assertEqual(files[0].path.read_bytes(), big)
        self.assertEqual(oct(files[0].path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(files[0].kind(), "jpeg")

    def test_limits(self):
        body, ct = multipart({}, [("r1_front", "a.jpg", "image/jpeg", jpeg_bytes() + b"\0" * 5000)])
        _, files = parse(body, ct, self.spool, max_file=1000)
        self.assertTrue(files[0].too_big)
        self.assertFalse(files[0].path.exists())                  # nothing kept on disk
        body, ct = multipart({f"f{i}": "x" for i in range(20)}, [])
        with self.assertRaises(sell_upload.UploadError) as cm:
            parse(body, ct, self.spool, max_parts=10)
        self.assertEqual(cm.exception.status, 413)
        body, ct = multipart({"note": "x" * (sell_upload.MAX_TEXT_TOTAL + 10)}, [])
        with self.assertRaises(sell_upload.UploadError):
            parse(body, ct, self.spool)

    def test_malformed(self):
        for body in (b"no boundary here at all", b"------mecTestBoundary7MA4YWxk\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nunterminated"):
            with self.assertRaises(sell_upload.UploadError):
                parse(body, "multipart/form-data; boundary=----mecTestBoundary7MA4YWxk", self.spool)
        self.assertIsNone(sell_upload.boundary_of("multipart/form-data"))

    def test_sniff(self):
        self.assertEqual(sell_upload.sniff(jpeg_bytes()[:32]), "jpeg")
        self.assertEqual(sell_upload.sniff(png_bytes()[:32]), "png")
        b = io.BytesIO()
        Image.new("RGB", (10, 10)).save(b, "WEBP")
        self.assertEqual(sell_upload.sniff(b.getvalue()[:32]), "webp")
        self.assertEqual(sell_upload.sniff(b"\0\0\0\x18ftypheic\0\0\0\0mif1heic"), "heic")
        self.assertIsNone(sell_upload.sniff(b"\0\0\0\x18ftypavif\0\0\0\0"))
        self.assertIsNone(sell_upload.sniff(b"%PDF-1.7"))

    def test_exif_gps_stripped_and_rotated(self):
        src = self.spool / "in.jpg"
        src.write_bytes(jpeg_bytes((2400, 1200), exif=gps_exif(6)))
        self.assertIn(b"SecretPhoneMaker", src.read_bytes())
        dest = self.spool / "out"
        meta = sell_upload.process_image(src, "jpeg", dest, "r1-front")
        full, thumb = dest / "r1-front.webp", dest / "r1-front.thumb.webp"
        for f in (full, thumb):
            raw = f.read_bytes()
            self.assertNotIn(b"Exif", raw)
            self.assertNotIn(b"SecretPhoneMaker", raw)
            self.assertNotIn(b"GPS", raw)
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
            with Image.open(f) as im:
                self.assertEqual(im.format, "WEBP")
                self.assertEqual(len(im.getexif()), 0)
                self.assertNotIn("exif", im.info)
        with Image.open(full) as im:
            self.assertEqual(im.size, (800, 1600))                # upright (orientation 6) and <= 1600 px
        with Image.open(thumb) as im:
            self.assertLessEqual(max(im.size), 400)
        self.assertTrue(meta["preview"])

    def test_png_with_alpha_and_bad_image(self):
        src = self.spool / "in.png"
        src.write_bytes(png_bytes())
        sell_upload.process_image(src, "png", self.spool / "o", "ov1")
        with Image.open(self.spool / "o" / "ov1.webp") as im:
            self.assertEqual(im.mode, "RGB")
        bad = self.spool / "bad.jpg"
        bad.write_bytes(b"\xff\xd8\xff" + b"garbage" * 50)
        with self.assertRaises(sell_upload.ImageRejected):
            sell_upload.process_image(bad, "jpeg", self.spool / "o", "r1-front")
        with self.assertRaises(sell_upload.ImageRejected):                 # PNG bytes claimed as JPEG
            sell_upload.process_image(src, "jpeg", self.spool / "o", "r1-label")

    def test_pixel_cap(self):
        src = self.spool / "huge.png"
        Image.new("L", (6000, 5000)).save(src, "PNG")           # 30 MP, compresses to almost nothing
        with self.assertRaises(sell_upload.ImageRejected):
            sell_upload.process_image(src, "png", self.spool / "o", "ov1")

    def test_heic_without_decoder_kept_as_original(self):
        if sell_upload.HEIF:
            self.skipTest("pillow-heif installed")
        src = self.spool / "in.heic"
        src.write_bytes(b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 100)
        meta = sell_upload.process_image(src, "heic", self.spool / "o", "r1-front")
        self.assertFalse(meta["preview"])
        f = self.spool / "o" / "r1-front.heic"
        self.assertTrue(f.exists())
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")


# --------------------------------------------------------------------------- endpoint
def load_server():
    spec = importlib.util.spec_from_file_location("mec_admin_server_sell", REPO / "admin" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Endpoint(unittest.TestCase):
    """POST /sell/submit and the admin endpoints against a real server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.srv_mod = load_server()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv_mod.H)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        use_temp_store()
        self.srv_mod.sell.CONFIG = REPO / "content" / "sell" / "sell.json"
        self.srv_mod.SELL_MIN_FREE_DISK = 0
        self.srv_mod.TOKEN = ""
        for rl in (self.srv_mod.SELL_RL_SHORT, self.srv_mod.SELL_RL_DAY, self.srv_mod.SELL_RL_ALL):
            rl.hits.clear()

    def raw_post(self, body, ctype, accept_json=True, ip="10.0.0.1", origin=None):
        h = {"Content-Type": ctype, "X-Forwarded-For": ip}
        if accept_json:
            h["Accept"] = "application/json"
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.base + "/sell/submit", data=body, headers=h, method="POST")
        try:
            r = urllib.request.build_opener(NoRedirect).open(req, timeout=20)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def post(self, fields=None, files=None, **kw):
        body, ct = multipart(dict(CONTACT, **ROW1) if fields is None else fields, row_files() if files is None else files)
        return self.raw_post(body, ct, **kw)

    def get(self, path, token=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = urllib.request.urlopen(urllib.request.Request(self.base + path, headers=h), timeout=10)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_json_success_stores_processed_photos(self):
        code, _, body = self.post(files=row_files(front=jpeg_bytes((2000, 1500), exif=gps_exif(1)), back=png_bytes()))
        j = json.loads(body)
        self.assertEqual(code, 200, body)
        self.assertTrue(j["ok"])
        self.assertNotIn("test@example.com", body.decode())
        rows = sell.read_all()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["v"], r["handoff"], r["records"][0]["artist"]), (2, "ship", "Al Green"))
        self.assertNotIn("_files", r)
        d = sell.photo_dir(r["id"])
        names = sorted(p.name for p in d.iterdir())
        self.assertEqual(names, ["r1-back.thumb.webp", "r1-back.webp", "r1-front.thumb.webp", "r1-front.webp",
                                 "r1-label.thumb.webp", "r1-label.webp"])
        for p in d.iterdir():
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            self.assertNotIn(b"GPS", p.read_bytes())
        self.assertEqual([p.name for p in sell.uploads_dir().iterdir()], [r["id"]])   # spool cleaned up

    def test_no_js_redirect_and_thanks_page(self):
        code, h, _ = self.post(accept_json=False)
        self.assertEqual(code, 303)
        loc = h["Location"]
        self.assertTrue(loc.startswith("/sell/submit?r="))
        page = urllib.request.urlopen(self.base + loc, timeout=5).read().decode()
        self.assertIn("We got your details", page)
        self.assertNotIn("test@example.com", page)

    def test_no_js_validation_error_page(self):
        code, _, body = self.post(dict(CONTACT), files=[], accept_json=False)
        self.assertEqual(code, 400)
        self.assertIn(b"Go back and fix it", body)
        self.assertIn(b"at least one record", body)
        self.assertEqual(sell.read_all(), [])

    def test_validation_errors_leave_nothing_on_disk(self):
        code, _, body = self.post(dict(CONTACT, **ROW1, count="x", email="x"))
        self.assertEqual(code, 400)
        self.assertEqual(set(json.loads(body)["errors"]), {"count", "email"})
        self.assertEqual(sell.read_all(), [])
        self.assertEqual(list(sell.uploads_dir().iterdir()), [])

    def test_non_image_rejected(self):
        code, _, body = self.post(files=[("r1_front", "f.jpg", "image/jpeg", b"not an image" * 30),
                                         ("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])
        self.assertEqual(code, 400)
        self.assertIn("r1", json.loads(body)["errors"])
        self.assertEqual(list(sell.uploads_dir().iterdir()), [])
        # magic bytes say JPEG but it doesn't decode: caught at processing
        code, _, body = self.post(files=[("r1_front", "f.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x" * 400),
                                         ("r1_label", "l.jpg", "image/jpeg", jpeg_bytes())])
        self.assertEqual(code, 400)
        self.assertIn("couldn't read", json.loads(body)["errors"]["r1"])
        self.assertEqual(list(sell.uploads_dir().iterdir()), [])
        self.assertEqual(sell.read_all(), [])

    def test_per_file_limit(self):
        old = self.srv_mod.sell_upload.MAX_FILE
        self.srv_mod.sell_upload.MAX_FILE = 4000
        try:
            code, _, body = self.post(files=row_files(front=jpeg_bytes((900, 900)) + b"\0" * 5000))
        finally:
            self.srv_mod.sell_upload.MAX_FILE = old
        self.assertEqual(code, 400)
        self.assertIn("over 8 MB", json.loads(body)["errors"]["r1"])

    def test_request_limit_before_reading(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /sell/submit HTTP/1.1\r\nHost: x\r\nAccept: application/json\r\n"
                  b"Content-Type: multipart/form-data; boundary=abc\r\n"
                  b"Content-Length: " + str(sell_upload.MAX_REQUEST + 1).encode() + b"\r\n\r\n")
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        s.close()
        resp = resp.decode("latin-1")
        self.assertIn(" 413 ", resp.splitlines()[0])
        self.assertIn("over 120 MB", resp)
        code, _, _ = self.raw_post(b"x" * 17000, "application/x-www-form-urlencoded")
        self.assertEqual(code, 413)

    def test_urlencoded_old_form_gets_clear_error(self):
        body = urllib.parse.urlencode(dict(CONTACT, **ROW1), doseq=True).encode()
        code, _, resp = self.raw_post(body, "application/x-www-form-urlencoded")
        self.assertEqual(code, 400)
        self.assertIn("r1", json.loads(resp)["errors"])         # rows need photos now
        self.assertEqual(self.raw_post(b"a=1", "text/plain")[0], 415)

    def test_big_lot_overview_only(self):
        f = dict(CONTACT, count="100-499")
        code, _, body = self.post(f, [("overview", f"c{i}.jpg", "image/jpeg", jpeg_bytes((300, 200))) for i in range(3)])
        self.assertEqual(code, 200, body)
        r = sell.read_all()[0]
        self.assertEqual((r["records"], r["overview"]), ([], ["ov1", "ov2", "ov3"]))

    def test_honeypot_blocks(self):
        code, _, _ = self.post(dict(CONTACT, **ROW1, website="http://spam.example"))
        self.assertEqual(code, 200)
        self.assertEqual(sell.read_all(), [])
        self.assertEqual(list(sell.uploads_dir().iterdir()), [])

    def test_rate_limit(self):
        codes = [self.post(ip="10.9.9.9")[0] for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])
        self.assertEqual(self.post(ip="10.9.9.10")[0], 200)

    def test_origin(self):
        self.assertEqual(self.post(origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(origin="https://musicedcafe.com")[0], 200)

    def test_admin_endpoints_need_token(self):
        self.post()
        sid = sell.read_all()[0]["id"]
        for path in ("/admin/api/sell", "/admin/api/sell.csv", f"/admin/api/sell/detail?id={sid}",
                     f"/admin/api/sell/photo?id={sid}&p=r1-front&size=thumb", f"/admin/api/sell/photo?id={sid}&p=r1-front&size=full",
                     f"/admin/api/sell/valuation.csv?id={sid}", "/admin/api/sell/rules"):
            self.assertIn(self.get(path)[0], (401, 503), path)
            self.srv_mod.TOKEN = "t" * 40
            self.assertEqual(self.get(path, token="wrong-token")[0], 401, path)
            self.srv_mod.TOKEN = ""
        for path in ("/admin/api/sell/review", "/admin/api/sell/search", "/admin/api/sell/pick", "/admin/api/sell/rules",
                     "/admin/api/sell/delete?id=x"):
            req = urllib.request.Request(self.base + path, data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertIn(cm.exception.code, (401, 503), path)

    def test_admin_photo_with_token(self):
        self.post()
        sid = sell.read_all()[0]["id"]
        self.srv_mod.TOKEN = tok = "t" * 40
        try:
            code, h, body = self.get(f"/admin/api/sell/photo?id={sid}&p=r1-front&size=thumb", tok)
            self.assertEqual((code, h["Content-Type"], h["Cache-Control"]), (200, "image/webp", "no-store"))
            self.assertEqual(body[:4], b"RIFF")
            for bad in (f"id={sid}&p=../../submissions&size=thumb", f"id={sid}&p=r1-front.webp&size=full",
                        f"id=../x&p=r1-front&size=thumb", f"id={sid}&p=r9-front&size=full", f"id={sid}&p=r1-front&size=raw"):
                self.assertEqual(self.get("/admin/api/sell/photo?" + bad, tok)[0], 404, bad)
            code, _, body = self.get(f"/admin/api/sell/detail?id={sid}", tok)
            j = json.loads(body)
            self.assertEqual(j["photo_meta"]["r1-front"], {"thumb": True, "full": True})
            self.assertEqual(j["calc"]["totals"]["records"], 1)
            # delete removes the photos too
            req = urllib.request.Request(self.base + f"/admin/api/sell/delete?id={sid}", data=b"", method="POST",
                                         headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(urllib.request.urlopen(req, timeout=5).status, 200)
            self.assertFalse(sell.photo_dir(sid).exists())
        finally:
            self.srv_mod.TOKEN = ""

    def test_admin_review_and_rules_flow(self):
        self.post()
        sid = sell.read_all()[0]["id"]
        self.srv_mod.TOKEN = tok = "t" * 40

        def jpost(path, body):
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=10)
                return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            code, j = jpost("/admin/api/sell/review", {"id": sid, "status": "reviewing",
                                                       "records": {"1": {"manual_resale": "60", "sleeve_grade": "Good (G)"}}})
            self.assertEqual(code, 200)
            row = j["calc"]["rows"][0]
            self.assertEqual((row["resale"], row["offer"], row["resale_source"]), (60.0, 5.0, "manual"))
            self.assertEqual(j["calc"]["totals"]["shipping"], 0.55)
            self.assertEqual(jpost("/admin/api/sell/review", {"id": sid, "status": "lost"})[0], 400)
            self.assertEqual(jpost("/admin/api/sell/review", {"id": sid, "records": {"7": {"manual_resale": 1}}})[0], 400)
            code, j = jpost("/admin/api/sell/search", {"id": sid, "key": "1"})
            self.assertEqual((code, j["result"]), (200, "skipped"))          # no DISCOGS_TOKEN: no call
            code, j = jpost("/admin/api/sell/rules", {"rules": {"ship_per_record": "0.60"}})
            self.assertEqual((code, j["rules"]["ship_per_record"]), (200, 0.6))
            self.assertEqual(oct(sell_offer.RULES_FILE.stat().st_mode & 0o777), "0o600")
            self.assertEqual(jpost("/admin/api/sell/rules", {"rules": {"tiers": [[15, 1], [5, 0]]}})[0], 400)
            code, _, body = self.get(f"/admin/api/sell/valuation.csv?id={sid}", tok)
            self.assertEqual(code, 200)
            self.assertIn("offer_total,5.0", body.decode("utf-8-sig"))
        finally:
            self.srv_mod.TOKEN = ""


# --------------------------------------------------------------------------- offer calculator
class OfferRules(unittest.TestCase):
    def setUp(self):
        use_temp_store()
        self.rules = sell_offer.load_rules()

    def offer(self, x):
        return sell_offer.rule_offer(Decimal(str(x)), self.rules)

    def test_defaults(self):
        self.assertEqual(self.rules["tiers"], [[5, 0], [15, 1], [30, 2], [50, 3]])
        self.assertEqual(self.rules["top"], {"percent": 8, "floor": 3, "cap": 7})
        self.assertEqual(self.rules["ship_per_record"], 0.55)
        self.assertFalse(sell_offer.RULES_FILE.exists())

    def test_boundaries(self):
        cases = [(0, 0), (4.99, 0), (5, 1), (14.99, 1), (15, 2), (29.99, 2), (30, 3), (49.99, 3),
                 (50, 4),            # 50 x 8% = 4
                 (56.25, 5),         # 4.5 rounds half up
                 (56.24, 4), (87.49, 7), (87.5, 7), (93.75, 7),   # 7.5 -> 8 -> capped at 7
                 (100, 7), (10000, 7)]
        for resale, want in cases:
            self.assertEqual(self.offer(resale), Decimal(want), resale)
        self.assertIsNone(sell_offer.rule_offer(None, self.rules))

    def test_floor_and_configurable(self):
        r, errs = sell_offer.save_rules({"top": {"percent": 2, "floor": 3, "cap": 9}, "tiers": [[10, 0], [50, 2]]})
        self.assertEqual(errs, [])
        self.rules = sell_offer.load_rules()
        self.assertEqual(self.offer(9.99), 0)
        self.assertEqual(self.offer(10), 2)
        self.assertEqual(self.offer(60), 3)          # 2% of 60 = 1.2 -> floor 3
        self.assertEqual(self.offer(1000), 9)        # 20 -> cap 9
        for bad in ({"tiers": [[5, 0], [5, 1]]}, {"top": {"percent": 8, "floor": 5, "cap": 3}}, {"ship_per_record": "-1"},
                    {"sleeve_factors": {"Shiny": 1}}, {"ship_per_record": "abc"}):
            self.assertTrue(sell_offer.save_rules(bad)[1], bad)


class Calc(unittest.TestCase):
    SUB = {"id": "subABCDEFGH123", "handoff": "ship", "records": [
        {"n": 1, "artist": "A", "title": "One", "catno": "C1", "photos": {"front": "r1-front", "label": "r1-label"}},
        {"n": 2, "artist": "B", "title": "Two", "catno": "", "photos": {}},
        {"n": 3, "artist": "C", "title": "Three", "catno": "", "photos": {}},
        {"n": 4, "artist": "D", "title": "Four", "catno": "", "photos": {}}]}
    VAL = {101: {"price_suggestions": {"Very Good (VG)": {"currency": "USD", "value": 20.0},
                                       "Very Good Plus (VG+)": {"currency": "USD", "value": 60.0}},
                 "stats": {"lowest_price": {"currency": "USD", "value": 3.0}, "num_for_sale": 9}},
           102: {"price_suggestions": {}, "stats": {"lowest_price": {"currency": "USD", "value": 12.0}, "num_for_sale": 2}},
           103: {"price_suggestions": {}, "stats": {"lowest_price": None, "num_for_sale": None}}}

    def setUp(self):
        use_temp_store()

    def calc(self, rv, sub=None):
        rv = dict({"status": "new", "records": {}, "extra": []}, **rv)
        return sell_offer.compute(sub or self.SUB, rv, sell_offer.load_rules(), self.VAL)

    def test_resale_sources_and_totals(self):
        c = self.calc({"records": {"1": {"release_id": 101}, "2": {"release_id": 102},
                                   "3": {"release_id": 103}, "4": {"release_id": 101, "vinyl_grade": "Very Good Plus (VG+)",
                                                                   "override_offer": 6}}})
        r = {x["key"]: x for x in c["rows"]}
        self.assertEqual((r["1"]["resale"], r["1"]["offer"]), (20.0, 2.0))                        # suggestion at VG (default)
        self.assertEqual((r["2"]["resale"], r["2"]["offer"]), (12.0, 1.0))                        # lowest x 1.0
        self.assertIn("lowest listed", r["2"]["resale_source"])
        self.assertEqual((r["3"]["resale"], r["3"]["offer"], r["3"]["buying"]), (None, None, False))   # needs a value
        self.assertEqual((r["4"]["resale"], r["4"]["rule_offer"], r["4"]["offer"]), (60.0, 5.0, 6.0))  # override wins
        t = c["totals"]
        self.assertEqual((t["buying"], t["valued"], t["offer_total"]), (3, 3, 9.0))
        self.assertEqual(t["shipping"], 1.65)                     # 3 records bought x 0.55, shipped
        self.assertEqual(t["total_cost"], 10.65)
        self.assertEqual(t["resale_total"], 92.0)
        self.assertEqual(t["margin"], 81.35)
        self.assertEqual(c["final_offer"], 9.0)
        self.assertIn("Suggested offer: $9.00 for 3 of 4 records", c["suggested_note"])
        self.assertIn("1 still need a value", c["suggested_note"])

    def test_sleeve_factor_and_manual(self):
        c = self.calc({"records": {"1": {"release_id": 101, "sleeve_grade": "Good (G)"},
                                   "2": {"release_id": 102, "manual_resale": 31}}})
        r = {x["key"]: x for x in c["rows"]}
        self.assertEqual(r["1"]["resale"], 14.0)                  # 20 x 0.7
        self.assertEqual(r["1"]["offer"], 1.0)
        self.assertEqual((r["2"]["resale"], r["2"]["offer"], r["2"]["resale_source"]), (31.0, 3.0, "manual"))

    def test_shipping_vs_pickup(self):
        recs = {"records": {"1": {"release_id": 101}, "2": {"release_id": 102}}}
        self.assertEqual(self.calc(recs)["totals"]["shipping"], 1.1)
        t = self.calc(dict(recs, ship_method="pickup"))["totals"]
        self.assertEqual((t["ship_method"], t["shipping"], t["total_cost"]), ("pickup", 0.0, 3.0))
        sub = dict(self.SUB, handoff="dropoff")
        self.assertEqual(self.calc(recs, sub)["totals"]["shipping"], 0.0)       # drop-off defaults to pickup
        self.assertEqual(self.calc(dict(recs, ship_method="ship"), sub)["totals"]["shipping"], 1.1)
        self.assertEqual(self.calc(recs, dict(self.SUB, handoff="unsure"))["totals"]["ship_method"], "ship")
        self.assertNotIn("prepaid", self.calc(dict(recs, ship_method="pickup"))["suggested_note"])

    def test_extra_records(self):
        sid = self.SUB["id"]
        sell_offer.update_review(sid, self.SUB, {"add_extra": {"artist": "E", "title": "Five"}})
        rv = sell_offer.load_review(sid)
        self.assertEqual(rv["extra"][0]["key"], "x1")
        sell_offer.update_review(sid, self.SUB, {"records": {"x1": {"manual_resale": 16}}})
        c = sell_offer.compute(self.SUB, sell_offer.load_review(sid), sell_offer.load_rules(), {})
        self.assertEqual(c["rows"][-1]["offer"], 2.0)
        sell_offer.update_review(sid, self.SUB, {"remove_extra": "x1"})
        self.assertEqual(sell_offer.load_review(sid)["extra"], [])
        self.assertEqual(oct(sell_offer.review_path(sid).stat().st_mode & 0o777), "0o600")


class FakeDiscogs:
    def __init__(self, ps=None, stats=None, fail=False, results=None):
        self.ps = ps if ps is not None else {"Very Good (VG)": {"currency": "USD", "value": 7.5}, "Bogus": {"value": 1}}
        self.stats = stats or {"lowest_price": {"currency": "USD", "value": 4.0}, "num_for_sale": 3, "blocked_from_sale": False}
        self.fail, self.results, self.calls = fail, results, []

    def price_suggestions(self, rid):
        self.calls.append(("ps", rid))
        if self.fail:
            raise discogs.DiscogsError("HTTP 500", 500)
        return self.ps

    def marketplace_stats(self, rid, curr="USD"):
        self.calls.append(("stats", rid, curr))
        if self.fail:
            raise discogs.DiscogsError("HTTP 500", 500)
        return self.stats

    def search(self, **p):
        self.calls.append(("search", p))
        return {"results": (self.results or {}).get(tuple(sorted(p.items())), [])}


class Valuation(unittest.TestCase):
    def setUp(self):
        use_temp_store()

    def test_cache_ttl_refresh_and_failures(self):
        fake = FakeDiscogs()
        v = sell_offer.get_valuation(555, client=fake)
        self.assertEqual(v["price_suggestions"], {"Very Good (VG)": {"currency": "USD", "value": 7.5}})   # junk keys dropped
        self.assertEqual(v["stats"]["num_for_sale"], 3)
        self.assertTrue(sell_offer.valuation_path(555).exists())
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(sell_offer.get_valuation(555, client=fake)["release_id"], 555)
        self.assertEqual(len(fake.calls), 2)                                     # served from the cache
        sell_offer.get_valuation(555, client=fake, refresh=True)
        self.assertEqual(len(fake.calls), 4)
        old = json.loads(sell_offer.valuation_path(555).read_text())
        old["fetched_at"] = "2020-01-01T00:00:00Z"
        sell_offer.valuation_path(555).write_text(json.dumps(old))
        sell_offer.get_valuation(555, client=fake)
        self.assertEqual(len(fake.calls), 6)                                     # TTL expired -> refetched
        broken = FakeDiscogs(fail=True)
        v = sell_offer.get_valuation(555, client=broken, refresh=True)
        self.assertEqual(v["stats"]["num_for_sale"], 3)                          # both failed: old numbers kept
        self.assertIn("price_suggestions", v["last_error"])
        os.environ.pop("DISCOGS_TOKEN", None)
        self.assertIsNone(sell_offer.get_valuation(777))                         # no token, no cache: nothing, no call

    def test_client_paths(self):
        seen = []

        class R:
            headers = {}

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=30):
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
            return R(b'{"lowest_price": null, "num_for_sale": null, "blocked_from_sale": false}')
        c = discogs.Client("tok123", read_only=True, urlopen=opener, sleep=lambda s: None)
        c.price_suggestions(249504)
        c.marketplace_stats(249504)
        self.assertEqual(seen[0][:2], ("GET", "https://api.discogs.com/marketplace/price_suggestions/249504"))
        self.assertEqual(seen[1][:2], ("GET", "https://api.discogs.com/marketplace/stats/249504?curr_abbr=USD"))
        self.assertEqual(seen[0][2], "Discogs token=tok123")

    def test_search_and_pick(self):
        sub = Calc.SUB
        cand = {"id": 101, "title": "A - One", "label": ["L"], "catno": "C1", "format": ["Vinyl", "LP"], "year": "1970",
                "country": "US", "uri": "/release/101", "community": {"have": 50, "want": 9}}
        fake = FakeDiscogs(results={(("artist", "A"), ("catno", "C1"), ("type", "release")): [cand]})
        res = sell_offer.search(sub["id"], sub, "1", client=fake)
        self.assertEqual([c["release_id"] for c in res["candidates"]], [101])
        self.assertEqual(res["candidates"][0]["url"], "https://www.discogs.com/release/101")
        rv = sell_offer.load_review(sub["id"])
        self.assertEqual(rv["records"]["1"]["release_id"], 101)                  # a single candidate is pre-picked
        res = sell_offer.search(sub["id"], sub, "2", client=fake)                # no catno -> artist + title (vinyl)
        self.assertEqual(fake.calls[-1], ("search", {"type": "release", "artist": "B", "release_title": "Two", "format": "Vinyl"}))
        self.assertEqual(res["candidates"], [])
        out = sell_offer.pick(sub["id"], sub, "2", "https://www.discogs.com/release/4242-Some-Title", client=FakeDiscogs())
        self.assertEqual(out["release_id"], 4242)
        self.assertTrue(sell_offer.valuation_path(4242).exists())
        with self.assertRaises(ValueError):
            sell_offer.pick(sub["id"], sub, "9", 5, client=FakeDiscogs())


# --------------------------------------------------------------------------- privacy + built page
class Privacy(unittest.TestCase):
    def test_gitignored_and_not_served(self):
        self.assertIn("data/sell/", (REPO / ".gitignore").read_text().splitlines())
        nginx = (REPO / "nginx.conf").read_text()
        self.assertIn("root /srv/public/live;", nginx)
        compose = (REPO / "docker-compose.yml").read_text()
        web = compose.split("  admin:")[0]
        self.assertIn("./public:/srv/public:ro", web)
        self.assertNotIn("./data", web)                     # nginx never sees data/ (uploads, rules, reviews)
        self.assertFalse(str(sell.uploads_dir()).startswith(str(REPO / "public")))

    def test_admin_shell_has_no_rule_values(self):
        page = (REPO / "admin" / "index.html").read_text()
        for needle in ("0.55", "[5, 0]", "[[5,0]", "0.85", "percent: 8"):
            self.assertNotIn(needle, page)


class BuiltPage(unittest.TestCase):
    """Builds the site into a temp dir from the repo data (public/live is not touched)."""

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="mec-sell-build-"))
        code = ("import sys; sys.path.insert(0, %r)\nfrom pathlib import Path\nimport mec.merge as m\n"
                "m.REVIEW_REPORT = Path(%r) / 'rr.json'\nfrom mec.site import Site\nSite(Path(%r) / 'site').build()\n"
                % (str(REPO), str(cls.out), str(cls.out)))
        env = dict(os.environ, MEC_ROOT=str(REPO))
        cls.ok = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, timeout=300).returncode == 0
        cls.site = cls.out / "site"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def setUp(self):
        if not self.ok:
            self.skipTest("site build failed (no data?)")

    def test_sell_page(self):
        import re
        h = (self.site / "sell" / "index.html").read_text()
        for needle in ('action="/sell/submit"', 'enctype="multipart/form-data"', "Sell Us Your Records",
                       "We review every record and make a fair offer",
                       "We cover shipping. If you&#x27;re shipping, we&#x27;ll send you a prepaid label.",
                       "Local pickup or drop-off", "Packing tips",
                       'name="website"', 'type="checkbox" name="consent"', 'name="handoff"', 'name="overview"',
                       "For 40+ records, send a few crate/spine photos and we'll ask for details on the ones we're interested in.",
                       'data-max-rows="40"', "Add another record"):
            self.assertIn(needle, h)
        for i in range(1, 6):                                       # 5 rows without JS
            for k in ("artist", "title", "catno", "front", "label", "back"):
                self.assertIn(f'name="r{i}_{k}"', h)
        self.assertNotIn('name="r6_artist"', h)
        self.assertEqual(h.count('type="file" accept="image/*"'), 16)  # 5 x 3 + overview
        self.assertNotIn("LocalBusiness", h)
        self.assertNotIn("PostalAddress", h)

    def test_no_offer_amounts_public(self):
        import re
        sell_page = (self.site / "sell" / "index.html").read_text()
        home = (self.site / "index.html").read_text()
        i = home.index('id="h-sell"')
        teaser = home[i:home.index("</section>", i)]
        for name, h in (("sell", sell_page), ("home teaser", teaser)):
            self.assertEqual(re.findall(r"\$\s?\d", h), [], name)                 # no dollar amounts at all
            for bad in ("$1&ndash;$3", "$3&ndash;$7", "$50", "Typical offers", "resale value", "per record for",
                        "0.40", "0.55", "about $22", "sell-table", "so-price"):
                self.assertNotIn(bad, h, (name, bad))
        thanks = (self.site / "sell" / "thanks" / "index.html").read_text()
        self.assertEqual(re.findall(r"\$\s?\d", thanks), [])
        for f in list(self.site.rglob("*.json")) + list(self.site.rglob("*.html")) + list(self.site.rglob("*.js")):
            t = f.read_text(errors="replace")
            for needle in ("offer_rules", "ship_per_record", "lowest_price_factor", "sleeve_factors", "rule_offer"):
                self.assertNotIn(needle, t, (f, needle))
        sm = (self.site / "sitemap.xml").read_text()
        self.assertIn("/sell/</loc>", sm)
        self.assertNotIn("/sell/thanks/", sm)


if __name__ == "__main__":
    unittest.main()

"""Merch reservation tests: validation, honeypot, rate limit, body cap, storage.

Run:  python3 -m unittest discover -s tests   (or python3 tests/test_merch.py)
Everything is written to a temp dir; the real data/merch/ is never touched and
the admin server is imported with MEC_ROOT pointing at the temp dir, so it reads
no .env and no credentials.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if "MEC_ROOT" not in os.environ:
    os.environ["MEC_ROOT"] = tempfile.mkdtemp(prefix="mec-test-")
TMP = Path(os.environ["MEC_ROOT"])
os.environ.pop("EBAY_CLIENT_ID", None)
os.environ.pop("EBAY_CLIENT_SECRET", None)
sys.path.insert(0, str(REPO))

from mec import merch  # noqa: E402

CAT = merch.load_catalog(REPO / "content" / "merch" / "merch.json")
GOOD = {"item": "coffee-flowin-t-shirt", "opt_size": "XL", "opt_color": "Black", "qty": "2",
        "name": "Test Person", "email": "test@example.com", "note": "hi", "consent": "yes"}


def use_temp_store():
    d = TMP / "data" / "merch"
    shutil.rmtree(d, ignore_errors=True)
    merch.STORE_DIR, merch.STORE = d, d / "reservations.jsonl"


class Validation(unittest.TestCase):
    def check(self, **change):
        form = dict(GOOD, **change)
        for k, v in list(form.items()):
            if v is None:
                del form[k]
        return merch.validate(form, CAT)

    def test_catalog_shape(self):
        slugs = [i["slug"] for i in CAT["items"]]
        self.assertEqual(len(slugs), 8)
        self.assertEqual(len(set(slugs)), 8)
        cols = {c["slug"] for c in CAT["collections"]}
        for it in CAT["items"]:
            self.assertIn(it["collection"], cols)
            self.assertEqual(it["status"], "preorder")
            self.assertTrue((REPO / "assets/static/img/merch" / f"{it['image']}-480.webp").exists())
            self.assertTrue((REPO / "assets/static/img/merch" / f"{it['image']}-960.webp").exists())

    def test_good(self):
        clean, err = self.check()
        self.assertEqual(err, {})
        self.assertEqual(clean["options"], {"size": "XL", "color": "Black"})
        self.assertEqual(clean["qty"], 2)
        self.assertEqual(clean["item_name"], "Keep the Coffee Flowin T-Shirt")

    def test_parse_qs_lists_accepted(self):
        clean, err = merch.validate({k: [v] for k, v in GOOD.items()}, CAT)
        self.assertEqual(err, {})

    def test_unknown_item(self):
        self.assertIn("item", self.check(item="free-car")[1])
        self.assertIn("item", self.check(item=None)[1])

    def test_bad_options(self):
        self.assertIn("opt_size", self.check(opt_size="XXXXL")[1])
        self.assertIn("opt_color", self.check(opt_color=None)[1])

    def test_single_value_option_defaults(self):
        form = dict(GOOD, item="spin-it-dad-hat")
        del form["opt_size"]
        clean, err = merch.validate(form, CAT)
        self.assertEqual(err, {})
        self.assertEqual(clean["options"], {"size": "One size"})   # extra opt_color is ignored

    def test_item_without_options(self):
        clean, err = merch.validate(dict(GOOD, item="spin-it-mug"), CAT)
        self.assertEqual(err, {})
        self.assertEqual(clean["options"], {})

    def test_qty_range(self):
        for bad in ("0", "11", "-1", "2.5", "x", ""):
            self.assertIn("qty", self.check(qty=bad)[1], bad)
        for ok in ("1", "10"):
            self.assertNotIn("qty", self.check(qty=ok)[1])

    def test_name_email_note(self):
        self.assertIn("name", self.check(name="   ")[1])
        self.assertIn("name", self.check(name="x" * 81)[1])
        for bad in ("nope", "a@b", "a b@c.com", "@c.com", "a@" + "b" * 260 + ".com"):
            self.assertIn("email", self.check(email=bad)[1], bad)
        self.assertIn("note", self.check(note="x" * 501)[1])
        clean, _ = self.check(name="  Ann \x00\x07 Lee  ", note=None)
        self.assertEqual(clean["name"], "Ann Lee")
        self.assertEqual(clean["note"], "")

    def test_consent_required(self):
        self.assertIn("consent", self.check(consent=None)[1])
        self.assertIn("consent", self.check(consent="no")[1])

    def test_honeypot(self):
        self.assertFalse(merch.is_spam(GOOD))
        self.assertTrue(merch.is_spam(dict(GOOD, website="http://spam")))


class Storage(unittest.TestCase):
    def setUp(self):
        use_temp_store()

    def test_append_find_delete_totals_csv(self):
        clean, _ = merch.validate(GOOD, CAT)
        a = merch.append(clean)
        b = merch.append(dict(clean, qty=3, name="=HYPERLINK(1)"))
        self.assertEqual(oct(merch.STORE.stat().st_mode & 0o777), "0o600")
        self.assertEqual(len(merch.read_all()), 2)
        self.assertEqual(merch.find(a["id"])["name"], "Test Person")
        self.assertIsNone(merch.find("not-an-id"))
        t = merch.totals(merch.read_all())
        self.assertEqual(t, [{"item": "coffee-flowin-t-shirt", "item_name": "Keep the Coffee Flowin T-Shirt",
                              "options": "XL, Black", "qty": 5, "reservations": 2}])
        csv_text = merch.to_csv(merch.read_all())
        self.assertIn("'=HYPERLINK(1)", csv_text)     # formula injection neutralised
        self.assertTrue(merch.delete(b["id"]))
        self.assertFalse(merch.delete(b["id"]))
        self.assertEqual([r["id"] for r in merch.read_all()], [a["id"]])

    def test_rate_limiter(self):
        rl = merch.RateLimiter(2, 60)
        self.assertTrue(rl.allow("1.2.3.4", 0))
        self.assertTrue(rl.allow("1.2.3.4", 1))
        self.assertFalse(rl.allow("1.2.3.4", 2))
        self.assertTrue(rl.allow("5.6.7.8", 2))
        self.assertTrue(rl.allow("1.2.3.4", 61))


def load_server():
    spec = importlib.util.spec_from_file_location("mec_admin_server", REPO / "admin" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Endpoint(unittest.TestCase):
    """POST /merch/reserve against a real server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.srv_mod = load_server()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv_mod.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        use_temp_store()
        self.srv_mod.merch.STORE_DIR, self.srv_mod.merch.STORE = merch.STORE_DIR, merch.STORE
        self.srv_mod.merch.CATALOG = REPO / "content" / "merch" / "merch.json"
        self.srv_mod.MERCH_RL_SHORT.hits.clear()
        self.srv_mod.MERCH_RL_DAY.hits.clear()
        self.srv_mod.MERCH_RL_ALL.hits.clear()

    def post(self, form, accept_json=True, ip="10.0.0.1", raw=None, origin=None):
        body = raw if raw is not None else urllib.parse.urlencode(form).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-For": ip}
        if accept_json:
            h["Accept"] = "application/json"
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.base + "/merch/reserve", data=body, headers=h, method="POST")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req, timeout=5)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_json_success(self):
        code, _, body = self.post(GOOD)
        j = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"])
        self.assertNotIn("email", j["reservation"])   # never echoed back
        rows = merch.read_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "test@example.com")

    def test_no_js_redirect_and_thanks_page(self):
        code, h, _ = self.post(GOOD, accept_json=False)
        self.assertEqual(code, 303)
        loc = h["Location"]
        self.assertTrue(loc.startswith("/merch/reserve?r="))
        page = urllib.request.urlopen(self.base + loc, timeout=5).read().decode()
        self.assertIn("Your reservation is in", page)
        self.assertIn("Keep the Coffee Flowin T-Shirt", page)
        self.assertNotIn("test@example.com", page)

    def test_validation_errors(self):
        code, _, body = self.post(dict(GOOD, qty="99", email="x"))
        self.assertEqual(code, 400)
        self.assertEqual(set(json.loads(body)["errors"]), {"qty", "email"})
        code, _, body = self.post(dict(GOOD, qty="99"), accept_json=False)
        self.assertEqual(code, 400)
        self.assertIn(b"Go back and fix it", body)
        self.assertEqual(merch.read_all(), [])

    def test_honeypot_blocks(self):
        code, _, body = self.post(dict(GOOD, website="http://spam.example"))
        self.assertEqual(code, 200)               # looks like success to the bot
        self.assertEqual(merch.read_all(), [])     # but nothing is stored

    def test_rate_limit(self):
        codes = [self.post(GOOD, ip="10.9.9.9")[0] for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])
        self.assertEqual(self.post(GOOD, ip="10.9.9.10")[0], 200)

    def test_body_cap_and_origin(self):
        code, _, _ = self.post(None, raw=b"note=" + b"x" * 9000)
        self.assertEqual(code, 413)
        self.assertEqual(self.post(GOOD, origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(GOOD, origin="https://musicedcafe.com")[0], 200)

    def test_admin_api_needs_token(self):
        for path in ("/admin/api/merch", "/admin/api/merch.csv"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertIn(cm.exception.code, (401, 503))


if __name__ == "__main__":
    unittest.main()

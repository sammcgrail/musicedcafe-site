"""Discogs integration tests (mec/discogs.py), all against a mocked HTTP layer.

Run:  python3 -m unittest discover -s tests   (or python3 tests/test_marketplace_discogs.py)

No real network call can happen: discogs._urlopen is replaced by a function
that fails the test, every Client gets a FakeDiscogs opener, DISCOGS_TOKEN is
removed from the environment, and MEC_ROOT points at a temp dir (so no .env is
read). Named to sort after test_import.py, which sets MEC_ROOT first.
"""
import email.message
import importlib.util
import io
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
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if "MEC_ROOT" not in os.environ:
    os.environ["MEC_ROOT"] = tempfile.mkdtemp(prefix="mec-test-")
TMP = Path(os.environ["MEC_ROOT"])
assert TMP.resolve() != REPO.resolve(), "tests must never run against the real repo data"
for _k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "DISCOGS_TOKEN"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(REPO))

from mec import discogs, merge, store  # noqa: E402


def _no_network(*a, **k):
    raise AssertionError("un-mocked network call")


discogs._urlopen = _no_network
USER = "music_ed_cafe"


# ----------------------------------------------------------------------------- fake API

class _Resp:
    def __init__(self, status, body: bytes, headers):
        self.status, self._body, self.headers = status, body, headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hdrs(extra=None):
    h = email.message.Message()
    for k, v in (extra or {}).items():
        h[k] = str(v)
    return h


class FakeDiscogs:
    """Minimal in-memory Discogs: search, releases, listings, inventory, orders."""

    def __init__(self):
        self.calls = []                 # (method, path, query dict, body dict|None, headers)
        self.search_results = {}        # frozenset(query items) -> list, or callable(q) -> list
        self.search_fn = None
        self.listings = {}              # id -> listing dict (owner view, incl external_id)
        self.orders = []
        self.releases = {}
        self.next_id = 9000
        self.fail = {}                  # (method, path) -> [status codes to return first]
        self.remaining = 60

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlparse(req.full_url)
        assert u.scheme == "https" and u.netloc == "api.discogs.com", req.full_url
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        body = json.loads(req.data) if req.data else None
        method, path = req.get_method(), u.path
        self.calls.append((method, path, q, body, dict(req.header_items())))
        codes = self.fail.get((method, path))
        if codes:
            code = codes.pop(0)
            raise urllib.error.HTTPError(req.full_url, code, "err", _hdrs({"X-Discogs-Ratelimit-Remaining": 0}),
                                         io.BytesIO(b'{"message": "simulated"}'))
        status, payload = self.route(method, path, q, body)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", _hdrs(), io.BytesIO(json.dumps(payload).encode()))
        raw = b"" if payload is None else json.dumps(payload).encode()
        return _Resp(status, raw, _hdrs({"X-Discogs-Ratelimit": 60, "X-Discogs-Ratelimit-Remaining": self.remaining}))

    def page(self, items, q, key):
        per, pg = int(q.get("per_page", 50)), int(q.get("page", 1))
        pages = max(1, -(-len(items) // per))
        return {"pagination": {"page": pg, "pages": pages, "items": len(items), "per_page": per},
                key: items[(pg - 1) * per: pg * per]}

    def route(self, method, path, q, body):
        if path == "/oauth/identity":
            return 200, {"id": 1, "username": USER}
        if path == "/database/search":
            key = {k: v for k, v in q.items() if k not in ("per_page", "page", "type")}
            res = self.search_fn(key) if self.search_fn else self.search_results.get(frozenset(key.items()), [])
            return 200, self.page(list(res), q, "results")
        if path.startswith("/releases/"):
            rid = int(path.rsplit("/", 1)[1])
            return (200, self.releases[rid]) if rid in self.releases else (404, {"message": "Release not found."})
        if path == "/marketplace/listings" and method == "POST":
            self.next_id += 1
            self.listings[self.next_id] = dict(body, id=self.next_id, price={"value": body["price"], "currency": "USD"},
                                               release={"id": body["release_id"]})
            return 201, {"listing_id": self.next_id, "resource_url": f"https://api.discogs.com/marketplace/listings/{self.next_id}"}
        if path.startswith("/marketplace/listings/"):
            lid = int(path.rsplit("/", 1)[1])
            if lid not in self.listings:
                return 404, {"message": "The requested resource was not found."}
            if method == "DELETE":
                del self.listings[lid]
                return 204, None
            if method == "POST":
                if self.listings[lid].get("status") == "Sold":
                    return 403, {"message": "cannot edit a sold listing"}
                self.listings[lid].update(body, price={"value": body["price"], "currency": "USD"})
                return 204, None
        if path == f"/users/{USER}/inventory":
            items = list(self.listings.values())
            if q.get("status"):
                items = [x for x in items if x.get("status", "").lower() == q["status"].lower()]
            return 200, self.page(items, q, "listings")
        if path == "/marketplace/orders":
            return 200, self.page(list(self.orders), q, "orders")
        return 404, {"message": "not found"}

    def writes(self):
        return [(m, p) for m, p, *_ in self.calls if m != "GET"]


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(fake, read_only=False, clock=None):
    clock = clock or Clock()
    return discogs.Client("test-token", read_only=read_only, urlopen=fake, sleep=clock.sleep, clock=clock)


def cand(rid, catno="SP-4658", label=("A&M Records",), fmt=("Vinyl", "LP", "Album"), year="1977",
         country="US", title="Carpenters - Passage"):
    return {"id": rid, "catno": catno, "label": list(label), "format": list(fmt), "year": year, "country": country,
            "title": title, "thumb": f"https://i.discogs.com/{rid}.jpg", "uri": f"/release/{rid}-x"}


def rec(iid="100000000001", **kw):
    r = {"id": iid, "status": "active", "kind": "record", "artist": "Carpenters", "album": "Passage",
         "label": "A&M Records", "catalog_number": "SP 4658", "format_type": "LP", "record_size": '12"',
         "speed": "33 RPM", "release_year": "1977", "pressing_year": None, "country": "United States",
         "media_grade": "VG+", "sleeve_grade": "VG", "price": "29.99", "currency": "USD",
         "path": f"/records/carpenters-passage-{iid}/", "ebay_url": f"https://www.ebay.com/itm/{iid}",
         "ebay_title": "Carpenters Passage LP 1977 A&M SP-4658 VG+", "condition_notes": "Light sleeve wear.",
         "conflicts": [], "images": []}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "data", ignore_errors=True)
        shutil.rmtree(TMP / "config", ignore_errors=True)
        shutil.copytree(REPO / "config", TMP / "config")
        (TMP / "data" / "source" / "items").mkdir(parents=True)
        (TMP / "data" / "editorial").mkdir(parents=True)
        os.environ.pop("DISCOGS_TOKEN", None)
        self.fake = FakeDiscogs()

    def ebay_ok(self, complete=True):
        store.record_attempt("browse_api", True, {"complete_snapshot": complete})

    def approve(self, iid="100000000001", rid=111, on=True):
        m = discogs.load_matches()
        m[iid] = {"status": "confident", "auto_status": "confident", "chosen_release_id": rid, "candidates": []}
        discogs.save_matches(m)
        discogs.decide(iid, "approve", by="Edwin")
        if on:
            discogs.set_listing_toggle(iid, True, by="Edwin")


# ----------------------------------------------------------------------------- tests

class NoToken(Base):
    def test_everything_skips_without_token(self):
        for fn in (discogs.match_records, discogs.sync_listings, discogs.poll):
            res = fn(records=[rec()]) if fn is not discogs.poll else fn()
            self.assertEqual(res["result"], "skipped")
            self.assertEqual(res["reason"], "skipped: no DISCOGS_TOKEN")
        self.assertIsNone(discogs.make_client())
        self.assertEqual(discogs.load_state()["last_poll"]["result"], "skipped")

    def test_cli_skips_and_prints_reason(self):
        from mec import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["discogs", "poll", "--no-build"])
        self.assertEqual(code, 0)
        self.assertIn("skipped: no DISCOGS_TOKEN", buf.getvalue())

    def test_client_refuses_empty_token(self):
        with self.assertRaises(discogs.DiscogsError):
            discogs.Client("")


class ClientBehaviour(Base):
    def test_headers_auth_and_user_agent(self):
        client(self.fake).identity()
        h = {k.lower(): v for k, v in self.fake.calls[0][4].items()}
        self.assertEqual(h["authorization"], "Discogs token=test-token")
        self.assertEqual(h["user-agent"], "MusicEdCafe/1.0 +https://musicedcafe.com")

    def test_throttle_keeps_60_per_minute(self):
        clk = Clock()
        c = client(self.fake, clock=clk)
        start = clk.t
        for _ in range(61):
            c.identity()
        self.assertGreaterEqual(clk.t - start, 60.0)   # 61 calls need >= 60 s

    def test_low_remaining_header_pauses(self):
        clk = Clock()
        self.fake.remaining = 1
        client(self.fake, clock=clk).identity()
        self.assertIn(discogs.LOW_WATER_PAUSE, clk.sleeps)

    def test_429_retried_with_backoff(self):
        clk = Clock()
        self.fake.fail[("GET", "/oauth/identity")] = [429, 429]
        self.assertEqual(client(self.fake, clock=clk).identity()["username"], USER)
        self.assertEqual(len(self.fake.calls), 3)
        self.assertTrue(any(s >= 5 for s in clk.sleeps))

    def test_429_gives_up_eventually(self):
        self.fake.fail[("GET", "/oauth/identity")] = [429] * 10
        with self.assertRaises(discogs.DiscogsError) as e:
            client(self.fake).identity()
        self.assertEqual(e.exception.status, 429)

    def test_post_500_not_retried(self):
        self.fake.fail[("POST", "/marketplace/listings")] = [500, 500]
        with self.assertRaises(discogs.DiscogsError):
            client(self.fake).create_listing({"release_id": 1, "condition": discogs.VG, "price": 1, "status": "Draft"})
        self.assertEqual(len(self.fake.calls), 1)

    def test_read_only_client_refuses_writes(self):
        c = client(self.fake, read_only=True)
        for fn in (lambda: c.create_listing({}), lambda: c.edit_listing(1, {}), lambda: c.delete_listing(1)):
            with self.assertRaises(discogs.ReadOnlyViolation):
                fn()
        self.assertEqual(self.fake.calls, [])

    def test_pagination_reads_all_pages(self):
        for i in range(250):
            self.fake.listings[i + 1] = {"id": i + 1, "status": "For Sale"}
        self.assertEqual(len(client(self.fake).inventory(USER)), 250)


class Normalising(unittest.TestCase):
    def test_catno(self):
        self.assertEqual(discogs.norm_catno("SP-4658"), discogs.norm_catno("sp 4658"))
        self.assertEqual(discogs.norm_catno("SP-4658"), "SP4658")
        self.assertEqual(discogs.norm_catno("PZ2 35517"), discogs.norm_catno("PZ2-35517"))
        self.assertNotEqual(discogs.norm_catno("SP-4658"), discogs.norm_catno("SP-4659"))
        self.assertEqual(discogs.catno_set("509990292346, TMR092"), {"509990292346", "TMR092"})
        self.assertEqual(discogs.catno_set("none"), set())

    def test_label_and_country(self):
        self.assertEqual(discogs.norm_label("A&M Records"), discogs.norm_label("A & M"))
        self.assertEqual(discogs.norm_label("Capitol Records, Inc."), "capitol")
        self.assertEqual(discogs.norm_label("Columbia (2)"), "columbia")
        self.assertEqual(discogs.norm_country("United States"), "us")
        self.assertEqual(discogs.norm_country("USA"), "us")
        self.assertEqual(discogs.norm_country("US"), "us")
        self.assertEqual(discogs.norm_country("United Kingdom"), "uk")

    def test_format_classes(self):
        self.assertEqual(discogs.record_format(rec()), "LP")
        self.assertEqual(discogs.record_format(rec(format_type="Single", record_size='7"', speed="45 RPM")), '7"')
        self.assertEqual(discogs.record_format(rec(format_type="Single", record_size='12"', speed="45 RPM")), '12" single')
        self.assertEqual(discogs.candidate_format(["Vinyl", '7"', "45 RPM", "Single"]), '7"')
        self.assertEqual(discogs.candidate_format(["CD", "Album"]), "not vinyl")
        self.assertEqual(discogs.format_compat("LP", '7"'), "inconsistent")
        self.assertEqual(discogs.format_compat("single (size unknown)", '7"'), "consistent")

    def test_title_only_year_never_used(self):
        self.assertIsNone(discogs.record_year(rec(release_year=None, listing_year="1977")))
        self.assertEqual(discogs.record_year(rec(release_year="1977", pressing_year="1980")), "1980")


class Matching(Base):
    def scored(self, r, cands):
        return [discogs.score_candidate(r, c) for c in cands]

    def test_single_exact_is_confident(self):
        r = rec()
        st, rid, _ = discogs.classify(r, self.scored(r, [cand(1)]))
        self.assertEqual((st, rid), ("confident", 1))

    def test_catno_normalisation_scores_exact(self):
        c = discogs.score_candidate(rec(catalog_number="SP 4658"), cand(1, catno="SP-4658"))
        self.assertTrue(c["signals"]["catno"])
        self.assertEqual(c["signals"]["label"], "exact")
        self.assertEqual(c["signals"]["format"], "consistent")

    def test_multi_pressing_without_country_or_year_is_review(self):
        r = rec(country=None, release_year=None)
        st, _, why = discogs.classify(r, self.scored(r, [cand(1, country="US"), cand(2, country="Canada")]))
        self.assertEqual(st, "review")
        self.assertIn("2 pressings share catno", why)

    def test_multi_pressing_same_country_and_year_is_review(self):
        r = rec()
        st, _, _ = discogs.classify(r, self.scored(r, [cand(1), cand(2)]))   # both US 1977
        self.assertEqual(st, "review")

    def test_country_disambiguates(self):
        r = rec(release_year=None)
        st, rid, why = discogs.classify(r, self.scored(r, [cand(1, country="Canada"), cand(2, country="US")]))
        self.assertEqual((st, rid), ("confident", 2))
        self.assertIn("country", why)

    def test_year_disambiguates(self):
        r = rec(release_year="1977")
        st, rid, _ = discogs.classify(r, self.scored(r, [cand(1, year="1981"), cand(2, year="1977")]))
        self.assertEqual((st, rid), ("confident", 2))

    def test_other_label_same_catno_still_blocks(self):
        # a second pressing with the same catno but a label we can't confirm: review
        r = rec()
        st, _, _ = discogs.classify(r, self.scored(r, [cand(1), cand(2, label=("Unknown Label",))]))
        self.assertEqual(st, "review")

    def test_label_mismatch_is_review(self):
        r = rec()
        st, rid, _ = discogs.classify(r, self.scored(r, [cand(1, label=("Other",))]))
        self.assertEqual((st, rid), ("review", 1))

    def test_wrong_format_is_not_confident(self):
        r = rec()
        st, _, _ = discogs.classify(r, self.scored(r, [cand(1, fmt=("CD", "Album"))]))
        self.assertNotEqual(st, "confident")
        r7 = rec(format_type="Single", record_size='7"', speed="45 RPM")
        st, _, _ = discogs.classify(r7, self.scored(r7, [cand(1)]))   # LP candidate for a 7"
        self.assertNotEqual(st, "confident")

    def test_no_candidates_is_no_match(self):
        self.assertEqual(discogs.classify(rec(), [])[0], "no_match")

    def test_truncated_results_are_review(self):
        r = rec()
        self.assertEqual(discogs.classify(r, self.scored(r, [cand(1)]), truncated=True)[0], "review")

    def test_search_order_catno_then_artist_title(self):
        hits = []

        def fn(q):
            hits.append(q)
            if "catno" in q:
                return []
            return [cand(5, catno="SP-9999")]
        self.fake.search_fn = fn
        r = rec()
        cands, _, _ = discogs.find_candidates(r, client(self.fake))
        self.assertEqual(hits[0], {"catno": "SP 4658", "label": "A&M Records"})
        self.assertEqual(hits[1], {"catno": "SP 4658", "artist": "Carpenters"})
        self.assertEqual(hits[2], {"artist": "Carpenters", "release_title": "Passage", "format": "Vinyl", "year": "1977"})
        self.assertEqual(cands[0]["release_id"], 5)

    def test_no_artist_title_search_when_catno_hits(self):
        self.fake.search_fn = lambda q: [cand(1)]
        discogs.find_candidates(rec(), client(self.fake))
        self.assertEqual(len(self.fake.calls), 1)

    def test_match_records_is_dry_run_and_keeps_decisions(self):
        self.fake.search_fn = lambda q: [cand(1)]
        recs = [rec("1"), rec("2"), rec("3")]
        m = {"2": {"status": "approved", "approved_release_id": 77, "approved_by": "Edwin", "approved_at": "x",
                   "fingerprint": discogs.fingerprint(recs[1])}}
        discogs.save_matches(m)
        res = discogs.match_records(recs, client(self.fake))
        self.assertEqual(res["matched"], 2)
        out = discogs.load_matches()
        self.assertEqual(out["1"]["status"], "confident")
        self.assertEqual(out["2"]["approved_release_id"], 77)       # untouched
        self.assertEqual(self.fake.writes(), [])                     # never lists anything
        self.assertEqual(discogs.load_listings(), {})
        self.assertLessEqual(len(out["1"]["candidates"]), 5)
        self.assertTrue(out["1"]["candidates"][0]["url"].startswith("https://www.discogs.com/"))

    def test_decision_during_match_run_survives(self):
        recs = [rec("1"), rec("2")]

        def fn(q):
            if not discogs.load_matches().get("2"):   # Edwin approves "2" mid-run
                discogs.save_matches({"2": {"status": "approved", "approved_release_id": 9, "approved_by": "E",
                                            "approved_at": "t"}})
            return [cand(1)]
        self.fake.search_fn = fn
        discogs.match_records(recs, client(self.fake), rematch=True)
        self.assertEqual(discogs.load_matches()["2"]["status"], "approved")

    def test_rerun_skips_unchanged_records(self):
        self.fake.search_fn = lambda q: [cand(1)]
        discogs.match_records([rec()], client(self.fake))
        n = len(self.fake.calls)
        discogs.match_records([rec()], client(self.fake))
        self.assertEqual(len(self.fake.calls), n)


class Decisions(Base):
    def setUp(self):
        super().setUp()
        discogs.save_matches({"1": {"status": "review", "auto_status": "review", "chosen_release_id": 10,
                                    "candidates": [{"release_id": 10}, {"release_id": 11}]},
                              "2": {"status": "confident", "auto_status": "confident", "chosen_release_id": 20,
                                    "candidates": [{"release_id": 20}]},
                              "3": {"status": "confident", "auto_status": "confident", "chosen_release_id": 30,
                                    "candidates": [{"release_id": 30}]}})

    def test_approve_pick_manual_skip(self):
        m = discogs.decide("1", "pick", release_id=11, by="Edwin")
        self.assertEqual((m["status"], m["approved_release_id"], m["approved_by"]), ("approved", 11, "Edwin"))
        self.assertTrue(m["approved_at"])
        with self.assertRaises(ValueError):
            discogs.decide("1", "pick", release_id=99)
        self.releases = self.fake.releases[4242] = {"id": 4242, "title": "Passage", "artists": [{"name": "Carpenters"}],
                                                    "year": 1977, "country": "US", "uri": "https://www.discogs.com/release/4242"}
        m = discogs.decide("1", "manual", release_id="4242", by="Edwin", client=client(self.fake, read_only=True))
        self.assertEqual(m["approved_release_id"], 4242)
        self.assertTrue(m["manual_release"]["verified"])
        with self.assertRaises(ValueError):
            discogs.decide("1", "manual", release_id=5, client=client(self.fake, read_only=True))  # 404
        m = discogs.decide("1", "skip")
        self.assertEqual(m["status"], "skipped")
        self.assertFalse(m["list_on_discogs"])

    def test_toggle_requires_approval(self):
        with self.assertRaises(ValueError):
            discogs.set_listing_toggle("2", True)
        discogs.decide("2", "approve")
        self.assertTrue(discogs.set_listing_toggle("2", True)["list_on_discogs"])

    def test_approve_all_confident_needs_matching_count(self):
        with self.assertRaises(ValueError):
            discogs.approve_all_confident(expected=5)
        self.assertEqual(discogs.approve_all_confident(expected=2, by="Edwin")["approved"], 2)
        m = discogs.load_matches()
        self.assertEqual(m["2"]["status"], "approved")
        self.assertEqual(m["1"]["status"], "review")
        self.assertIn("Edwin", m["3"]["approved_by"])
        self.assertFalse(m["2"].get("list_on_discogs"))   # approval alone never lists


class Conditions(unittest.TestCase):
    def test_grade_mapping(self):
        self.assertEqual(discogs.map_grade("Strong VG+")[0], "Very Good Plus (VG+)")
        self.assertEqual(discogs.map_grade("VG+")[0], "Very Good Plus (VG+)")
        self.assertEqual(discogs.map_grade("Very Good Plus (VG+)")[0], "Very Good Plus (VG+)")
        self.assertEqual(discogs.map_grade("NM")[0], "Near Mint (NM or M-)")
        self.assertEqual(discogs.map_grade("M-")[0], "Near Mint (NM or M-)")
        self.assertEqual(discogs.map_grade("Mint")[0], "Mint (M)")
        self.assertEqual(discogs.map_grade("G+")[0], "Good Plus (G+)")
        self.assertEqual(discogs.map_grade("Fair")[0], "Fair (F)")
        v, note = discogs.map_grade("EX")
        self.assertEqual(v, "Very Good Plus (VG+)")      # rounded down, never up
        self.assertIn("no Discogs equivalent", note)
        self.assertEqual(discogs.map_grade("VG-")[0], "Good Plus (G+)")
        self.assertEqual(discogs.map_grade("Generic", sleeve=True)[0], "Generic")
        self.assertIsNone(discogs.map_grade("Generic")[0])          # not a vinyl grade
        self.assertIsNone(discogs.map_grade("plays great")[0])
        self.assertEqual(discogs.map_grade(None), (None, "missing"))
        for v in discogs.GRADE_MAP.values():
            self.assertIn(v, discogs.MEDIA_CONDITIONS)

    def test_unknown_grade_leaves_listing_out_and_flags(self):
        m = {"status": "approved", "approved_release_id": 1, "approved_by": "E", "approved_at": "t", "list_on_discogs": True}
        fields, problems, _ = discogs.desired_listing(rec(media_grade="plays great"), m, discogs.load_settings())
        self.assertIsNone(fields)
        self.assertTrue(any("vinyl grade" in p for p in problems))
        fields, problems, _ = discogs.desired_listing(rec(media_grade=None), m, discogs.load_settings())
        self.assertIsNone(fields)

    def test_sleeve_rules(self):
        lp_no_sleeve = rec(sleeve_grade=None)
        self.assertTrue(discogs.conditions_for(lp_no_sleeve)["problems"])
        single = rec(format_type="Single", record_size='7"', speed="45 RPM", sleeve_grade=None,
                     description_text="Comes in a generic company sleeve.")
        self.assertEqual(discogs.conditions_for(single)["sleeve_condition"], "Generic")
        pic = rec(format_type="Single", record_size='7"', sleeve_grade=None,
                  description_text="Original picture sleeve plus generic company sleeve.")
        self.assertIsNone(discogs.conditions_for(pic)["sleeve_condition"])
        self.assertEqual(discogs.conditions_for(lp_no_sleeve, {"sleeve_condition": "Not Graded"})["sleeve_condition"],
                         "Not Graded")


class Pricing(unittest.TestCase):
    def p(self, price, **s):
        return str(discogs.apply_price_rule(price, dict(discogs.DEFAULT_SETTINGS, **s)))

    def test_same_as_ebay(self):
        self.assertEqual(self.p("29.99"), "29.99")
        self.assertEqual(self.p("12"), "12.00")

    def test_percent_and_rounding(self):
        self.assertEqual(self.p("29.99", price_mode="percent", price_percent=-10, price_rounding=".99"), "26.99")
        self.assertEqual(self.p("12.00", price_mode="percent", price_percent=10, price_rounding=".99"), "12.99")
        self.assertEqual(self.p("29.99", price_mode="percent", price_percent=-10, price_rounding=".00"), "27.00")
        self.assertEqual(self.p("10.05", price_mode="percent", price_percent=-10, price_rounding="none"), "9.05")
        self.assertEqual(self.p("1.00", price_mode="percent", price_percent=-50, price_rounding=".99"), "0.99")
        self.assertEqual(self.p("1.00", price_mode="percent", price_percent=-80, price_rounding=".00"), "1.00")

    def test_bad_prices(self):
        self.assertIsNone(discogs.apply_price_rule(None))
        self.assertIsNone(discogs.apply_price_rule("0"))
        self.assertIsNone(discogs.apply_price_rule("abc"))


class SettingsAndComments(Base):
    def test_validation(self):
        _, errs = discogs.save_settings({"listing_status": "Live"})
        self.assertTrue(errs)
        _, errs = discogs.save_settings({"comment_template": "Vinyl {vinyl"})
        self.assertTrue(errs)
        s, errs = discogs.save_settings({"price_mode": "percent", "price_percent": -5, "allow_offers": True})
        self.assertEqual(errs, [])
        self.assertEqual(discogs.load_settings()["price_percent"], -5.0)
        self.assertEqual(discogs.load_settings()["listing_status"], "Draft")   # default stays Draft

    def test_comment(self):
        c = discogs.conditions_for(rec())
        text = discogs.render_comment(rec(), c, discogs.load_settings())
        self.assertIn("Vinyl: Very Good Plus (VG+)", text)
        self.assertIn("Ships from Lynn, MA", text)
        self.assertIn("https://musicedcafe.com/records/carpenters-passage-100000000001/", text)
        self.assertIn("Light sleeve wear.", text)


class ListingSync(Base):
    IID = "100000000001"

    def run_sync(self, records=None, **kw):
        return discogs.sync_listings(records or [rec(self.IID)], client(self.fake), **kw)

    def test_create_is_idempotent(self):
        self.ebay_ok()
        self.approve(self.IID)
        res = self.run_sync()
        self.assertEqual(res["done"], {"create": 1})
        posts = [c for c in self.fake.calls if c[0] == "POST"]
        body = posts[0][3]
        self.assertEqual(body["external_id"], self.IID)
        self.assertEqual(body["status"], "Draft")
        self.assertEqual(body["release_id"], 111)
        self.assertEqual(body["condition"], "Very Good Plus (VG+)")
        self.assertEqual(body["sleeve_condition"], "Very Good (VG)")
        self.assertEqual(body["price"], 29.99)
        L = discogs.load_listings()[self.IID]
        self.assertEqual(L["listing_id"], 9001)
        n = len(self.fake.calls)
        res = self.run_sync()
        self.assertEqual(res["done"], {})
        self.assertEqual(self.fake.writes(), [("POST", "/marketplace/listings")])   # still just the one create
        self.assertGreater(len(self.fake.calls), n)                                # it did read the inventory

    def test_crash_after_create_adopts_instead_of_duplicating(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.fake.listings[555] = {"id": 555, "status": "Draft", "external_id": self.IID, "release": {"id": 111}}
        res = self.run_sync()
        self.assertEqual(res["done"], {"adopt": 1})
        self.assertNotIn(("POST", "/marketplace/listings"), self.fake.writes())
        self.assertEqual(discogs.load_listings()[self.IID]["listing_id"], 555)

    def test_price_and_condition_changes_update(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.run_sync()
        self.fake.listings[9001]["status"] = "For Sale"     # Edwin published the draft on Discogs
        res = self.run_sync([rec(self.IID, price="24.99", media_grade="VG")])
        self.assertEqual(res["done"], {"update": 1})
        edit = [c for c in self.fake.calls if c[0] == "POST" and c[1] == "/marketplace/listings/9001"][-1][3]
        self.assertEqual(edit["price"], 24.99)
        self.assertEqual(edit["condition"], "Very Good (VG)")
        self.assertEqual(edit["status"], "For Sale")        # his choice is kept

    def test_nothing_listed_without_stored_approval(self):
        self.ebay_ok()
        # a hand-edited matches.json with the toggle on but no human approval
        discogs.save_matches({self.IID: {"status": "confident", "chosen_release_id": 1, "list_on_discogs": True}})
        self.run_sync()
        self.assertEqual(self.fake.writes(), [])
        # approved but toggle off
        self.approve(self.IID, on=False)
        self.run_sync()
        self.assertEqual(self.fake.writes(), [])

    def test_unknown_grade_blocked_not_listed(self):
        self.ebay_ok()
        self.approve(self.IID)
        res = self.run_sync([rec(self.IID, media_grade="plays great")])
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(res["planned"], {"blocked": 1})

    def test_ended_on_ebay_deletes_after_complete_snapshot(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.run_sync()
        res = self.run_sync([rec(self.IID, status="ended")])
        self.assertEqual(res["done"], {"delete": 1})
        self.assertIn(("DELETE", "/marketplace/listings/9001"), self.fake.writes())
        self.assertEqual(discogs.load_listings()[self.IID]["status"], "deleted")

    def test_partial_snapshot_never_deletes(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.run_sync()
        self.ebay_ok(complete=False)
        res = self.run_sync([rec(self.IID, status="ended")])
        self.assertNotIn(("DELETE", "/marketplace/listings/9001"), self.fake.writes())
        self.assertEqual(res["planned"], {"deferred_delete": 1})
        self.assertIn(9001, self.fake.listings)

    def test_failed_ebay_sync_changes_nothing(self):
        self.approve(self.IID)
        store.record_attempt("browse_api", False, {}, ["boom"])
        res = self.run_sync()
        self.assertEqual(res["result"], "refused")
        self.assertEqual(self.fake.calls, [])

    def test_failed_inventory_read_changes_nothing(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.fake.fail[("GET", f"/users/{USER}/inventory")] = [503] * 10
        res = self.run_sync()
        self.assertEqual(res["result"], "failed")
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(discogs.load_listings(), {})

    def test_toggle_off_unlists(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.run_sync()
        discogs.set_listing_toggle(self.IID, False)
        res = self.run_sync()
        self.assertEqual(res["done"], {"unlist": 1})
        self.assertEqual(self.fake.listings[9001]["status"], "Draft")
        discogs.set_listing_toggle(self.IID, True)
        res = self.run_sync()
        self.assertEqual(res["done"], {"update": 1})

    def test_mass_delete_guard(self):
        self.ebay_ok()
        ids = [str(100000000010 + i) for i in range(5)]
        for i in ids:
            self.approve(i)
        self.run_sync([rec(i) for i in ids])
        res = self.run_sync([rec(i, status="ended") for i in ids])
        self.assertEqual(res["result"], "refused")
        self.assertFalse(any(m == "DELETE" for m, _ in self.fake.writes()))

    def test_sold_on_discogs_not_relisted_before_poll(self):
        self.ebay_ok()
        self.approve(self.IID)
        self.run_sync()
        self.fake.listings[9001]["status"] = "Sold"
        self.run_sync()                                       # marks the listing sold
        self.assertEqual(discogs.load_listings()[self.IID]["status"], "sold")
        res = self.run_sync()                                 # record still "active" locally
        self.assertEqual(self.fake.writes(), [("POST", "/marketplace/listings")])
        self.assertEqual(res["planned"], {"blocked": 1})


class CrossSale(Base):
    IID = "100000000001"

    def seed(self, qty=1):
        cs = store.ChangeSet()
        store.apply_active_snapshot(cs, [{"item_id": self.IID, "title": "Carpenters Passage LP", "price": "29.99",
                                          "currency": "USD", "quantity": qty, "raw": {"price": {"value": "29.99"}}}],
                                    "browse_api", complete=True)
        cs.commit()
        discogs.save_listings({self.IID: {"listing_id": 9001, "status": "For Sale", "created_at": "2026-10-01T00:00:00Z",
                                          "pushed": {"release_id": 111}}})

    def sold_order(self, status="New Order", lid=9001):
        self.fake.orders.append({"id": "123-1", "status": status, "created": "2026-10-20T09:00:00-07:00",
                                 "items": [{"id": lid, "release": {"id": 111}, "price": {"value": 27.5, "currency": "USD"}}],
                                 "buyer": {"username": "someone"}, "shipping_address": "SECRET ADDRESS"})

    def test_discogs_sale_marks_sold_and_survives_ebay_sync(self):
        self.seed()
        self.sold_order()
        with mock.patch("mec.site.build", return_value={"built": True}) as b:
            res = discogs.poll(client(self.fake, read_only=True))
        self.assertEqual(res["new_sales"], 1)
        b.assert_called_once()                                          # site rebuilt
        self.assertEqual(self.fake.writes(), [])                        # poll only reads
        it = store.load_items()[self.IID]
        self.assertEqual((it["status"], it["status_source"]), ("sold", "discogs"))
        self.assertEqual(it["discogs_sale"]["order_id"], "123-1")
        self.assertEqual(it["sold_at"], "2026-10-20")
        self.assertNotIn("SECRET ADDRESS", json.dumps(it))             # buyer data never stored
        self.assertNotIn("someone", json.dumps(it))
        alerts = discogs.load_alerts()
        self.assertEqual(alerts[0]["type"], "discogs_sale")
        self.assertEqual(alerts[0]["links"]["discogs_order"], "https://www.discogs.com/sell/order/123-1")
        self.assertEqual([x["id"] for x in discogs.end_on_ebay_list()], [self.IID])
        # the eBay listing is still live (Edwin hasn't ended it): a complete eBay sync
        cs = store.ChangeSet()
        store.apply_active_snapshot(cs, [{"item_id": self.IID, "title": "Carpenters Passage LP", "price": "29.99",
                                          "currency": "USD", "quantity": 1, "raw": {}}], "browse_api", complete=True)
        cs.commit()
        it = store.load_items()[self.IID]
        self.assertEqual((it["status"], it["status_source"]), ("sold", "discogs"))
        self.assertEqual(it["discogs_sale"]["ebay_quantity"], 1)
        self.assertEqual(it["price"]["value"], "27.50")                 # sale price kept
        recs = {r["id"]: r for r in merge.build_records()[0]}
        self.assertEqual(recs[self.IID]["status"], "sold")              # site shows it as Sold
        # Edwin ends it on eBay: it leaves the next complete snapshot
        cs = store.ChangeSet()
        store.apply_active_snapshot(cs, [], "browse_api", complete=True)
        cs.commit()
        it = store.load_items()[self.IID]
        self.assertEqual(it["status"], "sold")
        self.assertTrue(it["discogs_sale"]["ebay_listing_gone_at"])
        self.assertEqual(discogs.end_on_ebay_list(), [])

    def test_poll_is_idempotent(self):
        self.seed()
        self.sold_order()
        discogs.poll(client(self.fake, read_only=True), rebuild=False)
        res = discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(res["new_sales"], 0)
        self.assertEqual(len([a for a in discogs.load_alerts() if a["type"] == "discogs_sale"]), 1)
        self.assertEqual(len(store.load_items()[self.IID]["sales"]), 1)

    def test_sold_inventory_also_detects(self):
        self.seed()
        self.fake.listings[9001] = {"id": 9001, "status": "Sold", "external_id": self.IID,
                                    "price": {"value": 29.99, "currency": "USD"}, "release": {"id": 111}}
        res = discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(res["new_sales"], 1)
        self.assertEqual(store.load_items()[self.IID]["status"], "sold")

    def test_unmapped_and_cancelled_orders_ignored(self):
        self.seed()
        self.sold_order(lid=4444)                       # not one of our listings
        self.sold_order(status="Cancelled (Per Buyer's Request)")
        res = discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(res["new_sales"], 0)
        self.assertEqual(store.load_items()[self.IID]["status"], "active")

    def test_no_listings_means_no_order_calls(self):
        res = discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(res["result"], "ok")
        self.assertEqual([c[1] for c in self.fake.calls], ["/oauth/identity"])

    def test_poll_requires_read_only_client(self):
        with self.assertRaises(discogs.ReadOnlyViolation):
            discogs.poll(client(self.fake), rebuild=False)

    def test_poll_failure_changes_nothing(self):
        self.seed()
        self.sold_order()
        self.fake.fail[("GET", "/marketplace/orders")] = [503] * 10
        res = discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(res["result"], "failed")
        self.assertEqual(store.load_items()[self.IID]["status"], "active")

    def test_relist_needs_ebay_quantity_and_confirmation(self):
        self.seed()
        self.sold_order()
        discogs.poll(client(self.fake, read_only=True), rebuild=False)
        with self.assertRaises(ValueError):
            discogs.confirm_relist(self.IID, by="Edwin")       # no eBay snapshot since the sale
        cs = store.ChangeSet()                                 # eBay shows quantity 0 -> still refused
        store.apply_active_snapshot(cs, [{"item_id": self.IID, "title": "t", "price": "29.99", "currency": "USD",
                                          "quantity": 0, "raw": {}}], "browse_api", complete=True)
        cs.commit()
        with self.assertRaises(ValueError):
            discogs.confirm_relist(self.IID, by="Edwin")
        self.assertEqual(store.load_items()[self.IID]["status"], "sold")
        cs = store.ChangeSet()
        store.apply_active_snapshot(cs, [{"item_id": self.IID, "title": "t", "price": "29.99", "currency": "USD",
                                          "quantity": 1, "raw": {}}], "browse_api", complete=True)
        cs.commit()
        discogs.confirm_relist(self.IID, by="Edwin")
        it = store.load_items()[self.IID]
        self.assertEqual(it["status"], "active")
        self.assertNotIn("discogs_sale", it)
        # the old Sold order / inventory entry must not re-mark it
        self.fake.listings[9001] = {"id": 9001, "status": "Sold", "external_id": self.IID}
        discogs.poll(client(self.fake, read_only=True), rebuild=False)
        self.assertEqual(store.load_items()[self.IID]["status"], "active")

    def test_ebay_sale_report_after_discogs_sale_flags_double_sale(self):
        self.seed()
        self.sold_order()
        discogs.poll(client(self.fake, read_only=True), rebuild=False)
        cs = store.ChangeSet()
        store.apply_sales(cs, [{"item_id": self.IID, "title": "t", "price": "29.99", "currency": "USD",
                                "quantity": 1, "date": "2026-10-20", "raw": {}}], "transaction_report")
        cs.commit()
        it = store.load_items()[self.IID]
        self.assertEqual(it["status_source"], "discogs")
        self.assertEqual([x["id"] for x in discogs.overview()["double_sales"]], [self.IID])

    def test_alert_hook(self):
        got = []
        discogs.register_alert_hook(got.append)
        try:
            discogs.add_alert("test", "hello")
        finally:
            discogs.ALERT_HOOKS.remove(got.append)
        self.assertEqual(got[0]["message"], "hello")
        self.assertEqual(discogs.ack_alerts(), 1)


# ----------------------------------------------------------------------------- admin

def load_server():
    os.environ["ADMIN_TOKEN"] = "test-admin-token"
    spec = importlib.util.spec_from_file_location("mec_admin_server_dc", REPO / "admin" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TOKEN = "test-admin-token"
    return mod


class Admin(Base):
    @classmethod
    def setUpClass(cls):
        cls.srv = load_server()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None, token="test-admin-token"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_auth_required(self):
        with mock.patch.object(self.srv.time, "sleep"):
            code, _ = self.call("/admin/api/discogs", token="wrong")
            self.assertEqual(code, 401)
            code, _ = self.call("/admin/api/discogs/decide", {"id": "1", "action": "approve"}, token="wrong")
            self.assertEqual(code, 401)

    def test_overview_and_decisions(self):
        discogs.save_matches({"1": {"status": "confident", "auto_status": "confident", "chosen_release_id": 5,
                                    "candidates": [{"release_id": 5}]}})
        code, j = self.call("/admin/api/discogs")
        self.assertEqual(code, 200)
        self.assertFalse(j["token_present"])
        self.assertEqual(j["match_counts"], {"confident": 1})
        code, j = self.call("/admin/api/discogs/approve-confident", {"by": "Edwin"})
        self.assertEqual(code, 400)                                   # needs the confirmed count
        code, j = self.call("/admin/api/discogs/approve-confident", {"by": "Edwin", "expected": 1})
        self.assertEqual((code, j["approved"]), (200, 1))
        code, j = self.call("/admin/api/discogs/toggle", {"id": "1", "on": True, "by": "Edwin"})
        self.assertEqual(code, 200)
        self.assertTrue(discogs.load_matches()["1"]["list_on_discogs"])
        code, j = self.call("/admin/api/discogs/decide", {"id": "1", "action": "pick", "release_id": 999})
        self.assertEqual(code, 400)
        code, j = self.call("/admin/api/discogs/settings", {"listing_status": "Nope"})
        self.assertEqual(code, 400)

    def test_check_now_and_sync_skip_without_token(self):
        code, j = self.call("/admin/api/discogs/check-now", {})
        self.assertEqual((code, j["result"]), (200, "skipped"))
        code, j = self.call("/admin/api/discogs/plan")
        self.assertEqual((code, j["result"]), (200, "skipped"))

    def test_scheduler_poll_is_read_only_and_inert(self):
        self.srv.discogs_scheduler(interval=1, once=True)              # no token: skipped
        self.assertEqual(discogs.load_state()["last_poll"]["result"], "skipped")
        made = []
        real = discogs.make_client

        def spy(read_only=False):
            made.append(read_only)
            return client(self.fake, read_only=read_only)
        with mock.patch.object(discogs, "make_client", spy):
            self.srv.discogs_scheduler(interval=1, once=True)
        self.assertEqual(made, [True])                                 # the scheduler only ever gets a read-only client
        self.assertEqual(self.fake.writes(), [])
        self.assertIs(discogs.make_client, real)


# ----------------------------------------------------------------------------- quick pick

def entry(r, cands, status="review", **kw):
    sc = sorted((discogs.score_candidate(r, c) for c in cands), key=lambda c: -c["score"])
    e = {"status": status, "auto_status": status, "chosen_release_id": sc[0]["release_id"] if sc else None,
         "candidates": sc, "fingerprint": discogs.fingerprint(r), "approved_release_id": None,
         "approved_by": None, "approved_at": None}
    e.update(kw)
    return e


def comm(have, **kw):
    c = cand(**kw)
    c["community"] = {"have": have, "want": 1}
    return c


class QuickPick(Base):
    def pick(self, r, cands):
        return discogs.quick_order(r, [discogs.score_candidate(r, c) for c in cands])

    def test_country_filter_beats_haves(self):
        q = self.pick(rec(), [comm(5, rid=1, country="US"), comm(900, rid=2, country="UK")])
        self.assertEqual(q["chosen"], 1)
        self.assertIn("country United States: 1 of 2", q["steps"][0])

    def test_country_filter_skipped_when_nothing_from_that_country(self):
        q = self.pick(rec(), [comm(5, rid=1, country="UK"), comm(9, rid=2, country="Canada")])
        self.assertEqual(q["chosen"], 2)
        self.assertIn("no pressing from the record's country", q["warnings"])

    def test_exact_format_before_era(self):
        # an older 12" (no LP descriptor) loses to the LP: format is step (b), era (c)
        q = self.pick(rec(release_year=None), [comm(50, rid=1, fmt=("Vinyl", '12"', '33 ⅓ RPM'), year="1975"),
                                               comm(1, rid=2, fmt=("Vinyl", "LP", "Album"), year="1980")])
        self.assertEqual(q["chosen"], 2)
        # a 7" record: the 45 RPM 7" wins over a 7" at 33 (speed contradiction)
        r7 = rec(format_type="Single", record_size='7"', speed="45 RPM", release_year=None)
        q = self.pick(r7, [comm(99, rid=3, fmt=("Vinyl", '7"', "33 ⅓ RPM")), comm(1, rid=4, fmt=("Vinyl", '7"', "45 RPM"))])
        self.assertEqual(q["chosen"], 4)

    def test_original_era(self):
        cands = [comm(900, rid=1, year="1976"), comm(5, rid=2, year="1979"), comm(800, rid=3, year="1985"),
                 comm(999, rid=4, year="")]
        # release year 1978, no pressing dated 1978: the earliest at or after it
        q = self.pick(rec(release_year="1978"), cands)
        self.assertEqual(q["chosen"], 2)
        self.assertTrue(any("earliest at or after 1978: 1979" in s for s in q["steps"]))
        # release year known and dated exactly: the year filter (a) picks it
        q = self.pick(rec(release_year="1985"), cands)
        self.assertEqual(q["chosen"], 3)
        # release year unknown: the earliest overall
        q = self.pick(rec(release_year=None), cands)
        self.assertEqual(q["chosen"], 1)
        # every pressing is older than the release year: the earliest overall
        q = self.pick(rec(release_year="1990"), cands)
        self.assertEqual(q["chosen"], 1)

    def test_have_tie_break(self):
        q = self.pick(rec(), [comm(10, rid=1), comm(500, rid=2), comm(70, rid=3)])
        self.assertEqual((q["chosen"], q["tie_break"], q["ranked"]), (2, "have", [2, 3, 1]))

    def test_have_from_release_cache_and_fallback(self):
        cands = [cand(5), cand(6)]
        q = self.pick(rec(), cands)
        self.assertEqual(q["chosen"], 5)                             # no haves: score, then lowest id
        self.assertTrue(q["tie_break"].startswith("have missing"))
        self.assertEqual(sorted(q["missing_have"]), [5, 6])
        discogs.write_json(discogs.release_path(6), {"id": 6, "community": {"have": 40}})
        discogs.write_json(discogs.release_path(5), {"id": 5, "community": {"have": 3}})
        q = self.pick(rec(), cands)
        self.assertEqual((q["chosen"], q["tie_break"]), (6, "have"))

    def test_title_mismatch_preferred_away_and_flagged(self):
        q = self.pick(rec(), [comm(900, rid=1, title="Carpenters - Something Else"), comm(1, rid=2)])
        self.assertEqual(q["chosen"], 2)
        q = self.pick(rec(album="Lou Rawls Live!", artist="Lou Rawls"), [comm(1, rid=3, title="Lou Rawls - Live!")])
        self.assertEqual(q["warnings"], [])
        q = self.pick(rec(), [comm(1, rid=4, title="Carpenters - Horizon")])
        self.assertIn(discogs.TITLE_WARNING, q["warnings"])

    def test_no_shared_catno_is_excluded(self):
        self.assertIsNone(self.pick(rec(), [cand(1, catno="OTHER-1")]))

    def test_apply_stores_quick_and_never_lists_by_itself(self):
        self.ebay_ok()
        r = rec("100000000001")
        discogs.save_matches({
            "100000000001": entry(r, [comm(10, rid=1), comm(50, rid=2)]),
            "100000000002": entry(r, [], status="no_match"),
            "100000000003": entry(r, [cand(7)], status="confident"),                      # US 1977: exact
            "100000000004": entry(rec(release_year="1980"), [cand(8)], status="confident"),  # year differs
        })
        dry = discogs.quickpick()
        self.assertEqual((dry["result"], dry["would_pick"]), ("dry_run", 1))
        self.assertEqual(discogs.load_matches()["100000000001"]["status"], "review")   # dry run wrote nothing
        with self.assertRaises(ValueError):
            discogs.quickpick(apply=True, expected=5)
        res = discogs.quickpick(apply=True, expected=1)
        self.assertEqual(res["picked"], 1)
        m = discogs.load_matches()
        q = m["100000000001"]
        self.assertEqual((q["status"], q["chosen_release_id"], q["approved_release_id"]), ("quick", 2, 2))
        self.assertIs(q["pressing_verified"], False)
        self.assertEqual(q["approved_by"], "edwin-quick-rule")
        self.assertTrue(q["approved_at"] and q["quick_reason"])
        self.assertEqual(m["100000000002"]["status"], "no_match")
        self.assertNotIn("pressing_verified", m["100000000002"])
        self.assertEqual(m["100000000003"]["status"], "confident")
        self.assertIs(m["100000000003"]["pressing_verified"], True)
        self.assertIs(m["100000000004"]["pressing_verified"], False)
        # still nothing listed: the per-record toggle is required
        discogs.sync_listings([r], client(self.fake))
        self.assertEqual(self.fake.writes(), [])
        discogs.set_listing_toggle("100000000001", True, by="Edwin")
        discogs.sync_listings([r], client(self.fake))
        body = [c for c in self.fake.calls if c[0] == "POST"][0][3]
        self.assertEqual((body["release_id"], body["status"]), (2, "Draft"))
        self.assertTrue(body["comments"].endswith(discogs.UNVERIFIED_NOTE))

    def test_rematch_never_overwrites_quick(self):
        r = rec()
        discogs.save_matches({r["id"]: entry(r, [comm(1, rid=1), comm(2, rid=2)])})
        discogs.quickpick(apply=True)
        self.fake.search_fn = lambda q: [cand(77)]
        discogs.match_records([r], client(self.fake, read_only=True), rematch=True)
        self.assertEqual(discogs.load_matches()[r["id"]]["status"], "quick")

    def test_enrich_fetches_only_tied_top3_once(self):
        r = rec()
        discogs.save_matches({"1": entry(r, [cand(i) for i in (1, 2, 3, 4)]),
                              "2": entry(r, [cand(9)])})   # single pressing: nothing to fetch
        for i in (1, 2, 3, 4, 9):
            self.fake.releases[i] = {"id": i, "title": "Passage", "community": {"have": i * 10, "want": 1},
                                     "identifiers": [{"type": "Matrix / Runout", "value": f"SP-4658-A{i}"}]}
        c = client(self.fake, read_only=True)
        res = discogs.enrich_releases(c, top=3)
        self.assertEqual((res["fetched"], res["planned"]), (3, 3))
        self.assertEqual(sorted(int(p.rsplit("/", 1)[1]) for m, p, *_ in self.fake.calls), [1, 2, 3])
        self.assertEqual(discogs.enrich_releases(c, top=3)["planned"], 0)     # cached
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(discogs.quickpick()["tie_breaks"], {"have (known for 3 of 4 tied)": 1, "none needed": 1})


class PressingComment(Base):
    def test_unverified_and_verified_lines(self):
        r, settings = rec(), discogs.load_settings()
        cond = discogs.conditions_for(r)
        text = discogs.render_comment(r, cond, settings, {"pressing_verified": False})
        self.assertTrue(text.endswith("\nPressing variant not verified; see photos on the listing page."))
        m = {"pressing_verified": True, "matrix": {"side_a": "M7-907RA1", "side_b": "M7-907RB1 MASTERED BY CAPITOL"}}
        text = discogs.render_comment(r, cond, settings, m)
        self.assertTrue(text.endswith("\nMatrix/Runout: A: M7-907RA1 / B: M7-907RB1 MASTERED BY CAPITOL"))
        self.assertNotIn("not verified", text)
        # verified by exact country+year+catno, no runout typed: no line at all
        text = discogs.render_comment(r, cond, settings, {"pressing_verified": True})
        self.assertNotIn("Matrix", text)
        self.assertNotIn("not verified", text)

    def test_pressing_line_survives_the_length_cap(self):
        r = rec(condition_notes="x " * 2000)
        s = dict(discogs.load_settings(), comment_template="{notes}{notes}{notes}")
        text = discogs.render_comment(r, discogs.conditions_for(r), s, None)
        self.assertLessEqual(len(text), discogs.COMMENT_MAX)
        self.assertTrue(text.endswith(discogs.UNVERIFIED_NOTE))


# ----------------------------------------------------------------------------- verify pressing

def release(rid, *idents, country="US", year=1977):
    return {"id": rid, "title": "Diana", "year": year, "country": country, "notes": "Pressed at Keel.",
            "labels": [{"name": "Motown", "catno": "M8-936M1"}], "community": {"have": 100, "want": 5},
            "identifiers": [{"type": t, "description": d, "value": v} for t, d, v in idents]}


R1 = release(1, ("Matrix / Runout", "Side A", "M7-907RA1 MASTERED BY CAPITOL"),
             ("Matrix / Runout", "Side B", "M7-907RB1 MASTERED BY CAPITOL"), ("Barcode", "", "M7-907RA2"))
R2 = release(2, ("Matrix / Runout", "Side A", "M7 - 907 - RA2 STERLING"),
             ("Matrix / Runout", "Side B", "M7 - 907 - RB2 STERLING"),
             ("Lacquer Cut Code", "", "△ 17721"))
R3 = release(3, ("Barcode", "", "0 7464-12345"))


class VerifyMatching(Base):
    def test_normalisation(self):
        n = discogs.norm_matrix
        self.assertEqual(n("M7-907RA1"), n("m7 907 ra1"))
        self.assertEqual(n("M7-907RA1"), n("M7–907–RA1"))           # en dashes
        self.assertEqual(n("△ 17721"), n("Δ-17721"))                 # △ and Δ
        self.assertEqual(n("▽17721"), n("delta 17721"))
        self.assertEqual(n("Mastered by Capitol"), "MASTEREDBYCAPITOL")

    def test_similarity(self):
        sim = discogs.runout_similarity
        self.assertEqual(sim("m7-907-ra1", "M7-907RA1"), 1.0)
        self.assertGreaterEqual(sim("STERLING", "M7 - 907 - RA2 STERLING"), 0.6)
        self.assertGreater(sim("M7-907RA1", "M7-907RA1 MASTERED BY CAPITOL"), 0.85)
        self.assertGreater(sim("MASTERED BY CAPITOL", "M7-907RA1 MASTERED BY CAPITOL"), 0.85)
        self.assertEqual(sim("M7-907RA1", "M7-907RA2"), 0.75)                 # one character off
        self.assertLess(sim("M7-907RA1", "M7-907RA2"), sim("M7-907RA1", "M7907RA1"))
        self.assertEqual(sim("", "M7-907RA1"), 0.0)

    def test_auto_match_picks_the_right_pressing(self):
        for r in (R1, R2, R3):
            discogs.write_json(discogs.release_path(r["id"]), discogs.trim_release(r))
        res = discogs.auto_match({"side_a": "m7907ra1", "side_b": "M7-907-RB1 mastered by capitol"}, [1, 2, 3])
        self.assertEqual((res["best"], res["clear"]), (1, True))
        self.assertGreaterEqual(res["ranked"][0]["score"], 90)
        self.assertLess(res["ranked"][1]["score"], 40)
        self.assertEqual(res["ranked"][0]["hits"]["side_a"]["description"], "Side A")
        res = discogs.auto_match({"side_a": "M7 907 RA2 sterling", "lacquer": "▽17721"}, [1, 2, 3])
        self.assertEqual(res["best"], 2)
        res = discogs.auto_match({"side_a": "STERLING"}, [1, 2, 3])
        self.assertEqual(res["best"], 2)
        # the barcode on R1 / R3 is never compared with a runout
        self.assertEqual(discogs.score_runout({"side_a": "M7-907RA2"}, R3)["score"], 0)
        self.assertIn("no runout", discogs.score_runout({"side_a": "x"}, R3)["note"])
        # nothing close: no best
        self.assertIsNone(discogs.auto_match({"side_a": "ZZZ-999"}, [1, 2])["best"])


class VerifyConfirm(Base):
    IID = "100000000001"

    def listed_quick(self):
        """A quick-picked record (release 1), listed on Discogs as a Draft (listing 9001)."""
        self.ebay_ok()
        r = rec(self.IID)
        discogs.save_matches({self.IID: entry(r, [comm(10, rid=1), comm(5, rid=2)])})
        discogs.quickpick(apply=True)
        discogs.set_listing_toggle(self.IID, True, by="Edwin")
        discogs.sync_listings([r], client(self.fake))
        self.assertEqual(discogs.load_listings()[self.IID]["listing_id"], 9001)
        return r

    def test_confirm_edits_listing_release_and_comment(self):
        r = self.listed_quick()
        self.fake.listings[9001]["status"] = "For Sale"          # Edwin published it
        res = discogs.confirm_pressing(self.IID, 2, {"side_a": "M7-907RA2", "side_b": "M7-907RB2"}, by="Edwin")
        self.assertTrue(res["listing_live"])
        m = discogs.load_matches()[self.IID]
        self.assertEqual((m["status"], m["chosen_release_id"], m["pressing_verified"]), ("approved", 2, True))
        self.assertEqual(m["matrix"]["side_a"], "M7-907RA2")
        self.assertTrue(m["matrix"]["verified_at"])
        out = discogs.sync_listings([r], client(self.fake), only_ids={self.IID})
        self.assertEqual(out["done"], {"update": 1})
        edit = [c for c in self.fake.calls if c[0] == "POST" and c[1] == "/marketplace/listings/9001"][-1][3]
        self.assertEqual(edit["release_id"], 2)
        self.assertEqual(edit["status"], "For Sale")              # his status is kept
        self.assertTrue(edit["comments"].endswith("Matrix/Runout: A: M7-907RA2 / B: M7-907RB2"))
        self.assertNotIn("DELETE", [w[0] for w in self.fake.writes()])

    def test_recreate_when_release_edit_is_refused(self):
        r = self.listed_quick()
        discogs.confirm_pressing(self.IID, 2, {"side_a": "M7-907RA2"}, by="Edwin")
        self.fake.fail[("POST", "/marketplace/listings/9001")] = [422]
        out = discogs.sync_listings([r], client(self.fake))
        self.assertEqual(out["done"], {"recreate": 1})
        self.assertIn(("DELETE", "/marketplace/listings/9001"), self.fake.writes())
        L = discogs.load_listings()[self.IID]
        self.assertEqual((L["listing_id"], L["status"], L["recreated_from"]), (9002, "Draft", 9001))
        self.assertEqual(self.fake.listings[9002]["release_id"], 2)
        self.assertEqual(self.fake.listings[9002]["status"], "Draft")
        n = len(self.fake.writes())
        self.assertEqual(discogs.sync_listings([r], client(self.fake))["done"], {})   # idempotent
        self.assertEqual(len(self.fake.writes()), n)

    def test_recreate_crash_between_delete_and_create_finishes_next_run(self):
        r = self.listed_quick()
        discogs.confirm_pressing(self.IID, 2, {"side_a": "M7-907RA2"}, by="Edwin")
        self.fake.fail[("POST", "/marketplace/listings/9001")] = [422]
        self.fake.fail[("POST", "/marketplace/listings")] = [500]    # the create fails after the delete
        out = discogs.sync_listings([r], client(self.fake))
        self.assertEqual(out["result"], "partial")
        self.assertEqual(discogs.load_listings()[self.IID]["status"], "recreate_pending")
        self.assertNotIn(9001, self.fake.listings)
        out = discogs.sync_listings([r], client(self.fake))
        self.assertEqual(out["done"], {"create": 1})
        creates = [w for w in self.fake.writes() if w == ("POST", "/marketplace/listings")]
        self.assertEqual(len(creates), 3)                            # first listing, the failed one, the retry
        self.assertEqual(len(self.fake.listings), 1)
        L = discogs.load_listings()[self.IID]
        self.assertEqual(L["status"], "Draft")
        self.assertEqual(self.fake.listings[L["listing_id"]]["release_id"], 2)

    def test_confirm_rules_and_skip(self):
        self.listed_quick()
        with self.assertRaises(ValueError):
            discogs.confirm_pressing(self.IID, 999, {"side_a": "x"})             # not a candidate
        with self.assertRaises(ValueError):
            discogs.confirm_pressing(self.IID, 2, {})                            # nothing typed
        m = discogs.skip_pressing(self.IID, by="Edwin")
        self.assertTrue(m["verify_skipped_at"])
        self.assertEqual(m["status"], "quick")
        self.assertFalse(m["pressing_verified"])

    def test_queue_order_progress_and_search(self):
        recs = [rec("100000000001", price="10.00"), rec("100000000002", price="99.00"),
                rec("100000000003", price="50.00", artist="Diana Ross", album="Diana"),
                rec("100000000004", price="70.00")]
        discogs.save_matches({x["id"]: entry(x, [cand(1), cand(2)]) for x in recs})
        discogs.quickpick(apply=True)
        discogs.confirm_pressing("100000000004", 1, {"side_a": "A1"})
        discogs.skip_pressing("100000000002")
        q = discogs.verify_queue(recs)
        self.assertEqual((q["verified"], q["total"]), (1, 4))
        self.assertEqual([x["id"] for x in q["rows"]], ["100000000003", "100000000001", "100000000002"])
        self.assertEqual([x["id"] for x in discogs.verify_queue(recs, q="diana")["rows"]], ["100000000003"])
        self.assertEqual([x["id"] for x in discogs.verify_queue(recs, q="00000004")["rows"]], ["100000000004"])
        self.assertEqual(len(discogs.verify_queue(recs, show="all")["rows"]), 4)

    def test_site_shows_matrix_only_when_verified(self):
        from types import SimpleNamespace
        from mec import site
        ns = SimpleNamespace(pressings={"1": "A: M7-907RA1 / B: M7-907RB1"}, format_label=lambda r: "LP")
        row = lambda iid: site.Site.detail_rows(ns, {"id": iid, "provenance": {}, "genres": [], "country": "US"})
        self.assertIn("<dt>Matrix/Runout</dt><dd>A: M7-907RA1 / B: M7-907RB1", row("1"))
        self.assertNotIn("Matrix", row("2"))
        discogs.save_matches({"1": {"pressing_verified": True, "matrix": {"side_a": "X1"}},
                              "2": {"pressing_verified": False, "matrix": {"side_a": "Y1"}},
                              "3": {"pressing_verified": True}})
        self.assertEqual(site.Site._load_pressings(None), {"1": "A: X1"})


class BulkEnable(Base):
    def test_enable_quick_and_confident_with_grades_only(self):
        self.ebay_ok()
        recs = [rec("100000000001"), rec("100000000002", sleeve_grade=None),
                rec("100000000003"), rec("100000000004", media_grade=None)]
        m = {x["id"]: entry(x, [comm(1, rid=1), comm(2, rid=2)]) for x in recs[:2]}
        m["100000000003"] = entry(recs[2], [cand(5)], status="confident")
        m["100000000004"] = entry(recs[3], [cand(6)], status="confident")
        discogs.save_matches(m)
        discogs.quickpick(apply=True)
        pv = discogs.enable_preview(recs)
        self.assertEqual(sorted(pv["eligible"]), ["100000000001", "100000000003"])
        self.assertEqual(sorted(x["id"] for x in pv["needs_grade"]), ["100000000002", "100000000004"])
        with self.assertRaises(ValueError):
            discogs.enable_quick_confident(recs, expected=4)
        self.assertEqual(discogs.enable_quick_confident(recs, expected=2, by="Edwin")["turned_on"], 2)
        mm = discogs.load_matches()
        self.assertEqual(mm["100000000003"]["status"], "approved")
        self.assertTrue(mm["100000000003"]["list_on_discogs"] and mm["100000000001"]["list_on_discogs"])
        self.assertFalse(mm["100000000002"].get("list_on_discogs"))
        out = discogs.sync_listings(recs, client(self.fake))
        self.assertEqual(out["done"], {"create": 2})
        self.assertEqual({b["status"] for m_, p, q, b, h in self.fake.calls if m_ == "POST"}, {"Draft"})


class VerifyAdmin(Base):
    @classmethod
    def setUpClass(cls):
        cls.srv = load_server()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    call = Admin.call

    def setUp(self):
        super().setUp()
        self.recs = [rec("100000000001", price="40.00", ebay_title="Secret Title Not In Page")]
        p = mock.patch.object(self.srv, "records_cached", lambda ttl=20.0: self.recs)
        p.start()
        self.addCleanup(p.stop)
        discogs.save_matches({"100000000001": entry(self.recs[0], [comm(1, rid=1), comm(2, rid=2)])})

    def test_auth_required_on_verify_and_apis(self):
        with urllib.request.urlopen(self.base + "/admin/verify", timeout=10) as r:
            page = r.read().decode()
        self.assertIn('id="token"', page)                              # static shell: the same sign-in as /admin/
        self.assertNotIn("Secret Title", page)                         # holds no record data
        self.assertNotIn("test-admin-token", page)
        with mock.patch.object(self.srv.time, "sleep"):
            for path, body in [("/admin/api/discogs/verify/queue", None),
                               ("/admin/api/discogs/verify/record?id=100000000001", None),
                               ("/admin/api/discogs/verify/release?item=100000000001&rid=1", None),
                               ("/admin/api/discogs/enable-preview", None),
                               ("/admin/api/discogs/verify/match", {"id": "100000000001", "side_a": "x"}),
                               ("/admin/api/discogs/verify/confirm", {"id": "100000000001", "release_id": 1, "side_a": "x"}),
                               ("/admin/api/discogs/verify/skip", {"id": "100000000001"}),
                               ("/admin/api/discogs/quickpick", {"apply": True, "expected": 1}),
                               ("/admin/api/discogs/enable-quick-confident", {"expected": 1})]:
                for tok in ("wrong", ""):
                    code, j = self.call(path, body, token=tok)
                    self.assertEqual(code, 401, (path, tok))
        self.assertEqual(discogs.load_matches()["100000000001"]["status"], "review")   # nothing changed

    def test_verify_flow_over_http(self):
        code, j = self.call("/admin/api/discogs/quickpick", {"apply": True})
        self.assertEqual(code, 400)                                    # apply needs the confirmed count
        code, j = self.call("/admin/api/discogs/quickpick", {})
        self.assertEqual((code, j["would_pick"]), (200, 1))
        code, j = self.call("/admin/api/discogs/quickpick", {"apply": True, "expected": 1})
        self.assertEqual((code, j["picked"]), (200, 1))
        code, j = self.call("/admin/api/discogs/verify/queue")
        self.assertEqual((code, j["verified"], j["total"], j["rows"][0]["id"]), (200, 0, 1, "100000000001"))
        code, j = self.call("/admin/api/discogs/verify/record?id=100000000001")
        self.assertEqual(code, 200)
        self.assertEqual([c["release_id"] for c in j["candidates"]][0], 2)   # the current pick first
        self.assertFalse(any(c["fetched"] for c in j["candidates"]))
        # lazy release fetch: refused for a release that isn't a candidate, cached after one GET
        code, _ = self.call("/admin/api/discogs/verify/release?item=100000000001&rid=55")
        self.assertEqual(code, 400)
        code, j = self.call("/admin/api/discogs/verify/release?item=100000000001&rid=1")
        self.assertEqual((code, j["fetched"]), (200, False))           # no token: cache only
        self.fake.releases[1] = R1
        with mock.patch.object(discogs, "make_client", lambda read_only=False: client(self.fake, read_only=True)):
            code, j = self.call("/admin/api/discogs/verify/release?item=100000000001&rid=1")
            self.assertEqual((code, j["fetched"], j["identifiers"][0]["value"]), (200, True, "M7-907RA1 MASTERED BY CAPITOL"))
            self.assertEqual(len(j["identifiers"]), 2)                 # the barcode is left out
            self.call("/admin/api/discogs/verify/release?item=100000000001&rid=1")
        self.assertEqual([p for m, p, *_ in self.fake.calls], ["/releases/1"])   # second call came from the cache
        self.assertTrue(discogs.release_path(1).exists())
        code, j = self.call("/admin/api/discogs/verify/match", {"id": "100000000001", "side_a": "m7 907 ra1"})
        self.assertEqual((code, j["best"]), (200, 1))
        code, j = self.call("/admin/api/discogs/verify/confirm", {"id": "100000000001", "release_id": 1,
                                                                 "side_a": "M7-907RA1", "score": 100, "by": "Edwin"})
        self.assertEqual(code, 200)
        self.assertFalse(j["listing_live"])
        m = discogs.load_matches()["100000000001"]
        self.assertEqual((m["chosen_release_id"], m["pressing_verified"], m["matrix"]["verified_by"]), (1, True, "Edwin"))
        code, j = self.call("/admin/api/discogs/verify/queue")
        self.assertEqual((j["verified"], j["total"], j["rows"]), (1, 1, []))
        code, j = self.call("/admin/api/discogs/verify/skip", {"id": "nope"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()

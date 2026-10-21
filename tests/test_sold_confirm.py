"""eBay sold confirmation, getItem reuse, hourly scheduler, /sold/ order, no shop leak.

Run:  python3 -m unittest discover -s tests   (or python3 tests/test_sold_confirm.py)

Every eBay call goes through browse._req, which is replaced by a fake that
fails the test on any URL it does not know. EBAY_* / DISCOGS_TOKEN are removed
from the environment and MEC_ROOT points at a temp dir, so no .env is read and
no real data is touched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if "MEC_ROOT" not in os.environ:
    os.environ["MEC_ROOT"] = tempfile.mkdtemp(prefix="mec-test-")
TMP = Path(os.environ["MEC_ROOT"])
assert TMP.resolve() != REPO.resolve(), "tests must never run against the real repo data"
for _k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "DISCOGS_TOKEN", "IG_ACCESS_TOKEN"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(REPO))

from mec import browse, confirm_sold, discogs, ended, merge, store  # noqa: E402
from mec.util import SYNC_STATE, load_json, write_json  # noqa: E402

SELLER = "music_ed_cafe"
TODAY = datetime.now(timezone.utc).date().isoformat()
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()


def getitem(iid, sold=1, avail=0, end=f"{YESTERDAY}T08:18:22.000Z", price="9.99", seller=SELLER,
            title="Johnny Mathis – Christmas Eve LP", oos=True):
    av = {"estimatedAvailabilityStatus": "OUT_OF_STOCK" if oos else "IN_STOCK",
          "estimatedAvailableQuantity": avail, "estimatedSoldQuantity": sold, "estimatedRemainingQuantity": avail}
    d = {"itemId": f"v1|{iid}|0", "legacyItemId": iid, "title": title, "price": {"value": price, "currency": "USD"},
         "estimatedAvailabilities": [av], "seller": {"username": seller},
         "image": {"imageUrl": f"https://i.ebayimg.com/images/g/{iid}/s-l1600.jpg"},
         "additionalImages": [{"imageUrl": f"https://i.ebayimg.com/images/g/{iid}b/s-l1600.jpg"}]}
    if end:
        d["itemEndDate"] = end
    return d


class FakeEbay:
    """URL -> payload (dict) or an exception to raise. Unknown URLs fail the test."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def legacy(self, iid, payload):
        self.routes[f"get_item_by_legacy_id?legacy_item_id={iid}"] = payload

    def __call__(self, method, url, headers, data=None, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if url.endswith(key) or key in url:
                if isinstance(val, Exception):
                    raise val
                return json.loads(json.dumps(val))
        raise AssertionError(f"un-mocked eBay call: {url}")


def put_item(iid, status, changed="2026-10-21T10:20:09Z", **kw):
    r = store.new_item(iid, "2026-10-19T00:26:11Z")
    r.update(status=status, status_source="browse_api", status_changed_at=changed, title=f"Record {iid}",
             price={"value": "14.99", "currency": "USD", "source": "browse_api: current price"},
             quantity_available=0 if status == "unavailable" else None,
             status_reason="listed with 0 available" if status == "unavailable" else "gone")
    r.update(kw)
    store.ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    (store.ITEMS_DIR / f"{iid}.json").write_text(json.dumps(r), encoding="utf-8")
    return r


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "data", ignore_errors=True)
        shutil.rmtree(TMP / "config", ignore_errors=True)
        shutil.copytree(REPO / "config", TMP / "config")
        (TMP / "data" / "source" / "items").mkdir(parents=True)
        (TMP / "data" / "editorial").mkdir(parents=True)
        self.fake = FakeEbay()
        p = mock.patch.object(browse, "_req", self.fake)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(confirm_sold.time, "sleep")
        s.start()
        self.addCleanup(s.stop)

    def run_confirm(self, **kw):
        return confirm_sold.run(SELLER, token="test-token", spacing=0, log=lambda *_: None, **kw)


class Confirm(Base):
    def test_unavailable_with_sold_quantity_becomes_sold(self):
        put_item("127889422585", "unavailable")
        self.fake.legacy("127889422585", getitem("127889422585", price="9.99"))
        res = self.run_confirm()
        self.assertEqual((res["sold"], res["sold_ids"]), (1, ["127889422585"]))
        r = store.load_items()["127889422585"]
        self.assertEqual((r["status"], r["status_source"]), ("sold", "ebay_api"))
        self.assertEqual(r["sold_at"], YESTERDAY)                      # itemEndDate's date
        self.assertEqual(r["price"]["value"], "9.99")
        self.assertEqual(r["quantity_available"], 0)
        self.assertEqual(r["sales"], [{"date": YESTERDAY, "price": "9.99", "currency": "USD", "quantity": 1,
                                       "source": "ebay_api"}])        # same shape as the CSV path
        self.assertEqual(r["sold_check"]["result"], "sold")
        # images merged into the ended-listing cache (only "found" entries are written)
        e = ended.load()["127889422585"]
        self.assertEqual((e["status"], len(e["images"])), ("found", 2))
        # merged record shows Sold with Edwin's photo; never re-checked
        m = {x["id"]: x for x in merge.build_records()[0]}["127889422585"]
        self.assertEqual((m["status"], m["sold_at"]), ("sold", YESTERDAY))
        self.assertTrue(m["images"])
        self.assertEqual(confirm_sold.candidates(store.load_items()), [])
        # the eBay sync state that gates Discogs deletes is untouched
        self.assertNotEqual(store.read_state().get("last_attempt_source"), "ebay_api")
        self.assertEqual(load_json(SYNC_STATE)["last_sold_confirm"]["sold"], 1)

    def test_sold_date_falls_back_to_first_zero_quantity_then_today(self):
        put_item("1", "unavailable", changed="2026-10-20T10:20:09Z")
        put_item("2", "ended", changed="2026-10-20T10:20:09Z")
        put_item("3", "unavailable", changed="2026-10-20T10:20:09Z")
        self.fake.legacy("1", getitem("1", end=None))
        self.fake.legacy("2", getitem("2", end=None))
        self.fake.legacy("3", getitem("3", end="2099-01-01T00:00:00.000Z"))   # a future end date is ignored
        self.run_confirm()
        items = store.load_items()
        self.assertEqual(items["1"]["sold_at"], "2026-10-20")
        self.assertEqual(items["2"]["sold_at"], TODAY)
        self.assertEqual(items["3"]["sold_at"], "2026-10-20")

    def test_ended_with_zero_sold_stays_ended(self):
        put_item("200", "ended")
        self.fake.legacy("200", getitem("200", sold=0))
        res = self.run_confirm()
        self.assertEqual((res["sold"], res["not_sold"]), (0, 1))
        r = store.load_items()["200"]
        self.assertEqual((r["status"], r["sales"], r["sold_at"]), ("ended", [], None))
        self.assertEqual(r["sold_check"]["result"], "not_sold_final")
        self.assertEqual(confirm_sold.candidates(store.load_items()), [])  # Edwin ended it: stop asking

    def test_still_available_is_not_sold(self):
        put_item("201", "unavailable")
        self.fake.legacy("201", getitem("201", sold=1, avail=2, oos=False))
        self.run_confirm()
        self.assertEqual(store.load_items()["201"]["status"], "unavailable")

    def test_404_changes_nothing_and_counts_tries(self):
        before = put_item("300", "unavailable")
        self.fake.legacy("300", browse.BrowseError('HTTP 404 for x: {"errors":[{"errorId":11003}]}'))
        res = self.run_confirm()
        self.assertEqual(res["not_found"], 1)
        r = store.load_items()["300"]
        for k in ("status", "status_source", "status_changed_at", "sold_at", "sales", "price"):
            self.assertEqual(r[k], before[k], k)
        self.assertEqual((r["sold_check"]["tries"], r["sold_check"]["result"]), (1, "not_found"))
        self.assertNotIn("300", ended.load())                  # failures are not written to the photo cache
        for n in range(2, confirm_sold.MAX_TRIES + 1):
            self.run_confirm()
            self.assertEqual(store.load_items()["300"]["sold_check"]["tries"], n)
        calls = len(self.fake.calls)
        res = self.run_confirm()                               # cap reached: no more lookups
        self.assertEqual((res["candidates"], len(self.fake.calls)), (0, calls))
        # a new status change (relisted, then gone again) starts a fresh count
        r = store.load_items()["300"]
        put_item("300", "unavailable", changed="2026-11-01T00:00:00Z", sold_check=r["sold_check"])
        self.assertEqual(confirm_sold.candidates(store.load_items()), ["300"])

    def test_network_error_is_retried(self):
        put_item("301", "ended")
        self.fake.legacy("301", browse.BrowseError("network error for x: timeout"))
        self.assertEqual(self.run_confirm()["error"], 1)
        self.assertEqual(store.load_items()["301"]["status"], "ended")
        self.fake.legacy("301", getitem("301"))
        self.assertEqual(self.run_confirm()["sold"], 1)
        self.assertEqual(store.load_items()["301"]["status"], "sold")

    def test_other_seller_and_skips(self):
        put_item("400", "unavailable")
        put_item("401", "active", quantity_available=1)
        put_item("402", "sold", sold_at="2026-01-01", sales=[{"date": "2026-01-01"}])
        put_item("403", "ended", discogs_sale={"date": "2026-10-01"})
        self.assertEqual(confirm_sold.candidates(store.load_items()), ["400"])
        self.fake.legacy("400", getitem("400", seller="someone_else"))
        self.assertEqual(self.run_confirm()["other_seller"], 1)
        self.assertEqual(store.load_items()["400"]["status"], "unavailable")

    def test_dry_run_writes_nothing(self):
        put_item("500", "unavailable")
        self.fake.legacy("500", getitem("500"))
        res = self.run_confirm(dry_run=True)
        self.assertEqual((res["result"], res["sold"]), ("dry_run", 1))
        self.assertEqual(store.load_items()["500"]["status"], "unavailable")
        self.assertEqual(ended.load(), {})

    def test_no_credentials_skips_without_calls(self):
        put_item("600", "unavailable")
        res = confirm_sold.run(SELLER, log=lambda *_: None)
        self.assertEqual(res["result"], "skipped")
        self.assertEqual(self.fake.calls, [])

    def test_discogs_listing_removed_after_ebay_sale(self):
        iid = "128123687010"
        put_item(iid, "unavailable", raw={"browse_api": getitem(iid, title="Chuck Mangione – Feels So Good LP")})
        store.record_attempt("browse_api", True, {"complete_snapshot": True})   # the hourly sync succeeded
        self.fake.legacy(iid, getitem(iid, price="14.99"))
        self.run_confirm()
        may_run, may_delete, _ = discogs.ebay_gate()
        self.assertTrue(may_run and may_delete)                # confirm did not disturb the gate
        discogs.save_matches({iid: {"status": "approved", "approved_release_id": 1, "chosen_release_id": 1,
                                    "list_on_discogs": True, "candidates": []}})
        listings = {iid: {"listing_id": 9001, "status": "For Sale", "pushed": {"price": 14.99}}}
        recs = merge.build_records()[0]
        plan = discogs.plan_listings(recs, discogs.load_matches(), listings, discogs.load_settings(),
                                     [{"id": 9001, "external_id": iid, "status": "For Sale"}], may_delete)
        self.assertEqual([(a["op"], a["id"], a["listing_id"]) for a in plan], [("delete", iid, 9001)])


class GetItemReuse(Base):
    """An hourly sync must not re-fetch every listing (Browse: 5,000 calls/day)."""

    def setUp(self):
        super().setUp()
        os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"] = "id", "secret"
        self.addCleanup(os.environ.pop, "EBAY_CLIENT_ID", None)
        self.addCleanup(os.environ.pop, "EBAY_CLIENT_SECRET", None)
        self.fake.routes["/identity/v1/oauth2/token"] = {"access_token": "t"}
        self.ids = [str(100000000700 + i) for i in range(5)]
        self.set_search(self.ids)
        for i in self.ids:
            self.fake.routes[f"/item/v1%7C{i}%7C0"] = getitem(i, sold=0, avail=1, oos=False, end=None,
                                                             price="10.00", title=f"Record {i}")
        sl = mock.patch.object(browse.time, "sleep")
        sl.start()
        self.addCleanup(sl.stop)

    def set_search(self, ids, price="10.00"):
        self.fake.routes["item_summary/search"] = {"total": len(ids), "itemSummaries": [
            {"itemId": f"v1|{i}|0", "legacyItemId": i, "title": f"Record {i}",
             "price": {"value": price if i == ids[0] else "10.00", "currency": "USD"}} for i in ids]}

    def getitem_calls(self):
        return sum(1 for u in self.fake.calls if "/item/v1%7C" in u)

    def test_second_sync_reuses_unchanged_items(self):
        self.assertEqual(browse.sync(log=lambda *_: None)["result"], "ok")
        self.assertEqual(self.getitem_calls(), 5)
        res = browse.sync(log=lambda *_: None)
        self.assertEqual(res["result"], "ok")
        self.assertEqual(self.getitem_calls(), 5)               # nothing changed: no getItem at all
        self.assertEqual(res["coverage"]["api_calls"]["getitem_reused"], 5)
        self.set_search(self.ids, price="12.00")                # a price change in search -> refetch that one
        browse.sync(log=lambda *_: None)
        self.assertEqual(self.getitem_calls(), 6)

    def test_reuse_expires(self):
        browse.sync(log=lambda *_: None)
        cs = store.ChangeSet()
        for i in self.ids:
            cs.items[i]["api_item_fetched_at"] = "2020-01-01T00:00:00Z"
            cs.touch(i)
        cs.commit()
        browse.sync(log=lambda *_: None)
        self.assertEqual(self.getitem_calls(), 10)

    def test_sold_item_leaves_search_and_is_looked_up(self):
        browse.sync(log=lambda *_: None)
        gone = self.ids[-1]
        self.set_search(self.ids[:-1])
        self.fake.legacy(gone, getitem(gone, sold=1, avail=0))
        res = browse.sync(log=lambda *_: None)
        self.assertEqual(res["result"], "ok")
        self.assertEqual(store.load_items()[gone]["status"], "unavailable")
        self.assertEqual(confirm_sold.candidates(store.load_items()), [gone])
        self.assertEqual(self.run_confirm()["sold"], 1)
        self.assertEqual(store.load_items()[gone]["status"], "sold")


def load_server():
    import importlib.util
    os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
    spec = importlib.util.spec_from_file_location("mec_admin_server_sold", REPO / "admin" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


UTC = timezone.utc


class Scheduler(Base):
    srv = None

    def setUp(self):
        super().setUp()
        if Scheduler.srv is None:
            Scheduler.srv = load_server()
        self.t = [datetime(2026, 10, 21, 0, 0, tzinfo=UTC)]
        self.fired = []

    def set_sync_cfg(self, **kw):
        cfg = json.loads((TMP / "config" / "site.json").read_text())
        cfg["sync"].update(kw)
        (TMP / "config" / "site.json").write_text(json.dumps(cfg))

    def run_sched(self, minutes):
        def clock():
            return self.t[0]

        def sleep(s):
            self.t[0] += timedelta(seconds=s)

        def refresh():
            self.fired.append(self.t[0])
            return {"result": "ok"}
        with mock.patch.object(self.srv, "do_refresh", refresh), mock.patch.object(self.srv, "log"):
            self.srv.scheduler(clock=clock, sleep=sleep, max_loops=minutes)

    def test_config_is_hourly(self):
        self.assertEqual(json.loads((REPO / "config" / "site.json").read_text())["sync"]["interval_minutes"], 60)

    def test_interval_fires_every_hour(self):
        self.run_sched(181)                                    # minutes 0..180
        self.assertEqual([f.strftime("%H:%M") for f in self.fired], ["00:00", "01:00", "02:00", "03:00"])

    def test_busy_lock_retries_next_minute(self):
        self.srv.LOCK.acquire()
        try:
            self.run_sched(3)
        finally:
            self.srv.LOCK.release()
        self.assertEqual(self.fired, [])                       # 409 every minute, nothing recorded as run
        self.run_sched(2)
        self.assertEqual([f.strftime("%H:%M") for f in self.fired], ["00:03"])

    def test_daily_fallback_when_interval_is_null(self):
        self.set_sync_cfg(interval_minutes=None, daily_at_utc="10:15")
        self.run_sched(2 * 24 * 60)
        self.assertEqual([f.strftime("%d %H:%M") for f in self.fired], ["21 10:15", "22 10:15"])

    def test_restart_does_not_resync_inside_the_interval(self):
        store.record_attempt("browse_api", True, {})
        self.t[0] = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
        self.run_sched(30)
        self.assertEqual(self.fired, [])
        self.run_sched(30)
        self.assertEqual(len(self.fired), 1)

    def test_instagram_stays_daily(self):
        from mec import instagram
        ig_calls = []

        def ig_sync(log=print):
            ig_calls.append(1)
            write_json(instagram.STATE, {"last_attempt": datetime.now(UTC).isoformat()})
            return {"result": "ok"}
        with mock.patch.object(browse, "sync", return_value={"result": "failed"}), \
                mock.patch.object(instagram, "sync", ig_sync):
            for _ in range(3):                                  # three hourly runs on one day
                res = self.srv.do_refresh()
            self.assertEqual(len(ig_calls), 1)
            self.assertEqual(res["instagram"]["result"], "skipped")
            write_json(instagram.STATE, {"last_attempt": f"{YESTERDAY}T23:59:00Z"})   # a new UTC day
            self.srv.do_refresh()
            self.assertEqual(len(ig_calls), 2)
        self.assertFalse(self.srv.instagram_due(datetime.now(UTC)))
        self.assertTrue(self.srv.instagram_due(datetime.now(UTC) + timedelta(days=1)))

    def test_refresh_confirms_sales_before_build(self):
        from mec import instagram, photos
        order = []
        with mock.patch.object(browse, "sync", return_value={"result": "ok"}), \
                mock.patch.object(instagram, "sync", return_value={"result": "skipped"}), \
                mock.patch.object(confirm_sold, "run", side_effect=lambda *a, **k: order.append("confirm") or {"result": "ok"}), \
                mock.patch.object(merge, "build_records", return_value=([], {})), \
                mock.patch.object(photos, "run", side_effect=lambda *a, **k: order.append("photos") or {}), \
                mock.patch.object(self.srv, "do_build", side_effect=lambda: order.append("build") or {}):
            res = self.srv.do_refresh()
        self.assertEqual(order, ["confirm", "photos", "build"])
        self.assertEqual(res["sold_confirm"], {"result": "ok"})
        order.clear()
        with mock.patch.object(browse, "sync", return_value={"result": "failed"}), \
                mock.patch.object(instagram, "sync", return_value={"result": "skipped"}), \
                mock.patch.object(confirm_sold, "run", side_effect=lambda *a, **k: order.append("confirm")), \
                mock.patch.object(self.srv, "do_build", side_effect=lambda: order.append("build")):
            self.srv.do_refresh()
        self.assertEqual(order, [])                              # a failed sync confirms and builds nothing


class SitePages(Base):
    """/sold/ order and no leak of unavailable / sold records into Shop."""

    def setUp(self):
        super().setUp()
        for d in ("assets", "content"):
            if not (TMP / d).exists():
                (TMP / d).symlink_to(REPO / d)
        self.active = "128000000001"
        put_item(self.active, "active", quantity_available=1, title="Manhattans It Feels So Good LP",
                 listing={"type": "FIXED_PRICE", "start_date": "2026-07-06"})
        # an unavailable record changed today, with no confirmed sale
        put_item("128123687010", "unavailable", changed="2026-10-21T10:20:09Z",
                 title="Chuck Mangione Feels So Good LP", listing={"start_date": "2026-10-09"})
        # sold records: one sold yesterday (newest), one long ago
        put_item("127889422585", "sold", changed="2026-10-21T11:00:00Z", sold_at="2026-10-21",
                 sales=[{"date": "2026-10-21"}], listing={"start_date": "2026-05-28"})
        put_item("100000000001", "sold", changed="2026-10-19T00:00:00Z", sold_at="2026-08-01",
                 sales=[{"date": "2026-08-01"}])
        # ended long ago, old listing start date
        put_item("100000000002", "ended", changed="2026-10-20T03:00:00Z", listing={"start_date": "2025-01-01"})
        from mec import site
        self.site = site
        self.out = TMP / "public" / "test-build"
        shutil.rmtree(self.out, ignore_errors=True)
        site.Site(self.out).build()

    def ids_in(self, rel):
        import re
        html = (self.out / rel).read_text(encoding="utf-8")
        return re.findall(r'<li class="card" data-id="(\d+)"', html), html

    def test_sold_order(self):
        ids, html = self.ids_in("sold/index.html")
        self.assertEqual(ids, ["127889422585", "128123687010", "100000000002", "100000000001"])
        import re
        dates = re.findall(r'<li class="card" data-id="\d+" data-status="\w+"[^>]*data-date="([^"]*)"', html)
        self.assertEqual(dates, sorted(dates, reverse=True))   # site.js re-sorts by data-date: same order
        self.assertIn('data-id="128123687010" data-status="unavailable"', html)
        card = html[html.index('data-id="128123687010"'):]
        card = card[:card.index("</li>")]
        self.assertIn("stamp-ended", card)                     # unconfirmed: "Ended" stamp, but near the top

    def test_no_shop_leak(self):
        ids, html = self.ids_in("shop/index.html")
        self.assertEqual(ids, [self.active])
        self.assertNotIn("128123687010", html)
        feat, _ = self.ids_in("featured/index.html") if (self.out / "featured/index.html").exists() else ([], "")
        self.assertNotIn("128123687010", feat)
        idx = json.loads((self.out / "search-index.json").read_text())
        st = {r["id"]: r["status"] for r in idx["records"]}
        self.assertEqual(st["128123687010"], "unavailable")    # in the index for /sold/ search only;
        # site.js only enriches cards that are on the page, so Shop never shows it


if __name__ == "__main__":
    unittest.main()

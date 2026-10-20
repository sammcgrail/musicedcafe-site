"""Import + sync safety tests.  Run:  python3 -m unittest discover -s tests

Each test runs against a throwaway copy of the repo's config in a temp dir, so
real data is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="mec-test-"))
os.environ["MEC_ROOT"] = str(TMP)
os.environ.pop("EBAY_CLIENT_ID", None)
os.environ.pop("EBAY_CLIENT_SECRET", None)
sys.path.insert(0, str(REPO))

from mec import browse, merge, store  # noqa: E402
from mec.csv_import import ImportError_, import_csv_text  # noqa: E402

FIX = REPO / "tests" / "fixtures"


def active_csv(rows):
    head = '"Item number","Title","Available quantity","Currency","Current price"\n'
    return head + "".join(f'"{i}","{t}","{q}","USD","{p}"\n' for i, t, q, p in rows)


def all_source_text() -> str:
    out = []
    for p in (TMP / "data").rglob("*"):
        if p.is_file():
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "data", ignore_errors=True)
        shutil.rmtree(TMP / "config", ignore_errors=True)
        shutil.copytree(REPO / "config", TMP / "config")
        (TMP / "data" / "source" / "items").mkdir(parents=True)
        (TMP / "data" / "editorial").mkdir(parents=True)

    def status(self, iid):
        return store.load_items()[iid]["status"]


class TransactionReport(Base):
    def test_preamble_filter_and_pii_dropped(self):
        res = import_csv_text((FIX / "transaction-report-sample.csv").read_text())
        self.assertEqual(res["format"], "transaction_report")
        self.assertEqual(res["rows_valid"], 2)           # 2 Orders; Payout + Refund filtered
        self.assertEqual(res["rows_filtered_out"], 2)
        items = store.load_items()
        self.assertEqual(sorted(items), ["100000000001", "100000000002"])
        self.assertEqual(items["100000000001"]["status"], "sold")
        self.assertEqual(items["100000000001"]["price"]["value"], "12.00")
        self.assertEqual(items["100000000001"]["listing_url"], "https://www.ebay.com/itm/100000000001")
        text = all_source_text()
        self.assertNotIn("SENTINEL", text, "buyer / ship-to / description columns must never be stored")
        self.assertNotIn("00000", text.replace("100000000001", "").replace("100000000002", "").replace("200000000001", ""))

    def test_reimport_is_idempotent(self):
        t = (FIX / "transaction-report-sample.csv").read_text()
        import_csv_text(t)
        import_csv_text(t)
        self.assertEqual(len(store.load_items()["100000000001"]["sales"]), 1)

    def test_dry_run_writes_nothing(self):
        import_csv_text((FIX / "transaction-report-sample.csv").read_text(), dry_run=True)
        self.assertEqual(store.load_items(), {})


class ActiveSnapshots(Base):
    def test_seller_hub_sample(self):
        res = import_csv_text((FIX / "seller-hub-active-sample.csv").read_text())
        self.assertEqual(res["format"], "seller_hub_active")
        self.assertEqual(res["active_listed"], 4)
        self.assertTrue(all(r["status"] == "active" for r in store.load_items().values()))
        self.assertEqual(store.load_items()["100000000104"]["price"]["value"], "12.50")

    def test_complete_snapshot_ends_missing_but_never_sells(self):
        import_csv_text(active_csv([(100000000201 + i, f"Record {i}", 1, 10) for i in range(6)]))
        res = import_csv_text(active_csv([(100000000201 + i, f"Record {i}", 1, 10) for i in range(5)]))
        self.assertEqual(res["ended"], 1)
        self.assertEqual(self.status("100000000206"), "ended")
        self.assertNotEqual(self.status("100000000206"), "sold")

    def test_mass_disappearance_is_refused_and_changes_nothing(self):
        import_csv_text(active_csv([(100000000301 + i, f"Record {i}", 1, 10) for i in range(10)]))
        before = json.dumps(store.load_items(), sort_keys=True)
        with self.assertRaises(store.SyncRefused):
            import_csv_text(active_csv([(100000000301, "Record 0", 1, 10)]))
        self.assertEqual(before, json.dumps(store.load_items(), sort_keys=True))

    def test_file_with_bad_rows_is_partial(self):
        import_csv_text(active_csv([(100000000401 + i, f"Record {i}", 1, 10) for i in range(4)]))
        bad = active_csv([(100000000401, "Record 0", 1, 10)]) + '"not-an-id","x","1","USD","1"\n'
        res = import_csv_text(bad)
        self.assertFalse(res["complete_snapshot"])
        self.assertEqual(res["ended"], 0)
        with self.assertRaises(ImportError_):
            import_csv_text(bad, complete=True)

    def test_duplicate_rows_merge(self):
        res = import_csv_text(active_csv([(100000000501, "A", 1, 10), (100000000501, "A", 2, 10)]))
        self.assertEqual(res["duplicate_item_ids"], 1)
        self.assertEqual(store.load_items()["100000000501"]["quantity_available"], 3)

    def test_sale_after_active(self):
        import_csv_text(active_csv([(100000000001, "Joan", 1, 12)]), complete=False)
        # the active snapshot predates the sale (Oct 12) -> the sale wins
        p = TMP / "data" / "source" / "items" / "100000000001.json"
        rec = json.loads(p.read_text())
        rec["last_seen_active_at"] = "2026-10-01T00:00:00Z"
        p.write_text(json.dumps(rec))
        import_csv_text((FIX / "transaction-report-sample.csv").read_text())
        self.assertEqual(self.status("100000000001"), "sold")

    def test_still_listed_after_sale_stays_active(self):
        # an active snapshot taken AFTER the sale means a multi-quantity listing
        import_csv_text(active_csv([(100000000001, "Joan", 2, 12)]), complete=False)
        import_csv_text((FIX / "transaction-report-sample.csv").read_text())
        self.assertEqual(self.status("100000000001"), "active")

    def test_real_report_headers_category_skip_and_auction(self):
        head = ('Item number,Title,Variation details,Custom label (SKU),Available quantity,Format,Currency,'
                'Start price,Auction Buy It Now price,Reserve price,Current price,Sold quantity,Watchers,Bids,'
                'Start date,End date,eBay category 1 name,eBay category 1 number,eBay category 2 name,'
                'eBay category 2 number,Condition,CD:Professional Grader - (ID: 27501),eBay Product ID(ePID),'
                'Listing site,P:UPC,P:EAN,P:ISBN\n')
        rows = ('"100000000801",Rec One LP VG+,,,"1","FIXED_PRICE","USD",20,,,20,"0","","",'
                '"May-18-26 00:57:48 PDT","Nov-18-26 00:57:48 PST","Vinyl Records","176985","","","Used","","","US","","",""\n'
                '"100000000802",Rec Two LP,,,"1","Auction","USD",9.99,25,8,12.5,"0","2","3",'
                '"Oct-10-26 10:00:00 PDT","Oct-20-26 10:00:00 PDT","Vinyl Records","176985","","","Used","","","US","","",""\n'
                '"100000000803",A Book Hardcover,,,"1","FIXED_PRICE","USD",16.99,,,16.99,"0","","",'
                '"Sep-06-26 17:48:27 PDT","Nov-06-26 17:48:27 PST","Books","261186","","","Used","","","US","","",""\n')
        res = import_csv_text(head + rows, complete=True)
        self.assertEqual(res["format"], "seller_hub_active")
        self.assertEqual(res["active_listed"], 2)
        self.assertEqual([x["item_id"] for x in res["skipped_non_vinyl"]], ["100000000803"])
        items = store.load_items()
        self.assertNotIn("100000000803", items)
        a = items["100000000802"]
        self.assertEqual(a["listing"]["type"], "AUCTION")
        self.assertEqual(a["listing"]["bids"], 3)
        self.assertEqual(a["listing"]["end_date"], "2026-10-20")
        self.assertEqual(a["price"]["value"], "12.50")           # current price, not start price
        self.assertEqual(items["100000000801"]["listing"]["start_date"], "2026-05-18")

    def test_unknown_csv_rejected(self):
        with self.assertRaises(ImportError_):
            import_csv_text("a,b,c\n1,2,3\n")


class BrowseWithoutKeys(Base):
    def test_sync_is_inert(self):
        import_csv_text(active_csv([(100000000601, "A", 1, 10)]))
        before = json.dumps(store.load_items(), sort_keys=True)
        res = browse.sync()
        self.assertEqual(res["result"], "skipped")
        self.assertEqual(before, json.dumps(store.load_items(), sort_keys=True))
        st = store.read_state()
        self.assertEqual(st["last_result"], "skipped")


class Editorial(Base):
    def test_editorial_wins_on_presentation_not_price(self):
        import_csv_text(active_csv([(100000000701, "Some Title LP", 1, 10)]))
        (TMP / "data" / "editorial" / "100000000701.json").write_text(json.dumps(
            {"artist": "Edited Artist", "album": "Edited Album", "price": "1.00", "status": "sold",
             "blurb": "Edwin's note"}))
        recs, _ = merge.build_records()
        r = recs[0]
        self.assertEqual(r["artist"], "Edited Artist")
        self.assertEqual(r["price"], "10.00")
        self.assertEqual(r["status"], "active")
        self.assertIn("price", r["editorial_ignored"])
        # a later sync keeps the editorial file untouched
        import_csv_text(active_csv([(100000000701, "Some Title LP", 1, 11)]))
        self.assertIn("Edited Artist", (TMP / "data" / "editorial" / "100000000701.json").read_text())
        recs, _ = merge.build_records()
        self.assertEqual(recs[0]["price"], "11.00")
        self.assertEqual(recs[0]["artist"], "Edited Artist")


class CoverArtScoring(unittest.TestCase):
    def rel(self, artist, title, ptype="Album", cat=None, fmt='12" Vinyl', date="1977"):
        return {"id": "x", "title": title, "date": date, "artist-credit": [{"name": artist}],
                "release-group": {"primary-type": ptype, "id": "rg"},
                "label-info": [{"catalog-number": cat}] if cat else [], "media": [{"format": fmt}]}

    def test_strict_matching(self):
        from mec.coverart import ACCEPT, score_release
        lp = {"artist": "Joan Armatrading", "album": "Show Some Emotion", "format_type": "LP",
              "catalog_number": "SP-4663", "format_slugs": ["lp"], "release_year": "1977"}
        s, _ = score_release(lp, self.rel("Joan Armatrading", "Show Some Emotion", cat="SP 4663"))
        self.assertGreaterEqual(s, 0.95)
        self.assertLess(score_release(lp, self.rel("Joan Armatrading", "To the Limit"))[0], ACCEPT)
        self.assertLess(score_release(lp, self.rel("Joan Osborne", "Show Some Emotion"))[0], ACCEPT)
        self.assertLess(score_release(lp, self.rel("Joan Armatrading", "Show Some Emotion", ptype="Single"))[0], ACCEPT)
        single = {"artist": "Aerosmith", "album": "Dream On / Somebody", "format_type": "Single",
                  "format_slugs": ["7-inch"]}
        self.assertGreaterEqual(score_release(single, self.rel("Aerosmith", "Dream On", ptype="Single", fmt='7" Vinyl'))[0], ACCEPT)
        self.assertLess(score_release(single, self.rel("Aerosmith", "Dream On", ptype="Album"))[0], ACCEPT)
        # normalisation: "The", "&", punctuation
        ew = {"artist": "Earth Wind & Fire", "album": "That's The Way Of The World", "format_type": "LP", "format_slugs": ["lp"]}
        self.assertGreaterEqual(score_release(ew, self.rel("Earth, Wind & Fire", "That’s the Way of the World"))[0], ACCEPT)


class TitleParsing(unittest.TestCase):
    def test_no_pressing_inference(self):
        from mec.normalize import from_title
        t = from_title("Joan Armatrading - Show Some Emotion LP 1977 A&M SP-4663 VG+ Vinyl VG Cover")
        self.assertEqual(t["artist"][0], "Joan Armatrading")
        self.assertEqual(t["media_grade"][0], "VG+")
        self.assertEqual(t["sleeve_grade"][0], "VG")
        self.assertEqual(t["listing_year"][0], "1977")
        self.assertNotIn("release_year", t)
        self.assertNotIn("pressing_year", t)
        bare = from_title("Some Album LP VG+")
        self.assertNotIn("media_grade", bare)
        self.assertIn("title_grade", bare)


if __name__ == "__main__":
    unittest.main()

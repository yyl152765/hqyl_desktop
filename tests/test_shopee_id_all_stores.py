"""Selection and retry contracts for synthetic catalogues larger than 200 stores."""
from datetime import date
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.services import shopee_id_monthly_invoice as service
from tests.shopee_id_fixtures import income_file
from tests.test_shopee_id_monthly_invoice import FakeInvoiceGateway


def catalog(count=211):
    return [{"store_id": str(i), "store_name": f"测试-SP印尼{i:03d}",
             "platform_name": "Shopee-印尼-本土"} for i in range(1, count + 1)]


class AllIndonesiaStoresTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_all_selection_needs_no_names_and_ignores_stale_names(self):
        result = service.validate_request({"store_scope": "all_indonesia", "month": "2026-10",
                                          "store_names": "泰国001", "output_dir": str(self.root)},
                                         today=date(2026, 11, 25))
        self.assertEqual(result["store_scope"], "all_indonesia")
        self.assertEqual(result["store_names"], [])
        for invalid in (None, [], "all", "", 1):
            with self.subTest(scope=invalid), self.assertRaises(ValueError):
                service.validate_store_selection({"store_scope": invalid})

    def test_legacy_named_callers_keep_required_names_and_duplicate_checks(self):
        self.assertEqual(service.validate_store_selection({"store_names": "印尼001"}),
                         {"store_scope": "named", "store_names": ["印尼001"]})
        for payload in ({}, {"store_scope": "named"}, {"store_names": "印尼001\n印尼001"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.validate_store_selection(payload)

    def test_named_mode_accepts_more_than_200_unique_names(self):
        names = [f"印尼{i:03d}" for i in range(1, 251)]
        selection = service.validate_store_selection({"store_names": names})
        result = service.select_stores(selection, catalog(250))
        self.assertTrue(result["can_start"], result)
        self.assertEqual(len(result["stores"]), 250)

    def test_all_selects_only_confirmed_shopee_indonesia_and_stable_order(self):
        valid = catalog()
        rejected = [
            {"store_id": "901", "store_name": "印尼901", "platform_name": "Lazada-印尼"},
            {"store_id": "902", "store_name": "印尼902", "platform_name": "Shopee-泰国"},
            {"store_id": "903", "store_name": "泰国903", "platform_name": "Shopee-印尼"},
            {"store_id": "904", "store_name": "品牌店", "platform_name": "Shopee"},
            {"store_id": "905", "store_name": "印尼905", "platform_name": "Shopee", "country": "VN"},
            {"store_id": "906", "store_name": "LZ印尼906", "platform_name": "Shopee-印尼"},
            {"store_id": "907", "store_name": "印尼907", "platform_name": ""},
        ]
        result = service.select_stores({"store_scope": "all_indonesia"}, rejected + valid[::-1])
        self.assertTrue(result["can_start"])
        self.assertEqual([s["store_id"] for s in result["stores"]], [s["store_id"] for s in valid])
        valid[0]["store_name"] = "changed after selection"
        self.assertNotEqual(result["stores"][0]["store_name"], valid[0]["store_name"])

    def test_all_can_use_site_metadata_and_same_name_with_distinct_stable_ids(self):
        rows = [{"store_id": str(i), "store_name": "相同品牌店", "platform_name": "Shopee-印尼"}
                for i in (1, 2)]
        result = service.select_stores({"store_scope": "all_indonesia"}, rows)
        self.assertEqual([s["store_id"] for s in result["stores"]], ["1", "2"])
        self.assertFalse(service.match_stores(["相同品牌店"], rows)["can_start"])

    def test_empty_or_invalid_catalogue_does_not_create_runnable_selection(self):
        self.assertFalse(service.select_stores({"store_scope": "all_indonesia"}, [])["can_start"])
        for rows in (None, [None], [{"store_name": "印尼001"}], catalog(1) * 2):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                service.select_stores({"store_scope": "all_indonesia"}, rows)

    def test_old_manifest_defaults_to_named_scope_and_invalid_scope_rejected(self):
        manifest = service.create_batch(self.root, "synthetic-profile", "2026-10", catalog(1))
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record.pop("store_scope")
        manifest.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(service.load_batch(manifest)["store_scope"], "named")
        record["store_scope"] = "unknown"
        manifest.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            service.load_batch(manifest)

    def test_211_store_snapshot_runs_every_store_and_retry_only_incomplete(self):
        rows = service.select_stores({"store_scope": "all_indonesia"}, catalog())["stores"]
        manifest = service.create_batch(self.root, "synthetic-profile", "2026-10", rows,
                                        store_scope="all_indonesia")
        source = income_file(self.root / "fixture")

        def export(_store, _month, attempt):
            target = attempt / "Income_Report" / source["filename"]
            target.parent.mkdir()
            shutil.copyfile(source["path"], target)
            return {"row_count": 0, "files": [{**source, "path": str(target)}],
                    "evidence": {"empty_text": "No receipt found"}}

        expected = [s["store_id"] for s in rows]
        plans = dict.fromkeys(expected, export)
        plans["201"] = RuntimeError("synthetic interrupted store")
        gateway = FakeInvoiceGateway("synthetic-profile", plans)
        first = service.run_batch(manifest, gateway)
        self.assertEqual(gateway.exported_stores, expected)
        self.assertEqual(first["store_scope"], "all_indonesia")
        self.assertEqual(first["store_count"], 211)
        self.assertEqual(first["completed_count"], 210)
        self.assertTrue(first["can_retry"])
        # External lists changing after creation must not alter this batch.
        rows.append(catalog(212)[-1])
        retry = FakeInvoiceGateway("synthetic-profile", dict.fromkeys(expected, export))
        second = service.run_batch(manifest, retry)
        self.assertEqual(retry.exported_stores, ["201"])
        self.assertTrue(second["is_complete"])
        self.assertEqual(second["income_report_count"], 211)
        record = service.load_batch(manifest, "synthetic-profile")
        self.assertEqual([s["store_id"] for s in record["stores"]], expected)
        self.assertEqual([s["attempts"] for s in record["stores"]], [2 if s == "201" else 1 for s in expected])
        with self.assertRaises(ValueError):
            service.run_batch(manifest, FakeInvoiceGateway("other-profile", {}))

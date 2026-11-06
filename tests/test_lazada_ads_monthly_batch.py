"""Batch execution and retry tests for Lazada Ads Monthly Report."""
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.services.lazada_ads_monthly import (
    FORMULA_VERSION,
    LazadaAdsMonthlyService,
    validate_ads_monthly_request,
)
from backend.services.lazada_ads_monthly_page import SpendResult


class TestLazadaAdsMonthlyBatch(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.output_root = self.tmp_dir / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_validate_request(self):
        req = {
            "month": "2026-10",
            "country": "TH",
            "store_names": ["Store 1", "Store 2", "Store 1"],  # deduplication
            "cli_profile": "default",
            "output_root": str(self.output_root),
        }
        res = validate_ads_monthly_request(req)
        self.assertEqual(res["month"], "2026-10")
        self.assertEqual(res["start_date"], "2026-10-01")
        self.assertEqual(res["end_date"], "2026-10-31")
        self.assertEqual(res["country"], "TH")
        self.assertEqual(res["store_names"], ["Store 1", "Store 2"])

    def test_run_batch_mock_two_stores(self):
        mock_cli = MagicMock()
        mock_cli.profile = "test-prof"
        mock_cli.identity_fingerprint.return_value = 'test-identity'
        mock_cli.ready.return_value = None
        mock_cli.list_stores.return_value = [
            {"store_id": "1001", "store_name": "Store TH", "platform_name": "lazada", "site": "TH"},
            {"store_id": "1002", "store_name": "Store MY", "platform_name": "lazada", "site": "MY"},
        ]
        mock_cli.match_store.side_effect = lambda name, stores: next(s for s in stores if s["store_name"] == name)

        service = LazadaAdsMonthlyService(cli=mock_cli)

        # Mock page adapter methods
        with patch("backend.services.lazada_ads_monthly.LazadaMonthlyPageAdapter") as mock_page_cls:
            adapter = mock_page_cls.return_value
            # Store 1: 903.15 - 49.59 - 0 = 853.56
            from PIL import Image
            def spend(value):
                def collect(start, end, path):
                    Image.new('RGB',(50,50),'white').save(path)
                    return SpendResult(value, Decimal(value), 'exact', 'THB')
                return collect
            def coupons(start,end,path):
                Image.new('RGB',(50,50),'white').save(path)
                return {'is_empty':True,'filters_verified':True,'expected_count':0,
                        'start_date':start,'end_date':end,'country':'TH','currency':'THB'}
            adapter.collect_ads_spend.side_effect = spend('903.15')
            adapter.collect_affiliate_spend.side_effect = spend('49.59')
            adapter.prepare_and_search_coupons.side_effect = coupons
            adapter.evidence = {'fixture':True}

            req = {
                "month": "2026-10",
                "country": "AUTO",
                "store_names": ["Store TH"],
                "cli_profile": "test-prof",
                "output_root": str(self.output_root),
            }

            res = service.run_batch(req)
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "success")
            self.assertTrue(Path(res["workbook_path"]).is_file())
            self.assertTrue(Path(res["manifest_path"]).is_file())

            stores = res["stores"]
            self.assertEqual(len(stores), 1)
            self.assertEqual(stores[0]["actual_spend"], "853.56")
            self.assertEqual(stores[0]["status"], "success")

            # Verify manifest content
            with open(res["manifest_path"], "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["formula_version"], FORMULA_VERSION)
            self.assertEqual(manifest["status"], "success")

    def test_batch_cancellation(self):
        mock_cli = MagicMock()
        mock_cli.profile = "test-prof"
        mock_cli.identity_fingerprint.return_value = 'test-identity'
        mock_cli.list_stores.return_value = [
            {"store_id": "1001", "store_name": "Store A", "platform_name": "lazada", "site": "TH"},
            {"store_id": "1002", "store_name": "Store B", "platform_name": "lazada", "site": "TH"},
        ]
        mock_cli.match_store.side_effect = lambda name, stores: next(s for s in stores if s["store_name"] == name)

        service = LazadaAdsMonthlyService(cli=mock_cli)

        # Cancel immediately
        res = service.run_batch(
            {
                "month": "2026-10",
                "country": "AUTO",
                "store_names": ["Store A", "Store B"],
                "output_root": str(self.output_root),
            },
            is_cancelled=lambda: True,
        )
        self.assertEqual(res["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

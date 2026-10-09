from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "superbrowser_process"))
from implement.shopee import ads_verified_collection as collection
from implement.shopee import id_shopee_ads_recharge as id_site
from implement.shopee import vn_shopee_ads_recharge as vn_site


class VerifiedCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dtd = Mock()
        self.dtd.get_access_token.return_value = "private-token"
        self.dtd.get_union_id.return_value = "operator"
        self.config = {"app_key": "private-key", "app_secret": "private-secret",
                       "app_userid": "user", "config_doc_id": "doc", "config_sheet": "越南"}
        self.snapshot = {"amount": Decimal(6471781), "text": "₫6.471.781",
                         "collected_at": datetime.now(timezone.utc).isoformat()}
        self.balance_reader = Mock(return_value=(SimpleNamespace(text="₫790.329"), Decimal(790329), None))
        self.options = dict(
            webdriver_util=SimpleNamespace(driver=object()), store_name="刘-越南072-VN002",
            site_code="vn", date_input_xpath="//date", amount_xpath="//expense",
            read_balance=self.balance_reader, dtd=self.dtd, doc_config=self.config,
            output_directory=self.temp.name, collector=Mock(return_value=self.snapshot),
        )

    def test_one_validated_snapshot_and_later_balance_are_forwarded_without_reparse(self):
        guard_result = {"ready": True, "is_recharge": True, "recharge_amount": 8450000, "row": 56}
        with patch.object(collection, "sync_verified_ads_budget", return_value=guard_result) as sync:
            result = collection.prepare_verified_ads_budget(**self.options)
        self.assertEqual(result[:2], (True, 8450000))
        self.options["collector"].assert_called_once()
        self.balance_reader.assert_called_once()
        forwarded = sync.call_args.kwargs
        self.assertEqual(forwarded["expense_snapshot"]["amount"], Decimal(6471781))
        self.assertIn("balance_captured_at", forwarded["expense_snapshot"])
        self.assertEqual(forwarded["balance"], Decimal(790329))
        files = list(Path(self.temp.name).rglob("*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        for secret in ("private-key", "private-token", "private-secret"):
            self.assertNotIn(secret, text)
        self.assertTrue(json.loads(text)["budget_check"]["ready"])

    def test_collection_failure_still_invalidates_sheet_without_zero_substitution(self):
        self.options["collector"].side_effect = RuntimeError("private browser details")
        with patch.object(collection, "sync_verified_ads_budget", return_value={
            "ready": False, "reasons": ["缺少采集快照"]
        }) as sync:
            with self.assertRaises(collection.AdsDataValidationError):
                collection.prepare_verified_ads_budget(**self.options)
        self.assertEqual(sync.call_args.kwargs["expense_snapshot"], {})
        self.assertIsNone(sync.call_args.kwargs["balance"])
        self.balance_reader.assert_not_called()
        text = next(Path(self.temp.name).rglob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("private browser details", text)

    def test_changed_balance_is_rejected_and_never_forwarded_as_valid(self):
        self.balance_reader.return_value = (SimpleNamespace(text="₫790.329"), Decimal(790328), None)
        with patch.object(collection, "sync_verified_ads_budget", return_value={"ready": False}) as sync:
            with self.assertRaises(collection.AdsDataValidationError):
                collection.prepare_verified_ads_budget(**self.options)
        self.assertEqual(sync.call_args.kwargs["expense_snapshot"], {})

    def test_hqyl_output_cannot_be_redirected_to_local_default(self):
        managed = Path(self.temp.name) / "managed"
        with patch.dict(os.environ, {"HQYL_OUTPUT_DIR": str(managed), "HQYL_ATTEMPT_ID": "test"}):
            path = collection._evidence_path("Z:/untrusted-output", "vn")
        self.assertTrue(path.is_relative_to(managed))

    def test_document_exception_does_not_expose_credentials(self):
        self.dtd.get_access_token.side_effect = RuntimeError("private-secret")
        with self.assertRaises(collection.AdsDataValidationError) as raised:
            collection.prepare_verified_ads_budget(**self.options)
        self.assertNotIn("private-secret", str(raised.exception))

    def test_final_evidence_failure_invalidates_already_ready_sheet(self):
        with (
            patch.object(collection, "_write_evidence", side_effect=[None, OSError("disk full")]),
            patch.object(collection, "sync_verified_ads_budget", side_effect=[
                {"ready": True, "is_recharge": True, "recharge_amount": 10000, "row": 56},
                {"ready": False},
            ]) as sync,
        ):
            with self.assertRaises(collection.AdsDataValidationError):
                collection.prepare_verified_ads_budget(**self.options)
        self.assertEqual(sync.call_args_list[-1].kwargs["expense_snapshot"], {})


class SiteIntegrationTests(unittest.TestCase):
    def test_invalid_custom_amount_stops_before_any_page_action(self):
        for site in (id_site, vn_site):
            browser = Mock()
            for amount in ("Error 503", "Rp1.4m", Decimal("0.5"), "-100", 0):
                with self.subTest(site=site.SITE_CODE, amount=amount):
                    result = site.recharge_ads(browser, "store", amount)
                    self.assertFalse(result[0])
                    self.assertEqual(browser.mock_calls, [])

    def test_legacy_document_entry_cannot_authorize_unverified_amounts(self):
        for site in (id_site, vn_site):
            with self.subTest(site=site.SITE_CODE), patch.object(site.dtd, "get_access_token") as auth:
                with self.assertRaises(collection.AdsDataValidationError):
                    site.ansy_dingtalk_doc(100, 200, "store")
                auth.assert_not_called()

    def test_both_country_and_warehouse_paths_block_payment_after_review_failure(self):
        for site in (id_site, vn_site):
            for name in ("普通店", "仓发店"):
                browser = Mock()
                with (
                    self.subTest(site=site.SITE_CODE, store=name),
                    patch.object(site, "WebdriverUtil", return_value=browser),
                    patch.object(site, "_ensure_login", return_value=True),
                    patch.object(site, "close_alert"),
                    patch.object(site.time, "sleep"),
                    patch.object(site, "_wait_for_ads_credit", return_value=(object(), 100, None)),
                    patch.object(site, "prepare_verified_ads_budget", side_effect=collection.AdsDataValidationError("待核验")),
                    patch.object(site, "_write_error"),
                    patch.object(site, "recharge_ads") as pay,
                ):
                    result = getattr(site, f"{site.SITE_CODE}_shopee_ads_recharge")(object(), name)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["needs_review"])
                pay.assert_not_called()


if __name__ == "__main__":
    unittest.main()

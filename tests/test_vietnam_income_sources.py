from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.services.vietnam_income_collection import (
    create_collection_run,
    load_manifest,
    validate_collection_payload,
    write_manifest,
)
from backend.services.vietnam_income_sources import (
    ZiniaoSourcesCollectionJob,
    ads_report_url,
    affiliate_export_params,
    build_ads_credit_request,
    build_ads_export_payload,
    is_ads_credit_transaction,
    run_ziniao_sources_collection,
    validate_ziniao_sources_collection_payload,
    vn_day_timestamp,
)


class VietnamIncomeSourcesTests(unittest.TestCase):
    def test_vietnam_day_timestamp_uses_gmt7_boundaries(self) -> None:
        self.assertEqual(vn_day_timestamp("2026-05-01"), 1777568400)
        self.assertEqual(vn_day_timestamp("2026-05-31", end_of_day=True), 1780246799)

    def test_ads_report_url_matches_shopee_ads_page_export_range(self) -> None:
        url = ads_report_url("https://banhang.shopee.vn/account/signin", "2026-05-01", "2026-05-31")
        self.assertTrue(url.startswith("https://banhang.shopee.vn/portal/marketing/pas/index?"))
        self.assertIn("from=1777568400", url)
        self.assertIn("to=1780246799", url)
        self.assertIn("type=new_cpc_homepage", url)
        self.assertIn("group=custom", url)

    def test_ads_export_payload_uses_overall_ads_export_contract(self) -> None:
        payload = build_ads_export_payload("2026-05-01", "2026-05-31")
        self.assertEqual(payload["language"], "en")
        self.assertEqual(payload["report_type"], "product_homepage_v2__overall")
        self.assertEqual(payload["start_time"], 1777568400)
        self.assertEqual(payload["end_time"], 1780246799)

    def test_affiliate_export_params_use_purchase_time_filter(self) -> None:
        params = json.loads(affiliate_export_params("2026-05-01", "2026-05-31"))
        self.assertEqual(
            params,
            {
                "conversionReportFilter": {
                    "purchase_time_s": 1777568400,
                    "purchase_time_e": 1780246799,
                }
            },
        )

    def test_ads_credit_request_uses_wallet_transaction_history_contract(self) -> None:
        request = build_ads_credit_request("2026-05-01", "2026-05-31", limit=100, offset=200)
        self.assertEqual(request["start_time"], 1777568400)
        self.assertEqual(request["end_time"], 1780246799)
        self.assertEqual(request["transaction_type_list"], [])
        self.assertEqual(request["limit"], 100)
        self.assertEqual(request["offset"], 200)
        self.assertTrue(request["new_transaction_log_flag"])

    def test_ads_credit_classifier_matches_expected_credit_types(self) -> None:
        self.assertTrue(
            is_ads_credit_transaction(
                {"transaction_type": "free_ads_credit_roas_protection", "amount": 10}
            )
        )
        self.assertTrue(
            is_ads_credit_transaction(
                {"received_from": "Free Ads Credit Rebate", "amount": 10}
            )
        )
        self.assertTrue(
            is_ads_credit_transaction(
                {"transaction_type": "technical_support_fee_ads_credit", "amount": 10},
                store_type="warehouse",
            )
        )
        self.assertFalse(
            is_ads_credit_transaction(
                {"transaction_type": "technical_support_fee_ads_credit", "amount": 10},
                store_type="normal",
            )
        )
        self.assertFalse(
            is_ads_credit_transaction(
                {"transaction_type": "gms_product_deduction", "amount": -10}
            )
        )

    def test_sources_job_validation_reuses_manifest_without_persisting_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = validate_collection_payload(
                {
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [{"name": "VN normal store"}],
                }
            )
            created = create_collection_run(request)
            client_path = Path(temp_dir) / "ziniao.exe"
            client_path.touch()
            job = validate_ziniao_sources_collection_payload(
                {
                    "manifest_path": created["manifest_path"],
                    "company": "company",
                    "username": "user",
                    "password": "secret",
                },
                client_path=client_path,
            )
            self.assertEqual(job.username, "user")
            manifest_text = Path(created["manifest_path"]).read_text(encoding="utf-8")
            self.assertNotIn("secret", manifest_text)

    def test_sources_collection_skips_when_three_source_reports_are_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = validate_collection_payload(
                {
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [{"name": "VN normal store"}],
                }
            )
            created = create_collection_run(request)
            manifest_path = Path(created["manifest_path"])
            output_file = manifest_path.parent / "raw" / "already_done.csv"
            output_file.write_text("a\n1\n", encoding="utf-8")
            manifest = load_manifest(manifest_path)
            for report_type in ("ads", "affiliate", "ads_credit"):
                manifest["stores"][0]["reports"][report_type]["status"] = "success"
                manifest["stores"][0]["reports"][report_type]["file"] = str(output_file.relative_to(manifest_path.parent))
            write_manifest(manifest_path, manifest)

            result = run_ziniao_sources_collection(
                ZiniaoSourcesCollectionJob(
                    manifest_path=manifest_path,
                    company="company",
                    username="user",
                    password="secret",
                    client_path=Path(temp_dir) / "missing-ziniao.exe",
                )
            )

            self.assertEqual(result["skipped_count"], 3)
            self.assertEqual(result["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()

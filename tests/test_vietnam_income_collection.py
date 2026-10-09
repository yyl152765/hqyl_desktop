from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from backend.app_bridge import AppBridge
from backend.config_store import ConfigStore
from backend.services.vietnam_income_collection import (
    REPORT_TYPES,
    create_collection_run,
    default_collection_period,
    infer_store_type,
    load_manifest,
    report_is_completed,
    safe_store_name,
    validate_collection_payload,
    write_manifest,
)
from backend.services.vietnam_income_subsidy import (
    SubsidyCollectionJob,
    build_income_request,
    build_warehouse_income_request,
    filter_payouts_by_date,
    flatten_income_item,
    normal_income_url,
    run_subsidy_collection,
    validate_subsidy_collection_payload,
)


class VietnamIncomeCollectionTests(unittest.TestCase):
    def test_default_period_is_previous_calendar_month(self) -> None:
        self.assertEqual(default_collection_period(date(2026, 6, 27)), "2026-05")
        self.assertEqual(default_collection_period(date(2026, 1, 2)), "2025-12")

    def test_payload_infers_store_types_from_names_and_deduplicates_names(self) -> None:
        request = validate_collection_payload(
            {
                "period": "2026-05",
                "output_dir": ".",
                "stores": [
                    {"name": "越南一店"},
                    {"name": "越南仓发二店"},
                    {"name": "越南一店", "type": "warehouse"},
                ],
            }
        )

        self.assertEqual(request.start_date, "2026-05-01")
        self.assertEqual(request.end_date, "2026-05-31")
        self.assertEqual(
            [(store.name, store.store_type) for store in request.stores],
            [("越南一店", "normal"), ("越南仓发二店", "warehouse")],
        )

    def test_store_type_only_depends_on_store_name(self) -> None:
        self.assertEqual(infer_store_type("越南普通店"), "normal")
        self.assertEqual(infer_store_type("越南仓发店"), "warehouse")
        self.assertEqual(infer_store_type("仓发-越南店"), "warehouse")

    def test_create_run_writes_pending_manifest_and_source_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = validate_collection_payload(
                {
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [
                        {"name": "越南/普通店"},
                        {"name": "越南仓发店"},
                    ],
                }
            )
            result = create_collection_run(
                request,
                account_name="紫鸟公司",
                now=datetime(2026, 6, 27, 10, 30, 0),
            )

            manifest_path = Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["status"], "created")
            self.assertEqual(manifest["summary"]["store_count"], 2)
            self.assertEqual(manifest["summary"]["pending_count"], 8)
            self.assertEqual(manifest["account_name"], "紫鸟公司")

            for store in manifest["stores"]:
                self.assertNotIn("/", store["safe_name"])
                self.assertEqual(set(store["reports"]), set(REPORT_TYPES))
                self.assertTrue(
                    all(report["status"] == "pending" for report in store["reports"].values())
                )
                for report_type in REPORT_TYPES:
                    report_dir = manifest_path.parent / "raw" / "ziniao" / store["safe_name"] / report_type
                    self.assertTrue(report_dir.is_dir())

            serialized = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("password", serialized.casefold())
            self.assertNotIn("secret", serialized.casefold())

    def test_safe_store_name_is_stable_and_windows_safe(self) -> None:
        self.assertEqual(safe_store_name("越南/A店"), safe_store_name("越南/A店"))
        self.assertNotRegex(safe_store_name('A<>:"/\\|?*店'), r'[<>:"/\\|?*]')

    def test_bridge_requires_ziniao_account_and_creates_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = AppBridge()
            bridge.config_store = ConfigStore(Path(temp_dir) / "settings.json")
            missing = bridge.create_vietnam_collection_run(
                {
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [{"name": "越南一店"}],
                }
            )
            self.assertFalse(missing["ok"])
            self.assertTrue(missing["requires_account"])

            added = bridge.add_account(
                {
                    "vendor": "ziniao",
                    "name": "越南紫鸟",
                    "username": "ziniao-user",
                    "password": "secret-value",
                    "extra": {"company": "越南紫鸟"},
                }
            )
            account_id = added["account_state"]["accounts"][0]["id"]
            created = bridge.create_vietnam_collection_run(
                {
                    "account_id": account_id,
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [{"name": "越南一店"}],
                }
            )

            self.assertTrue(created["ok"])
            self.assertTrue(Path(created["manifest_path"]).is_file())
            manifest_text = Path(created["manifest_path"]).read_text(encoding="utf-8")
            self.assertNotIn("ziniao-user", manifest_text)
            self.assertNotIn("secret-value", manifest_text)

    def test_normal_income_url_uses_compact_date_range(self) -> None:
        self.assertEqual(
            normal_income_url(
                "https://banhang.shopee.vn/account/signin",
                "2026-05-01",
                "2026-05-31",
            ),
            "https://banhang.shopee.vn/portal/finance/income?type=2&dateRange=2026050120260531",
        )

    def test_income_request_matches_observed_shopee_contract(self) -> None:
        request = build_income_request("2026-05-01", "2026-05-31")
        self.assertEqual(request["income_category"], 2)
        self.assertEqual(request["pagination_info"], {"direction": 0, "limit": 10})
        self.assertEqual(
            request["local_query_condition"],
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
        )

    def test_flatten_income_item_uses_adjustment_as_subsidy(self) -> None:
        row = flatten_income_item(
            {
                "local_income_detail": {
                    "order_income_info": {
                        "order_sn": "ORDER-1",
                        "payment_method_name": "Cash on Delivery",
                    },
                    "income_amount": 100,
                    "adjustment_income_amount": 15,
                    "net_income_amount": 115,
                }
            },
            "越南店铺",
        )
        self.assertEqual(row["店铺名称"], "越南店铺")
        self.assertEqual(row["订单编号"], "ORDER-1")
        self.assertEqual(row["补贴金额"], 15)

    def test_warehouse_payouts_are_filtered_by_payout_date(self) -> None:
        payouts = [
            {"payout_date": "2026-04-30"},
            {"payout_date": "2026-05-01"},
            {"payout_date": "2026-05-31"},
            {"payout_date": "2026-06-01"},
        ]
        selected = filter_payouts_by_date(payouts, "2026-05-01", "2026-05-31")
        self.assertEqual([item["payout_date"] for item in selected], ["2026-05-01", "2026-05-31"])

    def test_warehouse_request_uses_payout_ids(self) -> None:
        request = build_warehouse_income_request(["payout-a", "payout-b"])
        self.assertEqual(request["cb_query_condition"]["payout_ids"], ["payout-a", "payout-b"])
        self.assertEqual(request["pagination_info"]["limit"], 10)

    def test_subsidy_job_validation_never_stores_credentials_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = validate_collection_payload(
                {
                    "period": "2026-05",
                    "output_dir": temp_dir,
                    "stores": [{"name": "越南一店"}],
                }
            )
            created = create_collection_run(request)
            client_path = Path(temp_dir) / "ziniao.exe"
            client_path.touch()
            job = validate_subsidy_collection_payload(
                {
                    "manifest_path": created["manifest_path"],
                    "company": "公司",
                    "username": "user",
                    "password": "secret",
                },
                client_path=client_path,
            )
            self.assertEqual(job.username, "user")
            manifest_text = Path(created["manifest_path"]).read_text(encoding="utf-8")
            self.assertNotIn("secret", manifest_text)

    def test_report_is_completed_requires_existing_success_file_or_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            existing = Path(temp_dir) / "done.csv"
            existing.write_text("a\n1\n", encoding="utf-8")

            self.assertTrue(report_is_completed(manifest_path, {"status": "success", "file": "done.csv"}))
            self.assertTrue(report_is_completed(manifest_path, {"status": "no_data", "file": ""}))
            self.assertFalse(report_is_completed(manifest_path, {"status": "success", "file": "missing.csv"}))
            self.assertFalse(report_is_completed(manifest_path, {"status": "pending", "file": "done.csv"}))

    def test_subsidy_collection_skips_when_income_already_completed(self) -> None:
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
            manifest["stores"][0]["reports"]["income"]["status"] = "success"
            manifest["stores"][0]["reports"]["income"]["file"] = str(output_file.relative_to(manifest_path.parent))
            write_manifest(manifest_path, manifest)

            result = run_subsidy_collection(
                SubsidyCollectionJob(
                    manifest_path=manifest_path,
                    company="company",
                    username="user",
                    password="secret",
                    client_path=Path(temp_dir) / "missing-ziniao.exe",
                )
            )

            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(result["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()

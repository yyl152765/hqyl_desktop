from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.services.vietnam_income_collection import write_manifest
from backend.services.vietnam_report_dashboard import (
    get_vietnam_report_dashboard,
    list_vietnam_report_runs,
    save_vietnam_report_config,
    get_vietnam_report_anomalies,
    derive_vietnam_manifest_status,
)


class VietnamReportDashboardTests(unittest.TestCase):
    def test_v1_manifest_lacking_sections_still_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "run_id": "test_run",
                "period": "2026-05",
                "stores": []
            }
            manifest_path = run_dir / "manifest.json"
            write_manifest(manifest_path, manifest)

            dashboard = get_vietnam_report_dashboard(manifest_path)
            self.assertTrue(dashboard["ok"])
            self.assertEqual(dashboard["period"], "2026-05")
            self.assertEqual(dashboard["state"], "draft")
            self.assertEqual(dashboard["totals"], {})
            self.assertEqual(dashboard["stores"], [])

    def test_validation_failed_becomes_blocking_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "run_id": "test_run",
                "period": "2026-05",
                "stores": [],
                "mabang": {
                    "validation": {
                        "status": "failed",
                        "order_count_diff": 5,
                        "receivable_diff": "150.00",
                        "store_mismatches": [{"store_name": "店铺A", "order_count_diff": 2, "receivable_diff": "50.00"}],
                    }
                }
            }
            manifest_path = run_dir / "manifest.json"
            write_manifest(manifest_path, manifest)

            dashboard = get_vietnam_report_dashboard(manifest_path)
            self.assertEqual(dashboard["state"], "blocked")
            exceptions = dashboard["exceptions"]
            self.assertTrue(any(e["category"] == "mabang_validation" and e["severity"] == "blocking" for e in exceptions))
            self.assertTrue(any(e["store_name"] == "店铺A" and e["title"] == "店铺级收支差异" for e in exceptions))

    def test_save_and_retrieve_profit_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest = {
                "schema_version": 1,
                "run_id": "test_run",
                "period": "2026-05",
                "stores": []
            }
            manifest_path = run_dir / "manifest.json"
            write_manifest(manifest_path, manifest)

            res = save_vietnam_report_config({
                "manifest_path": str(manifest_path),
                "profit_rates": {
                    "店铺A": "12.5%",
                    "店铺B": "0.08"
                }
            })
            self.assertTrue(res["ok"])

            dashboard = get_vietnam_report_dashboard(manifest_path)
            config = dashboard["config"]
            self.assertEqual(config["profit_rates"]["店铺A"], "0.125")
            self.assertEqual(config["profit_rates"]["店铺B"], "0.08")

    def test_files_include_existence_flags_and_missing_output_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            unmatched = run_dir / "unmatched.csv"
            unmatched.write_text("类型,说明\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "run_id": "test_run",
                "period": "2026-05",
                "stores": [],
                "final": {
                    "status": "ready",
                    "output_file": "missing.xlsx",
                    "unmatched_file": unmatched.name,
                },
            }
            manifest_path = run_dir / "manifest.json"
            write_manifest(manifest_path, manifest)

            dashboard = get_vietnam_report_dashboard(manifest_path)
            self.assertEqual(dashboard["state"], "final_file_missing")
            self.assertFalse(dashboard["files"]["output_exists"])
            self.assertTrue(dashboard["files"]["unmatched_exists"])


if __name__ == "__main__":
    unittest.main()

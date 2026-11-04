from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from backend.services import lazada_withdrawal_statistics as service
from tests.test_lazada_withdrawal_statement_fallback import statement_runtime


class ThailandWithdrawalRouteTests(unittest.TestCase):
    def test_worker_collects_real_thai_withdrawals_instead_of_income_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with statement_runtime(root, "TH", [[]]) as runtime:
                runtime.withdrawal.collect_withdrawal_transaction_details.return_value = {
                    "outcome": "complete", "collection_complete": True, "empty_confirmed": False,
                    "currency": "THB", "amount": "1020.25", "record_count": 1,
                    "transaction_records": [{
                        "transaction_number": "fixture-transaction", "time": "15 Sep 2026 08:22:49",
                        "raw_amount": "-1,020.25", "amount_abs": "1020.25", "currency": "THB",
                        "raw_remark": "Paid\n30.00USD (1THB=0.02940USD)", "remark": "30.00USD",
                    }],
                }
                loader = Mock()
                loader.open_store.return_value = {"browserId": "fixture", "downloadPath": str(runtime.downloads)}
                loader.get_driver.return_value = runtime.driver
                with (
                    patch.object(service, "_process_income_statements") as archive,
                    patch.object(service, "_dismiss_balance_update_modal"),
                    patch.object(service, "_dismiss_guided_tours"),
                    patch.object(service, "_scroll_to_balance_transactions"),
                    patch.object(service, "_select_withdrawal_filter", return_value=(True, {}, {})),
                    patch.object(service, "_transaction_table_state", return_value={"ready": True}),
                ):
                    result = service._run_one_browser(
                        loader, {"browserId": "fixture", "browserName": "测试店铺"},
                        runtime.job, root, runtime.logger,
                    )
                self.assertEqual(result.status, "success", result.message)
                self.assertEqual(result.collection_method, "withdrawal_transactions")
                self.assertTrue(result.collection_complete)
                self.assertEqual(result.record_count, 1)
                self.assertFalse(result.no_data)
                archive.assert_not_called()
                runtime.withdrawal.open_lazada_withdrawal_page.assert_not_called()
                runtime.withdrawal.collect_withdrawal_transaction_details.assert_called_once_with(runtime.driver, "TH")
                workbook = load_workbook(result.local_files[0])
                try:
                    row = list(workbook.active.values)[1]
                    self.assertEqual(row[:3], (datetime(2026, 9, 15, 8, 22, 49), "Withdrawal", -1020.25))
                    self.assertIn("30.00USD", row[3])
                    self.assertEqual(row[4:], ("泰国", "测试店铺"))
                finally:
                    workbook.close()
                runtime.drive.ensure_path.assert_called_once_with(
                    ["9月", "测试店铺"], parent_id=service.COUNTRIES["TH"]["drive_root_id"],
                )

    def test_incomplete_or_unconfirmed_empty_results_cannot_export_successful_workbook(self):
        cases = [
            {"outcome": "partial", "collection_complete": False},
            {"outcome": "parse_failed", "collection_complete": False},
            {"outcome": "confirmed_empty", "collection_complete": True, "empty_confirmed": False},
            {"outcome": "confirmed_empty", "collection_complete": True, "empty_confirmed": True, "currency": "PHP"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            for collection in cases:
                with self.subTest(collection=collection):
                    runtime = Mock()
                    runtime.collect_withdrawal_transaction_details.return_value = collection
                    task = service.LazadaWithdrawalStatisticsQuery(
                        company="fixture", username="fixture", password="fixture", country="TH",
                        start_date="2026-09-01", end_date="2026-09-30", store_names=("测试店铺",),
                    )
                    with self.assertRaises(RuntimeError):
                        service._collect_verified_withdrawals(Mock(), task, runtime)

    def test_consistency_failure_from_complete_collector_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "TH", [[]]) as runtime:
                runtime.withdrawal.validate_transaction_record_consistency.return_value = (
                    False, "逐笔明细交易日期不在查询范围内",
                )
                with self.assertRaisesRegex(RuntimeError, "日期不在查询范围内"):
                    service._collect_verified_withdrawals(runtime.driver, runtime.job, runtime.withdrawal)


class WithdrawalFilterSelectionTests(unittest.TestCase):
    def test_selected_withdrawal_filter_is_not_toggled_off(self):
        driver = Mock()
        state = {"found": True, "className": "next-btn next-btn-primary button-groups"}
        with (
            patch.object(service, "_scroll_to_balance_transactions"),
            patch.object(service, "_withdrawal_button_state", return_value=state),
        ):
            selected, _, _ = service._select_withdrawal_filter(driver)
        self.assertTrue(selected)
        driver.execute_script.assert_not_called()

    def test_missing_history_filter_never_clicks_an_unscoped_withdrawal_button(self):
        driver = Mock()
        with (
            patch.object(service, "_scroll_to_balance_transactions"),
            patch.object(service, "_withdrawal_button_state", return_value={"found": False}),
        ):
            selected, _, _ = service._select_withdrawal_filter(driver)
        self.assertFalse(selected)
        driver.execute_script.assert_not_called()


if __name__ == "__main__":
    unittest.main()

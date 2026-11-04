from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services import lazada_withdrawal_statistics as service


def task(root, country="TH"):
    return service.LazadaWithdrawalStatisticsQuery(
        company="fixture", username="fixture", password="fixture", country=country,
        start_date="2026-09-01", end_date="2026-09-30", store_names=("测试店铺",), output_root=str(root),
        dingtalk_app_key="selected-app", dingtalk_app_secret="selected-secret", dingtalk_user_id="selected-user",
    )


def operator_fixture():
    context = {"sheets": {"TH": {"name": "泰国", "sheet_id": "fixture-sheet"}}}
    rows = [{"row": 2, "store_name": "测试店铺", "trade_time": "2026-09-01~2026-09-30"}]
    return SimpleNamespace(
        get_access_token=Mock(return_value="selected-token"), get_union_id=Mock(return_value="selected-union"),
        load_sheet_context=Mock(return_value=context), validate_sheet_period=Mock(return_value=rows),
        build_store_task_for_collection=Mock(return_value=({"country": "TH", "row_number": 2, "sheet_name": "泰国"}, "唯一匹配")),
        apply_withdrawal_result_to_sheet=Mock(return_value={"status": "written", "reason": "写表成功"}),
        DINGTALK_DOC_NODE="https://fixture.invalid/authorized-sheet",
        load_runtime_configs=Mock(return_value=({}, {})), LazadaWithdrawalSettings=Mock(),
        _merge_settings_into_config=Mock(side_effect=lambda config, settings: config),
        _filter_lazada_stores=Mock(return_value=([{"browserName": "测试店铺"}], [])),
    )


def complete_result(root):
    path = root / "withdrawal-collection.json"
    path.write_text(json.dumps({
        "country": "TH", "store_name": "测试店铺", "start_date": "2026-09-01", "end_date": "2026-09-30",
        "outcome": "complete", "collection_complete": True, "empty_confirmed": False,
        "currency": "THB", "record_count": 1, "amount": "100.00", "transaction_records": [],
    }), encoding="utf-8")
    return service.StoreResult(
        "TH", "测试店铺", "测试店铺", "2026-09-01", "2026-09-30",
        status="success", collection_method="withdrawal_transactions", collection_file=str(path),
        collection_complete=True, message="提现流水Excel已归档",
    )


class OnlineRegistrationTests(unittest.TestCase):
    def test_real_shared_guard_accepts_desktop_evidence_for_withdrawal_and_confirmed_empty(self):
        service._ensure_superbrowser_path()
        from main.lazada import lazada_balance_withdrawal_operator_service as operator

        period = "2026-09-01~2026-09-30"
        context = {"token": "fixture-token", "union_id": "fixture-union", "sheets": {"TH": {
            "name": "泰国", "sheet_id": operator.SHEET_CONFIG["TH"]["sheet_id"], "last_non_empty_row": 2,
        }}}
        rows = [{"row": 2, "store_name": "测试店铺", "trade_time": period}]

        def read(_token, _union, _doc, _sheet, target):
            return {
                "A2:D2": [["测试店铺", period, "", ""]],
                "A2:B2": [["测试店铺", period]],
                "E1": [["合计（usd）"]],
            }[target]

        for empty in (False, True):
            with self.subTest(empty=empty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = complete_result(root)
                source = json.loads(Path(result.collection_file).read_text(encoding="utf-8"))
                if empty:
                    source.update(outcome="confirmed_empty", empty_confirmed=True, amount="0.00", record_count=0)
                    result.status = "no_data"
                    result.no_data = True
                else:
                    source["transaction_records"] = [{
                        "transaction_number": "fixture-transaction", "time": "15 Sep 2026 08:22:49",
                        "raw_amount": "-100.00", "amount_abs": "100.00", "currency": "THB",
                        "raw_remark": "提现成功\n3.00USD(1THB = 0.03000000USD)",
                        "remark": "3.00USD(1THB = 0.03000000USD)",
                    }]
                    source["remark"] = source["transaction_records"][0]["remark"]
                Path(result.collection_file).write_text(json.dumps(source), encoding="utf-8")
                with (
                    patch.object(operator, "get_sheet_content", return_value={
                        "sheetid": context["sheets"]["TH"]["sheet_id"], "last_non_empty_row": 2,
                    }),
                    patch.object(operator, "safe_read_range", side_effect=read),
                    patch.object(operator, "safe_update_range") as update,
                ):
                    service._register_thailand_withdrawal(result, task(root), context, rows, operator, Mock())
                self.assertEqual(result.sheet_write_status, "written", result.message)
                update.assert_called_once()
                self.assertEqual(update.call_args.args[4], "B2:E2")
                written = update.call_args.args[5][0]
                self.assertEqual(written[0], period)
                self.assertEqual(written[1], "0.00" if empty else "100.00")
                self.assertEqual(written[3], "0.00" if empty else "3.00")

    def test_thailand_run_uses_saved_operator_and_shared_write_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator = operator_fixture()
            result = complete_result(root)
            loader = Mock()
            loader.load_super_browser.return_value = [{"browserName": "测试店铺"}]
            reference = (operator, Mock(), Mock(return_value=loader), None)
            with (
                patch.object(service, "_reference_modules", return_value=reference),
                patch.object(service, "_run_one_browser", return_value=result),
                patch.object(service, "_make_logger", return_value=(Mock(), root / "run.log")),
            ):
                response = service.run_lazada_withdrawal_statistics(task(root))
            self.assertTrue(response["success"], response["message"])
            self.assertEqual(response["stores"][0]["sheet_write_status"], "written")
            operator.get_access_token.assert_called_once_with("selected-app", "selected-secret")
            operator.get_union_id.assert_called_once_with("selected-token", "selected-user")
            self.assertEqual(operator.load_sheet_context.call_args.kwargs, {
                "countries": ("TH",), "credentials": ("selected-token", "selected-union"),
            })
            write_args = operator.apply_withdrawal_result_to_sheet.call_args.args
            self.assertEqual(write_args[1]["expected_trade_time"], "2026-09-01~2026-09-30")
            self.assertEqual(write_args[2]["record"]["collection_outcome"], "complete")
            self.assertNotIn("selected-secret", json.dumps(response))

    def test_sheet_period_mismatch_stops_before_opening_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            operator = operator_fixture()
            operator.validate_sheet_period.side_effect = ValueError("在线表统计日期与查询日期不一致")
            loader_type = Mock()
            with (
                patch.object(service, "_reference_modules", return_value=(operator, Mock(), loader_type, None)),
                patch.object(service, "_run_one_browser") as collect,
                patch.object(service, "_make_logger", return_value=(Mock(), Path(directory) / "run.log")),
            ):
                response = service.run_lazada_withdrawal_statistics(task(Path(directory)))
            self.assertFalse(response["success"])
            loader_type.assert_not_called()
            collect.assert_not_called()
            operator.apply_withdrawal_result_to_sheet.assert_not_called()

    def test_ph_and_my_keep_existing_archive_flow_without_online_registration(self):
        for country in ("PH", "MY"):
            with self.subTest(country=country), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = complete_result(root)
                result.country = country
                operator = operator_fixture()
                loader = Mock()
                loader.load_super_browser.return_value = [{"browserName": "测试店铺"}]
                with (
                    patch.object(service, "_reference_modules", return_value=(operator, Mock(), Mock(return_value=loader), None)),
                    patch.object(service, "_run_one_browser", return_value=result),
                    patch.object(service, "_make_logger", return_value=(Mock(), root / "run.log")),
                ):
                    response = service.run_lazada_withdrawal_statistics(task(root, country))
                self.assertTrue(response["success"])
                operator.load_sheet_context.assert_not_called()
                operator.apply_withdrawal_result_to_sheet.assert_not_called()

    def test_failed_online_guard_is_partial_success_even_when_excel_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = complete_result(root)
            operator = operator_fixture()
            operator.apply_withdrawal_result_to_sheet.return_value = {
                "status": "blocked_period_mismatch", "reason": "在线表统计日期已变化",
            }
            service._register_thailand_withdrawal(
                result, task(root), operator.load_sheet_context(), operator.validate_sheet_period(), operator, Mock(),
            )
            self.assertEqual(result.status, "partial_success")
            self.assertEqual(result.sheet_write_status, "blocked_period_mismatch")
            self.assertIn("在线表未完成", result.message)

    def test_income_archive_cannot_reach_online_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = complete_result(root)
            result.collection_method = "income_statement"
            operator = operator_fixture()
            service._register_thailand_withdrawal(result, task(root), {}, [], operator, Mock())
            operator.apply_withdrawal_result_to_sheet.assert_not_called()
            self.assertEqual(result.sheet_write_status, "blocked_collection_guard")
            self.assertEqual(result.status, "partial_success")

    def test_foreign_or_stale_local_evidence_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = complete_result(root)
            path = Path(result.collection_file)
            source = json.loads(path.read_text(encoding="utf-8"))
            source["end_date"] = "2026-10-31"
            path.write_text(json.dumps(source), encoding="utf-8")
            operator = operator_fixture()
            service._register_thailand_withdrawal(
                result, task(root), operator.load_sheet_context(), operator.validate_sheet_period(), operator, Mock(),
            )
            self.assertEqual(result.status, "partial_success")
            operator.apply_withdrawal_result_to_sheet.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Offline selection tests: fake sheet reads only, without service import effects."""

import ast
import copy
from datetime import datetime
import logging
from pathlib import Path
import re
import unittest
from unittest.mock import Mock


SUPERBROWSER_ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
TODAY = "2026-09-11"
PCFG = {"dingtalk": {"doc_config": {
    "app_key": "fake", "app_secret": "fake", "app_userid": "fake",
    "config_doc_id": "fake", "config_sheet": "fake",
}}}


class _Clock:
    @staticmethod
    def today():
        return datetime(2026, 9, 11, 12)


class _FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.reads = []
        self.write = Mock(side_effect=AssertionError("Selection must never write to the sheet"))
        self.pay = Mock(side_effect=AssertionError("Selection must never initiate payment"))

    def read(self, *_args):
        cell_range = _args[-1]
        self.reads.append(cell_range)
        match = re.fullmatch(r"(A|O|P|Q)2:(A|O|P|R)(\d+)", cell_range)
        if not match or int(match[3]) != len(self.rows) + 1:
            raise AssertionError(f"Unexpected range: {cell_range}")
        columns = {"A": ("shop_name",), "O": ("recharge_amount",),
                   "P": ("need_recharge",), "Q": ("update_time", "update_message")}
        return copy.deepcopy([[row[column] for column in columns[match[1]]] for row in self.rows])


def _load_selector(site, sheet):
    source = SUPERBROWSER_ROOT / "main" / "shopee" / f"{site}_shopee_ads_recharge_operator_service.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_get_cell", "_is_today_yyyy_mm_dd", "_is_success_message", "_get_recharge_plan_shop"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(functions) != len(names):
        raise AssertionError("Selection helper functions missing")
    scope = {
        "logging": logging, "datetime": _Clock,
        "get_access_token": lambda *_args: "fake-token",
        "get_union_id": lambda *_args: "fake-union",
        "get_sheet_row_count": lambda *_args: len(sheet.rows) + 1,
        "safe_read_range": sheet.read, "safe_update_range": sheet.write,
        "shopee_ads_recharge": sheet.pay,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
    return scope["_get_recharge_plan_shop"]


def _row(status="待核验", day=TODAY, message="", name="测试店铺"):
    return {"shop_name": name, "recharge_amount": "旧金额尚未核验", "need_recharge": status,
            "update_time": day, "update_message": message}


class PendingSelectionTests(unittest.TestCase):
    def select(self, site, rows):
        sheet = _FakeSheet(rows)
        result = _load_selector(site, sheet)(Mock(spec=logging.Logger), PCFG)
        sheet.write.assert_not_called()
        sheet.pay.assert_not_called()
        self.assertEqual([f"A2:A{len(rows) + 1}", f"O2:O{len(rows) + 1}",
                          f"P2:P{len(rows) + 1}", f"Q2:R{len(rows) + 1}"], sheet.reads)
        return result

    def test_today_pending_without_success_returns_a_collection_plan(self):
        for site in ("vn", "id"):
            for message in ("", "支付失败", "待核验：限额配置缺失", "采集数据已保存，等待核验"):
                with self.subTest(site=site, message=message):
                    row = _row(message=message)
                    plan = self.select(site, [row])
                    self.assertEqual([row], plan)
                    self.assertEqual("待核验", plan[0]["need_recharge"])
                    self.assertNotIn("ready", plan[0])
                    self.assertNotIn("is_recharge", plan[0])

    def test_today_pending_with_any_existing_success_evidence_is_skipped(self):
        for site in ("vn", "id"):
            for message in ("支付成功", "余额复查确认支付成功", "Payment Successful",
                            "completed", "successful"):
                with self.subTest(site=site, message=message):
                    self.assertEqual([], self.select(site, [_row(message=message)]))

    def test_quoted_today_pending_is_handled_as_today(self):
        for site in ("vn", "id"):
            with self.subTest(site=site):
                self.assertEqual(1, len(self.select(site, [_row(day=" '2026-09-11 ", message="支付失败")])))
                self.assertEqual([], self.select(site, [_row(day="'2026-09-11", message="支付成功")]))

    def test_pending_on_other_dates_preserves_daily_collection_behavior(self):
        for site in ("vn", "id"):
            for day in ("2026-09-10", "2026-09-12", "", "未记录"):
                for message in ("", "支付失败", "支付成功"):
                    with self.subTest(site=site, day=day, message=message):
                        row = _row(day=day, message=message)
                        self.assertEqual([row], self.select(site, [row]))

    def test_today_required_with_success_still_skips(self):
        for site in ("vn", "id"):
            with self.subTest(site=site):
                self.assertEqual([], self.select(site, [_row(status="需充值", message="支付成功")]))

    def test_today_required_without_success_preserves_selection(self):
        for site in ("vn", "id"):
            for message in ("", "支付失败"):
                with self.subTest(site=site, message=message):
                    row = _row(status="需充值", message=message)
                    self.assertEqual([row], self.select(site, [row]))

    def test_today_not_required_or_unknown_states_still_skip(self):
        for site in ("vn", "id"):
            for status in ("无需充值", "未知状态", "#N/A", "等待人工审批"):
                for message in ("", "支付失败", "支付成功"):
                    with self.subTest(site=site, status=status, message=message):
                        self.assertEqual([], self.select(site, [_row(status=status, message=message)]))

    def test_blank_status_and_older_states_keep_existing_behavior(self):
        for site in ("vn", "id"):
            for row in (_row(status=""), _row(status="无需充值", day="2026-09-10"),
                        _row(status="未知状态", day="2026-09-10")):
                with self.subTest(site=site, row=row):
                    self.assertEqual([row], self.select(site, [row]))

    def test_mixed_rows_keep_identity_and_status_for_later_verification(self):
        for site in ("vn", "id"):
            with self.subTest(site=site):
                pending = _row(name="待核验店铺", message="采集未完成")
                previous = _row(name="昨日店铺", day="2026-09-10")
                rows = [_row(name="成功店铺", message="支付成功"), pending,
                        _row(name="未知店铺", status="未知状态"), previous, _row(name="")]
                self.assertEqual([pending, previous], self.select(site, rows))


if __name__ == "__main__":
    unittest.main()

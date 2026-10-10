"""Offline end-to-end service tests: no real browser, network or DingTalk writes."""
import json
import contextlib
import logging
import re
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services import lazada_ads_data as service
from backend.services import lazada_ads_page as page_core


TODAY = date(2026, 10, 9)
TARGET = date(2026, 10, 8)


def make_query(**changes):
    payload = dict(company="company", username="configured-user", password="configured-password",
                   dingtalk_app_key="configured-app", dingtalk_app_secret="configured-secret",
                   dingtalk_user_id="configured-operator", store_names="shop A", target_date=TARGET.isoformat())
    payload.update(changes)
    return service.validate_lazada_ads_payload(payload, today=TODAY)


class FakeSheet:
    def __init__(self, names=("shop A",), target=TARGET, initial=None, header_labels=("广告费", "业绩")):
        self.target = target
        self.cells = {}
        self.last_non_empty_row = len(names) + 2
        self.writes = []
        self.bad_readback = False
        self.change_identity_on_write = False
        self.names_reads = 0
        self.header_labels = list(header_labels)
        for row, name in enumerate(names, 3):
            self.cells[f"K{row}"] = name
        self.cells.update(initial or {})

    def _header_rows(self):
        # 真实钉钉表：第 1 行日期序列号、第 2 行「当日占比」辅助数值、第 3 行指标行、第 4 行数据行。
        return [
            [(self.target - date(1899, 12, 30)).days, ""],
            [round(self.target.day / 31, 15), ""],
            list(self.header_labels),
            ["", ""],
        ]

    def read(self, address):
        if re.fullmatch(r"[A-Z]+1:[A-Z]+4", address):
            return self._header_rows()
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", address)
        if not match:
            raise AssertionError(f"Unsupported fake range: {address}")
        first, start, last, end = match.groups()
        start, end = int(start), int(end)
        if first == last:
            if first == "K" and start == end:
                self.names_reads += 1
                if self.change_identity_on_write and self.names_reads > 1:
                    return [["replaced shop"]]
            return [[self.cells.get(f"{first}{row}", "")] for row in range(start, end + 1)]
        if first == "AH" and last == "AI":
            return [[self.cells.get(f"AH{row}", ""), self.cells.get(f"AI{row}", "")] for row in range(start, end + 1)]
        raise AssertionError(f"Unsupported fake columns: {address}")

    def update(self, address, values):
        self.writes.append((address, values))
        if not self.bad_readback:
            self.cells[address.split(":")[0]] = values[0][0]


class FakeRuntime:
    def __init__(self, names=("shop A",), fail_open=(), fail_close=False):
        self.names = names
        self.fail_open = fail_open
        self.fail_close = fail_close
        self.events = []

    def start(self):
        self.events.append("start")

    def list_browsers(self):
        return [{"browserName": name, "id": str(index)} for index, name in enumerate(self.names)]

    def open_browser(self, browser, download_path):
        name = browser["browserName"]
        self.events.append(f"open:{name}")
        if name in self.fail_open:
            raise RuntimeError("open failed")
        return SimpleNamespace(page=name)

    def close_browser(self, opened):
        self.events.append(f"close:{opened.page}")
        if self.fail_close:
            raise RuntimeError("close failed")

    def shutdown(self):
        self.events.append("shutdown")


class QueryTests(unittest.TestCase):
    def test_missing_empty_or_whitespace_uses_yesterday_month_and_yesterday(self):
        for empty_fields in ({}, {"sheet_name": "", "target_date": ""},
                             {"sheet_name": " \t\n ", "target_date": " \t\n "}):
            with self.subTest(empty_fields=empty_fields):
                payload = make_query().__dict__.copy()
                payload.pop("sheet_name")
                payload.pop("target_date")
                payload.update(empty_fields)
                query = service.validate_lazada_ads_payload(payload, today=date(2026, 10, 1))
                self.assertEqual(query.sheet_name, "26年9月")
                self.assertEqual(query.target_date, "2026-09-30")
        self.assertEqual(service.default_sheet_name(date(2027, 1, 1)), "26年12月")
        self.assertEqual(service.default_target_date(date(2027, 1, 1)), "2026-12-31")

    def test_sheet_and_date_defaults_do_not_replace_explicit_other_field(self):
        query = make_query(sheet_name=" \t ", target_date=" 2026-09-30 ")
        self.assertEqual(query.sheet_name, "26年10月")
        self.assertEqual(query.target_date, "2026-09-30")
        query = make_query(sheet_name=" 补录页 ", target_date=" \n ")
        self.assertEqual(query.sheet_name, "补录页")
        self.assertEqual(query.target_date, TARGET.isoformat())

    def test_explicit_date_is_not_shifted_and_store_names_are_deduplicated(self):
        query = make_query(target_date="2026-10-01", store_names=" shop A \n\nshop A\nSHOP A\nshop A extra")
        self.assertEqual(query.target_date, "2026-10-01")
        self.assertEqual(query.store_names, ("shop A", "shop A extra"))

    def test_invalid_inputs(self):
        for changes in ({"target_date": "2026-10-10"}, {"target_date": "2026-02-30"},
                        {"target_date": "2026/10/08"}, {"store_names": ""},
                        {"password": ""}, {"dingtalk_app_secret": ""}, {"dingtalk_user_id": ""},
                        {"socket_port": -1}, {"browser_window_mode": "invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                make_query(**changes)

    def test_today_is_accepted_and_custom_sheet_is_retained(self):
        self.assertEqual(make_query(target_date=TODAY.isoformat(), sheet_name="补录页").sheet_name, "补录页")

    def test_workbook_link_and_default(self):
        self.assertEqual(make_query().workbook_id, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(make_query(workbook_id="https://alidocs.dingtalk.com/i/p/example?docKey=doc_123").workbook_id, "doc_123")
        self.assertEqual(make_query(workbook_id="https://alidocs.dingtalk.com/spreadsheetv2/doc-456").workbook_id, "doc-456")

    def test_nodes_link_from_personal_space_is_accepted(self):
        link = f"https://alidocs.dingtalk.com/i/nodes/{service.DEFAULT_WORKBOOK_ID}?utm_scene=person_space"
        self.assertEqual(make_query(workbook_id=link).workbook_id, service.DEFAULT_WORKBOOK_ID)

    def test_supported_workbook_path_precedes_dentry_key(self):
        for path in ("spreadsheetv2/fixture-workbook/edit", "i/nodes/fixture-workbook"):
            with self.subTest(path=path):
                link = f"https://alidocs.dingtalk.com/{path}?dentryKey=fixture-entry"
                self.assertEqual(service.normalize_workbook_id(link), "fixture-workbook")

    def test_doc_key_precedes_paths_and_dentry_key_remains_a_fallback(self):
        for path in ("spreadsheetv2/fixture-path/edit", "i/nodes/fixture-path"):
            with self.subTest(path=path):
                link = f"https://alidocs.dingtalk.com/{path}?dentryKey=fixture-entry&docKey=fixture-workbook"
                self.assertEqual(service.normalize_workbook_id(link), "fixture-workbook")
        self.assertEqual(service.normalize_workbook_id("https://alidocs.dingtalk.com/i/p/shared?dentryKey=fixture-entry"), "fixture-entry")

    def test_daily_columns_follow_legacy_layout(self):
        for day, columns in enumerate((("M", "N"), ("P", "Q"), ("S", "T"), ("V", "W"),
                                       ("Y", "Z"), ("AB", "AC"), ("AE", "AF"), ("AH", "AI")), 1):
            self.assertEqual(service.column_layout(date(2026, 10, day)), columns)


class SheetMetadataTests(unittest.TestCase):
    def test_zero_based_last_non_empty_row_includes_final_shop(self):
        from backend.core import dingtalk_workbook
        meta = {"id": "fixture-sheet", "lastNonEmptyRow": 3}
        with patch.object(service.DingTalkAdsSheet, "_refresh_token"), \
                patch.object(dingtalk_workbook, "get_sheet_meta_by_name", return_value=meta):
            sheet = service.DingTalkAdsSheet(make_query())
        self.assertEqual(sheet.last_non_empty_row, 4)
        source = FakeSheet(("first shop", "final shop"))
        with patch.object(sheet, "read", side_effect=source.read) as read:
            rows = service._sheet_rows(sheet)
        read.assert_called_once_with("K3:K4")
        self.assertEqual(rows["final shop"], [(4, "final shop")])


class RunnerTests(unittest.TestCase):
    def run_job(self, sheet=None, runtime=None, actions=None, names="shop A"):
        sheet = sheet or FakeSheet()
        runtime = runtime or FakeRuntime()
        actions = actions or Mock(collect=Mock(return_value={"advertising": Decimal("0"), "performance": Decimal("12.34"), "errors": []}))
        with tempfile.TemporaryDirectory() as directory:
            query = make_query(store_names=names, output_root=directory)
            messages = []
            with patch.object(service.time, "sleep"):
                result = service.run_lazada_ads_data(query, messages.append,
                    runtime_factory=lambda q, logger: runtime, sheet_factory=lambda q: sheet, page_actions=actions)
            saved = json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))
            self.assertEqual(saved, result)
            self.assertTrue(Path(result["log_file"]).exists())
            serialized = json.dumps(result) + Path(result["log_file"]).read_text(encoding="utf-8")
            for secret in (query.password, query.dingtalk_app_secret, query.dingtalk_app_key, query.dingtalk_user_id):
                self.assertNotIn(secret, serialized)
            return result, sheet, runtime, actions

    def test_real_runner_writes_zero_and_revenue_and_closes(self):
        result, sheet, runtime, actions = self.run_job()
        self.assertTrue(result["success"])
        self.assertTrue(result["is_complete"])
        self.assertEqual(result["success_store_count"], 1)
        self.assertEqual(sheet.writes, [("AH3:AH3", [["0"]]), ("AI3:AI3", [["12.34"]])])
        self.assertEqual(runtime.events, ["start", "open:shop A", "close:shop A", "shutdown"])
        self.assertEqual(actions.collect.call_args.args[2], TARGET)

    def test_existing_both_values_skip_without_browser(self):
        result, sheet, runtime, actions = self.run_job(sheet=FakeSheet(initial={"AH3": 0, "AI3": "12.5"}))
        self.assertTrue(result["success"])
        self.assertEqual(result["skipped_store_count"], 1)
        self.assertEqual(runtime.events, [])
        actions.collect.assert_not_called()
        self.assertFalse(sheet.writes)

    def test_only_pending_metric_is_written(self):
        result, sheet, _, actions = self.run_job(sheet=FakeSheet(initial={"AH3": 7}))
        self.assertEqual(sheet.writes, [("AI3:AI3", [["12.34"]])])
        self.assertEqual(result["stores"][0]["advertising"], 7)
        self.assertFalse(actions.collect.call_args.args[1].need_advertising)

    def test_missing_and_duplicate_sheet_names_fail_individually(self):
        result, sheet, runtime, _ = self.run_job(sheet=FakeSheet(("shop A", "shop A", "other")), names="shop A\nmissing")
        self.assertFalse(result["success"])
        self.assertEqual(result["failed_store_count"], 2)
        self.assertIn("重复", result["stores"][0]["message"])
        self.assertIn("未精确匹配", result["stores"][1]["message"])
        self.assertEqual(runtime.events, [])
        self.assertFalse(sheet.writes)

    def test_browser_missing_or_ambiguous_is_never_fuzzy_matched(self):
        for names in (("shop A extra",), ("shop A", "shop A")):
            with self.subTest(names=names):
                result, sheet, runtime, _ = self.run_job(runtime=FakeRuntime(names))
                self.assertFalse(result["is_complete"])
                self.assertFalse(sheet.writes)
                self.assertEqual(runtime.events, ["start", "shutdown"])

    def test_sheet_date_mismatch_fails_all_before_browser(self):
        result, sheet, runtime, _ = self.run_job(sheet=FakeSheet(target=date(2026, 9, 8)), names="shop A\nshop B")
        self.assertEqual(result["failed_store_count"], 2)
        self.assertIn("表头不匹配", result["stores"][0]["message"])
        self.assertFalse(sheet.writes)
        self.assertFalse(runtime.events)

    def test_metric_labels_below_a_ratio_row_are_accepted(self):
        # 回归：钉钉表改版后第 2 行是「当日占比」辅助数值，指标行下移到第 3 行。
        # 旧实现固定读第 2 行，会把 0.29… 当成指标并误报「表头不匹配」。
        result, sheet, _, _ = self.run_job(sheet=FakeSheet())
        self.assertTrue(result["success"], result["stores"][0]["message"])
        self.assertEqual(sheet.writes, [("AH3:AH3", [["0"]]), ("AI3:AI3", [["12.34"]])])

    def test_sheet_without_metric_labels_fails_before_browser(self):
        result, sheet, runtime, _ = self.run_job(sheet=FakeSheet(header_labels=("花费", "业绩")))
        self.assertEqual(result["failed_store_count"], 1)
        self.assertIn("表头不匹配", result["stores"][0]["message"])
        self.assertFalse(sheet.writes)
        self.assertFalse(runtime.events)

    def test_identity_is_rechecked_before_write(self):
        sheet = FakeSheet()
        sheet.change_identity_on_write = True
        result, sheet, _, _ = self.run_job(sheet=sheet)
        self.assertFalse(result["success"])
        self.assertIn("店铺行已发生变化", result["stores"][0]["message"])
        self.assertFalse(sheet.writes)

    def test_concurrent_fill_is_preserved(self):
        sheet = FakeSheet()
        def collected(*args):
            sheet.cells["AH3"] = 99
            return {"advertising": Decimal("0"), "performance": Decimal("12.34"), "errors": []}
        result, sheet, _, _ = self.run_job(sheet=sheet, actions=Mock(collect=Mock(side_effect=collected)))
        self.assertTrue(result["success"])
        self.assertEqual(sheet.writes, [("AI3:AI3", [["12.34"]])])
        self.assertEqual(result["stores"][0]["advertising"], 99)

    def test_write_readback_failure_is_not_retried_and_browser_closes(self):
        sheet = FakeSheet()
        sheet.bad_readback = True
        result, sheet, runtime, _ = self.run_job(sheet=sheet)
        self.assertEqual(len(sheet.writes), 1)
        self.assertFalse(result["success"])
        self.assertIn("回读不一致", result["stores"][0]["message"])
        self.assertIn("close:shop A", runtime.events)

    def test_partial_data_write_reports_failure(self):
        actions = Mock(collect=Mock(return_value={"advertising": None, "performance": Decimal("12"), "errors": ["广告报表不支持今日数据"]}))
        result, sheet, _, _ = self.run_job(actions=actions)
        self.assertFalse(result["success"])
        self.assertEqual(sheet.writes, [("AI3:AI3", [["12"]])])

    def test_store_failure_does_not_prevent_next_store_and_closes_in_order(self):
        actions = Mock(collect=Mock(side_effect=[RuntimeError("collection failed"), {"advertising": 2, "performance": 3}]))
        runtime = FakeRuntime(("shop A", "shop B"))
        result, _, runtime, _ = self.run_job(sheet=FakeSheet(("shop A", "shop B")), runtime=runtime, actions=actions, names="shop A\nshop B")
        self.assertEqual(result["failed_store_count"], 1)
        self.assertEqual(result["success_store_count"], 1)
        self.assertEqual(runtime.events, ["start", "open:shop A", "close:shop A", "open:shop B", "close:shop B", "shutdown"])

    def test_close_failure_stops_next_store(self):
        runtime = FakeRuntime(("shop A", "shop B"), fail_close=True)
        result, _, runtime, _ = self.run_job(sheet=FakeSheet(("shop A", "shop B")), runtime=runtime, names="shop A\nshop B")
        self.assertEqual(result["failed_store_count"], 2)
        self.assertNotIn("open:shop B", runtime.events)
        self.assertEqual(runtime.events[-1], "shutdown")

    def test_errors_redact_tokens_and_credentials(self):
        error = 'configured-secret https://example.test/?access_token=live-token-value&other=1 {"access_token":"json-token-value"}'
        actions = Mock(collect=Mock(side_effect=RuntimeError(error)))
        result, _, _, _ = self.run_job(actions=actions)
        self.assertNotIn("live-token-value", json.dumps(result))
        self.assertNotIn("json-token-value", json.dumps(result))
        self.assertIn("[已隐藏]", result["stores"][0]["message"])


class MetricTests(unittest.TestCase):
    def test_historical_revenue_ignores_realtime_section(self):
        text = "实时表现\n营收\n฿9999\n关键指标\n营收\n฿123.45"
        with patch.object(page_core, "page_body_text", return_value=text):
            self.assertEqual(page_core.extract_revenue(Mock()), Decimal("123.45"))

    def test_realtime_is_bound_to_actual_update_date(self):
        text = "Real-time Performance\nLast updated at 2026-10-09 11:00:00\nRevenue\nTHB12.50\nKey Metrics\nRevenue\nTHB99"
        with patch.object(page_core, "page_body_text", return_value=text), patch.object(page_core.time, "monotonic", side_effect=[0, 0, 30]):
            self.assertEqual(page_core.extract_realtime_revenue(Mock(), TODAY), Decimal("12.50"))
        with patch.object(page_core, "page_body_text", return_value=text), patch.object(page_core.time, "monotonic", side_effect=[0, 0, 30]):
            with self.assertRaises(page_core.WorkflowError):
                page_core.extract_realtime_revenue(Mock(), TARGET)

    def test_today_ads_fail_before_navigation(self):
        page = Mock()
        with self.assertRaises(page_core.WorkflowError):
            page_core.collect_advertising(page, page_core.ShopTask(3, "shop A"), date.today(), 90000)
        page.goto.assert_not_called()

    def test_compact_amounts_and_zero(self):
        for text, expected in (("฿0", "0"), ("THB12.5K", "12500"), ("฿1,234.50", "1234.50")):
            self.assertEqual(page_core.parse_amount(text), Decimal(expected))
        self.assertTrue(page_core.values_equal(0, Decimal("0")))
        self.assertFalse(page_core.values_equal("", Decimal("0")))
        self.assertFalse(page_core.values_equal(None, Decimal("0")))
        self.assertEqual(page_core.parse_iso_date("2026/10/9"), TODAY)

    def test_task_logging_context_is_restored(self):
        logger = Mock(spec=logging.Logger)
        original = page_core._ACTIVE_LOGGER.get()
        actions = page_core.PlaywrightLazadaAdsPageActions(logger)
        with patch.object(page_core, "collect_performance", side_effect=RuntimeError("metric error")):
            result = actions.collect(Mock(), page_core.ShopTask(3, "shop A", need_advertising=False), TARGET)
        self.assertEqual(result["errors"], ["metric error"])
        self.assertIs(page_core._ACTIVE_LOGGER.get(), original)


class LoginTests(unittest.TestCase):
    target = "https://sellercenter-th.lazada-seller.cn/ba/dashboard?dateRange=2026-10-08%7C2026-10-08&dateType=day"

    def helper_context(self, page):
        helper = page_core.PlaywrightMonthlyReportPageActions
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(helper, "_find_gsp_page", return_value=None))
        stack.enter_context(patch.object(helper, "_context_pages", side_effect=lambda current: [current]))
        stack.enter_context(patch.object(helper, "_visible_login_challenge", return_value=False))
        return stack

    def test_direct_page_keeps_same_page(self):
        page = Mock(url=self.target)
        with self.helper_context(page), patch.object(page_core, "goto_dom_ready") as goto:
            result = page_core.open_lazada_data_page(page, "shop", self.target, 90000)
        self.assertIs(result, page)
        goto.assert_called_once_with(page, self.target, 90000)

    def test_untrusted_redirect_never_submits_credentials(self):
        page = Mock(url="https://sellercenter-my.lazada-seller.cn/login")
        helper = page_core.PlaywrightMonthlyReportPageActions
        with self.helper_context(page), patch.object(page_core, "goto_dom_ready"), patch.object(helper, "_submit_prefilled_login") as submit:
            with self.assertRaises(page_core.LoginRequiredError):
                page_core.open_lazada_data_page(page, "shop", self.target, 90000)
        submit.assert_not_called()

    def test_registration_login_link_and_prefilled_login_are_reused(self):
        register_url = "https://sellercenter-th.lazada-seller.cn/apps/register/index"
        login_url = "https://sellercenter-th.lazada-seller.cn/apps/seller/login"
        page = Mock(url=register_url)
        helper = page_core.PlaywrightMonthlyReportPageActions
        def goto(current, url, timeout):
            current.url = register_url if url == self.target else url
        def submit(current):
            current.url = self.target
            return True
        with self.helper_context(page), patch.object(page_core, "goto_dom_ready", side_effect=goto), \
                patch.object(helper, "_is_auth_page_url", side_effect=lambda url: url in {register_url, login_url}), \
                patch.object(helper, "_is_login_page_url", side_effect=lambda url: url == login_url), \
                patch.object(helper, "_trusted_register_login_url", return_value=login_url) as register_link, \
                patch.object(helper, "_submit_prefilled_login", side_effect=submit) as submitted:
            result = page_core.open_lazada_data_page(page, "跨境 shop", self.target, 90000)
        self.assertIs(result, page)
        register_link.assert_called_once_with(page, "TH", self.target)
        submitted.assert_called_once_with(page)

    def test_new_target_tab_is_adopted_after_gsp_login(self):
        helper = page_core.PlaywrightMonthlyReportPageActions
        launcher = Mock(url="https://gsp.lazada-seller.cn/portal/login")
        home = Mock(url="https://gsp.lazada-seller.cn/portal/home")
        target = Mock(url=self.target)
        with self.helper_context(launcher), patch.object(helper, "_find_gsp_page", return_value=launcher), \
                patch.object(helper, "_ensure_gsp_home", return_value=home) as ensure_home, \
                patch.object(helper, "_context_pages", return_value=[launcher, home, target]), \
                patch.object(page_core, "goto_dom_ready") as goto:
            result = page_core.open_lazada_data_page(launcher, "跨境 shop", self.target, 90000)
        self.assertIs(result, target)
        ensure_home.assert_called_once_with(launcher)
        goto.assert_called_once_with(home, self.target, 90000)

    def test_visible_challenge_stops_before_submit(self):
        helper = page_core.PlaywrightMonthlyReportPageActions
        page = Mock(url="https://sellercenter-th.lazada-seller.cn/apps/seller/login")
        with self.helper_context(page), patch.object(helper, "_visible_login_challenge", return_value=True), \
                patch.object(page_core, "goto_dom_ready"), patch.object(helper, "_submit_prefilled_login") as submit:
            with self.assertRaises(page_core.LoginRequiredError):
                page_core.open_lazada_data_page(page, "shop", self.target, 90000)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

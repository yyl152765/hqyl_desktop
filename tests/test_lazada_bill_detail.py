"""Lazada 后台收支数据聚合服务的离线契约测试。"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.services import lazada_bill_detail as service
from backend.services import lazada_bill_page as page_module
from backend.services.lazada_ads_page import WorkflowError


class FakeSheet:
    """按 A:F 六列保存单元格的最小钉钉表格替身。"""

    def __init__(self, rows: dict[int, list[str]] | None = None):
        self.cells: dict[tuple[int, int], str] = {}
        for row_number, values in (rows or {}).items():
            for offset, value in enumerate(values):
                self.cells[(row_number, offset)] = str(value)
        self.updates: list[tuple[str, list[list[str]]]] = []

    @property
    def last_non_empty_row(self) -> int:
        return max((row for row, _column in self.cells), default=0)

    @staticmethod
    def _index(column: str) -> int:
        return ord(column) - ord("A")

    def read(self, cell_range: str) -> list[list[str]]:
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", cell_range)
        if match is None:
            raise AssertionError(f"unexpected range: {cell_range}")
        first_column, first_row, last_column, last_row = (
            self._index(match[1]), int(match[2]), self._index(match[3]), int(match[4]),
        )
        return [
            [
                self.cells.get((row, column), "")
                for column in range(first_column, last_column + 1)
            ]
            for row in range(first_row, last_row + 1)
        ]

    def update(self, cell_range: str, values: list[list[str]]) -> None:
        self.updates.append((cell_range, values))
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", cell_range)
        first_column, first_row = self._index(match[1]), int(match[2])
        for row_offset, row_values in enumerate(values):
            for column_offset, value in enumerate(row_values):
                self.cells[(first_row + row_offset, first_column + column_offset)] = str(value)


class FakePage:
    """最小页面替身：账单页 URL（非认证页）+ 重试等待 + 截图计数。"""

    def __init__(self, url: str = "https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index") -> None:
        self.url = url
        self.screenshots = 0
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(int(milliseconds))

    def screenshot(self, **_kwargs) -> bytes:
        self.screenshots += 1
        return b"\x89PNG\r\n\x1a\n"


class FakeOpened:
    def __init__(self, page: FakePage, oauth: str) -> None:
        self.page = page
        self.connection = None
        self.browser_oauth = oauth


class FakeRuntime:
    instances: list["FakeRuntime"] = []

    def __init__(self, query, logger, browsers=None):
        self.query = query
        self.logger = logger
        self.browsers = browsers if browsers is not None else [
            {"browserOauth": "oauth-a", "browserName": "店铺 A"},
            {"browserOauth": "oauth-b", "browserName": "店铺 B"},
        ]
        self.started = False
        self.shutdown_called = False
        self.closed: list[FakeOpened] = []
        FakeRuntime.instances.append(self)

    def start(self) -> None:
        self.started = True

    def list_browsers(self):
        return list(self.browsers)

    def open_browser(self, browser, download_path):
        del download_path
        return FakeOpened(FakePage(), str(browser.get("browserOauth")))

    def close_browser(self, opened):
        self.closed.append(opened)

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeActions:
    def __init__(self, metrics=None, error=None):
        self.metrics = metrics or {"total_amount": 1234.56, "revenue": 2000, "deductions": 765.44}
        self.error = error
        self.calls: list[tuple[str, date, date]] = []
        self.login_states: list[dict] = []
        self.pages: list[object] = []

    def collect(self, page, shop_name, profile, start_date, end_date, login_state=None):
        self.calls.append((shop_name, start_date, end_date))
        self.pages.append(page)
        # 运行器必须为每次尝试传入全新的 login_state（一次采集只提交一次密码登录）。
        self.login_states.append(login_state if login_state is not None else {})
        if self.error is not None:
            raise self.error
        return dict(self.metrics)


class FakeUploader:
    def __init__(self, *, available=True, status="uploaded", error=None):
        self._available = available
        self._status = status
        self._error = error
        self.calls: list[tuple[int, str, Path]] = []

    def available(self) -> bool:
        return self._available

    def upload(self, row_number, shop_name, image_path):
        self.calls.append((row_number, shop_name, Path(image_path)))
        if self._error is not None:
            raise self._error
        return {"status": self._status, "cell": f"F{row_number}"}


class LazadaBillDetailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.today = date(2026, 10, 9)
        FakeRuntime.instances = []
        # 默认按「本机未安装 dws」运行，使 F 列逻辑的降级行为可确定地验证。
        self.dws = patch("backend.services.lazada_bill_detail.shutil.which", return_value=None).start()
        # 默认截图根目录是「桌面\log」：测试里改到临时目录，绝不往用户桌面写文件。
        patch("backend.config_store.desktop_path", return_value=self.root).start()
        self.addCleanup(patch.stopall)

    def payload(self, **extra):
        base = {
            "username": "operator", "password": "private-password",
            "company": "公司 A",
            "dingtalk_app_key": "app-key", "dingtalk_app_secret": "app-secret",
            "dingtalk_user_id": "user-id",
            "country": "TH", "store_names": "店铺 A\n店铺 B",
        }
        base.update(extra)
        return base

    def query(self, **extra):
        options = {"default_output_root": str(self.root), "today": self.today}
        return service.validate_lazada_bill_detail_payload(self.payload(**extra), **options)

    def run_service(
        self, query, *, rows=None, sheet=None, actions=None, uploader=None, runtime=None,
        browsers=None, progress=None,
    ):
        sheet = sheet if sheet is not None else FakeSheet(rows)
        runtime_holder = {}

        def runtime_factory(q, logger):
            instance = (runtime or FakeRuntime)(q, logger, browsers=browsers)
            runtime_holder["runtime"] = instance
            return instance

        def save_screenshot(_page, screenshot_folder, shop_name, failed=False):
            target = Path(screenshot_folder) / ("debug" if failed else "") / f"{shop_name}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            # 把被截图页面的标记写进文件内容，便于断言「截的是哪一页」。
            target.write_text(str(getattr(_page, "tag", "")), encoding="utf-8")
            return target

        with patch("backend.services.lazada_bill_page.save_screenshot", side_effect=save_screenshot):
            payload = service.run_lazada_bill_detail(
                query,
                progress,
                runtime_factory=runtime_factory,
                sheet_factory=lambda _query: sheet,
                page_actions=actions or FakeActions(),
                image_uploader=uploader,
            )
        return payload, sheet, runtime_holder.get("runtime")


class CountryProfileTests(LazadaBillDetailTestCase):
    def test_five_countries_are_aggregated_with_own_site_parameters(self) -> None:
        options = service.country_options()
        self.assertEqual([item["code"] for item in options], ["TH", "PH", "MY", "ID", "VN"])
        self.assertEqual([item["name"] for item in options], ["泰国", "菲律宾", "马来西亚", "印尼", "越南"])
        for code in ("TH", "PH", "MY", "ID", "VN"):
            profile = service.COUNTRY_PROFILES[code]
            self.assertTrue(profile.finance_url.startswith("https://"))
            self.assertIn("/portal/apps/finance/myIncome/index", profile.finance_url)
            self.assertTrue(profile.legacy_workbook_id)
            self.assertTrue(profile.currency_pattern)
        self.assertEqual(service.COUNTRY_PROFILES["ID"].preferred_language, "简体中文")
        self.assertEqual(service.COUNTRY_PROFILES["VN"].preferred_language, "简体中文")
        self.assertEqual(service.COUNTRY_PROFILES["VN"].legacy_workbook_id, "pLdn55X2E5o4yno8")
        self.assertEqual(
            service.DEFAULT_WORKBOOK_ID, "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
            "五国账单 Sheet 同处一本钉钉文档",
        )
        self.assertEqual(
            [item["legacy_workbook_id"] for item in options][1:4],
            ["np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"] * 3,
        )
        self.assertNotEqual(
            service.COUNTRY_PROFILES["TH"].finance_url, service.COUNTRY_PROFILES["PH"].finance_url
        )
        self.assertEqual(service.resolve_country("马来").code, "MY")
        self.assertEqual(service.resolve_country("越南").code, "VN")
        self.assertEqual(service.resolve_country("th").code, "TH")

    def test_unknown_or_missing_country_is_rejected_with_supported_list(self) -> None:
        for value in ("", "US", "新加坡"):
            with self.subTest(country=value), self.assertRaisesRegex(ValueError, "请选择支持的国家"):
                service.resolve_country(value)


class ValidationTests(LazadaBillDetailTestCase):
    def test_defaults_use_current_month_sheet_and_previous_month_range(self) -> None:
        query = self.query()
        self.assertEqual(query.sheet_name, "26年10月")
        self.assertEqual(query.start_date, "2026-09-01")
        self.assertEqual(query.end_date, "2026-09-30")
        self.assertEqual(query.country, "TH")
        self.assertEqual(query.country_name, "泰国")
        self.assertEqual(query.workbook_id, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(query.dws_node, service.DEFAULT_WORKBOOK_ID, "截图默认写同一本文档")
        self.assertEqual(query.store_names, ("店铺 A", "店铺 B"))
        self.assertEqual(query.screenshot_root, service.default_screenshot_root())
        self.assertEqual(query.output_root, str(self.root))
        self.assertEqual(query.browser_window_mode, "normal")

    def test_explicit_sheet_range_and_country_defaults_are_honoured(self) -> None:
        query = self.query(
            country="PH", sheet_name="26年9月",
            start_date="2026-08-01", end_date="2026-08-31",
            store_names="店铺 A\n店铺 A\n 店铺 B ",
        )
        self.assertEqual(query.sheet_name, "26年9月")
        self.assertEqual((query.start_date, query.end_date), ("2026-08-01", "2026-08-31"))
        self.assertEqual(query.store_names, ("店铺 A", "店铺 B"))
        self.assertEqual(query.workbook_id, service.DEFAULT_WORKBOOK_ID)

    def test_previous_month_range_crosses_year_boundary(self) -> None:
        self.assertEqual(
            service.previous_month_range(date(2026, 1, 5)),
            {"start_date": "2025-12-01", "end_date": "2025-12-31"},
        )

    def test_credentials_stores_and_dates_are_validated(self) -> None:
        cases = (
            ({"username": "", "password": ""}, "请先绑定并选择紫鸟账号"),
            ({"dingtalk_app_key": ""}, "请在设置中配置钉钉应用凭证和操作人"),
            ({"store_names": "  \n "}, "请输入店铺名称"),
            ({"start_date": "2026-09-01", "end_date": "2026-08-01"}, "开始日期不能晚于结束日期"),
            ({"end_date": "2026-11-01"}, "结束日期不能晚于今天"),
            ({"start_date": "2026/09/01"}, "开始日期格式必须为 YYYY-MM-DD"),
            ({"browser_window_mode": "headless"}, "浏览器窗口模式无效"),
            ({"socket_port": 70000}, "紫鸟端口必须在 1～65535 之间"),
            ({"sheet_name": "x" * 101}, "指定 Sheet 名称无效"),
            ({"dws_node": "not-a-node"}, "钉钉图片节点格式无效"),
        )
        for extra, message in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.query(**extra)

    def test_image_node_defaults_to_the_shared_document_for_every_country(self) -> None:
        for country in ("TH", "PH", "MY", "ID", "VN"):
            with self.subTest(country=country):
                self.assertEqual(self.query(country=country).dws_node, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(
            self.query(country="VN", dws_node="AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPp").dws_node,
            "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPp",
        )
        self.assertEqual(
            self.query(country="TH", dws_node="https://alidocs.dingtalk.com/i/nodes/x").dws_node,
            "https://alidocs.dingtalk.com/i/nodes/x",
        )

    def test_comma_separated_stores_are_split(self) -> None:
        query = self.query(store_names="店铺 A，店铺 B,店铺 C")
        self.assertEqual(query.store_names, ("店铺 A", "店铺 B", "店铺 C"))

    def test_screenshot_root_defaults_to_desktop_log_and_explicit_root_wins(self) -> None:
        default_root = service.default_screenshot_root()
        self.assertTrue(default_root.endswith("log"), default_root)
        self.assertEqual(self.query().screenshot_root, default_root)
        explicit = str(self.root / "shots")
        self.assertEqual(self.query(screenshot_root=explicit).screenshot_root, explicit)
        self.assertEqual(
            self.query(screenshot_dir=explicit).screenshot_root, explicit,
            "前端旧字段名仍应被接受",
        )

    def test_screenshot_folder_groups_by_country_and_bill_range(self) -> None:
        folder = service.screenshot_folder(self.root, "泰国", "2026-09-01", "2026-09-30")
        self.assertEqual(folder.parent.parent, Path(self.root))
        self.assertEqual(folder.parent.name, "Lazada泰国账单明细截图")
        self.assertEqual(folder.name, "2026-09-01到2026-09-30")
        other = service.screenshot_folder(self.root, "菲律宾", "2026-09-01", "2026-09-30")
        self.assertEqual(other.parent.name, "Lazada菲律宾账单明细截图")
        self.assertNotEqual(other.parent, folder.parent)
        other_range = service.screenshot_folder(self.root, "泰国", "2026-08-01", "2026-08-31")
        self.assertNotEqual(other_range, folder)


class StoreMatchingTests(LazadaBillDetailTestCase):
    def test_shop_normalization_ignores_punctuation_and_full_width(self) -> None:
        self.assertEqual(
            service.normalize_shop_name(" 华南-LZ跨境泰国 001 TH01（主店） "),
            service.normalize_shop_name("华南lz跨境泰国001th01主店"),
        )

    def test_numbered_alias_key_requires_country_owner_numbers_and_chinese_suffix(self) -> None:
        key = service.lazada_numbered_store_key("华南-LZ跨境泰国001TH01主店")
        self.assertEqual(key, ("华南", "跨境", "th", "", "001", "01", "主店"))
        self.assertIsNone(service.lazada_numbered_store_key("华南-LZ跨境泰国001TH01"))
        self.assertIsNone(service.lazada_numbered_store_key("华南泰国001TH01主店"))

    def test_exact_and_unique_alias_resolution_with_ambiguity_protection(self) -> None:
        rows = [
            service.BillSheetRow(2, "华南LZ跨境泰国001TH01主店"),
            service.BillSheetRow(3, "华南LZ跨境泰国001TH01主店"),
            service.BillSheetRow(4, "华东LZ马来西亚002MY02备用店"),
        ]
        matched, issues = service.resolve_requested_stores(
            rows, ["华南-LZ 跨境泰国001TH01（主店）", "华东LZ马来西亚002MY02备用店", "不存在店铺"]
        )
        self.assertEqual(sorted(matched), ["华东LZ马来西亚002MY02备用店"])
        self.assertEqual(matched["华东LZ马来西亚002MY02备用店"].row_number, 4)
        self.assertIn("华南-LZ 跨境泰国001TH01（主店）", issues)
        self.assertIn("多个同名店铺", issues["华南-LZ 跨境泰国001TH01（主店）"]["message"])
        self.assertEqual(issues["不存在店铺"]["status"], "unmatched_store")
        self.assertEqual(issues["不存在店铺"]["message"], "钉钉指定 Sheet 的 A 列未匹配到该店铺")

    def test_duplicate_requested_names_cannot_reuse_one_row(self) -> None:
        rows = [service.BillSheetRow(2, "店铺 A")]
        matched, issues = service.resolve_requested_stores(rows, ["店铺 A", "店铺a"])
        self.assertEqual(list(matched), ["店铺 A"])
        self.assertEqual(len(issues), 1)
        message = next(iter(issues.values()))["message"]
        self.assertIn("已被", message)

    def test_browser_matching_prefers_exact_then_rejects_ambiguity(self) -> None:
        rows = [service.BillSheetRow(2, "店铺 A"), service.BillSheetRow(3, "店铺 B")]
        matches, unmatched = service.match_browsers_to_rows(
            rows,
            [
                {"browserOauth": "o1", "browserName": "店铺 A"},
                {"browserOauth": "o2", "browserName": "店铺 B"},
            ],
        )
        self.assertEqual([(row.row_number, label) for row, _browser, label in matches],
                         [(2, "店铺名精确匹配"), (3, "店铺名精确匹配")])
        self.assertEqual(unmatched, [])

        duplicated, _unmatched = service.match_browsers_to_rows(
            [service.BillSheetRow(2, "店铺 A")],
            [
                {"browserOauth": "o1", "browserName": "店铺 A"},
                {"browserOauth": "o2", "browserName": "店铺 A"},
            ],
        )
        self.assertEqual(duplicated, [])

    def test_unmatched_browser_is_reported(self) -> None:
        matches, unmatched = service.match_browsers_to_rows(
            [service.BillSheetRow(2, "店铺 A")], [{"browserOauth": "o1", "browserName": "其它店铺"}]
        )
        self.assertEqual(matches, [])
        self.assertEqual([row.row_number for row in unmatched], [2])

    def test_browser_matching_accepts_sheet_name_without_lz_suffix(self) -> None:
        matches, unmatched = service.match_browsers_to_rows(
            [service.BillSheetRow(2, "华南LZ跨境泰国001")],
            [{"browserOauth": "o1", "browserName": "华南LZ跨境泰国001TH01主店"}],
        )
        self.assertEqual(unmatched, [])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][2], "唯一LZ名称扩展匹配")


class SheetAccessTests(LazadaBillDetailTestCase):
    def test_read_bill_rows_skips_blank_names_and_keeps_row_numbers(self) -> None:
        sheet = FakeSheet({
            2: ["店铺 A", "100", "200", "300"],
            3: ["", "1", "", ""],
            4: ["店铺 B", "", "", ""],
        })
        rows = service.read_bill_rows(sheet)
        self.assertEqual([(row.row_number, row.shop_name) for row in rows], [(2, "店铺 A"), (4, "店铺 B")])
        self.assertTrue(rows[0].has_values)
        self.assertFalse(rows[1].has_values)

    def test_read_bill_rows_uses_last_non_empty_row(self) -> None:
        self.assertEqual(service.read_bill_rows(FakeSheet({})), [])
        self.assertEqual(service.read_bill_rows(FakeSheet({1: ["店铺 A"]})), [])
        self.assertEqual(service._sheet_last_row({"lastNonEmptyRow": 4}), 5)
        self.assertEqual(service._sheet_last_row({"rowCount": 7}), 7)
        self.assertEqual(service._sheet_last_row({}), 0)

    def test_write_bill_metrics_writes_and_verifies(self) -> None:
        sheet = FakeSheet({2: ["店铺 A", "", "", ""]})
        outcome = service.write_bill_metrics(
            sheet,
            service.BillSheetRow(2, "店铺 A"),
            {"total_amount": 1234.5, "revenue": -20, "deductions": 99.5},
        )
        self.assertTrue(outcome["written"])
        self.assertEqual(outcome["cell_range"], "B2:D2")
        self.assertEqual(sheet.read("B2:D2"), [["1234.5", "-20", "99.5"]])
        self.assertEqual(len(sheet.updates), 1)

    def test_write_bill_metrics_skips_complete_rows_and_overwrites_on_demand(self) -> None:
        row = service.BillSheetRow(2, "店铺 A", "1", "2", "3")
        sheet = FakeSheet({2: ["店铺 A", "1", "2", "3"]})
        outcome = service.write_bill_metrics(sheet, row, {"total_amount": 5, "revenue": 6, "deductions": 7})
        self.assertFalse(outcome["written"])
        self.assertEqual(outcome["reason"], "existing")
        self.assertEqual(sheet.updates, [])
        overwritten = service.write_bill_metrics(
            sheet, row, {"total_amount": 5, "revenue": 6, "deductions": 7}, overwrite=True
        )
        self.assertTrue(overwritten["written"])
        self.assertEqual(sheet.read("B2:D2"), [["5", "6", "7"]])

    def test_write_bill_metrics_refuses_changed_row_and_bad_metrics(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "店铺行已发生变化"):
            service.write_bill_metrics(
                FakeSheet({2: ["其它店铺", "", "", ""]}),
                service.BillSheetRow(2, "店铺 A"),
                {"total_amount": 1, "revenue": 2, "deductions": 3},
            )
        with self.assertRaisesRegex(WorkflowError, "收入未取得有效数据"):
            service.write_bill_metrics(
                FakeSheet({2: ["店铺 A", "", "", ""]}),
                service.BillSheetRow(2, "店铺 A"),
                {"total_amount": 1, "revenue": None, "deductions": 3},
            )

    def test_write_bill_metrics_reports_readback_mismatch(self) -> None:
        class LyingSheet(FakeSheet):
            def update(self, cell_range, values):  # 写入被远端忽略
                self.updates.append((cell_range, values))

        with patch("backend.services.lazada_bill_detail.time.sleep"):
            with self.assertRaisesRegex(WorkflowError, "写入后回读不一致"):
                service.write_bill_metrics(
                    LyingSheet({2: ["店铺 A", "", "", ""]}),
                    service.BillSheetRow(2, "店铺 A"),
                    {"total_amount": 1, "revenue": 2, "deductions": 3},
                )


class ImageUploaderTests(LazadaBillDetailTestCase):
    def test_image_node_validation_accepts_url_and_dentry_uuid(self) -> None:
        self.assertEqual(
            service.validate_image_node("https://alidocs.dingtalk.com/i/nodes/abc"),
            "https://alidocs.dingtalk.com/i/nodes/abc",
        )
        self.assertEqual(service.validate_image_node("np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"),
                         "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l")
        self.assertEqual(service.validate_image_node(""), "")
        with self.assertRaisesRegex(ValueError, "钉钉图片节点格式无效"):
            service.validate_image_node("pLdn55X2E5o4yno8")

    def test_uploader_reports_availability_from_dws_command(self) -> None:
        uploader = service.DwsImageUploader("np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", "26年10月")
        self.assertFalse(uploader.available(), "未安装 dws 时必须报告不可用")
        self.dws.return_value = r"C://tools//dws.exe"
        self.assertTrue(uploader.available())

    def test_dws_detection_reports_path_and_missing_hint(self) -> None:
        self.assertFalse(service.dws_available())
        self.assertEqual(service.dws_path(), "")
        self.assertIn("未安装 dws", service.DWS_MISSING_HINT)
        self.assertIn("PATH", service.DWS_MISSING_HINT)
        self.dws.return_value = r"C:\Users\Demo\.local\bin\dws.exe"
        self.assertTrue(service.dws_available())
        self.assertEqual(service.dws_path(), r"C:\Users\Demo\.local\bin\dws.exe")

    def test_dws_missing_error_is_actionable(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "未找到 dws 命令"):
            service._run_dws_json(["sheet", "list", "--node", "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"])

    def test_uploader_refuses_pending_metrics_and_occupied_cells(self) -> None:
        uploader = service.DwsImageUploader("np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", "26年10月")
        header = {"F": {"value": "图片"}}
        pending = {"A": {"value": "店铺 A"}, "B": {"value": ""}, "C": {"value": "1"}, "D": {"value": "2"}}
        with patch.object(service, "_dws_read_row", side_effect=[header, pending]):
            with self.assertRaisesRegex(WorkflowError, "尚未完整写入"):
                uploader.inspect(2, "店铺 A")
        occupied = {
            "A": {"value": "店铺 A"}, "B": {"value": "1"}, "C": {"value": "2"}, "D": {"value": "3"},
            "F": {"value": "已有文字"},
        }
        occupied_uploader = service.DwsImageUploader("np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", "26年10月")
        with patch.object(service, "_dws_read_row", side_effect=[header, occupied]):
            self.assertEqual(occupied_uploader.inspect(2, "店铺 A")["status"], "occupied")

    def test_image_column_must_be_f_or_later(self) -> None:
        self.assertEqual(service.IMAGE_COLUMN, "F")
        node = "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"
        self.assertEqual(service.DwsImageUploader(node, "26年10月").column, "F")
        self.assertEqual(service.DwsImageUploader(node, "26年10月", "h").column, "H")
        for column in ("A", "B", "C", "D", "E", "a"):
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "不能写入 A～E 列"):
                service.validate_image_column(column)
            with self.subTest(column=column), self.assertRaises(ValueError):
                service.DwsImageUploader(node, "26年10月", column)
        for column in ("F", "G", "AA"):
            with self.subTest(column=column):
                self.assertEqual(service.validate_image_column(column), column)

    def test_list_workbook_sheets_returns_named_sheets_only(self) -> None:
        with (
            patch("backend.core.dingtalk_workbook.get_access_token", return_value="token") as token,
            patch("backend.core.dingtalk_workbook.get_union_id", return_value="union") as union,
            patch(
                "backend.core.dingtalk_workbook.get_all_sheets",
                return_value=[
                    {"name": "26年10月", "id": "sheet-1"},
                    {"name": "  ", "id": "sheet-blank"},
                    {"name": "26年9月"},
                ],
            ) as sheets,
        ):
            result = service.list_workbook_sheets(
                "workbook-1", app_key="app-key", app_secret="app-secret", user_id="user-id"
            )
        self.assertEqual(
            result, [{"name": "26年10月", "id": "sheet-1"}, {"name": "26年9月", "id": ""}]
        )
        token.assert_called_once_with("app-key", "app-secret")
        union.assert_called_once_with("token", "user-id")
        self.assertEqual(sheets.call_args.args[2], "workbook-1")

    def test_uploader_refuses_mismatched_header(self) -> None:
        uploader = service.DwsImageUploader("np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l", "26年10月")
        with patch.object(service, "_dws_read_row", return_value={"F": {"value": "备注"}}):
            with self.assertRaisesRegex(WorkflowError, "表头应为"):
                uploader.inspect(2, "店铺 A")


class RunnerTests(LazadaBillDetailTestCase):
    def test_runner_writes_rows_saves_screenshots_and_reports_counts(self) -> None:
        uploader = FakeUploader()
        payload, sheet, runtime = self.run_service(
            self.query(),
            rows={2: ["店铺 A", "", "", ""], 3: ["店铺 B", "", "", ""]},
            uploader=uploader,
        )
        self.assertTrue(payload["success"], payload["message"])
        self.assertEqual(payload["country"], "TH")
        self.assertEqual(payload["country_name"], "泰国")
        self.assertEqual(payload["sheet_name"], "26年10月")
        self.assertEqual((payload["start_date"], payload["end_date"]), ("2026-09-01", "2026-09-30"))
        self.assertEqual(payload["input_store_count"], 2)
        self.assertEqual(payload["matched_store_count"], 2)
        self.assertEqual(payload["success_store_count"], 2)
        self.assertEqual(payload["failed_store_count"], 0)
        self.assertEqual(payload["image_uploaded_count"], 2)
        self.assertEqual(sheet.read("B2:D2"), [["1234.56", "2000", "765.44"]])
        self.assertEqual(sheet.read("B3:D3"), [["1234.56", "2000", "765.44"]])
        for store in payload["stores"]:
            self.assertEqual(store["status"], "success")
            self.assertEqual(store["image_status"], "uploaded")
            self.assertTrue(store["screenshot"])
            self.assertTrue(Path(store["screenshot"]).is_file())
            self.assertEqual(store["written_range"], f"B{store['row']}:D{store['row']}")
        self.assertEqual([call[1] for call in uploader.calls], ["店铺 A", "店铺 B"])
        self.assertTrue(runtime.started)
        self.assertTrue(runtime.shutdown_called)
        self.assertEqual(len(runtime.closed), 2)
        self.assertTrue(Path(payload["output_file"]).is_file())
        self.assertTrue(Path(payload["log_file"]).is_file())
        saved = json.loads(Path(payload["output_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["success_store_count"], 2)

    def test_runner_passes_selected_range_and_profile_to_page_actions(self) -> None:
        actions = FakeActions()
        query = self.query(start_date="2026-07-01", end_date="2026-07-31", country="VN")
        self.run_service(query, rows={2: ["店铺 A", "", "", ""]}, actions=actions)
        self.assertEqual(len(actions.calls), 1)
        shop_name, start, end = actions.calls[0]
        self.assertEqual(shop_name, "店铺 A")
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-07-01", "2026-07-31"))

    def test_runner_retries_generic_failures_with_a_fresh_login_state_each_time(self) -> None:
        captured: list[dict] = []

        class FlakyActions:
            def collect(self, page, shop_name, profile, start_date, end_date, login_state=None):
                del page, shop_name, profile, start_date, end_date
                state = login_state if login_state is not None else {}
                captured.append(state)
                if len(captured) < 3:
                    # 模拟第一次登录失败：本次尝试内已提交过一次密码登录。
                    state["submitted"] = True
                    raise WorkflowError("页面未就绪")
                return {"total_amount": 1, "revenue": 2, "deductions": 3}

        payload, sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            actions=FlakyActions(),
        )
        self.assertEqual(len(captured), 3, "通用失败应重试到第 3 次")
        self.assertEqual(
            len({id(item) for item in captured}),
            3,
            "每次重试都必须是独立的 login_state，否则第二次会被判定为已提交过登录",
        )
        self.assertEqual(sheet.read("B2:D2"), [["1", "2", "3"]])
        self.assertEqual(payload["stores"][0]["status"], "success")

    def test_runner_stops_at_the_first_failure_when_it_needs_manual_login(self) -> None:
        cases = (
            (page_module.BillLoginRequired("需要人工登录"), "login_required"),
            (page_module.VerificationRequiredError("出现验证码"), "verification_required"),
        )
        for error, expected in cases:
            with self.subTest(status=expected):
                actions = FakeActions(error=error)
                payload, _sheet, _runtime = self.run_service(
                    self.query(store_names="店铺 A"),
                    rows={2: ["店铺 A", "", "", ""]},
                    actions=actions,
                )
                self.assertEqual(len(actions.calls), 1, "登录/验证类失败不做店内重试")
                self.assertEqual(payload["stores"][0]["status"], expected)
                self.assertIn("需人工登录/验证 1", payload["message"])

    def test_store_failure_message_is_logged_only_once(self) -> None:
        """故障原因不能刷两遍（一次带「不再店内重试」，收尾又原样重复一次）。"""
        lines: list[str] = []
        actions = FakeActions(
            error=page_module.BillLoginRequired("密码登录页账号或密码未填好，已跳过；不自动填写凭据")
        )
        self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            actions=actions,
            progress=lines.append,
        )
        hits = [line for line in lines if "密码登录页账号或密码未填好" in line]
        self.assertEqual(len(hits), 1, f"同一故障只应记录一次，实际日志：{lines}")
        self.assertTrue(any("不再店内重试" in line for line in lines), "保留「不再店内重试」这条即时告警")

    def test_successful_store_still_gets_a_final_summary_line(self) -> None:
        """成功行仍然要有一条收尾汇总，去重不能把成功提示也一起吞掉。"""
        lines: list[str] = []
        self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            progress=lines.append,
        )
        self.assertEqual(
            len([line for line in lines if "账单已写入指定 Sheet" in line]),
            1,
            f"成功行应恰好一条收尾汇总，实际日志：{lines}",
        )

    def test_runner_stops_retrying_when_the_browser_session_is_gone(self) -> None:
        actions = FakeActions(
            error=Exception("Target page, context or browser has been closed")
        )
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            actions=actions,
        )
        self.assertEqual(len(actions.calls), 1)
        self.assertEqual(payload["stores"][0]["status"], "browser_failed")

    def test_runner_never_recollects_after_the_sheet_write_was_submitted(self) -> None:
        actions = FakeActions()

        class UncertainSheet(FakeSheet):
            """首次回读为空（允许写入），写入后的回读故意不一致。"""

            def __init__(self, rows):
                super().__init__(rows)
                self.read_backs = 0

            def read(self, cell_range: str):
                if cell_range.strip().upper() == "B2:D2":
                    self.read_backs += 1
                    if self.read_backs > 1:
                        return [["999", "999", "999"]]
                return super().read(cell_range)

        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=str(self.root / "log")),
            sheet=UncertainSheet({2: ["店铺 A", "", "", ""]}),
            actions=actions,
        )
        self.assertEqual(len(actions.calls), 1, "已提交写入后不得重复采集")
        store = payload["stores"][0]
        self.assertEqual(store["status"], "write_failed")
        self.assertIn("未重复采集", store["message"])

    def test_runner_skips_failure_screenshots_on_authentication_pages(self) -> None:
        root = str(self.root / "log")

        class AuthPageRuntime(FakeRuntime):
            def open_browser(self, browser, download_path):
                del download_path
                return FakeOpened(
                    FakePage("https://sellercenter.lazada.com.my/app/seller/register"),
                    str(browser.get("browserOauth")),
                )

        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=root),
            rows={2: ["店铺 A", "", "", ""]},
            actions=FakeActions(error=WorkflowError("未找到收入详情入口")),
            runtime=AuthPageRuntime,
        )
        folder = service.screenshot_folder(root, "泰国", "2026-09-01", "2026-09-30")
        self.assertEqual(payload["stores"][0]["status"], "failed")
        self.assertEqual(list((folder / "debug").glob("*.jpg")), [], "认证页不保存可能含凭据的截图")

    def test_runner_saves_failure_screenshots_for_non_auth_pages(self) -> None:
        root = str(self.root / "log")
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=root),
            rows={2: ["店铺 A", "", "", ""]},
            actions=FakeActions(error=WorkflowError("未找到收入详情入口")),
        )
        folder = service.screenshot_folder(root, "泰国", "2026-09-01", "2026-09-30")
        self.assertEqual(payload["stores"][0]["status"], "failed")
        self.assertEqual(
            [item.name for item in (folder / "debug").glob("*.jpg")], ["店铺 A.jpg"]
        )

    def test_runner_screenshots_the_page_that_collected_the_data(self) -> None:
        """登录会另开标签页：数据在新标签上，截图绝不能仍用停在注册页的旧标签。"""
        stale = FakePage("https://sellercenter.lazada.com.my/app/seller/register")
        stale.tag = "register-tab"
        working = FakePage("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        working.tag = "income-tab"

        class TwoTabRuntime(FakeRuntime):
            def open_browser(self, browser, download_path):
                del download_path
                return FakeOpened(stale, str(browser.get("browserOauth")))

        class TabSwitchingActions(FakeActions):
            def collect(self, page, shop_name, profile, start_date, end_date, login_state=None):
                outcome = super().collect(
                    page, shop_name, profile, start_date, end_date, login_state
                )
                self.last_page = working
                return outcome

        uploader = FakeUploader()
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=str(self.root / "log")),
            rows={2: ["店铺 A", "", "", ""]},
            actions=TabSwitchingActions(),
            runtime=TwoTabRuntime,
            uploader=uploader,
        )
        store = payload["stores"][0]
        self.assertEqual(store["status"], "success")
        self.assertEqual(Path(store["screenshot"]).read_text(encoding="utf-8"), "income-tab")
        self.assertEqual(store["image_status"], "uploaded")
        # 上传到 F 列的必须是同一张「收入详情」截图。
        self.assertEqual(uploader.calls[0][2].read_text(encoding="utf-8"), "income-tab")

    def test_runner_uses_the_last_page_for_failure_screenshots_too(self) -> None:
        """采集中途失败时，失败截图同样取「最后到达的那一页」，而不是旧标签。"""
        stale = FakePage("https://sellercenter.lazada.com.my/app/seller/register")
        stale.tag = "register-tab"
        working = FakePage("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        working.tag = "income-tab"

        class TwoTabRuntime(FakeRuntime):
            def open_browser(self, browser, download_path):
                del download_path
                return FakeOpened(stale, str(browser.get("browserOauth")))

        class FailingActions(FakeActions):
            def collect(self, page, shop_name, profile, start_date, end_date, login_state=None):
                super().collect(page, shop_name, profile, start_date, end_date, login_state)
                self.last_page = working
                raise WorkflowError("未找到收入详情入口")

        root = str(self.root / "log")
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=root),
            rows={2: ["店铺 A", "", "", ""]},
            actions=FailingActions(),
            runtime=TwoTabRuntime,
        )
        folder = service.screenshot_folder(root, "泰国", "2026-09-01", "2026-09-30")
        self.assertEqual(payload["stores"][0]["status"], "failed")
        debug_files = list((folder / "debug").glob("*.jpg"))
        self.assertEqual(len(debug_files), 1)
        self.assertEqual(debug_files[0].read_text(encoding="utf-8"), "income-tab")

    def test_runner_never_uses_a_previous_stores_page(self) -> None:
        """actions.last_page 是复用的实例属性：上一家店的页面不能被当成这一家的截图对象。"""
        first = FakePage("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        first.tag = "first-store"
        second = FakePage("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        second.tag = "second-store"
        pages = [first, second]

        stale = FakePage("https://sellercenter.lazada.com.my/app/seller/register")
        stale.tag = "register-tab"

        class RotatingRuntime(FakeRuntime):
            def open_browser(self, browser, download_path):
                del download_path
                return FakeOpened(stale, str(browser.get("browserOauth")))

        class RotatingActions(FakeActions):
            def collect(self, page, shop_name, profile, start_date, end_date, login_state=None):
                outcome = super().collect(
                    page, shop_name, profile, start_date, end_date, login_state
                )
                self.last_page = pages[len(self.calls) - 1]
                return outcome

        payload, _sheet, _runtime = self.run_service(
            self.query(screenshot_root=str(self.root / "log")),
            rows={2: ["店铺 A", "", "", ""], 3: ["店铺 B", "", "", ""]},
            actions=RotatingActions(),
            runtime=RotatingRuntime,
        )
        tags = [
            Path(store["screenshot"]).read_text(encoding="utf-8") for store in payload["stores"]
        ]
        self.assertEqual(tags, ["first-store", "second-store"], "每家店都必须用自己的页面截图")

    def test_runner_saves_screenshots_into_country_and_range_folder(self) -> None:
        root = str(self.root / "log")
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", screenshot_root=root),
            rows={2: ["店铺 A", "", "", ""]},
        )
        expected = service.screenshot_folder(root, "泰国", "2026-09-01", "2026-09-30")
        store = payload["stores"][0]
        self.assertEqual(payload["screenshot_root"], root)
        self.assertEqual(payload["screenshot_dir"], str(expected))
        self.assertEqual(Path(store["screenshot"]).parent, expected)
        self.assertEqual(expected.parent.name, "Lazada泰国账单明细截图")
        self.assertEqual(expected.name, "2026-09-01到2026-09-30")

    def test_runner_skips_rows_that_already_have_values_without_starting_browser(self) -> None:
        payload, sheet, runtime = self.run_service(
            self.query(),
            rows={2: ["店铺 A", "1", "2", "3"], 3: ["店铺 B", "", "", ""]},
            browsers=[{"browserOauth": "oauth-b", "browserName": "店铺 B"}],
        )
        self.assertEqual(payload["skipped_store_count"], 1)
        self.assertEqual(payload["success_store_count"], 1)
        self.assertEqual(sheet.updates[0][0], "B3:D3")
        skipped = next(item for item in payload["stores"] if item["status"] == "skipped")
        self.assertIn("B:D 已有数据", skipped["message"])
        self.assertEqual(skipped["total_amount"], "1")
        self.assertIsNotNone(runtime)

    def test_runner_does_not_start_runtime_when_nothing_is_pending(self) -> None:
        payload, sheet, runtime = self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "1", "2", "3"]},
        )
        self.assertIsNone(runtime)
        self.assertEqual(sheet.updates, [])
        self.assertEqual(payload["skipped_store_count"], 1)
        self.assertEqual(payload["success_store_count"], 0)

    def test_runner_reports_unmatched_and_failed_stores(self) -> None:
        actions = FakeActions(error=WorkflowError("账单页未找到独立的起始/结束日期输入框"))
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A\n不在表里的店铺"),
            rows={2: ["店铺 A", "", "", ""]},
            actions=actions,
        )
        statuses = {item["requested_store_name"]: item["status"] for item in payload["stores"]}
        self.assertEqual(statuses["不在表里的店铺"], "unmatched_store")
        self.assertEqual(statuses["店铺 A"], "failed")
        self.assertFalse(payload["success"])
        self.assertEqual(payload["failed_store_count"], 2)
        failed = next(item for item in payload["stores"] if item["requested_store_name"] == "店铺 A")
        self.assertIn("起始/结束日期", failed["message"])

    def test_runner_marks_local_only_when_dws_is_not_installed(self) -> None:
        query = self.query(store_names="店铺 A")
        self.assertEqual(
            query.dws_node, service.DEFAULT_WORKBOOK_ID,
            "未显式配置图片文档时，截图写进同一本文档",
        )
        payload, _sheet, _runtime = self.run_service(
            query, rows={2: ["店铺 A", "", "", ""]}
        )
        self.assertFalse(payload["image_sync_available"])
        self.assertEqual(payload["image_local_only_count"], 1)
        self.assertIn("未安装 dws", payload["image_sync_note"])
        self.assertIn("PATH", payload["image_sync_note"], "必须给出可执行的解决办法")
        self.assertEqual(payload["stores"][0]["image_status"], "local_only")
        self.assertTrue(payload["stores"][0]["screenshot"])

    def test_runner_keeps_local_screenshot_when_dws_is_missing(self) -> None:
        uploader = FakeUploader(available=False)
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", dws_node="np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"),
            rows={2: ["店铺 A", "", "", ""]},
            uploader=uploader,
        )
        self.assertEqual(payload["stores"][0]["status"], "success")
        self.assertEqual(payload["stores"][0]["image_status"], "local_only")
        self.assertIn("未安装 dws", payload["image_sync_note"])
        self.assertEqual(uploader.calls, [])

    def test_runner_records_image_upload_failure_without_losing_written_data(self) -> None:
        uploader = FakeUploader(error=WorkflowError("F2 已有非图片内容，未覆盖"))
        payload, sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A", dws_node="np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"),
            rows={2: ["店铺 A", "", "", ""]},
            uploader=uploader,
        )
        store = payload["stores"][0]
        self.assertEqual(store["status"], "success")
        self.assertEqual(store["image_status"], "failed")
        self.assertIn("F 列截图写入失败", store["message"])
        self.assertEqual(payload["image_failed_count"], 1)
        self.assertEqual(sheet.read("B2:D2"), [["1234.56", "2000", "765.44"]])

    def test_runner_redacts_credentials_from_messages(self) -> None:
        actions = FakeActions(error=WorkflowError("登录失败 private-password app-secret"))
        payload, _sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            actions=actions,
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private-password", serialized)
        self.assertNotIn("app-secret", serialized)
        self.assertIn("已隐藏", payload["stores"][0]["message"])

    def test_runner_reports_store_when_sheet_cannot_be_read(self) -> None:
        def failing_factory(_query):
            raise WorkflowError("钉钉工作簿中没有指定 Sheet：26年10月")

        payload = service.run_lazada_bill_detail(
            self.query(store_names="店铺 A"),
            sheet_factory=failing_factory,
            page_actions=FakeActions(),
        )
        self.assertFalse(payload["success"])
        self.assertEqual(payload["failed_store_count"], 1)
        self.assertIn("没有指定 Sheet", payload["stores"][0]["message"])

    def test_runner_reports_unmatched_browser_before_collecting(self) -> None:
        payload, sheet, _runtime = self.run_service(
            self.query(store_names="店铺 A"),
            rows={2: ["店铺 A", "", "", ""]},
            browsers=[{"browserOauth": "oauth-x", "browserName": "其它店铺"}],
        )
        self.assertEqual(payload["stores"][0]["status"], "unmatched_store")
        self.assertEqual(sheet.updates, [])
        self.assertEqual(payload["image_local_only_count"], 0)


if __name__ == "__main__":
    unittest.main()

"""Lazada 后台收支数据页的前端契约测试（HTML / JS / 导航 / 桥接）。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HTML = FRONTEND / "pages" / "lazada-bill-detail.html"
SCRIPT = FRONTEND / "assets" / "pages" / "lazada-bill-detail.js"


class LazadaBillFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HTML.read_text(encoding="utf-8")
        self.javascript = SCRIPT.read_text(encoding="utf-8")
        self.common = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")
        self.dashboard = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.bridge = (ROOT / "backend" / "app_bridge.py").read_text(encoding="utf-8")

    def test_page_has_its_own_html_and_script(self) -> None:
        self.assertTrue(HTML.is_file())
        self.assertTrue(SCRIPT.is_file())
        self.assertIn("../assets/pages/lazada-bill-detail.js", self.html)
        self.assertIn("../assets/common.js", self.html)

    def test_every_referenced_dom_id_exists_in_html(self) -> None:
        referenced = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', self.javascript))
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        self.assertEqual(sorted(referenced - html_ids), [])
        self.assertGreater(len(referenced), 15)

    def test_html_has_no_smart_quotes_in_attributes(self) -> None:
        self.assertEqual(re.findall(r"\b(?:id|class|type|style|placeholder|aria-[\w-]+)=[“”]", self.html), [])

    def test_page_registers_in_sidebar_dashboard_and_launcher(self) -> None:
        self.assertIn('id: "lazada_bill_detail", href: "lazada-bill-detail.html"', self.common)
        self.assertIn("后台收支数据", self.common)
        self.assertIn('lazada_bill_detail: { href: "lazada-bill-detail.html"', self.dashboard)
        self.assertIn('{ key: "lazada_bill_detail", name: "Lazada 后台收支数据"', self.dashboard)
        launcher = (ROOT / "launcher" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"lazada-bill-detail": "lazada-bill-detail.html"', launcher)

    def test_page_declares_task_key_and_bridge_calls(self) -> None:
        self.assertIn('key: "lazada_bill_detail"', self.javascript)
        self.assertIn('taskKey: "lazada_bill_detail"', self.javascript)
        self.assertIn("get_lazada_bill_detail_info", self.javascript)
        self.assertIn("save_lazada_bill_preferences", self.javascript)
        self.assertIn("start_lazada_bill_detail", self.javascript)
        self.assertIn("title: \"Lazada 后台收支数据\"", self.javascript)

    def test_bridge_exposes_info_preferences_and_start_handlers(self) -> None:
        self.assertIn('"key": "lazada_bill_detail"', self.bridge)
        self.assertIn("def get_lazada_bill_detail_info", self.bridge)
        self.assertIn("def save_lazada_bill_preferences", self.bridge)
        self.assertIn("def start_lazada_bill_detail", self.bridge)
        self.assertIn('tool="lazada_bill_detail"', self.bridge)
        self.assertIn("from backend.services import lazada_bill_detail", self.bridge)

    def test_preview_bridge_provides_offline_demo_apis(self) -> None:
        for method in (
            "async get_lazada_bill_detail_info()",
            "async save_lazada_bill_preferences(payload)",
            "async start_lazada_bill_detail(payload)",
        ):
            with self.subTest(method=method):
                self.assertIn(method, self.common)
        self.assertIn('startPreviewTask("lazada_bill_detail"', self.common)
        self.assertIn("hqyl.preview.lazadaBillPreferences", self.common)
        self.assertIn("async list_lazada_bill_sheets(payload)", self.common)
        self.assertIn('image_column: "F"', self.common)

    def test_collection_form_has_country_sheet_range_stores_and_screenshot_fields(self) -> None:
        self.assertIn('id="lazadaBillCountry"', self.html)
        self.assertIn('<select id="lazadaBillSheetName"></select>', self.html)
        self.assertIn('id="lazadaBillRefreshSheetsBtn"', self.html)
        self.assertIn('id="lazadaBillScreenshotHint"', self.html)
        self.assertIn('id="lazadaBillStartDate" type="date" required', self.html)
        self.assertIn('id="lazadaBillEndDate" type="date" required', self.html)
        self.assertIn('id="lazadaBillStores"', self.html)
        self.assertIn('id="lazadaBillWorkbookId"', self.html)
        self.assertIn('id="lazadaBillImageHint"', self.html)
        self.assertIn("五国共用一本", self.html)
        self.assertNotIn("lazadaBillDwsNode", self.html, "图片节点不再是独立输入框：截图写同一文档 F 列")
        self.assertIn('id="lazadaBillScreenshotDir"', self.html)
        self.assertIn('id="lazadaBillChooseScreenshotBtn"', self.html)
        self.assertIn("截图保存位置", self.html)
        self.assertIn("每行一个，需与指定 Sheet 的 A 列一致", self.html)
        self.assertIn("钉钉截图只写入 F 列（不会写入 A～E 列）", self.html)
        self.assertIn("按国家读取，优先同名 Sheet", self.html)

    def test_frontend_uses_previous_month_defaults_and_validates_dates(self) -> None:
        self.assertIn("$(\"lazadaBillStartDate\").value = range.start_date", self.javascript)
        self.assertIn("$(\"lazadaBillEndDate\").value = range.end_date", self.javascript)
        self.assertIn("开始日期不能晚于结束日期", self.javascript)
        self.assertIn("结束日期不能晚于今天", self.javascript)
        self.assertIn("$(\"lazadaBillEndDate\").max = todayIso()", self.javascript)

    def test_frontend_builds_country_aware_payload(self) -> None:
        for field in (
            "country: currentCountry()",
            'sheet_name: $("lazadaBillSheetName").value',
            'start_date: $("lazadaBillStartDate").value',
            'end_date: $("lazadaBillEndDate").value',
            "store_names: storeNames()",
            'workbook_id: workbookIdValue()',
            'screenshot_root: $("lazadaBillScreenshotDir").value.trim()',
            'dingtalk_operator_name: $("lazadaBillOperatorName").value.trim()',
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.javascript)
        self.assertIn("save_lazada_bill_preferences", self.javascript)
        self.assertIn("payload[key] = value", self.javascript)

    def test_frontend_reads_sheet_options_from_the_country_workbook(self) -> None:
        self.assertIn("list_lazada_bill_sheets", self.javascript)
        self.assertIn("renderSheetOptions", self.javascript)
        self.assertIn("preferred_sheet_name", self.javascript)
        self.assertIn("[preferredSheetName, defaultSheetName, previous]", self.javascript)
        self.assertIn('$("lazadaBillRefreshSheetsBtn").addEventListener("click", () => loadSheets(true))',
                      self.javascript)
        self.assertIn('if (key === "workbook_id") loadSheets(false);', self.javascript)
        self.assertIn("读取指定 Sheet 失败", self.javascript)

    def test_frontend_prefills_and_persists_one_shared_dingtalk_document(self) -> None:
        self.assertIn("function persistDefaultWorkbook()", self.javascript)
        self.assertIn('saved: () => savedPreferences.workbook_id || defaultWorkbookId', self.javascript)
        self.assertIn('$("lazadaBillWorkbookId").value = defaultWorkbookId;', self.javascript)
        self.assertIn("默认钉钉文档保存失败", self.javascript)
        self.assertIn("function renderImageHint()", self.javascript)
        self.assertIn("不会写入 A～E 列", self.javascript)

    def test_frontend_shows_the_resolved_screenshot_folder(self) -> None:
        self.assertIn("function renderScreenshotHint()", self.javascript)
        self.assertIn("Lazada${name}账单明细截图", self.javascript)
        self.assertIn("${start}到${end}", self.javascript)
        self.assertIn('$("lazadaBillStartDate").addEventListener("change", renderScreenshotHint);',
                      self.javascript)
        self.assertIn('result.screenshot_dir', self.javascript)

    def test_frontend_warns_when_dws_is_missing(self) -> None:
        """运行前就要提示 dws 缺失：否则用户会以为 F 列图片写好了。"""
        self.assertIn('id="lazadaBillDwsWarning"', self.html)
        self.assertIn('id="lazadaBillDwsHint"', self.html)
        self.assertIn('class="lazada-bill-warning store-field"', self.html)
        self.assertIn("未检测到 dws 命令", self.html)
        self.assertIn("function renderDwsWarning()", self.javascript)
        self.assertIn("dwsAvailable = result.dws_available !== false;", self.javascript)
        self.assertIn("dwsHint = result.dws_hint", self.javascript)
        self.assertIn("renderDwsWarning();", self.javascript)
        self.assertIn("未检测到 dws，图片不会写入", self.javascript)
        # 预览模式不应默认显示告警；?dws=0 可演示告警状态
        self.assertIn('get("dws") !== "0"', self.common)
        self.assertIn("dws_available: previewDws", self.common)

    def test_frontend_renders_written_columns_and_image_status(self) -> None:
        self.assertIn("<th>指定 Sheet</th>", self.html)
        self.assertIn("<th>总金额</th><th>收入</th><th>扣减项</th>", self.html)
        self.assertIn("money(store.total_amount)", self.javascript)
        self.assertIn("money(store.revenue)", self.javascript)
        self.assertIn("money(store.deductions)", self.javascript)
        self.assertIn("image_uploaded_count", self.javascript)
        self.assertIn("image_local_only_count", self.javascript)
        self.assertIn("store.sheet_name || range.sheet_name", self.javascript)
        self.assertIn("store.screenshot", self.javascript)

    def test_sidebar_metadata_keeps_existing_lazada_finance_search_results(self) -> None:
        """既有侧栏搜索用例按「lazada + 财务」只返回三个财务页面，新条目不得命中。"""
        entry = re.search(
            r'\{ id: "lazada_bill_detail".*?\},', self.common, re.DOTALL
        )
        self.assertIsNotNone(entry)
        self.assertNotIn("财务", entry.group(0))
        self.assertIn("Lazada", entry.group(0))
        ids = re.findall(r'\{ id: "([a-z_]+)"', self.common)
        self.assertLess(ids.index("lazada_withdrawal_statistics"), ids.index("lazada_bill_detail"))


if __name__ == "__main__":
    unittest.main()

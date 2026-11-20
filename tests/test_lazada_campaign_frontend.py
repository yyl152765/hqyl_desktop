"""Drive the local Excel UI through AppBridge and read back actual output files."""
from __future__ import annotations

import functools
import json
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings
from tests.test_lazada_campaign_bridge import write_prices

try:
    from playwright.sync_api import Error, expect, sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]


class BridgeHandler(SimpleHTTPRequestHandler):
    bridge: AppBridge

    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        allowed = {"get_lazada_campaign_workbook_info", "choose_lazada_campaign_files", "choose_lazada_campaign_sku_price_file", "inspect_lazada_campaign_files",
                   "export_lazada_campaign_template", "export_lazada_campaign_removed_skus", "start_lazada_campaign_workbook", "get_task_status", "get_latest_task_status", "open_path"}
        if method == "get_app_info":
            settings = self.bridge.config_store.load()
            result = {"ok": True, "app": {"name": "测试工作台", "version": "test"},
                      "settings": {"output_dir": settings.output_dir}, "account_state": {"accounts": [], "active_account_ids": {}}}
        elif method == "get_sidebar_preferences":
            result = {"ok": True, "preferences": {}}
        elif method in allowed:
            result = getattr(self.bridge, method)(*request.get("args", []))
        else:
            result = {"ok": False, "error": "测试接口不允许调用"}
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CampaignFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright unavailable")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except Error:
                continue
        else:
            raise unittest.SkipTest("Chromium/Edge unavailable")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sources = [write_prices(self.root / "第一.xlsx"), write_prices(self.root / "第二.xlsx")]
        self.selected = [str(path) for path in self.sources]
        self.reference_selected = None
        self.save_selected = None
        self.bridge = AppBridge()
        self.bridge.config_store = Mock()
        self.bridge.config_store.load.return_value = AppSettings(output_dir=str(self.root / "输出"))
        self.bridge._payload_with_account = Mock(side_effect=AssertionError("不能读取业务账号"))
        self.window = Mock()
        self.window.create_file_dialog.side_effect = lambda *args, **kwargs: self.save_selected if args and args[0] == 30 else self.selected if kwargs.get("allow_multiple") else self.reference_selected
        picker = patch.dict("sys.modules", {"webview": SimpleNamespace(OPEN_DIALOG=1, FileDialog=SimpleNamespace(SAVE=30), windows=[self.window])})
        picker.start()
        self.addCleanup(picker.stop)
        BridgeHandler.bridge = self.bridge
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(BridgeHandler, directory=str(ROOT / "frontend")))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.context.add_init_script("""window.pywebview = {api: new Proxy({}, {get: (_, method) => (...args) =>
            fetch('/api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({method,args})}).then(r=>r.json())})};""")
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.opened = []
        opener = patch("backend.app_bridge.os.startfile", side_effect=lambda path: self.opened.append(path))
        opener.start()
        self.addCleanup(opener.stop)

    def open_page(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/lazada-campaign-workbook.html")
        expect(self.page.locator("#chooseFilesBtn")).to_be_enabled()

    def choose_files(self):
        self.page.locator("#chooseFilesBtn").click()
        expect(self.page.locator("#sourceFileList [data-remove-file]")).to_have_count(len(self.selected))

    def write_sku_reference(self, entries):
        reference = self.root / "SKU价格.xlsx"
        book = Workbook()
        book.active.append(["SKU", "价格"])
        for entry in entries:
            book.active.append(entry)
        book.save(reference)
        book.close()
        return reference

    def test_batch_without_accounts_then_open_outputs_and_restore(self):
        originals = [path.read_bytes() for path in self.sources]
        self.open_page()
        self.assertEqual(self.page.locator("#campaignAccount, #bindAccountBtn, #campaignStores, #campaignUrl, #clientPath, [data-active-account-name]").count(), 0)
        form = self.page.locator("#campaignForm").bounding_box()
        log = self.page.locator(".log-surface").bounding_box()
        self.assertGreater(log["x"], form["x"] + form["width"])
        self.choose_files()
        self.page.locator("#inspectBtn").click()
        expect(self.page.locator("#inspectionMessage")).to_contain_text("第一.xlsx")
        expect(self.page.locator("#inspectionMessage")).to_contain_text("3 行")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 2 · 失败 0")
        expect(self.page.locator("#keptCount")).to_have_text("2")
        expect(self.page.locator("#totalCount")).to_have_text("6")
        expect(self.page.locator("#exceptionCount")).to_contain_text("异常行 2")
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        for file in task["result"]["files"]:
            book = load_workbook(file["processed_file"])
            try:
                self.assertEqual((book.active["A2"].value, book.active["D2"].value, book.active.max_row), ("0001", 95, 2))
            finally:
                book.close()
        self.assertEqual([path.read_bytes() for path in self.sources], originals)
        self.page.locator("#openArchiveBtn").click()
        self.page.wait_for_function("() => document.querySelector('#logBox').textContent.includes('任务完成')")
        self.page.reload()
        expect(self.page.locator("#completedCount")).to_have_text("完成 2 · 失败 0")
        expect(self.page.locator("#sourceFileList [data-remove-file]")).to_have_count(2)
        expect(self.page.locator("#discountThreshold")).to_have_value("95")
        expect(self.page.locator("#skuPriceFile")).to_have_value("")
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 0 行")
        self.assertFalse(self.errors)
        self.assertIn(task["result"]["archive_file"], self.opened)
        self.bridge._payload_with_account.assert_not_called()

    def test_corrupt_file_does_not_block_successful_file(self):
        self.sources[1].write_bytes(b"corrupt Excel")
        self.open_page()
        self.choose_files()
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 1")
        expect(self.page.locator("#resultBadge")).to_have_text("部分完成")
        expect(self.page.get_by_role("button", name="筛选 Excel", exact=True)).to_be_enabled()
        expect(self.page.locator("#openArchiveBtn")).to_be_enabled()
        self.assertFalse(self.errors)

    def test_recommended_mode_and_decimal_threshold_restore(self):
        self.selected = [str(write_prices(self.root / "推荐.xlsx", recommended=True))]
        self.open_page()
        self.choose_files()
        self.page.locator("#processingMode").select_option("invited")
        self.page.locator("#discountThreshold").fill("95.05")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#keptCount")).to_have_text("0")
        expect(self.page.locator("#updatedCount")).to_have_text("填充活动价 2 行")
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        self.assertEqual(task["context"]["threshold"], "0.9505")
        self.assertEqual(task["context"]["mode"], "invited")
        self.page.reload()
        expect(self.page.locator("#processingMode")).to_have_value("invited")
        expect(self.page.locator("#discountThreshold")).to_have_value("95.05")
        self.assertFalse(self.errors)

    def test_equal_price_option_exports_at_custom_discount_and_restores(self):
        source = self.root / "同价活动表.xlsx"
        book = Workbook()
        book.active.append(["卖家SKU", "售价", "大促价格 (必填)"])
        for row in (("same", 132, 132), ("above", 100, 95), ("below", 100, 93.49)):
            book.active.append(row)
        book.save(source)
        book.close()
        self.selected = [str(source)]
        self.open_page()
        self.choose_files()
        option = self.page.get_by_role("checkbox", name="已有活动价等于原价时，按最低折扣率改价")
        expect(option).not_to_be_checked()
        self.page.locator("#discountThreshold").fill("93.5")
        option.check()
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#updatedCount")).to_have_text("填充或调整活动价 1 行")
        expect(self.page.locator("#keptCount")).to_have_text("2")
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        self.assertEqual(task["context"]["threshold"], "0.935")
        self.assertIs(task["context"]["reprice_equal_prices"], True)
        output = load_workbook(task["result"]["files"][0]["processed_file"])
        try:
            self.assertEqual(list(output.active.iter_rows(min_row=2, values_only=True)),
                             [("same", 132, 123.42), ("above", 100, 95)])
        finally:
            output.close()
        self.page.reload()
        expect(option).to_be_checked()
        expect(self.page.locator("#discountThreshold")).to_have_value("93.5")
        self.assertFalse(self.errors)

    def test_localized_invited_template_detected_and_filled_automatically(self):
        source = self.root / "新版特邀.xlsx"
        book = Workbook()
        book.active.append(["Sku ID", "卖家SKU", "产品名称", "提醒", "当前价格", "当前存货",
                            "市场活动价", "流量加权价", "推荐价格", "Is Hero Product"])
        book.active.append(["1", "0001", "商品", None, 100, 5, None, 1, "<=95", "No"])
        book.active.append(["2", "0002", "商品", None, 100, 5, None, 999, "<=94", "No"])
        book.save(source)
        book.close()
        original = source.read_bytes()
        self.selected = [str(source)]
        self.open_page()
        self.choose_files()
        self.page.locator("#inspectBtn").click()
        expect(self.page.locator("#inspectionMessage")).to_contain_text("按推荐价填充")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#keptCount")).to_have_text("1")
        expect(self.page.locator("#updatedCount")).to_have_text("填充活动价 2 行")
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        output = load_workbook(task["result"]["files"][0]["processed_file"])
        try:
            self.assertEqual((output.active["G2"].value, output.active["H2"].value), (95, 1))
            self.assertEqual(output.active.max_row, 2)
        finally:
            output.close()
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.errors)

    def test_export_empty_template_before_importing_files(self):
        self.open_page()
        self.page.locator("#exportTemplateBtn").click()
        expect(self.page.locator("#templateNotice")).to_contain_text("模板已保存")
        self.page.wait_for_function("() => !document.querySelector('#exportTemplateBtn').disabled")
        self.assertEqual(len(self.opened), 1)
        book = load_workbook(self.opened[0])
        try:
            self.assertEqual(book.active.max_row, 1)
            self.assertEqual(book.active["C1"].value, "原价")
        finally:
            book.close()
        self.assertFalse(self.errors)

    def test_regular_recommended_range_fills_empty_campaign_price(self):
        source = self.root / "普通推荐范围.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.append(["卖家SKU", "Sku ID", "店铺sku", "商品名", "商品链接", "售价", "大促价格 (必填)",
                      "推荐价格", "库存", "促销库存", "促销限购", "品牌", "类目", "备注"])
        instruction = "大促价格是你提到这个活动的商品价格。如果你同一商品在相同的时间段提报了2个价格，提报的最低价会被应用。"
        sheet.append([None, None, None, None, None, None, instruction,
                      "大促推荐价是你可以提报这个活动价格范围", "库存代表当前存货", None, None, None, None, None])
        for sku, base, recommended in (("0001", 100, "0 < Price <= 95"),
                                       ("0002", 100, "0 < Price <= 94.99"),
                                       ("0003", 0, "0 < Price <= 95")):
            sheet.append([sku, sku, "店铺SKU", "商品", "https://example.invalid/product", base, None,
                          recommended, 8, None, None, None, None, None])
        book.save(source)
        book.close()
        original = source.read_bytes()
        self.selected = [str(source)]
        self.open_page()
        self.choose_files()
        self.page.locator("#inspectBtn").click()
        expect(self.page.locator("#inspectionMessage")).to_contain_text("普通活动模板，3 行，异常 1 行")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#totalCount")).to_have_text("3")
        expect(self.page.locator("#keptCount")).to_have_text("1")
        expect(self.page.locator("#exceptionCount")).to_contain_text("异常行 1")
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        result = task["result"]["files"][0]
        self.assertEqual((result["mode"], result["removed_rows"]), ("regular", 1))
        output = load_workbook(result["processed_file"])
        try:
            self.assertEqual((output.active["A3"].value, output.active["G3"].value,
                              output.active["H3"].value), ("0001", 95, "0 < Price <= 95"))
            self.assertEqual(output.active["G2"].value, instruction)
            self.assertEqual(output.active.max_row, 3)
        finally:
            output.close()
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.errors)

    def test_manual_paths_deduplicate_limit_and_picker_cancel(self):
        self.open_page()
        self.choose_files()
        self.selected = None
        self.page.locator("#chooseFilesBtn").click()
        expect(self.page.locator("#chooseFilesBtn")).to_be_enabled()
        expect(self.page.locator("#sourceFileList [data-remove-file]")).to_have_count(2)
        self.page.locator(".campaign-paths summary").click()
        self.page.locator("#sourcePaths").fill(f'"{self.sources[0]}"\n\n{self.sources[0]}')
        expect(self.page.locator("#fileCount")).to_have_text("1")
        self.page.locator("#sourcePaths").fill("\n".join(f"C:\\商品{i}.xlsx" for i in range(101)))
        expect(self.page.locator("#startBtn")).to_be_disabled()
        expect(self.page.locator("#formIssues")).to_contain_text("100")
        self.page.locator("#clearFilesBtn").click()
        expect(self.page.locator("#sourceFileList")).to_contain_text("尚未选择文件")
        expect(self.page.locator("#startBtn")).to_be_disabled()
        self.assertFalse(self.errors)

    def test_optional_sku_reference_filters_restores_and_removes(self):
        source = self.root / "活动表.xlsx"
        book = Workbook()
        book.active.append(["SKU", "商品名称", "原价", "活动价"])
        for row in (("0001", "低于底价", 100, 95), ("0002", "等于底价", 100, 95),
                    ("未匹配", "无参考SKU", 100, 95), ("0004", "折扣先剔除", 100, 94.99),
                    ("bad", "原价为零", 0, 95)):
            book.active.append(row)
        book.save(source)
        book.close()
        reference = self.write_sku_reference([("0001", 96), ("0002", 95), ("0004", 200), ("bad", 100)])
        original = (source.read_bytes(), reference.read_bytes())
        self.selected = [str(source)]
        self.reference_selected = [str(reference)]
        self.open_page()
        self.choose_files()
        self.page.locator("#chooseSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        self.assertFalse(self.window.create_file_dialog.call_args.kwargs["allow_multiple"])
        self.page.locator("#inspectBtn").click()
        expect(self.page.locator("#inspectionMessage")).to_contain_text("SKU价格表：已读取 4 个SKU")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#keptCount")).to_have_text("2")
        expect(self.page.locator("#removedCount")).to_have_text("3")
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 1 行")
        expect(self.page.locator("#exceptionCount")).to_contain_text("异常行 1")
        expect(self.page.locator("#fileResults tr td")).to_have_count(9)
        task = self.bridge.get_latest_task_status("lazada_campaign_workbook")
        self.assertEqual(task["context"]["sku_price_file"], str(reference))
        result = task["result"]
        self.assertEqual((result["total_rows"], result["removed_rows"], result["sku_removed_rows"], result["unparseable_rows"]), (5, 1, 1, 1))
        output = load_workbook(result["files"][0]["processed_file"])
        try:
            self.assertEqual([output.active.cell(row, 1).value for row in (2, 3)], ["0002", "未匹配"])
            self.assertEqual(output.active.max_row, 3)
        finally:
            output.close()
        self.page.reload()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 1 行")
        self.page.locator("#clearSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_have_value("")
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 0 行")
        expect(self.page.locator("#clearSkuPriceBtn")).to_be_disabled()
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 1 · 失败 0")
        expect(self.page.locator("#keptCount")).to_have_text("3")
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 0 行")
        self.assertEqual((source.read_bytes(), reference.read_bytes()), original)
        self.bridge._payload_with_account.assert_not_called()
        self.assertFalse(self.errors)

    def test_conflicting_reference_blocks_inspect_and_start(self):
        reference = self.write_sku_reference([("0001", 95), ("0001", 96)])
        self.reference_selected = [str(reference)]
        self.open_page()
        self.choose_files()
        self.page.locator("#chooseSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        with patch.object(self.bridge.tasks, "start", wraps=self.bridge.tasks.start) as start_task:
            self.page.locator("#inspectBtn").click()
            expect(self.page.locator("#formIssues")).to_contain_text("冲突")
            expect(self.page.locator("#inspectBtn")).to_be_enabled()
            self.page.locator("#startBtn").click()
            expect(self.page.locator("#resultBadge")).to_have_text("启动失败")
            expect(self.page.locator("#logBox")).to_contain_text("冲突")
            start_task.assert_not_called()
        expect(self.page.locator("#openArchiveBtn")).to_be_disabled()
        self.assertFalse(self.errors)

    def test_removed_skus_preview_full_export_cancel_restore_and_busy(self):
        first, second = self.root / "第一份活动.xlsx", self.root / "第二份活动.xlsx"
        sku = '<img src=x onerror="alert(1)">'
        book = Workbook()
        book.active.title = "商品表"
        book.active.append(["SKU", "原价", "活动价"])
        for _ in range(102):
            book.active.append([sku, 100, 95])
        for row in (("折扣剔除", 100, 94.99), ("原价异常", 0, 95), ("保留", 100, 95)):
            book.active.append(row)
        book.save(first)
        book.close()
        book = Workbook()
        book.active.append(["SKU", "原价", "活动价"])
        book.active.append(["OTHER", 100, 95])
        book.save(second)
        book.close()
        reference = self.write_sku_reference([(sku, 96), ("折扣剔除", 1000), ("原价异常", 1000), ("保留", 95), ("OTHER", 100)])
        originals = [path.read_bytes() for path in (first, second, reference)]
        self.selected, self.reference_selected = [str(first), str(second)], [str(reference)]
        self.open_page()
        self.choose_files()
        self.page.locator("#chooseSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#completedCount")).to_have_text("完成 2 · 失败 0")
        first_button = self.page.locator('[data-view-removed-skus="0"]')
        first_button.click()
        expect(self.page.locator("#removedSkusPanel")).to_be_visible()
        expect(self.page.locator("#removedSkusTitle")).to_contain_text("第一份活动.xlsx · SKU价格剔除（102）")
        expect(self.page.locator("#removedSkusDescription")).to_contain_text("预览前 100 行")
        expect(self.page.locator("#removedSkusRows tr")).to_have_count(100)
        expect(self.page.locator("#removedSkusRows tr").first.locator("td").first).to_have_text(sku)
        self.assertEqual(self.page.locator("#removedSkusRows img").count(), 0)
        self.assertLessEqual(self.page.locator(".campaign-removed-table").bounding_box()["height"], 342)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#removedSkusPanel")).to_be_hidden()
        expect(first_button).to_be_focused()
        self.page.locator('[data-view-removed-skus="1"]').click()
        expect(self.page.locator("#removedSkusTitle")).to_contain_text("第二份活动.xlsx")
        expect(self.page.locator("#removedSkusRows tr")).to_have_count(1)
        expect(self.page.locator("#removedSkusRows")).to_contain_text("OTHER")
        first_button.click()
        target = self.root / "导出删除SKU.xlsx"
        self.save_selected = [str(target)]
        self.page.locator("#exportRemovedSkusBtn").click()
        expect(self.page.locator("#removedSkusMessage")).to_contain_text(str(target))
        self.assertEqual(self.window.create_file_dialog.call_args.args[0], 30)
        output = load_workbook(target)
        try:
            rows = list(output.active.values)
            self.assertEqual(rows[0], ("SKU", "活动价", "参考价格", "工作表", "原始行号", "删除原因", "源文件"))
            self.assertEqual(len(rows), 103)
            self.assertTrue(all(row[0] == sku and row[1:3] == (95, 96) and row[3] == "商品表" for row in rows[1:]))
            self.assertEqual([row[4] for row in rows[1:]], list(range(2, 104)))
            self.assertEqual(output.active["A2"].data_type, "s")
        finally:
            output.close()
        exported = target.read_bytes()
        message = self.page.locator("#removedSkusMessage").inner_text()
        self.save_selected = None
        self.page.locator("#exportRemovedSkusBtn").click()
        expect(self.page.locator("#exportRemovedSkusBtn")).to_be_enabled()
        expect(self.page.locator("#removedSkusMessage")).to_have_text(message)
        self.assertEqual(target.read_bytes(), exported)
        self.page.reload()
        expect(self.page.locator("#completedCount")).to_have_text("完成 2 · 失败 0")
        self.page.locator('[data-view-removed-skus="0"]').click()
        expect(self.page.locator("#removedSkusRows tr")).to_have_count(100)
        restored_target = self.root / "恢复后导出.xlsx"
        self.save_selected = str(restored_target)
        self.page.locator("#exportRemovedSkusBtn").click()
        expect(self.page.locator("#removedSkusMessage")).to_contain_text(str(restored_target))
        self.assertEqual(restored_target.read_bytes(), exported)
        self.page.evaluate("""() => {
            const realApi = window.pywebview.api; window.exportCalls = 0;
            window.pywebview.api = new Proxy(realApi, {get: (target, method) => method === 'export_lazada_campaign_removed_skus'
                ? () => { window.exportCalls++; return new Promise(resolve => {window.finishExport = resolve;}); } : target[method]});
        }""")
        self.page.locator("#exportRemovedSkusBtn").click()
        expect(self.page.locator("#exportRemovedSkusBtn")).to_be_disabled()
        expect(self.page.locator("#clearFilesBtn")).to_be_disabled()
        self.page.locator("#exportRemovedSkusBtn").dispatch_event("click")
        self.assertEqual(self.page.evaluate("window.exportCalls"), 1)
        self.page.evaluate("() => window.finishExport({ok:false,error:'保存失败示例'})")
        expect(self.page.locator("#removedSkusMessage")).to_have_text("保存失败示例")
        expect(self.page.locator("#exportRemovedSkusBtn")).to_be_enabled()
        self.page.locator("#closeRemovedSkusBtn").click()
        expect(self.page.locator('[data-view-removed-skus="0"]')).to_be_focused()
        self.page.locator('[data-view-removed-skus="0"]').click()
        self.page.locator("#clearFilesBtn").click()
        expect(self.page.locator("#removedSkusPanel")).to_be_hidden()
        self.assertEqual([path.read_bytes() for path in (first, second, reference)], originals)
        self.assertFalse(self.errors)

    def test_reference_picker_cancel_and_busy_controls(self):
        reference = self.write_sku_reference([("0001", 95)])
        self.open_page()
        self.choose_files()
        self.page.locator("#skuPriceFile").fill(str(reference))
        self.page.locator("#chooseSkuPriceBtn").click()
        expect(self.page.locator("#chooseSkuPriceBtn")).to_be_enabled()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        self.page.evaluate("""() => {
            const realApi = window.pywebview.api;
            window.pywebview.api = new Proxy(realApi, {get: (target, method) => method === 'choose_lazada_campaign_sku_price_file'
                ? () => new Promise(resolve => { window.finishReferencePicker = resolve; }) : target[method]});
        }""")
        self.page.locator("#chooseSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_be_disabled()
        expect(self.page.locator("#clearSkuPriceBtn")).to_be_disabled()
        expect(self.page.locator("#chooseFilesBtn")).to_be_disabled()
        expect(self.page.locator("#startBtn")).to_be_disabled()
        self.page.evaluate("() => window.finishReferencePicker({ok:true,cancelled:true})")
        expect(self.page.locator("#skuPriceFile")).to_be_enabled()
        expect(self.page.locator("#clearSkuPriceBtn")).to_be_enabled()
        expect(self.page.locator("#skuPriceFile")).to_have_value(str(reference))
        self.assertFalse(self.errors)

    def test_removed_skus_preview_survives_missing_complete_artifact(self):
        file_result = {"name": "部分完成.xlsx", "status": "success", "sku_removed_rows": 1,
                       "processed_file": str(self.sources[0]), "removed_sku_file": "",
                       "error": "删除SKU完整明细保存失败", "removed_sku_preview": [
                           {"sku": "0001", "activity_price": "95", "reference_price": "96",
                            "worksheet": "商品", "source_row": 2, "reason": "活动价低于SKU参考价格"}]}
        self.bridge.tasks.start("明细保存失败任务", lambda progress: {"file_count": 1, "success_count": 1,
                                                                    "is_complete": False, "files": [file_result]},
                                tool="lazada_campaign_workbook", context={"workflow": "local_excel", "source_files": [str(self.sources[0])]})
        self.open_page()
        expect(self.page.get_by_role("button", name="筛选 Excel", exact=True)).to_be_enabled()
        self.page.get_by_role("button", name="SKU价格剔除（1）", exact=True).click()
        expect(self.page.locator("#removedSkusRows")).to_contain_text("0001")
        expect(self.page.locator("#removedSkusDescription")).to_contain_text("完整明细未生成，暂不能导出")
        expect(self.page.locator("#exportRemovedSkusBtn")).to_be_disabled()
        self.assertFalse(self.errors)

    def test_old_local_context_without_sku_reference_still_restores(self):
        self.bridge.tasks.start("旧本地任务", lambda progress: {"file_count": 1, "kept_rows": 1,
                                                              "files": [{"name": "旧结果.xlsx", "status": "success", "sku_removed_rows": 1}]},
                                tool="lazada_campaign_workbook", context={"workflow": "local_excel", "source_files": [str(self.sources[0])], "mode": "plain"})
        self.open_page()
        expect(self.page.locator("#sourceFileList [data-remove-file]")).to_have_count(1)
        expect(self.page.locator("#skuPriceFile")).to_have_value("")
        expect(self.page.locator("#repriceEqualPrices")).not_to_be_checked()
        expect(self.page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 0 行")
        expect(self.page.locator("[data-view-removed-skus]")).to_have_count(0)
        self.assertFalse(self.errors)

    def test_older_backend_cannot_silently_ignore_sku_filter(self):
        old_info = self.bridge.get_lazada_campaign_workbook_info()
        old_info.pop("supports_sku_price_filter", None)
        with patch.object(self.bridge, "get_lazada_campaign_workbook_info", return_value=old_info):
            self.open_page()
        expect(self.page.locator("#chooseSkuPriceBtn")).to_be_disabled()
        expect(self.page.locator("#skuPriceFile")).to_be_disabled()
        expect(self.page.locator("#skuSupportNotice")).to_contain_text("当前后端未支持SKU筛选")
        self.choose_files()
        self.page.evaluate("""() => {
            const input = document.querySelector('#skuPriceFile');
            input.value = 'C:\\\\Reference\\\\SKU.xlsx'; input.dispatchEvent(new Event('input'));
        }""")
        with patch.object(self.bridge, "start_lazada_campaign_workbook") as start_task:
            self.page.locator("#inspectBtn").click()
            expect(self.page.locator("#formIssues")).to_contain_text("当前后端未支持SKU筛选，请重启或升级工作台")
            self.page.locator("#startBtn").click()
            expect(self.page.locator("#formIssues")).to_contain_text("当前后端未支持SKU筛选")
            start_task.assert_not_called()
        expect(self.page.locator("#clearSkuPriceBtn")).to_be_enabled()
        self.page.locator("#clearSkuPriceBtn").click()
        expect(self.page.locator("#skuPriceFile")).to_have_value("")
        self.assertFalse(self.errors)

    def test_sku_reference_preview_is_explicit_and_does_not_call_backend(self):
        context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(context.close)
        page = context.new_page()
        posts, errors = [], []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/lazada-campaign-workbook.html?preview=1")
        expect(page.locator("#chooseFilesBtn")).to_be_enabled()
        expect(page.locator("#previewNotice")).to_be_visible()
        page.locator("#chooseFilesBtn").click()
        page.locator("#chooseSkuPriceBtn").click()
        expect(page.locator("#skuPriceFile")).to_have_value("C:\\Preview\\SKU价格表.xlsx")
        expect(page.locator("#sourceFileList")).to_contain_text("商品表格.xlsx")
        page.locator("#inspectBtn").click()
        expect(page.locator("#inspectionMessage")).to_contain_text("界面演示，未读取真实 Excel")
        expect(page.locator("#inspectionMessage")).to_contain_text("示例 8 个SKU")
        page.locator("#startBtn").click()
        expect(page.locator("#completedCount")).to_have_text("演示数据 · 未处理真实文件")
        expect(page.locator("#skuRemovedCount")).to_have_text("SKU价格剔除 2 行")
        expect(page.locator("#removedCount")).to_have_text("6")
        expect(page.locator("#openArchiveBtn")).to_be_disabled()
        expect(page.locator("#openSummaryBtn")).to_be_disabled()
        expect(page.locator("#openDirectoryBtn")).to_be_disabled()
        page.get_by_role("button", name="SKU价格剔除（2）", exact=True).click()
        expect(page.locator("#removedSkusRows tr")).to_have_count(2)
        expect(page.locator("#removedSkusDescription")).to_contain_text("界面演示")
        expect(page.locator("#exportRemovedSkusBtn")).to_be_disabled()
        self.assertFalse(posts)
        self.assertFalse(errors)

    def test_old_download_task_does_not_restore_or_request_account(self):
        self.bridge.tasks.start("旧紫鸟任务", lambda progress: {"file_count": 99, "kept_rows": 99},
                                tool="lazada_campaign_workbook", context={"workflow": "download", "account_id": "z1", "store_names": ["old"]})
        self.open_page()
        expect(self.page.locator("#keptCount")).to_have_text("0")
        expect(self.page.locator("#openArchiveBtn")).to_be_disabled()
        expect(self.page.locator("#sourcePaths")).to_have_value("")
        self.assertFalse(self.errors)

    def test_dashboard_general_tools_filter_links_to_local_excel(self):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/dashboard.html")
        card = self.page.locator(".dashboard-tool", has_text="Excel 折扣筛选")
        expect(card).to_have_count(1)
        self.page.locator("#vendorTabs [data-vendor=local]").click()
        expect(self.page.locator(".dashboard-group")).to_have_count(1)
        expect(self.page.locator(".dashboard-group[data-vendor=local]")).to_be_visible()
        expect(card).to_contain_text("本地处理 · 无需绑定账号")
        card.click()
        expect(self.page.locator("#chooseFilesBtn")).to_be_enabled()
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()

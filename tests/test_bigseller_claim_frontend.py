"""Browser checks for claim-query recovery and explicit report warehouse selection."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
PARTIAL = {
    "source_file": "C:/Source/新品认领表.xlsx",
    "sheet_name": "新sku上架",
    "site": "all",
    "listing_scope": "live",
    "output_dir": "C:/Exports",
    "output_file": "C:/Exports/认领结果.xlsx",
    "checkpoint_file": "C:/Exports/BigSeller认领进度/progress.json",
    "sku_count": 3,
    "total_rows": 4,
    "matched_count": 1,
    "not_found_count": 1,
    "failed_count": 1,
    "confirmed_count": 2,
    "is_complete": False,
    "completion_message": "仍有 1 个 SKU 查询失败",
    "rows": [
        {"sku": '<SKU & "A">', "shop_name": "<店铺A>", "created_time": "2026-01-02 03:04:05", "listed_time": "2026-01-03 06:07:08", "status": "matched", "message": "同一条商品记录"},
        {"sku": "B", "status": "not_found", "message": "没有匹配的主 SKU"},
        {"sku": "C", "status": "failed", "message": "请求失败"},
    ],
}


class ClaimAndWarehouseFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is required for frontend behavior checks")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        else:
            raise unittest.SkipTest("Chromium/Edge is required for frontend behavior checks")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def open_page(self, module="bigseller-claim-query") -> None:
        html = (FRONTEND / "pages" / f"{module}.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>', "", html)
        html = re.sub(r"<link[^>]+>", "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""() => {
          window.submitted = [];
          window.taskSequence = 0;
          window.opened = [];
          window.latestTask = {ok: true, empty: true};
          window.startFailure = '';
          window.pywebview = {api: {
            async get_app_info() { return {
              ok: true, app: {name: '测试', version: 'test'},
              settings: {output_dir: 'C:/Exports', income_expense_category_ids: ['category-a'], income_expense_warehouse_keys: ['1100204', '1100500', '1096191']},
              account_state: {accounts: [{id: 'bs', vendor: 'bigseller', name: '测试BS', username: 'demo-bs'}, {id: 'mb', vendor: 'mabang', name: '测试马帮', username: 'demo-mb'}], active_account_ids: {bigseller: 'bs', mabang: 'mb'}},
              mabang_income_expense_report: {
                date_range: {start_date: '2026-09-01', end_date: '2026-09-30'},
                categories: [{id: 'category-a', name: '分类A'}],
                warehouses: [{key: '1100204', name: '印尼雅仓'}, {key: '1100500', name: '菲律宾雅仓'}, {key: '1096191', name: '马来雅仓'}, {key: 'other', name: '其他仓'}],
              },
            }; },
            async get_sidebar_preferences() { return {ok: true, preferences: {}}; },
            async get_latest_task_status() { return {ok: true, empty: true}; },
            async inspect_bigseller_claim_file() { return {ok: true, sheets: ['新sku上架', '另一个工作表'], sheet_name: '新sku上架'}; },
            async choose_bigseller_claim_file() { return {ok: true, path: 'C:/Source/新品认领表.xlsx', sheets: ['新sku上架', '另一个工作表'], sheet_name: '新sku上架'}; },
            async choose_bigseller_claim_checkpoint() { return {ok: true, path: 'C:/Exports/BigSeller认领进度/progress.json'}; },
            async start_bigseller_claim_query(payload) {
              submitted.push(payload);
              if (startFailure) return {ok: false, error: startFailure};
              latestTask = {ok: true, id: `task-${++taskSequence}`, status: 'running', logs: ['[BS认领进度 1/3]'], context: payload};
              return latestTask;
            },
            async get_task_status() { return latestTask; },
            async open_path(path) { opened.push(path); return {ok: true}; },
            async start_mabang_income_expense_report(payload) { submitted.push(payload); return {ok: false, error: '仅验证参数'}; },
          }};
        }""")
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages" / f"{module}.js"))
        self.page.wait_for_function("HQYL.state.page && HQYL.state.accounts.length === 2")

    def apply_result(self, result) -> None:
        self.page.evaluate("""result => {
          HQYL.state.page.setRunning(false);
          HQYL.state.page.applyResult(result);
        }""", result)

    def test_import_all_sites_payload_progress_and_busy_controls(self) -> None:
        self.open_page()
        expect(self.page.locator("#site")).to_have_value("all")
        self.page.locator("#chooseFileBtn").click()
        expect(self.page.locator("#sheetName")).to_have_value("新sku上架")
        self.page.locator("#runBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0]"), {
            "source_file": PARTIAL["source_file"], "sheet_name": PARTIAL["sheet_name"],
            "site": "all", "listing_scope": "live", "output_dir": "C:/Exports",
        })
        expect(self.page.locator("#progressCount")).to_have_text("1 / 3")
        expect(self.page.locator("#queryProgress")).to_have_attribute("aria-valuenow", "33")
        for selector in ("#site", "#sourceFile", "#sheetName", "#runBtn", "#chooseCheckpointBtn"):
            expect(self.page.locator(selector)).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_partial_result_escapes_values_and_retry_uses_original_conditions(self) -> None:
        self.open_page()
        self.apply_result(PARTIAL)
        expect(self.page.locator("#progressCount")).to_have_text("2 / 3")
        expect(self.page.locator("#resultTableBody tr")).to_have_count(3)
        cells = self.page.locator("#resultTableBody tr").first.locator("td")
        self.assertEqual(cells.all_text_contents()[:4], ['<SKU & "A">', "<店铺A>", "2026-01-02 03:04:05", "2026-01-03 06:07:08"])
        expect(self.page.locator("#resultTableBody script")).to_have_count(0)
        self.page.locator("#openFileBtn").click()
        self.assertEqual(self.page.evaluate("opened[0]"), PARTIAL["output_file"])
        self.page.locator("#sourceFile").fill("C:/Changed.xlsx")
        self.page.locator("#site").select_option("MY")
        self.page.locator("#outputDir").fill("C:/Changed")
        self.page.evaluate("startFailure = '账号与进度文件不一致'")
        self.page.locator("#retryFailedBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0]"), {
            "source_file": PARTIAL["source_file"], "sheet_name": PARTIAL["sheet_name"],
            "site": "all", "listing_scope": "live", "output_dir": PARTIAL["output_dir"],
            "resume_checkpoint": PARTIAL["checkpoint_file"],
        })
        expect(self.page.locator("#queryResult")).to_be_visible()
        expect(self.page.locator("#progressText")).to_contain_text("补查未启动，上次结果已保留")
        expect(self.page.locator("#retryFailedBtn")).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_export_failure_preview_and_checkpoint_invalidation(self) -> None:
        self.open_page()
        export_failed = {**PARTIAL, "confirmed_count": 3, "matched_count": 2, "failed_count": 0, "output_file": "", "completion_message": "Excel 导出未完成"}
        self.apply_result(export_failed)
        expect(self.page.locator("#retryFailedBtn")).to_have_text("重新导出已确认结果")
        expect(self.page.locator("#openFileBtn")).to_be_disabled()
        expect(self.page.locator("#failureNotice")).not_to_contain_text("仍有 0 个查询失败")
        self.apply_result({**PARTIAL, "is_preview": True})
        expect(self.page.locator("#previewNotice")).to_be_visible()
        expect(self.page.locator("#retryFailedBtn")).to_be_hidden()
        expect(self.page.locator("#openFileBtn")).to_be_disabled()
        expect(self.page.locator("#openOutputBtn")).to_be_disabled()
        self.page.locator("details summary").click()
        self.page.locator("#chooseCheckpointBtn").click()
        expect(self.page.locator("#resumeCheckpoint")).to_have_value(PARTIAL["checkpoint_file"])
        self.page.locator("#site").select_option("PH")
        expect(self.page.locator("#resumeCheckpoint")).to_have_value("")
        self.page.locator("#chooseCheckpointBtn").click()
        self.page.locator("#sourceFile").fill("C:/Another.xlsx")
        expect(self.page.locator("#resumeCheckpoint")).to_have_value("")
        self.assertEqual(self.errors, [])

    def test_warehouse_defaults_ignore_previous_settings_but_manual_selection_works(self) -> None:
        self.open_page("mabang-income-expense-report")
        selected = self.page.locator("#incomeExpenseWarehouseSelect option:checked")
        expect(selected).to_have_count(0)
        expect(self.page.locator("#incomeExpenseCategorySelect option:checked")).to_have_count(1)
        self.page.locator("#incomeExpenseRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0].warehouse_keys"), [])
        self.page.locator("#selectYacangWarehousesBtn").click()
        self.assertEqual(selected.evaluate_all("items => items.map(item => item.value)"), ["1100204", "1100500", "1096191"])
        self.page.locator("#clearSelectionsBtn").click()
        expect(selected).to_have_count(0)
        self.page.locator("#selectAllWarehousesBtn").click()
        expect(selected).to_have_count(4)
        self.page.evaluate("async () => HQYL.state.page.init(await pywebview.api.get_app_info())")
        expect(selected).to_have_count(0)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

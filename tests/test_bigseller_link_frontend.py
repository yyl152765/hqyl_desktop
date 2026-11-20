"""Exercise the real page and bridge contract in a local, offline browser."""
from __future__ import annotations

import functools
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error, expect, sync_playwright


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class LinkFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(FRONTEND)))
        cls.server.daemon_threads = True
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/pages/bigseller-link-query.html?preview=1&account=bigseller"

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1100})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.url)
        expect(self.page.locator("#outputDir")).not_to_have_value("")

    def test_batch_input_dedup_preview_navigation_and_export_buttons(self):
        page = self.page
        expect(page.locator("h1")).to_have_text("BS 抓取链接")
        expect(page.locator("#site")).to_have_value("PH")
        self.assertEqual(page.locator('a[href="bigseller-pagelist-stress.html"]').count(), 0)
        page.locator("#skuText").fill("001\n001,sku-a；sku-b")
        expect(page.locator("#skuInputSummary")).to_have_text("已输入 3 个唯一 SKU")
        page.locator("#runBtn").click()
        expect(page.locator("#previewNotice")).to_be_visible()
        expect(page.locator("#progressCount")).to_have_text("3 / 3")
        expect(page.locator("#resultTableBody tr")).to_have_count(3)
        expect(page.locator("#resultTableBody tr").nth(0).locator("td").nth(5)).to_have_text("在售")
        expect(page.locator("#resultTableBody tr").nth(1).locator("td").nth(5)).to_have_text("下架")
        expect(page.locator("#resultSummary")).to_contain_text("在售 1，下架 1")
        expect(page.locator("#resultSummary")).to_contain_text("菲律宾（PH）")
        expect(page.locator("#openFileBtn")).to_be_disabled()
        expect(page.locator("#taskBadge")).to_have_text("演示完成")
        self.assertEqual(self.errors, [])

    def test_real_bridge_payload_partial_failure_safe_links_and_refill(self):
        page = self.page
        page.evaluate("""() => {
          const rows = [
            { sku: '001', shop_name: '<img src=x onerror=alert(1)>', product_url: 'https://shopee.ph/product/1/2', item_id: '00002', listed_time: '2020-01-01 00:00:00', status: 'matched', message: '' },
            { sku: '002', shop_name: '', product_url: 'javascript:alert(1)', status: 'failed', message: '<script>bad</script>' },
            { sku: '003', shop_name: '', product_url: '', status: 'not_found', message: '未找到' },
          ];
          window.calls = [];
          window.task = { ok: true, id: 'real-contract', tool: 'bigseller_link_query', status: 'failed', logs: [], context: { sku_count: 3 }, result: {
            is_complete: false, completion_message: '结果不完整：1 个 SKU 查询失败', sku_count: 3, matched_count: 1, not_found_count: 1, failed_count: 1, confirmed_count: 2,
            site: 'VN', site_name: '越南', failed_skus: ['002', 'FAILED-BEYOND-PREVIEW'], rows, output_file: 'C:/Exports/result.xlsx', output_dir: 'C:/Exports',
          }};
          window.pywebview.api.start_bigseller_link_query = async (payload) => { window.calls.push(payload); return window.task; };
          window.pywebview.api.get_task_status = async () => window.task;
          window.pywebview.api.open_path = async (path) => { window.opened = path; return { ok: true }; };
        }""")
        page.locator("#skuText").fill("001\n002\n003\n001")
        page.locator("#site").select_option("VN")
        page.locator("#runBtn").click()
        expect(page.locator("#taskBadge")).to_have_text("结果不完整")
        expect(page.locator("#progressCount")).to_have_text("2 / 3")
        expect(page.locator("#queryProgress")).to_have_attribute("aria-valuenow", "66")
        self.assertEqual(page.evaluate("window.calls[0].sku_text"), "001\n002\n003")
        self.assertEqual(set(page.evaluate("window.calls[0]")), {"sku_text", "output_dir", "site"})
        self.assertEqual(page.evaluate("window.calls[0].site"), "VN")
        expect(page.locator("#resultSummary")).to_contain_text("越南（VN）")
        expect(page.locator("#resultTableBody a")).to_have_count(1)
        expect(page.locator("#resultTableBody a")).to_have_attribute("href", "https://shopee.ph/product/1/2")
        self.assertEqual(page.locator("#resultTableBody img, #resultTableBody script").count(), 0)
        page.locator("#openFileBtn").click()
        self.assertEqual(page.evaluate("window.opened"), "C:/Exports/result.xlsx")
        page.locator("#site").select_option("PH")
        expect(page.locator("#resultSummary")).to_contain_text("越南（VN）")
        page.locator("#retryFailedBtn").click()
        expect(page.locator("#site")).to_have_value("VN")
        expect(page.locator("#skuText")).to_have_value("002\nFAILED-BEYOND-PREVIEW")
        # Reject a subsequent start: the prior file and failure list remain usable.
        page.evaluate("window.pywebview.api.start_bigseller_link_query = async () => ({ok: false, error: '账号不可用'})")
        page.locator("#runBtn").click()
        expect(page.locator("#progressText")).to_contain_text("已保留上次结果")
        expect(page.locator("#taskBadge")).to_have_text("启动失败")
        expect(page.locator("#openFileBtn")).to_be_enabled()
        expect(page.locator("#resultTableBody tr")).to_have_count(3)
        self.assertEqual(self.errors, [])

    def test_large_batch_paginates_and_shows_limited_preview(self):
        page = self.page
        page.locator("#skuText").fill("\n".join(f"sku-{i}" for i in range(15001)))
        expect(page.locator("#skuInputSummary")).to_have_text("已输入 15001 个唯一 SKU")
        page.locator("#runBtn").click()
        expect(page.locator("#previewNotice")).to_be_visible()
        expect(page.locator("#progressCount")).to_have_text("15001 / 15001")
        expect(page.locator("#resultTableBody tr")).to_have_count(50)
        expect(page.locator("#resultPageText")).to_have_text("第 1 / 10 页")
        expect(page.locator("#resultLimitNote")).to_contain_text("预览前 500 条")
        self.assertNotIn("Excel 包含", page.locator("#resultLimitNote").inner_text())
        page.locator("#resultNextBtn").click()
        expect(page.locator("#resultPageText")).to_have_text("第 2 / 10 页")
        expect(page.locator("#resultTableBody tr").first.locator("td").first).to_have_text("sku-50")
        self.assertEqual(self.errors, [])

    def test_over_50000_unique_skus_is_rejected_before_starting_task(self):
        page = self.page
        page.evaluate("window.largeBatchCalls = 0; window.pywebview.api.start_bigseller_link_query = async () => { window.largeBatchCalls++; return {ok: false}; };")
        page.locator("#skuText").fill("\n".join(f"sku-{i}" for i in range(50001)))
        expect(page.locator("#skuInputSummary")).to_contain_text("超过 50000 个上限")
        page.locator("#runBtn").click()
        self.assertEqual(page.evaluate("window.largeBatchCalls"), 0)
        expect(page.locator("#taskBadge")).to_have_text("待运行")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

"""Exercise SKU mode selection and report rendering in an offline browser."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]


class SkuCoverageFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is required")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        else:
            raise unittest.SkipTest("Chromium is required")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        html = (ROOT / "frontend/pages/sku-inventory-query.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>|<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(ROOT / "frontend/assets/app.css"))
        self.page.evaluate('''() => {
          window.submitted = [];
          window.HQYL = {
            $: (id) => document.getElementById(id),
            activeAccount: () => ({id: "account-1"}),
            showToast: () => {}, appendLog: () => {}, openOutput: () => {},
            startTask: async (fn) => { await fn(); },
            api: () => ({
              get_sku_inventory_developers: async () => ({ok:true, developers:[{id:"101",name:"张三-开发"},{id:"102",name:"李四-开发"}]}),
              start_sku_inventory_query: async (payload) => { window.submitted.push(payload); return {ok:true}; },
            }),
            boot: async (page) => { window.skuPage = page; await page.init({settings:{output_dir:"C:/test"},date_range:{start_date:"2026-09-01",end_date:"2026-09-20"}}); },
          };
        }''')
        self.page.add_script_tag(path=str(ROOT / "frontend/assets/pages/sku-inventory-query.js"))

    def test_select_developer_and_styles_and_switch_back_to_inventory(self):
        expect(self.page.locator("#skuDateFields")).to_be_hidden()
        self.page.locator("#loadDevelopersBtn").click()
        self.page.locator("#developerSearch").fill("张三")
        self.page.locator('#developerChecklist input[value="101"]').check()
        self.page.locator("#skuLivenessTypes").select_option("2")
        self.page.locator("#skuInventoryRunBtn").click()
        payload = self.page.evaluate("window.submitted[0]")
        self.assertEqual(payload["query_mode"], "missing_warehouses")
        self.assertEqual(payload["liveness_types"], ["2"])
        self.assertEqual(payload["developer_ids"], ["101"])
        self.page.locator("#skuQueryMode").select_option("inventory")
        expect(self.page.locator("#skuDateFields")).to_be_visible()
        expect(self.page.locator("#livenessField")).to_be_hidden()
        self.page.locator("#skuInventoryRunBtn").click()
        payload = self.page.evaluate("window.submitted[1]")
        self.assertEqual(payload["query_mode"], "inventory")
        self.assertEqual(payload["start_date"], "2026-09-01")
        self.assertEqual(self.errors, [])

    def test_missing_warehouse_results_escape_text_paginate_and_keep_their_mode(self):
        self.page.evaluate('''() => skuPage.applyResult({query_mode:"missing_warehouses", sku_count:51, record_count:51, developer_count:1, warehouse_count:2, missing_sku_count:50,
          scope_warehouses:[{name:"泰国仓"},{name:"菲律宾仓"}], unclassified_warehouses:[{name:"待核对仓"}],
          rows:Array.from({length:51}, (_,i) => ({developer_name:"张三-开发",sku:`SKU-${i}`,product_name:'<img src=x onerror="window.injected=true">',liveness_name:"爆款",missing_warehouse_count:i===50?0:1,covered_warehouse_count:i===50?2:1,missing_warehouse_names:i===50?"":"菲律宾仓"}))
        })''')
        expect(self.page.locator("#coverageTableHead")).to_be_visible()
        expect(self.page.locator("#inventoryTableHead")).to_be_hidden()
        expect(self.page.locator("#skuInventoryTableBody tr")).to_have_count(50)
        expect(self.page.locator("#skuInventoryTableBody img")).to_have_count(0)
        expect(self.page.locator("#warehouseScopeWarning")).to_contain_text("待核对仓")
        self.page.locator("#skuQueryMode").select_option("inventory")
        self.page.locator("#skuInventoryNextBtn").click()
        expect(self.page.locator("#coverageTableHead")).to_be_visible()
        expect(self.page.locator("#skuInventoryTableBody tr")).to_have_count(1)
        expect(self.page.locator("#skuInventoryTableBody")).to_contain_text("已覆盖全部目标仓库")
        self.assertEqual(self.errors, [])

    def test_empty_coverage_and_previous_inventory_result(self):
        self.page.evaluate('''() => skuPage.applyResult({query_mode:"missing_warehouses", rows:[]})''')
        expect(self.page.locator("#skuInventoryEmpty")).to_contain_text("没有符合条件的爆款／旺款")
        self.page.evaluate('''() => skuPage.applyResult({rows:[{developer_name:"张三-开发",sku:"OLD",warehouse_name:"广州仓",transit_inventory:100}]})''')
        expect(self.page.locator("#inventoryTableHead")).to_be_visible()
        expect(self.page.locator("#coverageTableHead")).to_be_hidden()
        expect(self.page.locator("#warehouseScopeWarning")).to_be_hidden()
        expect(self.page.locator("#skuInventoryTableBody")).to_contain_text("100")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PAGE_SCRIPTS = {
    FRONTEND / "pages" / "temu-on-sale-export.html": FRONTEND / "assets" / "pages" / "temu-on-sale-export.js",
    FRONTEND / "pages" / "bigseller-sku-benchmark.html": FRONTEND / "assets" / "pages" / "bigseller-sku-benchmark.js",
    FRONTEND / "pages" / "bigseller-claim-query.html": FRONTEND / "assets" / "pages" / "bigseller-claim-query.js",
    FRONTEND / "pages" / "dashboard.html": FRONTEND / "assets" / "pages" / "dashboard.js",
    FRONTEND / "pages" / "mabang-arrival-query.html": FRONTEND / "assets" / "pages" / "mabang-arrival-query.js",
    FRONTEND / "pages" / "group-sales.html": FRONTEND / "assets" / "pages" / "group-sales.js",
    FRONTEND / "pages" / "mabang-income-expense-report.html": FRONTEND / "assets" / "pages" / "mabang-income-expense-report.js",
    FRONTEND / "pages" / "temu-shipping-channel.html": FRONTEND / "assets" / "pages" / "temu-shipping-channel.js",
    FRONTEND / "pages" / "purchase-log.html": FRONTEND / "assets" / "pages" / "purchase-log.js",
    FRONTEND / "pages" / "sku-inventory-query.html": FRONTEND / "assets" / "pages" / "sku-inventory-query.js",
    FRONTEND / "pages" / "developer-sales-income-summary.html": FRONTEND / "assets" / "pages" / "developer-sales-income-summary.js",
    FRONTEND / "pages" / "mabang-warehouse-permission.html": FRONTEND / "assets" / "pages" / "mabang-warehouse-permission.js",
    FRONTEND / "pages" / "mabang-developer-permission.html": FRONTEND / "assets" / "pages" / "mabang-developer-permission.js",
    FRONTEND / "pages" / "shopee-ads.html": FRONTEND / "assets" / "pages" / "shopee-ads.js",
    FRONTEND / "pages" / "lazada-withdrawal-statistics.html": FRONTEND / "assets" / "pages" / "lazada-withdrawal-statistics.js",
    FRONTEND / "pages" / "lazada-monthly-report.html": FRONTEND / "assets" / "pages" / "lazada-monthly-report.js",
    FRONTEND / "pages" / "lazada-ads-data.html": FRONTEND / "assets" / "pages" / "lazada-ads-data.js",
    FRONTEND / "pages" / "lazada-bill-detail.html": FRONTEND / "assets" / "pages" / "lazada-bill-detail.js",
    FRONTEND / "pages" / "vietnam-income-reconciliation.html": FRONTEND / "assets" / "pages" / "vietnam-income-reconciliation.js",
    FRONTEND / "pages" / "bigseller-sync.html": FRONTEND / "assets" / "pages" / "bigseller-sync.js",
    FRONTEND / "pages" / "bigseller-item-id-query.html": FRONTEND / "assets" / "pages" / "bigseller-item-id-query.js",
    FRONTEND / "pages" / "echotik-collect.html": FRONTEND / "assets" / "pages" / "echotik-collect.js",
    FRONTEND / "pages" / "settings.html": FRONTEND / "assets" / "pages" / "settings.js",
    FRONTEND / "pages" / "kec-reconciliation.html": FRONTEND / "assets" / "pages" / "kec-reconciliation.js",
}


class FrontendIntegrityTests(unittest.TestCase):
    def test_javascript_syntax(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript syntax check")

        for script in sorted((FRONTEND / "assets").rglob("*.js")):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [node, "--check", str(script)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_javascript_dom_ids_exist_in_html(self) -> None:
        for html_path, script_path in PAGE_SCRIPTS.items():
            with self.subTest(page=html_path.name):
                javascript = script_path.read_text(encoding="utf-8")
                html = html_path.read_text(encoding="utf-8")
                referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', javascript))
                html_ids = set(re.findall(r'\bid="([^"]+)"', html))
                self.assertEqual(sorted(referenced_ids - html_ids), [])

    def test_html_attributes_do_not_use_smart_quotes(self) -> None:
        for html_path in sorted(FRONTEND.rglob("*.html")):
            with self.subTest(page=html_path.name):
                html = html_path.read_text(encoding="utf-8")
                malformed_attributes = re.findall(
                    r"\b(?:id|class|type|style|viewBox|aria-[\w-]+)=[“”]",
                    html,
                )
                self.assertEqual(malformed_attributes, [])

    def test_hidden_attribute_is_enforced_globally(self) -> None:
        css = (FRONTEND / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important\s*;")

    def test_each_module_has_its_own_html_and_script(self) -> None:
        for html_path, script_path in PAGE_SCRIPTS.items():
            with self.subTest(page=html_path.name):
                self.assertTrue(html_path.is_file())
                self.assertTrue(script_path.is_file())
                html = html_path.read_text(encoding="utf-8")
                self.assertIn(f"../assets/pages/{script_path.name}", html)

    def test_mabang_warehouse_permission_has_guarded_preview_flow(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "mabang-warehouse-permission.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "mabang-warehouse-permission.html").read_text(encoding="utf-8")

        self.assertIn("start_mabang_warehouse_permission_preview", javascript)
        self.assertIn("start_mabang_warehouse_permission_batch", javascript)
        self.assertIn('id="previewBtn"', html)
        self.assertIn('id="applyBtn"', html)
        self.assertIn("invalidatePreview(true)", javascript)
        self.assertIn("missing_product_warehouse_ids", javascript)
        self.assertIn("missing_order_warehouse_ids", javascript)
        self.assertIn("missing_view_warehouse_ids", javascript)
        self.assertIn("product_added_count", javascript)
        self.assertIn("order_added_count", javascript)
        self.assertIn("warehouse_added_count", javascript)
        self.assertIn("permission_types", javascript)
        self.assertIn('id="permissionProduct"', html)
        self.assertIn('id="permissionOrder"', html)
        self.assertIn('id="permissionWarehouse"', html)
        self.assertEqual(html.count('name="permissionType" type="checkbox"'), 3)
        self.assertEqual(html.count('name="permissionType" type="checkbox" value=') , 3)
        self.assertIn("<th>查看商品</th><th>查看订单</th><th>查看仓库</th>", html)

    def test_dashboard_contains_mabang_warehouse_permission_entry(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("mabang_warehouse_permission", javascript)
        self.assertIn('href: "mabang-warehouse-permission.html"', javascript)

    def test_mabang_arrival_query_uses_request_export_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "mabang-arrival-query.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "mabang-arrival-query.html").read_text(encoding="utf-8")
        common_javascript = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")
        preview_match = re.search(
            r"async start_mabang_arrival_query\(\).*?(?=\n\s+async |\n\s+\};)",
            common_javascript,
            re.DOTALL,
        )

        self.assertIn("start_mabang_arrival_query", javascript)
        self.assertIn("remarks", javascript)
        self.assertIn('key: "mabang_arrival_query"', javascript)
        self.assertIn("HTTP 请求", html)
        self.assertIn("状态：全部", html)
        self.assertIn("搜索内容：备注", html)
        self.assertIn("合并共有项：关闭", html)
        self.assertIn("文件：每条备注独立导出", html)

        self.assertIn('id="arrivalOutputDir"', html)
        self.assertIn('id="arrivalChooseDirBtn"', html)
        self.assertIn("文件保存到所选目录\\到货查询", html)
        self.assertIn("choose_output_dir", javascript)
        self.assertIn("output_dir: selectedOutputRoot", javascript)

        self.assertIn("Array.isArray(result.output_files)", javascript)
        self.assertIn("Array.isArray(result.exports)", javascript)
        self.assertIn("result.output_root", javascript)
        self.assertIn("task?.context?.output_root", javascript)
        self.assertIn("outputFiles.length", javascript)
        self.assertIn('id="arrivalOutputSummary"', html)

        self.assertIsNotNone(preview_match)
        preview_contract = preview_match.group(0) if preview_match else ""
        self.assertIn("output_files:", preview_contract)
        self.assertIn("output_root:", preview_contract)
        self.assertIn("exports:", preview_contract)
        self.assertIn(".xls", preview_contract)

    def test_dashboard_contains_mabang_arrival_query_entry(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("mabang_arrival_query", javascript)
        self.assertIn('href: "mabang-arrival-query.html"', javascript)

    def test_dashboard_contains_sku_inventory_query_entry(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("sku_inventory_query", javascript)
        self.assertIn('href: "sku-inventory-query.html"', javascript)

    def test_dashboard_contains_mabang_income_expense_report_entry(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("mabang_income_expense_report", javascript)
        self.assertIn('href: "mabang-income-expense-report.html"', javascript)

    def test_lazada_monthly_report_frontend_and_bridge_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "lazada-monthly-report.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "lazada-monthly-report.html").read_text(encoding="utf-8")
        dashboard = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        common_javascript = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")
        bridge = (ROOT / "backend" / "app_bridge.py").read_text(encoding="utf-8")

        self.assertIn('key: "lazada_monthly_report"', javascript)
        self.assertIn('taskKey: "lazada_monthly_report"', javascript)
        self.assertIn("get_lazada_monthly_report_info", javascript)
        self.assertIn("start_lazada_monthly_report", javascript)
        self.assertIn("country:", javascript)
        self.assertIn("month:", javascript)
        self.assertIn('id="lazadaMonthlyMonth" type="month"', html)
        self.assertIn('href: "lazada-monthly-report.html"', dashboard)
        self.assertIn('id: "lazada_monthly_report", href: "lazada-monthly-report.html"', common_javascript)
        self.assertIn('"key": "lazada_monthly_report"', bridge)
        self.assertIn("def get_lazada_monthly_report_info", bridge)
        self.assertIn("def start_lazada_monthly_report", bridge)
        self.assertIn('tool="lazada_monthly_report"', bridge)
        self.assertIn("task.result.success === false", common_javascript)
        self.assertNotIn("lazadaMonthlyConcurrent", html)
        self.assertNotIn("lazadaMonthlyDriverPath", html)

    def test_mabang_income_expense_report_uses_shipping_time_category_and_warehouse_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "mabang-income-expense-report.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "mabang-income-expense-report.html").read_text(encoding="utf-8")

        self.assertIn("start_mabang_income_expense_report", javascript)
        self.assertIn("category_ids", javascript)
        self.assertIn("warehouse_keys", javascript)
        self.assertIn("incomeExpenseWarehouseSelect", javascript)
        self.assertIn("incomeExpenseStartDate", javascript)
        self.assertIn("incomeExpenseEndDate", javascript)
        self.assertIn("选择雅仓三项", html)
        self.assertIn("全选海外仓", html)
        self.assertIn("文件直接保存到平台输出目录", html)
        self.assertNotIn("钉钉", html)

    def test_sku_inventory_query_uses_developer_and_warehouse_detail_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "sku-inventory-query.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "sku-inventory-query.html").read_text(encoding="utf-8")

        self.assertIn("get_sku_inventory_developers", javascript)
        self.assertIn("start_sku_inventory_query", javascript)
        self.assertIn("developer_ids", javascript)
        self.assertIn("SKU＋仓库", html)
        self.assertIn("可用库存量 &gt; 0", html)
        self.assertIn("未发货数 &gt; 0", html)
        self.assertIn("在途量 &gt; 0", html)
        self.assertIn("当前可售天数", html)
        self.assertIn("未发货数", html)
        self.assertIn("row.unshipped_count", javascript)
        self.assertIn("<th>在途量</th>", html)
        self.assertIn("row.transit_inventory", javascript)

    def test_developer_sales_income_summary_uses_payment_usd_and_two_sheet_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "developer-sales-income-summary.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "developer-sales-income-summary.html").read_text(encoding="utf-8")

        self.assertIn("start_developer_sales_income_summary", javascript)
        self.assertIn("developer_names", javascript)
        self.assertIn("include_all", javascript)
        self.assertIn('id="includeAllPeople"', html)
        self.assertIn("付款开始日期", html)
        self.assertIn("付款结束日期", html)
        self.assertIn("收入-订单金额", html)
        self.assertIn("开发员、销售员两个子表", html)
        self.assertIn("采购流程", html)

    def test_all_task_log_panels_are_fixed_and_copyable(self) -> None:
        css = (FRONTEND / "assets" / "app.css").read_text(encoding="utf-8")
        common_javascript = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")

        for html_path in PAGE_SCRIPTS:
            if html_path.name in {"dashboard.html", "settings.html"}:
                continue
            with self.subTest(page=html_path.name):
                html = html_path.read_text(encoding="utf-8")
                self.assertIn('id="copyLogBtn"', html)
                self.assertIn('class="surface log-surface"', html)
                self.assertIn("20260623-log-panel", html)

        self.assertRegex(css, r"\.log-surface\s*\{[^}]*height:\s*clamp\(")
        self.assertRegex(css, r"\.log-box\s*\{[^}]*overflow-y:\s*scroll\s*;")
        self.assertIn('copyButton.addEventListener("click", copyLogs)', common_javascript)
        self.assertIn("navigator.clipboard?.writeText", common_javascript)
        self.assertIn("isLogPinnedToBottom", common_javascript)
        self.assertIn("pinnedToBottom ? box.scrollHeight : previousScrollTop", common_javascript)

    def test_echotik_category_filter_renders_secondary_categories(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "echotik-collect.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "echotik-collect.html").read_text(encoding="utf-8")

        self.assertIn("categoryChildren(category)", javascript)
        self.assertIn('group.className = "category-tree-group"', javascript)
        self.assertIn('className: "category-option-secondary"', javascript)
        self.assertIn("parentOption.input.checked = false", javascript)
        self.assertIn("二级类目", html)

    def test_echotik_keyword_is_optional_with_product_limit_guard(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "echotik-collect.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "echotik-collect.html").read_text(encoding="utf-8")

        self.assertIn("搜索关键词（可选）", html)
        self.assertIn('id="maxProductsInput"', html)
        self.assertIn("全部商品模式", javascript)
        self.assertIn("categoryPathIds", javascript)
        self.assertIn("category_paths: products.paths", javascript)
        self.assertIn("max_products: productLimit", javascript)
        self.assertIn("!hasProductFilters(filters)", javascript)
        self.assertNotIn('HQYL.showToast("请输入搜索关键词")', javascript)

    def test_bigseller_item_id_query_frontend_behavior(self) -> None:
        for relative_path in (
            "pages/bigseller-item-id-query.html",
            "assets/pages/bigseller-item-id-query.js",
            "assets/pages/dashboard.js",
        ):
            source = (FRONTEND / relative_path).read_text(encoding="utf-8")
            self.assertIn("SKU（含子SKU）", source)
            self.assertNotIn("Parent SKU", source)

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend behavior check")

        # Exercise the page through its actual event handlers and HQYL lifecycle,
        # without a BigSeller account or a real desktop bridge.
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[1];
const html = fs.readFileSync(path.join(root, 'pages/bigseller-item-id-query.html'), 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, style: {}, attrs: {}, handlers: {},
  addEventListener(name, handler) { this.handlers[name] = handler; },
  setAttribute(name, value) { this.attrs[name] = value; }, focus() {},
}]));
const calls = [], notices = [], opened = [], accountPrompts = [], logs = [];
let page, account = { id: 'test-bigseller' };
const bridge = {
  async start_bigseller_item_id_query(payload) { calls.push(JSON.parse(JSON.stringify(payload))); return { ok: true, id: 'test-task' }; },
  async choose_output_dir() { return { ok: true, path: 'C:/Chosen' }; },
  async open_path(file) { opened.push(file); return { ok: true }; },
};
const HQYL = {
  $: (id) => nodes[id], api: () => bridge, boot: (value) => { page = value; },
  activeAccount: () => account, showToast: (message) => notices.push(message), appendLog: (message) => logs.push(message),
  openAccountDialog: (vendor) => accountPrompts.push(vendor), openOutput: (value) => opened.push(value),
  async startTask(factory) { page.resetResult(); page.setRunning(true); try { return await factory(); } finally { page.setRunning(false); } },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/pages/bigseller-item-id-query.js'), 'utf8'), { HQYL });
(async () => {
  await page.init({ settings: { output_dir: 'C:/Exports' } });
  const submit = () => nodes.itemIdQueryForm.handlers.submit({ preventDefault() {} });
  nodes.skuText.value = ' , ;\n\t；';
  await submit();
  assert.equal(calls.length, 0);
  assert.match(notices.at(-1), /至少输入/);
  nodes.skuText.value = '  A B  ,C\tA B；D\r\nc;  E，C\n';
  nodes.skuText.handlers.input();
  assert.equal(nodes.skuCount.textContent, '5');
  account = null;
  await submit();
  assert.deepEqual(accountPrompts, ['bigseller']);
  assert.equal(calls.length, 0);
  account = { id: 'test-bigseller' };
  await submit();
  assert.deepEqual(calls[0], { sku_text: 'A B\nC\nD\nc\nE', output_dir: 'C:/Exports' });
  assert.match(logs.at(-1), /SKU（含子SKU） \/ Fuzzy Search \/ Views 降序/);
  assert.equal(nodes.skuText.disabled, false);
  nodes.skuText.value = Array.from({ length: 5001 }, (_, index) => `SKU-${index}`).join('\n');
  await submit();
  assert.equal(calls.length, 1);
  assert.match(notices.at(-1), /5000/);
  page.setRunning(true);
  for (const id of ['runBtn', 'skuText', 'clearSkuBtn', 'outputDir', 'chooseDirBtn']) assert.equal(nodes[id].disabled, true);
  await submit();
  assert.equal(calls.length, 1);
  page.setRunning(false);
  await nodes.chooseDirBtn.handlers.click();
  assert.equal(nodes.outputDir.value, 'C:/Chosen');

  nodes.skuText.value = 'A\nB\nC';
  page.resetResult();
  page.onTaskUpdate({ status: 'running', context: { sku_count: 3 }, logs: ['正在查询 [3/3]'] });
  assert.equal(nodes.progressCount.textContent, '0 / 3');
  page.onTaskUpdate({ status: 'running', logs: ['[BS进度 1/3]', '[BS进度 2/3]'] });
  assert.equal(nodes.progressCount.textContent, '2 / 3');
  assert.equal(nodes.queryProgress.attrs['aria-valuenow'], '67');
  page.onTaskUpdate({ status: 'failed', logs: [] });
  assert.match(nodes.progressText.textContent, /任务失败/);

  const rows = Array.from({ length: 501 }, (_, index) => ({ sku: `SKU-${index}`, item_id: '99999999999999999999', status: 'matched', message: '' }));
  rows[0] = { sku: '<svg onload="bad()">', item_id: '000123', status: 'failed', message: '<script>bad()</script>' };
  page.applyResult({ sku_count: 501, matched_count: 500, not_found_count: 0, failed_count: 1, rows, output_file: 'C:/Exports/result.xlsx', output_dir: 'C:/Exports' });
  assert.equal((nodes.resultTableBody.innerHTML.match(/<tr>/g) || []).length, 50);
  assert.ok(!nodes.resultTableBody.innerHTML.includes('<svg'));
  assert.ok(!nodes.resultTableBody.innerHTML.includes('<script>'));
  assert.ok(nodes.resultTableBody.innerHTML.includes('&lt;svg'));
  assert.ok(nodes.resultTableBody.innerHTML.includes('000123'));
  assert.ok(nodes.resultTableBody.innerHTML.includes('99999999999999999999'));
  assert.equal(nodes.taskBadge.textContent, '部分失败');
  assert.equal(nodes.failureNotice.hidden, false);
  assert.match(nodes.resultLimitNote.textContent, /500/);
  assert.equal(nodes.progressCount.textContent, '501 / 501');
  assert.equal(nodes.openFileBtn.disabled, false);
  await nodes.openFileBtn.handlers.click();
  assert.equal(opened.at(-1), 'C:/Exports/result.xlsx');
  for (let index = 0; index < 15; index++) nodes.resultNextBtn.handlers.click();
  assert.equal(nodes.resultPageText.textContent, '第 10 / 10 页');
  assert.ok(nodes.resultTableBody.innerHTML.includes('SKU-499'));
  assert.ok(!nodes.resultTableBody.innerHTML.includes('SKU-500'));
  assert.equal(nodes.resultNextBtn.disabled, true);
  nodes.resultPrevBtn.handlers.click();
  assert.equal(nodes.resultPageText.textContent, '第 9 / 10 页');
  page.applyResult({ sku_count: 1, failed_count: 1, rows: [rows[0]] });
  assert.equal(nodes.taskBadge.textContent, '查询失败');
  page.applyResult({ is_preview: true, sku_count: 1, matched_count: 1, rows: [{ sku: 'DEMO-SKU', item_id: 'DEMO-ITEM-0001', status: 'matched' }] });
  assert.equal(nodes.previewNotice.hidden, false);
  assert.equal(nodes.openFileBtn.disabled, true);
  assert.equal(nodes.openOutputBtn.disabled, true);
  assert.match(nodes.outputFile.textContent, /演示/);
  page.resetResult();
  assert.equal(nodes.queryResult.hidden, true);
  assert.equal(nodes.matchedCount.textContent, '0');
  nodes.clearSkuBtn.handlers.click();
  assert.equal(nodes.skuCount.textContent, '0');
  console.log('BigSeller item ID frontend behavior checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script, str(FRONTEND)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class KecReconciliationFrontendTests(unittest.TestCase):
    def test_page_uses_actual_volume_basis_and_bridge_contract(self) -> None:
        javascript = (FRONTEND / "assets" / "pages" / "kec-reconciliation.js").read_text(encoding="utf-8")
        html = (FRONTEND / "pages" / "kec-reconciliation.html").read_text(encoding="utf-8")

        self.assertIn('key: "kec_reconciliation"', javascript)
        self.assertIn('taskKeys: ["kec_reconciliation"]', javascript)
        self.assertIn("get_kec_reconciliation_info", javascript)
        self.assertIn("inspect_kec_reconciliation_source", javascript)
        self.assertIn("start_kec_reconciliation", javascript)
        self.assertIn("choose_kec_reconciliation_file", javascript)
        self.assertIn("kind: \"quote\"", javascript)
        # The migrated rule must stay visible on the page and drive the labels.
        self.assertIn("实际体积", html)
        self.assertIn("0.000001", html)
        self.assertIn("仓储费_体积差额合计", javascript)
        self.assertIn("storage_expected_amount", javascript)
        self.assertIn("账单 Total CBM", html)

    def test_page_behavior_without_a_desktop_bridge(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend behavior check")

        # Drive the page through its real handlers and HQYL lifecycle, with a stub
        # bridge: pick a bill, bind an account, run, then render and reset results.
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[1];
const html = fs.readFileSync(path.join(root, 'pages/kec-reconciliation.html'), 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, style: {}, attrs: {}, handlers: {}, children: [],
  addEventListener(name, handler) { this.handlers[name] = handler; },
  setAttribute(name, value) { this.attrs[name] = value; },
  appendChild(child) { this.children.push(child); },
}]));
const calls = [], notices = [], opened = [], accountPrompts = [], logs = [];
let page, account = { id: 'test-mabang' };
const bridge = {
  async get_kec_reconciliation_info() { return { ok: true, settings: {}, account_state: {}, basis: '实际体积（长 × 宽 × 高 × 数量 × 0.000001）' }; },
  async choose_kec_reconciliation_file(payload) {
    if (payload.kind === 'quote') return { ok: true, kind: 'quote', path: 'C:/报价表.xlsx', name: '报价表.xlsx' };
    return { ok: true, kind: 'input', path: 'C:/账单.xlsx', name: '账单.xlsx', input_file: 'C:/账单.xlsx', sheet_titles: { storage: 'Inventory CBM-仓储费' }, warnings: ['缺少 Total CBM 列'] };
  },
  async inspect_kec_reconciliation_source(payload) { return { ok: true, input_file: payload.input_file, sheet_titles: { storage: '仓储费' }, warnings: [] }; },
  async choose_output_dir() { return { ok: true, path: 'C:/Chosen' }; },
  async start_kec_reconciliation(payload) { calls.push(JSON.parse(JSON.stringify(payload))); return { ok: true, id: 't1' }; },
  async open_path(file) { opened.push(file); return { ok: true }; },
};
const HQYL = {
  $: (id) => nodes[id], api: () => bridge, boot: (value) => { page = value; },
  activeAccount: () => account, showToast: (message) => notices.push(message), appendLog: (message) => logs.push(message),
  openAccountDialog: (vendor) => accountPrompts.push(vendor), openOutput: (value) => opened.push(value),
  async startTask(factory) { page.resetResult(); page.setRunning(true); try { return await factory(); } finally { page.setRunning(false); } },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/pages/kec-reconciliation.js'), 'utf8'), {
  HQYL,
  document: { createElement(tag) { return { tagName: tag, textContent: '', style: {}, children: [], appendChild(child) { this.children.push(child); } }; } },
});
(async () => {
  await page.init({ settings: { output_dir: 'C:/Exports' } });
  assert.equal(nodes.kecOutputDir.value, 'C:/Exports');
  assert.match(nodes.kecBasisRule.textContent, /实际体积/);
  assert.equal(page.taskKeys[0], 'kec_reconciliation');

  const submit = () => nodes.kecReconciliationForm.handlers.submit({ preventDefault() {} });
  await submit();
  assert.equal(calls.length, 0);
  assert.match(notices.at(-1), /KEC 账单/);

  await nodes.kecChooseBillBtn.handlers.click();
  assert.equal(nodes.kecBillFile.value, 'C:/账单.xlsx');
  assert.match(nodes.kecBillWarning.textContent, /Total CBM/);
  await nodes.kecChooseQuoteBtn.handlers.click();
  assert.match(nodes.kecQuoteFile.value, /报价表/);

  account = null;
  await submit();
  assert.deepEqual(accountPrompts, ['mabang']);
  assert.equal(calls.length, 0);
  account = { id: 'test-mabang' };

  await submit();
  assert.deepEqual(calls[0], { account_id: 'test-mabang', input_file: 'C:/账单.xlsx', quote_file: 'C:/报价表.xlsx', output_dir: 'C:/Exports' });
  assert.match(logs[0], /实际体积/);

  page.setRunning(true);
  for (const id of ['kecRunBtn', 'kecBillFile', 'kecQuoteFile', 'kecOutputDir', 'kecChooseBillBtn', 'kecChooseQuoteBtn', 'kecChooseDirBtn']) assert.equal(nodes[id].disabled, true);
  page.setRunning(false);

  await nodes.kecChooseDirBtn.handlers.click();
  assert.equal(nodes.kecOutputDir.value, 'C:/Chosen');

  page.applyResult({
    success: true,
    output_file: 'C:/Exports/账单_核对结果.xlsx',
    avg_daily_orders: 140, outbound_base_fee: 0.75,
    storage_billed_amount: 21603.09, storage_expected_amount: 19694.02, storage_difference: 1909.07,
    summary: { '仓储费_账单金额': 3, '仓储费_核对金额': 2.4, '仓储费_异常数': 1, '仓储费_体积差额合计': 0.31,
               '卸货费_账单金额': 10, '卸货费_核对金额': 10, '卸货费_异常数': 0,
               '杂费_账单金额': 5, '杂费_核对金额': 4, '杂费_待确认数': 2, '出库订单数': 4210 },
  });
  assert.equal(nodes.storageBilledAmount.textContent, '21603.09');
  assert.equal(nodes.storageExpectedAmount.textContent, '19694.02');
  assert.equal(nodes.storageVolumeDiff.textContent, '0.310000');
  assert.equal(nodes.outputFile.textContent, '账单_核对结果.xlsx');
  assert.equal(nodes.kecResult.hidden, false);
  assert.equal(nodes.kecSummaryTableBody.children.length, 5);
  assert.equal(nodes.kecSummaryTableBody.children[0].children[3].textContent, '0.60');
  assert.equal(nodes.kecOpenFileBtn.disabled, false);
  assert.match(nodes.kecAuxSummary.textContent, /出库订单数/);
  assert.match(nodes.kecResultSummary.textContent, /140\.00/);

  await nodes.kecOpenFileBtn.handlers.click();
  assert.equal(opened.at(-1), 'C:/Exports/账单_核对结果.xlsx');

  page.resetResult();
  assert.equal(nodes.kecResult.hidden, true);
  assert.equal(nodes.outputFile.textContent, '未生成');
  assert.equal(nodes.kecOpenFileBtn.disabled, true);
  console.log('KEC reconciliation frontend behavior checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script, str(FRONTEND)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LazadaMonthlyReportBridgeContractTests(unittest.TestCase):
    def test_info_returns_month_countries_and_runtime_paths(self) -> None:
        from backend.app_bridge import AppBridge

        bridge = AppBridge()
        with patch(
            "backend.app_bridge.lazada_monthly_report_country_options",
            return_value=[{"code": "TH", "name": "泰国"}],
        ), patch(
            "backend.app_bridge.lazada_monthly_report_default_month",
            return_value="2026-07",
        ), patch(
            "backend.app_bridge.resolve_lazada_monthly_report_runtime_paths",
            return_value={"client_path": "C:/Ziniao.exe", "webdriver_path": "C:/driver"},
        ):
            result = bridge.get_lazada_monthly_report_info()

        self.assertEqual(result, {
            "ok": True,
            "countries": [{"code": "TH", "name": "泰国"}],
            "default_month": "2026-07",
            "client_path": "C:/Ziniao.exe",
            "webdriver_path": "C:/driver",
        })

    def test_start_injects_bound_account_and_registers_scoped_task(self) -> None:
        from backend.app_bridge import AppBridge
        from backend.config_store import AppSettings, BoundAccount

        account = BoundAccount(
            id="ziniao-account",
            vendor="ziniao",
            name="跨境团队",
            username="operator",
            password="secret",
            extra={"company": "寰球云联"},
        )
        settings = AppSettings(
            output_dir="C:/Exports",
            accounts=[account],
            active_account_ids={"ziniao": account.id},
        )
        bridge = AppBridge()
        bridge.config_store = SimpleNamespace(load=lambda: settings)
        started: dict[str, object] = {}

        class RecordingTasks:
            def start(self, name, runner, **kwargs):
                started.update(name=name, runner=runner, **kwargs)
                return {"ok": True, "id": "monthly-task"}

        bridge.tasks = RecordingTasks()
        captured_payload: dict[str, object] = {}
        job = SimpleNamespace(
            countries=("MY",),
            month="2026-07",
            store_names=("店铺 A", "店铺 B"),
            output_root="C:/Chosen",
        )

        def validate(payload, *, default_output_root):
            captured_payload.update(payload)
            self.assertEqual(default_output_root, "C:/Exports")
            return job

        with patch(
            "backend.app_bridge.validate_lazada_monthly_report_payload",
            side_effect=validate,
        ), patch(
            "backend.app_bridge.lazada_monthly_report_country_options",
            return_value=[
                {"code": "TH", "name": "泰国"},
                {"code": "MY", "name": "马来西亚"},
                {"code": "PH", "name": "菲律宾"},
            ],
        ):
            result = bridge.start_lazada_monthly_report({
                "account_id": account.id,
                "country": "MY",
                "month": "2026-07",
                "store_names": "店铺 A\n店铺 B",
                "output_dir": "C:/Chosen",
                "password": "",
            })

        self.assertEqual(result, {"ok": True, "id": "monthly-task"})
        self.assertEqual(captured_payload["username"], "operator")
        self.assertEqual(captured_payload["password"], "secret")
        self.assertEqual(captured_payload["company"], "寰球云联")
        self.assertEqual(captured_payload["output_root"], "C:/Chosen")
        self.assertEqual(started["tool"], "lazada_monthly_report")
        self.assertEqual(started["context"], {
            "account_id": account.id,
            "countries": ["MY"],
            "month": "2026-07",
            "store_count": 2,
            "output_root": "C:/Chosen",
        })


if __name__ == "__main__":
    unittest.main()

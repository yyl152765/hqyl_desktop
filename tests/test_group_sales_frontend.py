"""Category selection checks in a real browser, with an offline desktop bridge."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright, expect
except ImportError:
    sync_playwright = None


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
GROUPS = [
    {"id": "1057656", "name": "菲S-雷莹莹团队"},
    {"id": "1061048", "name": "泰国S-郭乐组"},
    {"id": "1042622", "name": "tk菲律宾"},
    {"id": "1038956", "name": "lazada马来"},
    {"id": "new", "name": '<新增分类 & "分组">'},
]


class GroupSalesFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is required for category browser checks")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        else:
            raise unittest.SkipTest("Chromium/Edge is required for category browser checks")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def open_page(self, saved_ids=None, groups=None, output_dir="C:/Exports", latest_task=None) -> None:
        html = (FRONTEND / "pages/group-sales.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>', "", html)
        html = re.sub(r'<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""({groups, savedIds, outputDir, latestTask}) => {
          window.fixtureGroups = groups;
          window.submitted = [];
          window.pendingLoads = [];
          window.deferLoads = false;
          window.loadFailure = '';
          window.acceptExports = false;
          window.lastExportTask = latestTask;
          window.openedOutputs = [];
          window.chooserInputs = [];
          window.chooserResult = {ok: true, path: 'D:/Chosen'};
          window.pywebview = {api: {
            async get_app_info() { return {
              ok: true, app: {name: '测试', version: 'test'},
              date_range: {start_date: '2026-09-01', end_date: '2026-09-20'},
              settings: {sales_group_ids: savedIds, output_dir: outputDir},
              account_state: {accounts: [{id: 'a', vendor: 'mabang', name: '测试账号', username: 'test'}], active_account_ids: {mabang: 'a'}},
            }; },
            async get_sidebar_preferences() { return {ok: true, preferences: {}}; },
            async get_latest_task_status(tool) { return tool === 'group_sales' && latestTask ? latestTask : {ok: true, empty: true}; },
            async get_task_status() { return window.lastExportTask || {ok: true, empty: true}; },
            async choose_output_dir(initial) { window.chooserInputs.push(initial); return window.chooserResult; },
            async reveal_path(path) { window.openedOutputs.push(path); return {ok: true}; },
            async get_group_sales_options(payload) {
              if (window.deferLoads) return new Promise(resolve => pendingLoads.push({accountId: payload.account_id, resolve}));
              if (window.loadFailure) return {ok: false, error: window.loadFailure};
              return {ok: true, groups: window.fixtureGroups};
            },
            async start_group_sales_report(payload) {
              window.submitted.push(payload);
              if (!window.acceptExports) return {ok: false, error: '仅验证提交参数'};
              const runDir = `${payload.output_dir}/菲律宾各组商品销量报表_20260901_20260920_12000${submitted.length}`;
              window.lastExportTask = {
                ok: true, id: `run-${submitted.length}`, tool: 'group_sales', status: 'success', logs: [],
                context: {output_root: payload.output_dir},
                result: {group_count: 1, record_count: 1, output_dir: runDir, output_root: payload.output_dir, output_file: `${runDir}/summary.xlsx`},
              };
              return window.lastExportTask;
            },
          }};
        }""", {
            "groups": GROUPS if groups is None else groups,
            "savedIds": ["1057656"] if saved_ids is None else saved_ids,
            "outputDir": output_dir, "latestTask": latest_task,
        })
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages/group-sales.js"))
        expect(self.page.locator("#reloadGroupsBtn")).to_have_text("重新加载")
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(len(GROUPS if groups is None else groups))

    def selected(self):
        return self.page.locator('#salesGroupSelect input:checked').evaluate_all("nodes => nodes.map(n => n.value)")

    def test_search_preserves_selection_and_submits_hidden_selected_categories(self) -> None:
        self.open_page()
        self.assertEqual(self.selected(), [])
        expect(self.page.locator("#skuCount")).to_have_text("0")
        self.page.locator("#salesGroupSearch").fill("菲S")
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.page.locator("#salesGroupSelect input").check()
        expect(self.page.locator("#skuCount")).to_have_text("1")
        self.page.locator("#salesGroupSearch").fill("  TK  ")
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.page.locator("#selectAllGroupsBtn").click()
        expect(self.page.locator("#skuCount")).to_have_text("2")
        self.page.locator("#salesGroupSearch").fill("1038956")
        self.page.locator("#salesGroupSelect input").check()
        self.page.locator("#salesGroupSearch").press("Enter")
        self.assertEqual(self.page.evaluate("submitted.length"), 0)
        self.page.locator("#groupRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        payload = self.page.evaluate("submitted[0]")
        self.assertEqual(payload["group_ids"], ["1057656", "1042622", "1038956"])
        self.assertEqual(payload["account_id"], "a")
        self.page.locator("#salesGroupSearch").fill("")
        self.assertEqual(self.selected(), payload["group_ids"])
        self.page.locator("#salesGroupSearch").fill("没有此分类")
        expect(self.page.locator("#salesGroupSelect")).to_contain_text("没有匹配")
        expect(self.page.locator("#selectAllGroupsBtn")).to_be_disabled()
        expect(self.page.locator("#skuCount")).to_have_text("3")
        self.page.locator("#clearGroupsBtn").click()
        self.page.locator("#salesGroupSearch").fill("")
        self.assertEqual(self.selected(), [])
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(5)
        self.assertEqual(self.selected(), [])
        self.page.locator("#selectAllGroupsBtn").click()
        self.assertEqual(len(self.selected()), 5)
        self.assertEqual(self.errors, [])

    def test_account_change_discards_stale_load_and_failure_allows_retry(self) -> None:
        self.open_page()
        self.page.evaluate("deferLoads = true")
        self.page.locator("#reloadGroupsBtn").click()
        self.page.evaluate("""() => {
          HQYL.state.accounts.push({id: 'b', vendor: 'mabang', name: '另一账号'});
          HQYL.state.activeAccountIds.mabang = 'b';
          HQYL.state.page.onAccountsChanged();
          pendingLoads[1].resolve({ok: true, groups: [{id: 'only-b', name: '账号B分类'}]});
        }""")
        expect(self.page.locator("#salesGroupSelect")).to_contain_text("账号B分类")
        self.page.evaluate("pendingLoads[0].resolve({ok: true, groups: fixtureGroups})")
        self.assertEqual(self.selected(), [])
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.page.evaluate("deferLoads = false; loadFailure = '权限不足'")
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect")).to_contain_text("权限不足")
        expect(self.page.locator("#groupRunBtn")).to_be_disabled()
        self.page.evaluate("loadFailure = ''; fixtureGroups = [{id: 'only-b', name: '账号B分类'}]")
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.assertEqual(self.selected(), [])
        self.assertEqual(self.errors, [])

    def test_all_categories_start_unselected_without_restoring_saved_selection(self) -> None:
        groups = GROUPS + [{"id": str(2000000 + i), "name": f"自定义分类{i}"} for i in range(212)]
        self.open_page(saved_ids=["new", "1042622", "deleted"], groups=groups)
        expect(self.page.locator("#groupSelectionSummary")).to_have_text("共 217 个分类 · 当前显示 217 个 · 已选 0 个")
        self.assertEqual(self.selected(), [])
        self.page.locator("#salesGroupSearch").fill('<新增分类 & "分组">')
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        expect(self.page.locator("#salesGroupSelect label span")).to_have_text('<新增分类 & "分组">')
        self.page.locator("#salesGroupSelect input").check()
        self.page.locator("#salesGroupSearch").fill("")
        expect(self.page.locator("#groupSelectionSummary")).to_have_text("共 217 个分类 · 当前显示 217 个 · 已选 1 个")
        self.assertEqual(self.selected(), ["new"])
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(217)
        self.assertEqual(self.selected(), ["new"])
        self.page.evaluate("HQYL.state.page.setRunning(true)")
        expect(self.page.locator("#salesGroupSearch")).to_be_disabled()
        expect(self.page.locator("#salesGroupSelect input").first).to_be_disabled()
        self.page.evaluate("HQYL.state.page.setRunning(false)")
        expect(self.page.locator("#salesGroupSelect input").first).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_manual_directory_stays_at_root_across_exports_and_opens_latest_result(self) -> None:
        self.open_page()
        self.page.locator("#salesGroupSelect input").first.check()
        self.page.locator("#groupOutputDir").fill("D:/自定义/新报表")
        self.page.evaluate("acceptExports = true")
        for count in (1, 2):
            self.page.locator("#groupRunBtn").click()
            self.page.wait_for_function("count => submitted.length === count", arg=count)
            expect(self.page.locator("#groupOutputDir")).to_have_value("D:/自定义/新报表")
            expect(self.page.locator("#groupOpenOutputBtn")).to_be_enabled()
        self.assertEqual(self.page.evaluate("submitted.map(p => p.output_dir)"), ["D:/自定义/新报表"] * 2)
        self.page.locator("#groupOpenOutputBtn").click()
        self.page.wait_for_function("openedOutputs.length === 1")
        self.assertEqual(self.page.evaluate("openedOutputs[0]"), self.page.evaluate("lastExportTask.result.output_file"))
        self.page.locator("#groupOutputDir").fill("E:/另一目录")
        self.page.evaluate("HQYL.state.page.applyResult(lastExportTask.result)")
        expect(self.page.locator("#groupOutputDir")).to_have_value("E:/另一目录")
        self.page.locator("#groupRunBtn").click()
        self.page.wait_for_function("submitted.length === 3")
        self.assertEqual(self.page.evaluate("submitted[2].output_dir"), "E:/另一目录")
        self.assertEqual(self.errors, [])

    def test_restored_legacy_result_does_not_change_saved_root(self) -> None:
        legacy = {
            "ok": True, "id": "old-run", "tool": "group_sales", "status": "success", "logs": [],
            "result": {"output_dir": "C:/Old/run/run", "output_file": "C:/Old/run/run/summary.xlsx", "record_count": 1},
        }
        self.open_page(output_dir="D:/报表", latest_task=legacy)
        expect(self.page.locator("#outputFile")).to_have_text(legacy["result"]["output_file"])
        expect(self.page.locator("#groupOutputDir")).to_have_value("D:/报表")
        self.page.locator("#salesGroupSelect input").first.check()
        self.page.locator("#groupRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0].output_dir"), "D:/报表")
        self.assertEqual(self.errors, [])

    def test_default_picker_cancel_and_empty_input(self) -> None:
        self.open_page(output_dir=None)
        expect(self.page.locator("#groupOutputDir")).to_have_value("D:\\报表\\")
        self.page.locator("#groupChooseOutputBtn").click()
        expect(self.page.locator("#groupOutputDir")).to_have_value("D:/Chosen")
        self.assertEqual(self.page.evaluate("chooserInputs[0]"), "D:\\报表\\")
        self.page.evaluate("chooserResult = {ok: false, cancelled: true}")
        self.page.locator("#groupChooseOutputBtn").click()
        expect(self.page.locator("#groupOutputDir")).to_have_value("D:/Chosen")
        self.page.locator("#salesGroupSelect input").first.check()
        self.page.locator("#groupOutputDir").fill("   ")
        self.page.locator("#groupRunBtn").click()
        expect(self.page.locator("#toast")).to_have_text("请输入输出目录")
        self.assertEqual(self.page.evaluate("submitted.length"), 0)
        self.page.evaluate("HQYL.state.page.setRunning(true)")
        expect(self.page.locator("#groupOutputDir")).to_be_disabled()
        expect(self.page.locator("#groupChooseOutputBtn")).to_be_disabled()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

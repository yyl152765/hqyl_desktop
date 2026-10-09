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

    def open_page(self, saved_ids=None, groups=None) -> None:
        html = (FRONTEND / "pages/group-sales.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>', "", html)
        html = re.sub(r'<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""({groups, savedIds}) => {
          window.fixtureGroups = groups;
          window.submitted = [];
          window.pendingLoads = [];
          window.deferLoads = false;
          window.loadFailure = '';
          window.pywebview = {api: {
            async get_app_info() { return {
              ok: true, app: {name: '测试', version: 'test'},
              date_range: {start_date: '2026-09-01', end_date: '2026-09-20'},
              settings: {sales_group_ids: savedIds, output_dir: 'C:/Exports'},
              account_state: {accounts: [{id: 'a', vendor: 'mabang', name: '测试账号', username: 'test'}], active_account_ids: {mabang: 'a'}},
            }; },
            async get_sidebar_preferences() { return {ok: true, preferences: {}}; },
            async get_latest_task_status() { return {ok: true, empty: true}; },
            async get_group_sales_options(payload) {
              if (window.deferLoads) return new Promise(resolve => pendingLoads.push({accountId: payload.account_id, resolve}));
              if (window.loadFailure) return {ok: false, error: window.loadFailure};
              return {ok: true, groups: window.fixtureGroups};
            },
            async start_group_sales_report(payload) { window.submitted.push(payload); return {ok: false, error: '仅验证提交参数'}; },
          }};
        }""", {"groups": GROUPS if groups is None else groups, "savedIds": ["1057656"] if saved_ids is None else saved_ids})
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages/group-sales.js"))
        expect(self.page.locator("#reloadGroupsBtn")).to_have_text("重新加载")
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(len(GROUPS if groups is None else groups))

    def selected(self):
        return self.page.locator('#salesGroupSelect input:checked').evaluate_all("nodes => nodes.map(n => n.value)")

    def test_search_preserves_selection_and_submits_hidden_selected_categories(self) -> None:
        self.open_page()
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
        self.assertEqual(self.selected(), ["only-b"])
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.page.evaluate("deferLoads = false; loadFailure = '权限不足'")
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect")).to_contain_text("权限不足")
        expect(self.page.locator("#groupRunBtn")).to_be_disabled()
        self.page.evaluate("loadFailure = ''; fixtureGroups = [{id: 'only-b', name: '账号B分类'}]")
        self.page.locator("#reloadGroupsBtn").click()
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        self.assertEqual(self.selected(), ["only-b"])
        self.assertEqual(self.errors, [])

    def test_all_categories_and_saved_selection_restore_without_markup_injection(self) -> None:
        groups = GROUPS + [{"id": str(2000000 + i), "name": f"自定义分类{i}"} for i in range(212)]
        self.open_page(saved_ids=["new", "1042622", "deleted"], groups=groups)
        expect(self.page.locator("#groupSelectionSummary")).to_have_text("共 217 个分类 · 当前显示 217 个 · 已选 2 个")
        self.assertEqual(self.selected(), ["1042622", "new"])
        self.page.locator("#salesGroupSearch").fill('<新增分类 & "分组">')
        expect(self.page.locator("#salesGroupSelect input")).to_have_count(1)
        expect(self.page.locator("#salesGroupSelect label span")).to_have_text('<新增分类 & "分组">')
        self.page.evaluate("HQYL.state.page.setRunning(true)")
        expect(self.page.locator("#salesGroupSearch")).to_be_disabled()
        expect(self.page.locator("#salesGroupSelect input")).to_be_disabled()
        self.page.evaluate("HQYL.state.page.setRunning(false)")
        expect(self.page.locator("#salesGroupSelect input")).to_be_enabled()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

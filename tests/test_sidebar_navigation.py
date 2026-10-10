"""Browser regression checks for the shared sidebar, without business services.

Run with ``python -m unittest discover -s tests -p test_sidebar_navigation.py``.
Playwright and an installed Chromium/Edge browser are optional; unavailable
browser dependencies skip this suite instead of affecting backend-only runs.
"""

from __future__ import annotations

import functools
import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
STORAGE_KEY = "hqyl.sidebar.v1"
GROUP_COUNTS = {"finance": 2, "mabang": 10, "ziniao": 8, "echotik": 1, "bigseller": 4}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


class SidebarNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is optional for sidebar browser checks")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        launch_errors = []
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError as error:
                launch_errors.append(str(error).splitlines()[0])
        else:
            raise unittest.SkipTest("No Chromium/Edge browser: " + "; ".join(launch_errors))
        cls.addClassCleanup(cls.browser.close)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(FRONTEND))
        )
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/pages/"

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1180, "height": 760})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)

    def open_page(self, name: str = "dashboard.html") -> None:
        response = self.page.goto(self.base_url + name)
        self.assertTrue(response and response.ok)
        expect(self.page.locator("#navSearch")).to_be_visible()

    def result_ids(self) -> list[str]:
        return self.page.locator("#navSearchResults a[data-page]").evaluate_all(
            "links => links.map(link => link.dataset.page)"
        )

    def favorite_ids(self) -> list[str]:
        return self.page.locator("#navFavoriteList [data-nav-favorite]").evaluate_all(
            "buttons => buttons.map(button => button.dataset.navFavorite)"
        )

    def expanded_groups(self) -> list[str]:
        return self.page.locator('#navGroups [data-nav-group][aria-expanded="true"]').evaluate_all(
            "buttons => buttons.map(button => button.dataset.navGroup)"
        )

    def add_favorite(self, module_id: str, query: str) -> None:
        previous_url = self.page.url
        self.page.locator("#navSearch").fill(query)
        self.page.locator(f'#navSearchResults [data-nav-favorite="{module_id}"]').click()
        self.assertEqual(self.page.url, previous_url, "Star buttons must not navigate")

    def test_all_routes_exist_and_reveal_the_active_module(self) -> None:
        self.open_page()
        routes = self.page.locator("#navGroups a[data-page]").evaluate_all(
            "links => links.map(link => ({id: link.dataset.page, href: link.getAttribute('href')}))"
        )
        self.assertEqual(len(routes), sum(GROUP_COUNTS.values()))
        self.assertEqual(len({route["id"] for route in routes}), sum(GROUP_COUNTS.values()))
        self.assertEqual(len({route["href"] for route in routes}), sum(GROUP_COUNTS.values()))
        for group_id, count in GROUP_COUNTS.items():
            self.assertEqual(self.page.locator(f"#navGroup-{group_id} a[data-page]").count(), count)
            expect(self.page.locator(f'[data-nav-group="{group_id}"] .nav-group-count')).to_have_text(str(count))
        for route in routes:
            with self.subTest(module=route["id"]):
                self.assertTrue((FRONTEND / "pages" / route["href"]).is_file())
                self.open_page(route["href"])
                active = self.page.locator('#navGroups a[aria-current="page"]')
                expect(active).to_have_attribute("data-page", route["id"])
                expect(active).to_have_class("nav-leaf active")
                expect(active).to_be_visible()
                self.assertIn(route["id"], self.page.locator("#navGroups a[data-page]").evaluate_all(
                    "links => links.filter(link => link.getClientRects().length).map(link => link.dataset.page)"
                ))

    def test_multigroup_folding_survives_navigation_and_current_group_opens(self) -> None:
        self.open_page()
        self.assertEqual(self.expanded_groups(), [])
        for group_id in ("finance", "mabang", "ziniao"):
            self.page.locator(f'[data-nav-group="{group_id}"]').click()
        self.page.locator('[data-nav-group="finance"]').click()
        self.page.locator('#navGroups a[data-page="purchase_log"]').click()
        expect(self.page).to_have_url(self.base_url + "purchase-log.html")
        self.assertEqual(self.expanded_groups(), ["mabang", "ziniao"])
        self.page.locator('.nav-footer a[data-page="settings"]').click()
        expect(self.page).to_have_url(self.base_url + "settings.html")
        self.assertEqual(self.expanded_groups(), ["mabang", "ziniao"])
        self.page.locator(".nav-collapse-all").click()
        self.assertEqual(self.expanded_groups(), [])
        self.page.reload()
        self.assertEqual(self.expanded_groups(), [])
        self.open_page("echotik-collect.html")
        self.assertEqual(self.expanded_groups(), ["echotik"])

    def test_search_uses_all_metadata_and_restores_collapsed_groups(self) -> None:
        self.open_page()
        self.page.locator('[data-nav-group="finance"]').click()
        search = self.page.locator("#navSearch")
        cases = {
            "  LaZaDa   财务  ": ["lazada_withdrawal_statistics", "lazada_balance_statistics", "lazada_monthly_report"],
            "菲律宾 Shopee 小组": ["group_sales"],
            "通用工具 权限": ["mabang_warehouse_permission", "mabang_developer_permission"],
            "采购流程": ["developer_sales_income_summary", "purchase_log"],
            "紫鸟 马帮": ["vietnam_income_reconciliation"],
            "付款日期 美元": ["developer_sales_income_summary"],
            "tIkToK 样品": ["sample_registration"],
            "TEMU 物流": ["temu_shipping_channel"],
        }
        for query, expected_ids in cases.items():
            with self.subTest(query=query):
                search.fill(query)
                self.assertEqual(self.result_ids(), expected_ids)
                expect(self.page.locator("#navGroups")).to_be_hidden()
                for path in self.page.locator("#navSearchResults .nav-result-path").all_text_contents():
                    self.assertIn(" / ", path)
        search.fill('<img src=x onerror="window.sidebarInjected=true">')
        self.assertEqual(self.result_ids(), [])
        expect(self.page.locator("#navSearchEmpty")).to_be_visible()
        self.assertFalse(self.page.evaluate("Boolean(window.sidebarInjected)"))
        self.page.locator(".nav-search-clear").click()
        self.assertEqual(self.expanded_groups(), ["finance"])
        expect(self.page.locator("#navGroups")).to_be_visible()

    def test_keyboard_shortcuts_and_ime_confirmation_do_not_misnavigate(self) -> None:
        self.open_page()
        self.page.keyboard.press("Control+k")
        search = self.page.locator("#navSearch")
        expect(search).to_be_focused()
        search.fill("Lazada")
        search.dispatch_event("compositionstart")
        search.press("Enter")
        expect(self.page).to_have_url(self.base_url + "dashboard.html")
        search.press("Escape")
        expect(search).to_have_value("Lazada")
        search.dispatch_event("compositionend")
        search.press("Escape")
        expect(search).to_have_value("")
        search.fill("no_such_module_987654")
        search.press("Enter")
        expect(self.page).to_have_url(self.base_url + "dashboard.html")
        search.fill("Lazada")
        search.press("Enter")
        expect(self.page).to_have_url(self.base_url + "lazada-withdrawal-statistics.html")

    def test_favorites_limit_order_removal_and_cross_page_persistence(self) -> None:
        self.open_page()
        expect(self.page.locator("#navFavorites")).to_be_hidden()
        entries = [
            ("sample_registration", "寄样"),
            ("purchase_log", "采购日志"),
            ("bigseller_sync", "产品/库存同步"),
            ("lazada_monthly_report", "月度账单"),
            ("echotik_collect", "商品达人采集"),
        ]
        for module_id, query in entries:
            self.add_favorite(module_id, query)
        expected_ids = [entry[0] for entry in entries]
        self.assertEqual(self.favorite_ids(), expected_ids)
        self.add_favorite("vietnam_income_reconciliation", "越南收支")
        self.assertEqual(self.favorite_ids(), expected_ids)
        expect(self.page.locator("#navAnnouncement")).to_contain_text("5")
        self.page.locator("#navSearch").fill("")
        self.page.locator('#navFavoriteList [data-nav-favorite="purchase_log"]').click()
        self.add_favorite("purchase_log", "采购日志")
        expected_ids.remove("purchase_log")
        expected_ids.append("purchase_log")
        self.assertEqual(self.favorite_ids(), expected_ids)
        expect(self.page.locator('#navSearchResults [data-nav-favorite="purchase_log"]')).to_have_attribute("aria-pressed", "true")
        self.page.locator('.nav-footer a[data-page="settings"]').click()
        expect(self.page).to_have_url(self.base_url + "settings.html")
        self.assertEqual(self.favorite_ids(), expected_ids)
        self.page.reload()
        self.assertEqual(self.favorite_ids(), expected_ids)

    def test_corrupt_and_unavailable_browser_storage_leave_navigation_usable(self) -> None:
        self.open_page()
        for raw in ("{invalid", "null", "[]", '{"favorites":{},"expandedGroups":42}'):
            with self.subTest(raw=raw):
                self.page.evaluate("([key, value]) => localStorage.setItem(key, value)", [STORAGE_KEY, raw])
                self.page.reload()
                expect(self.page.locator("#navSearch")).to_be_visible()
                self.assertEqual(self.favorite_ids(), [])
                self.assertEqual(self.page.locator("#navGroups a[data-page]").count(), sum(GROUP_COUNTS.values()))
        invalid_ids = {"favorites": ["removed", "purchase_log", "purchase_log", 5], "expandedGroups": ["missing", "ziniao", "ziniao"]}
        self.page.evaluate("([key, value]) => localStorage.setItem(key, value)", [STORAGE_KEY, json.dumps(invalid_ids)])
        self.page.reload()
        self.assertEqual(self.favorite_ids(), ["purchase_log"])
        self.assertEqual(self.expanded_groups(), ["ziniao"])
        self.context.add_init_script("""
            Storage.prototype.getItem = function () { throw new DOMException('Blocked', 'SecurityError'); };
            Storage.prototype.setItem = function () { throw new DOMException('Blocked', 'QuotaExceededError'); };
        """)
        self.page.reload()
        self.add_favorite("sample_registration", "寄样")
        self.assertEqual(self.favorite_ids(), ["sample_registration"])
        self.page.locator("#navSearch").fill("")
        self.page.locator('[data-nav-group="ziniao"]').click()
        self.assertEqual(self.expanded_groups(), ["ziniao"])

    def test_late_native_preferences_preserve_user_changes_and_merge_other_fields(self) -> None:
        bridge_script = """
            window.__nativeSaves = [];
            window.pywebview = {api: {
                get_app_info: async () => ({ok: false, error: 'Browser test: business init disabled'}),
                get_sidebar_preferences: () => new Promise(resolve => { window.__resolveNative = resolve; }),
                save_sidebar_preferences: async preferences => {
                    window.__nativeSaves.push(JSON.parse(JSON.stringify(preferences)));
                    return {ok: true, preferences};
                }
            }};
        """
        self.context.add_init_script(bridge_script)
        self.open_page()
        self.page.wait_for_function("typeof window.__resolveNative === 'function'")
        self.add_favorite("sample_registration", "寄样")
        self.page.evaluate("window.__resolveNative({ok: true, preferences: {favorites: ['purchase_log'], expandedGroups: ['ziniao']}})")
        self.page.wait_for_function("window.__nativeSaves.length > 0")
        self.assertEqual(self.favorite_ids(), ["purchase_log", "sample_registration"])
        self.assertEqual(self.expanded_groups(), ["ziniao"])
        self.assertEqual(self.page.evaluate("window.__nativeSaves.at(-1)"), {
            "favorites": ["purchase_log", "sample_registration"], "expandedGroups": ["ziniao"]
        })
        self.open_page("sample-registration.html")
        self.page.wait_for_function("typeof window.__resolveNative === 'function'")
        self.page.locator('[data-nav-group="mabang"]').click()
        self.page.evaluate("window.__resolveNative({ok: true, preferences: {favorites: ['purchase_log'], expandedGroups: ['mabang']}})")
        self.page.wait_for_function("window.__nativeSaves.length > 0")
        self.assertEqual(self.favorite_ids(), ["purchase_log"])
        self.assertNotIn("mabang", self.expanded_groups(), "Late hydration must respect the user's manual collapse")

    def test_late_native_load_preserves_slot_release_and_readd_order_at_favorite_limit(self) -> None:
        baseline = {
            "favorites": ["sample_registration", "purchase_log", "bigseller_sync",
                          "lazada_monthly_report", "echotik_collect"],
            "expandedGroups": [],
        }
        expected_ids = ["bigseller_sync", "lazada_monthly_report", "echotik_collect",
                        "vietnam_income_reconciliation", "sample_registration"]
        self.context.add_init_script(
            "localStorage.setItem(" + json.dumps(STORAGE_KEY) + ", "
            + json.dumps(json.dumps(baseline)) + ");\n" + """
            window.__nativeSaves = [];
            window.pywebview = {api: {
                get_app_info: async () => ({ok: false, error: 'Browser test: business init disabled'}),
                get_sidebar_preferences: () => new Promise(resolve => { window.__resolveNative = resolve; }),
                save_sidebar_preferences: async preferences => {
                    window.__nativeSaves.push(JSON.parse(JSON.stringify(preferences)));
                    return {ok: true, preferences};
                }
            }};
        """)
        for native_preferences in (baseline, None):
            with self.subTest(native_baseline="saved" if native_preferences else "first_migration"):
                self.open_page()
                self.page.wait_for_function("typeof window.__resolveNative === 'function'")
                self.assertEqual(self.favorite_ids(), baseline["favorites"])
                # Free A's slot before adding F, then free B's slot to re-add A last.
                # Removing A's earlier operation would make F appear to exceed the limit.
                self.add_favorite("sample_registration", "寄样")
                self.add_favorite("vietnam_income_reconciliation", "越南收支")
                self.add_favorite("purchase_log", "采购日志")
                self.add_favorite("sample_registration", "寄样")
                self.assertEqual(self.favorite_ids(), expected_ids)
                self.assertEqual(self.page.evaluate("window.__nativeSaves"), [])
                self.page.evaluate(
                    "preferences => window.__resolveNative({ok: true, preferences})",
                    native_preferences,
                )
                self.page.wait_for_function("window.__nativeSaves.length > 0")
                self.assertEqual(self.favorite_ids(), expected_ids)
                self.assertEqual(self.page.evaluate("window.__nativeSaves.at(-1)"), {
                    "favorites": expected_ids, "expandedGroups": [],
                })

    def test_native_saves_finish_in_order_before_rapid_navigation(self) -> None:
        self.context.add_init_script("""
            window.pywebview = {api: {
                get_app_info: async () => ({ok: false, error: 'Browser test: business init disabled'}),
                get_sidebar_preferences: async () => ({ok: true, preferences: JSON.parse(sessionStorage.getItem('nativePrefs'))}),
                save_sidebar_preferences: async preferences => {
                    const active = Number(sessionStorage.getItem('activeSaves') || 0) + 1;
                    sessionStorage.setItem('activeSaves', active);
                    sessionStorage.setItem('maxActiveSaves', Math.max(active, Number(sessionStorage.getItem('maxActiveSaves') || 0)));
                    await new Promise(resolve => setTimeout(resolve, 100));
                    sessionStorage.setItem('nativePrefs', JSON.stringify(preferences));
                    sessionStorage.setItem('activeSaves', Number(sessionStorage.getItem('activeSaves')) - 1);
                    const saved = JSON.parse(sessionStorage.getItem('savedCalls') || '[]');
                    saved.push(preferences);
                    sessionStorage.setItem('savedCalls', JSON.stringify(saved));
                    return {ok: true, preferences};
                }
            }};
        """)
        self.open_page("sample-registration.html")
        self.page.evaluate("""() => {
            document.querySelector('#navGroups [data-nav-favorite="sample_registration"]').click();
            document.querySelector('#navGroups [data-nav-favorite="group_sales"]').click();
            document.querySelector('.nav-footer a[data-page="settings"]').click();
        }""")
        expect(self.page).to_have_url(self.base_url + "settings.html")
        self.assertEqual(self.favorite_ids(), ["sample_registration", "group_sales"])
        self.assertEqual(self.page.evaluate("Number(sessionStorage.getItem('maxActiveSaves'))"), 1)
        saved_calls = self.page.evaluate("JSON.parse(sessionStorage.getItem('savedCalls'))")
        self.assertEqual(len(saved_calls), 2, "Boot must not register duplicate event handlers")
        self.assertEqual(saved_calls[-1]["favorites"], ["sample_registration", "group_sales"])
        self.assertEqual(self.page.evaluate("Number(sessionStorage.getItem('activeSaves'))"), 0)

    def test_bridge_ready_replays_cached_changes_after_an_early_page_change(self) -> None:
        self.open_page()
        self.add_favorite("sample_registration", "寄样")
        self.page.locator('.nav-footer a[data-page="settings"]').click()
        expect(self.page).to_have_url(self.base_url + "settings.html")
        self.assertEqual(self.favorite_ids(), ["sample_registration"])
        self.page.evaluate("""() => {
            window.__readySaves = [];
            window.pywebview = {api: {
                get_app_info: async () => ({ok: false, error: 'Browser test: business init disabled'}),
                get_sidebar_preferences: async () => ({ok: true, preferences: {
                    favorites: ['purchase_log'], expandedGroups: ['finance']
                }}),
                save_sidebar_preferences: async preferences => {
                    window.__readySaves.push(JSON.parse(JSON.stringify(preferences)));
                    return {ok: true, preferences};
                }
            }};
            window.dispatchEvent(new Event('pywebviewready'));
        }""")
        self.page.wait_for_function("window.__readySaves.length > 0")
        self.assertEqual(self.favorite_ids(), ["purchase_log", "sample_registration"])
        self.assertEqual(self.expanded_groups(), ["finance"])
        self.assertEqual(self.page.evaluate("window.__readySaves.at(-1)"), {
            "favorites": ["purchase_log", "sample_registration"], "expandedGroups": ["finance"]
        })

    def test_navigation_waits_for_a_new_save_added_while_first_save_is_pending(self) -> None:
        self.context.add_init_script("""
            window.__saveCalls = [];
            window.__saveResolvers = [];
            window.pywebview = {api: {
                get_app_info: async () => ({ok: false, error: 'Browser test: business init disabled'}),
                get_sidebar_preferences: async () => ({ok: true, preferences: JSON.parse(sessionStorage.getItem('nativePrefs'))}),
                save_sidebar_preferences: preferences => {
                    window.__saveCalls.push(JSON.parse(JSON.stringify(preferences)));
                    return new Promise(resolve => window.__saveResolvers.push(() => {
                        sessionStorage.setItem('nativePrefs', JSON.stringify(preferences));
                        resolve({ok: true, preferences});
                    }));
                }
            }};
        """)
        self.open_page("sample-registration.html")
        self.page.locator('#navGroups [data-nav-favorite="sample_registration"]').click()
        self.page.wait_for_function("window.__saveCalls.length === 1")
        self.page.evaluate("""() => {
            document.querySelector('.nav-footer a[data-page="settings"]').click();
            document.querySelector('#navGroups [data-nav-favorite="group_sales"]').click();
        }""")
        self.page.evaluate("window.__saveResolvers[0]()")
        self.page.wait_for_function("window.__saveCalls.length === 2")
        expect(self.page).to_have_url(self.base_url + "sample-registration.html")
        self.page.evaluate("window.__saveResolvers[1]()")
        expect(self.page).to_have_url(self.base_url + "settings.html")
        self.assertEqual(self.favorite_ids(), ["sample_registration", "group_sales"])
        self.assertEqual(self.page.evaluate("JSON.parse(sessionStorage.getItem('nativePrefs')).favorites"), [
            "sample_registration", "group_sales"
        ])

    def test_short_window_keeps_search_and_footer_fixed_while_modules_scroll(self) -> None:
        self.open_page()
        self.page.evaluate("""([key, groups]) => localStorage.setItem(key, JSON.stringify({
            favorites: ['sample_registration', 'purchase_log', 'bigseller_sync', 'lazada_monthly_report', 'echotik_collect'],
            expandedGroups: groups
        }))""", [STORAGE_KEY, list(GROUP_COUNTS)])
        for width, height in ((980, 640), (1180, 760)):
            with self.subTest(viewport=(width, height)):
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.reload()
                search_before = self.page.locator("#navSearch").bounding_box()
                settings_before = self.page.locator('.nav-footer a[data-page="settings"]').bounding_box()
                tree_before = self.page.locator(".nav-tree").bounding_box()
                self.assertIsNotNone(search_before)
                self.assertIsNotNone(settings_before)
                self.assertIsNotNone(tree_before)
                self.assertGreater(tree_before["height"], 100)
                self.assertGreaterEqual(search_before["y"], 0)
                self.assertLessEqual(settings_before["y"] + settings_before["height"], height)
                self.assertLessEqual(tree_before["y"] + tree_before["height"], settings_before["y"])
                row_heights = self.page.locator("#navGroups .nav-row").evaluate_all(
                    "rows => rows.map(row => row.getBoundingClientRect().height)"
                )
                self.assertTrue(all(36 <= value <= 40 for value in row_heights), row_heights)
                self.page.locator(".nav-tree").evaluate("tree => { tree.scrollTop = tree.scrollHeight; }")
                self.assertGreater(self.page.locator(".nav-tree").evaluate("tree => tree.scrollTop"), 0)
                search_after = self.page.locator("#navSearch").bounding_box()
                settings_after = self.page.locator('.nav-footer a[data-page="settings"]').bounding_box()
                self.assertAlmostEqual(search_before["y"], search_after["y"], delta=0.5)
                self.assertAlmostEqual(settings_before["y"], settings_after["y"], delta=0.5)
                last_link = self.page.locator('#navGroups a[data-page="bigseller_sku_benchmark"]').bounding_box()
                self.assertGreaterEqual(last_link["y"], tree_before["y"])
                self.assertLessEqual(last_link["y"] + last_link["height"], tree_before["y"] + tree_before["height"])
                for selector in ("#navSearch", '.nav-footer a[data-page="settings"]'):
                    self.assertTrue(self.page.locator(selector).evaluate("""element => {
                        const rect = element.getBoundingClientRect();
                        return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
                    }"""), f"{selector} is covered at {width}x{height}")


if __name__ == "__main__":
    unittest.main()

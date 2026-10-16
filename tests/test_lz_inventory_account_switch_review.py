"""Independent offline account-switch race checks against the actual page."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:
    sync_playwright = None


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class LzInventoryInvalidAccountReviewTests(unittest.TestCase):
    def test_explicit_invalid_or_wrong_vendor_id_never_falls_back_to_active_account(self):
        bridge = AppBridge.__new__(AppBridge)
        bridge.config_store = Mock()
        bridge.config_store.load.return_value = AppSettings(
            accounts=[
                BoundAccount("A", "mabang", "Fixture A", "fixture-user-a", "fixture-password-a"),
                BoundAccount("B", "mabang", "Fixture B", "fixture-user-b", "fixture-password-b"),
                BoundAccount("Z", "ziniao", "Fixture Z", "fixture-user-z", "fixture-password-z"),
            ],
            active_account_ids={"mabang": "B"},
        )
        bridge.tasks = Mock()
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses") as fetch:
            for invalid_id in ("not-bound", "Z", "", "   ", None, [], 0):
                with self.subTest(account_id=invalid_id):
                    payload = {"account_id": invalid_id, "warehouse_names": ["Fixture warehouse"], "output_dir": "D:/fixture-output"}
                    for method in (bridge.get_lz_warehouse_inventory_info, bridge.get_lz_warehouse_inventory_warehouses, bridge.start_lz_warehouse_inventory):
                        result = method(payload)
                        self.assertFalse(result["ok"], result)
            fetch.assert_not_called()
        bridge.tasks.start.assert_not_called()
        bridge.config_store.save.assert_not_called()


class LzInventoryAccountSwitchRaceReviewTests(unittest.TestCase):
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
            raise unittest.SkipTest("Chromium/Edge is required")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def open_page(self, **options):
        html = (FRONTEND / "pages/lz-warehouse-realtime-inventory.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>|<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.evaluate("""options => {
          window.reviewCalls = {info:[],catalog:[],start:[],select:[],poll:[]};
          window.reviewPending = {};
          window.reviewTasks = {};
          const records = ['A','B'].map(id => ({id,vendor:'mabang',name:`账号 ${id}`,username:`fixture-${id}`}));
          let activeId = options.initial === undefined ? 'B' : options.initial;
          const state = () => ({accounts:activeId ? records : [],active_account_ids:activeId ? {mabang:activeId} : {}});
          window.reviewSwitch = id => {activeId=id; HQYL.applyAccountState(state());};
          window.reviewInfo = id => ({ok:true,account_id:id,account_name:`账号 ${id}`,output_dir:'C:/ReviewExports'});
          window.reviewCatalog = id => ({ok:true,account_id:id,warehouses:[
            {id:`${id}-default`,name:id==='A'?'泰国TH04仓':'泰国TH05仓'},
            {id:`${id}-private`,name:`${id}专用仓`},
          ]});
          window.reviewTask = id => ({ok:true,id:`task-${id}`,tool:'lz_warehouse_inventory',status:'running',context:{account_id:id},logs:[`账号 ${id} 的任务日志`]});
          const delay = (kind,id) => new Promise((resolve,reject) => {reviewPending[`${kind}-${id}`]={resolve,reject};});
          window.pywebview = {api:{
            async get_app_info(){return {ok:true,app:{name:'Review',version:'test'},settings:{output_dir:'C:/Unrelated'},account_state:state()};},
            async get_sidebar_preferences(){return {ok:true,preferences:{}};},
            async get_lz_warehouse_inventory_info(payload){
              reviewCalls.info.push(payload || null); const id=payload?.account_id || activeId;
              return options.delayInfo===id ? delay('info',id) : reviewInfo(id);
            },
            async get_lz_warehouse_inventory_warehouses(payload){
              reviewCalls.catalog.push(payload || null); const id=payload?.account_id || activeId;
              return options.delayCatalog===id ? delay('catalog',id) : reviewCatalog(id);
            },
            async select_account(vendor,id){reviewCalls.select.push(id);activeId=id;return {ok:true,account_state:state()};},
            async get_latest_task_status(tool){return options.unscopedTask && tool ? {ok:true,id:'unscoped-task',status:'running',context:{},logs:['UNKNOWN ACCOUNT TASK']} : {ok:true,empty:true};},
            async start_lz_warehouse_inventory(payload){
              reviewCalls.start.push(payload); const id=payload.account_id;
              if(options.delayStart===id) return delay('start',id);
              return reviewTasks[`task-${id}`]=reviewTask(id);
            },
            async get_task_status(id){reviewCalls.poll.push(id);return options.delayPoll===id ? delay('poll',id) : reviewTasks[id] || {ok:false};},
            async save_scoped_output_dirs(){return {ok:true};},
          }};
        }""", options)
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages/lz-warehouse-realtime-inventory.js"))

    def expect_account_ready(self, account_id):
        expect(self.page.locator("#lzAccountNote")).to_contain_text(f"账号 {account_id}")
        expect(self.page.locator("#lzWarehouseChecklist")).to_contain_text(f"{account_id}专用仓")
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()

    def test_initial_active_b_is_kept_and_loaded_exactly_once_with_explicit_id(self):
        self.open_page()
        self.expect_account_ready("B")
        self.assertEqual(self.page.evaluate("reviewCalls.select"), [])
        for endpoint in ("info", "catalog"):
            self.assertEqual(self.page.evaluate(f"reviewCalls.{endpoint}"), [{"account_id": "B"}])
        self.assertEqual(self.errors, [])

    def test_old_a_info_success_cannot_override_ready_b(self):
        self.open_page(initial="A", delayInfo="A")
        self.page.wait_for_function("reviewPending['info-A'] !== undefined")
        self.page.evaluate("reviewSwitch('B')")
        self.expect_account_ready("B")
        self.page.evaluate("reviewPending['info-A'].resolve(reviewInfo('A'))")
        self.page.wait_for_function("reviewCalls.info.length >= 2")
        self.expect_account_ready("B")
        self.assertFalse(self.page.evaluate("reviewCalls.catalog.some(item => item?.account_id === 'A')"))
        self.assertEqual(self.page.evaluate("reviewCalls.select"), [])
        self.assertEqual(self.errors, [])

    def test_old_a_catalog_failure_cannot_erase_b_or_show_old_error(self):
        self.open_page(initial="A", delayCatalog="A")
        self.page.wait_for_function("reviewPending['catalog-A'] !== undefined")
        self.page.evaluate("reviewSwitch('B')")
        self.expect_account_ready("B")
        self.page.evaluate("reviewPending['catalog-A'].resolve({ok:false,error:'OLD_A_CATALOG_ERROR'})")
        self.expect_account_ready("B")
        expect(self.page.locator("#lzWarehouseStatus")).not_to_contain_text("OLD_A")
        expect(self.page.locator("#toast")).not_to_contain_text("OLD_A")
        self.assertEqual(self.errors, [])

    def test_old_a_start_failure_after_switch_cannot_change_b_task_ui(self):
        self.open_page(initial="A", delayStart="A")
        self.expect_account_ready("A")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("reviewPending['start-A'] !== undefined")
        self.page.evaluate("reviewSwitch('B')")
        self.expect_account_ready("B")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("HQYL.state.taskId === 'task-B'")
        self.page.evaluate("reviewPending['start-A'].resolve({ok:false,error:'OLD_A_START_ERROR'})")
        expect(self.page.locator("#taskBadge")).to_have_text("运行中")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("HQYL.state.taskId"), "task-B")
        expect(self.page.locator("#logBox")).to_contain_text("账号 B 的任务日志")
        expect(self.page.locator("#logBox")).not_to_contain_text("OLD_A")
        expect(self.page.locator("#toast")).not_to_contain_text("OLD_A")
        self.assertEqual(self.errors, [])

    def test_old_a_poll_rejection_after_switch_does_not_lock_b(self):
        self.open_page(initial="A", delayPoll="task-A")
        self.expect_account_ready("A")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("reviewPending['poll-task-A'] !== undefined")
        self.page.evaluate("reviewSwitch('B')")
        self.expect_account_ready("B")
        self.page.evaluate("reviewPending['poll-task-A'].reject(new Error('OLD_A_POLL_ERROR'))")
        self.expect_account_ready("B")
        expect(self.page.locator("#logBox")).not_to_contain_text("OLD_A")
        self.assertNotEqual(self.page.evaluate("HQYL.state.taskId"), "task-A")
        self.assertEqual(self.errors, [])

    def test_binding_first_account_recovers_without_reloading_and_unscoped_tasks_are_rejected(self):
        self.open_page(initial="", unscopedTask=True)
        expect(self.page.locator("#lzAccountNote")).to_contain_text("绑定")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("reviewCalls.info"), [])
        self.page.evaluate("reviewSwitch('B')")
        self.expect_account_ready("B")
        expect(self.page.locator("#logBox")).not_to_contain_text("UNKNOWN ACCOUNT TASK")
        self.assertEqual(self.page.evaluate("reviewCalls.select"), [])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

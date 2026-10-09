"""Browser contract tests with a local bridge; no seller pages or accounts are contacted."""
from __future__ import annotations

import functools
import shutil
import subprocess
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import Error, expect, sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]


class BalanceTaskAccountTests(unittest.TestCase):
    def test_delayed_bridge_responses_preserve_chosen_month_and_current_account(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const elements = new Map();
function element() {
  return {value:'',textContent:'',hidden:false,disabled:false,children:[],listeners:{},
    classList:{add(){},remove(){}}, addEventListener(name,fn){this.listeners[name]=fn;},
    appendChild(child){this.children.push(child);},replaceChildren(){this.children=[];},
    setAttribute(){},scrollHeight:0,scrollTop:0,clientHeight:0};
}
global.document = {body:{dataset:{balancePlatform:'temu'}},
  getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
  createElement:element,querySelectorAll(){return [];} };
global.window = {setTimeout(){},clearInterval(){},setInterval(){return 1;}};
const root = process.argv[1];
vm.runInThisContext(fs.readFileSync(path.join(root,'frontend/assets/common.js'),'utf8'));
const h = vm.runInThisContext('HQYL');
h.boot = page => {h.state.page = page;};
let pending, resolveStart, resolveInfo;
const startTask = h.startTask;
h.startTask = (...args) => {pending = startTask(...args);return pending;};
const accounts = [{id:'a',vendor:'ziniao'},{id:'b',vendor:'ziniao'}];
h.applyAccountState({accounts,active_account_ids:{ziniao:'a'}});
window.pywebview = {api:{
  async get_balance_statistics_info(){return new Promise(resolve=>{resolveInfo=resolve;});},
  async start_balance_statistics(){return new Promise(resolve=>{resolveStart=resolve;});}
}};
vm.runInThisContext(fs.readFileSync(path.join(root,'frontend/assets/pages/balance-statistics.js'),'utf8'));
(async()=>{
  const initialization = h.state.page.init({settings:{output_dir:'D:/output'}});
  elements.get('balanceMonth').value='2026-07';
  resolveInfo({ok:true,month:'2026-08',output_dir:'D:/output'});
  await initialization;
  assert.strictEqual(elements.get('balanceMonth').value,'2026-07');
  elements.get('storeNames').value='Account A Store';
  elements.get('balanceForm').listeners.submit({preventDefault(){}});
  assert.ok(resolveStart,'start request must be pending');
  h.applyAccountState({accounts,active_account_ids:{ziniao:'b'}});
  resolveStart({ok:false,error:'Account A store failed'});
  await pending;
  assert.strictEqual(elements.get('taskBadge').textContent,'待运行');
  assert.strictEqual(elements.get('logBox').textContent,'');
  assert.strictEqual(elements.get('startIssues').textContent,'');
  assert.strictEqual(elements.get('storeNames').value,'');
  assert.strictEqual(elements.get('retryBtn').disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script, str(ROOT)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


MOCK_BRIDGE = r"""(() => {
  const platform = location.pathname.includes('temu-') ? 'temu' : 'lazada';
  window.testAccounts = [
    {id:'ziniao-a',vendor:'ziniao',name:'测试公司',username:'operator',extra:{}},
    {id:'ziniao-b',vendor:'ziniao',name:'另一公司',username:'other',extra:{}}
  ];
  window.testStarted = [];
  window.testRetries = [];
  window.testOpened = [];
  window.testResult = {
    platform, account_id:'ziniao-a', is_complete:false,
    output_file:'D:/results/余额统计.xlsx',output_dir:'D:/results',manifest_path:'D:/results/manifest.json',
    summary:{total:2,success:1,partial:1,failed:0},
    stores:[
      {store_name:'测试店铺 A',country:'TH',currency:'THB',status:'success',
       values:platform === 'temu' ? {total:'-123.40',pending:0} : {income:'-123.40',balance:0,ads:'0.00',processing:'25.00'},
       evidence:{balance:'D:/results/a.png',pending:'D:/results/b.png'},captured_at:'2026-09-28 12:30:00',message:'已取得'},
      {store_name:'<img src=x onerror=alert(1)>',country:'SG',currency:'SGD',status:'partial',
       values:platform === 'temu' ? {total:null,pending:'0.00'} : {income:null,balance:null,ads:0,processing:null},
       evidence:{},captured_at:'2026-09-28 12:31:00',message:'资金页加载失败'}
    ]
  };
  const task = (status='running') => ({
    ok:true,id:'balance-fixture',tool:platform+'_balance_statistics',status,logs:['逐店采集中'],
    context:{platform,account_id:'ziniao-a'},...(status === 'success' ? {result:window.testResult} : {})
  });
  window.pywebview = {api:{
    get_app_info: async () => ({ok:true,app:{name:'测试平台',version:'dev'},settings:{output_dir:'D:/output'},account_state:{accounts:window.testAccounts,active_account_ids:{ziniao:'ziniao-a'}}}),
    get_sidebar_preferences: async () => ({ok:true,preferences:null}),
    save_sidebar_preferences: async () => ({ok:true}),
    get_balance_statistics_info: async payload => {window.testInfoPayload=payload;return {ok:true,month:'2026-08',output_dir:'D:/output',countries:[{code:'PH',name:'菲律宾'},{code:'MY',name:'马来西亚'},{code:'TH',name:'泰国'},{code:'ID',name:'印度尼西亚'},{code:'VN',name:'越南'},{code:'SG',name:'新加坡'}]};},
    get_latest_task_status: async tool => window.restoreFixture && tool === platform+'_balance_statistics' ? task('success') : ({ok:false,empty:true}),
    get_task_status: async () => task(window.testTerminal ? 'success' : 'running'),
    start_balance_statistics: async payload => {
      window.testStarted.push(payload);
      if (window.startError) return {ok:false,error:window.startError};
      return window.delayStart ? new Promise(resolve=>window.resolveStart=()=>resolve(task())) : task();
    },
    retry_balance_statistics: async payload => {
      window.testRetries.push(payload);
      return window.retryError ? {ok:false,error:window.retryError} : task();
    },
    choose_output_dir: async initial => {window.testChooserInitial=initial;return {ok:true,path:'D:/chosen'};},
    open_path: async path => {window.testOpened.push(path);return {ok:true};}
  }};
})()"""


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class BalanceStatisticsFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright unavailable")
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel="msedge")
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(ROOT / "frontend")))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def open(self, platform="temu", restore=False, setup=""):
        self.context.add_init_script(f"window.restoreFixture={'true' if restore else 'false'};\n" + MOCK_BRIDGE + "\n" + setup)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/{platform}-balance-statistics.html")
        expect(self.page.locator('[data-active-account-name][data-vendor="ziniao"]')).to_have_text("测试公司 · operator")
        self.page.wait_for_function("Boolean(window.testInfoPayload)")
        self.assertEqual(self.page.evaluate("window.testInfoPayload"), {"platform": platform})

    def test_temu_defaults_to_previous_month_and_sends_selected_month_with_account_id(self):
        self.open()
        expect(self.page.locator("#balanceMonth")).to_have_value("2026-08")
        expect(self.page.locator("#startBtn")).to_be_disabled()
        self.page.locator("#balanceMonth").fill("2026-07")
        self.page.locator("#storeNames").fill(" 测试店铺 A \n测试店铺 B\n测试店铺 A\n")
        expect(self.page.locator("#storeCount")).to_have_text("2")
        self.page.locator("#chooseOutputBtn").click()
        expect(self.page.locator("#balanceOutputDir")).to_have_value("D:/chosen")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#balanceMonth")).to_be_disabled()
        expect(self.page.locator("#storeNames")).to_be_disabled()
        expect(self.page.locator("#chooseOutputBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("window.testStarted"), [{
            "platform": "temu", "account_id": "ziniao-a", "month": "2026-07", "country": "AUTO",
            "store_names": "测试店铺 A\n测试店铺 B", "output_dir": "D:/chosen",
        }])
        self.assertEqual(self.errors, [])

    def test_lazada_supports_auto_and_all_six_sites_without_month_filter(self):
        self.open("lazada")
        expect(self.page.locator("#balanceMonth")).to_have_count(0)
        expect(self.page.locator("#balanceCountry")).to_have_value("AUTO")
        self.assertEqual(self.page.locator("#balanceCountry option").evaluate_all("options => options.map(option => option.value)"), ["AUTO", "PH", "MY", "TH", "ID", "VN", "SG"])
        self.page.locator("#balanceCountry").select_option("VN")
        self.page.locator("#storeNames").fill("越南店铺")
        self.page.locator("#startBtn").click()
        self.assertEqual(self.page.evaluate("window.testStarted"), [{
            "platform": "lazada", "account_id": "ziniao-a", "country": "VN", "store_names": "越南店铺", "output_dir": "D:/output",
        }])
        self.assertEqual(self.errors, [])

    def test_result_distinguishes_missing_from_zero_preserves_negative_and_escapes_content(self):
        self.open("lazada", restore=True)
        rows = self.page.locator("#storeResults tr")
        expect(rows).to_have_count(2)
        self.assertEqual(rows.nth(0).locator(".balance-amount").all_text_contents(), ["-123.40", "0", "0.00", "25.00"])
        self.assertEqual(rows.nth(1).locator(".balance-amount").all_text_contents(), ["未取得", "未取得", "0", "未取得"])
        expect(rows.nth(1)).to_contain_text("资金页加载失败")
        expect(self.page.locator("#storeResults img")).to_have_count(0)
        expect(rows.nth(1).locator("td").first).to_have_text("<img src=x onerror=alert(1)>")
        expect(self.page.locator("#taskBadge")).to_have_text("结果不完整")
        expect(self.page.locator("#attentionSummary")).to_have_text("部分 1 / 失败 0")
        expect(self.page.locator("#retryBtn")).to_be_enabled()
        self.page.locator("#openFileBtn").click()
        self.page.locator("#openDirectoryBtn").click()
        rows.nth(0).get_by_role("button").nth(0).click()
        self.assertEqual(self.page.evaluate("window.testOpened"), ["D:/results/余额统计.xlsx", "D:/results", "D:/results/a.png"])
        self.assertEqual(self.errors, [])

    def test_retry_uses_saved_manifest_and_keeps_result_when_start_fails(self):
        self.open(restore=True)
        expect(self.page.locator("#retryBtn")).to_be_enabled()
        self.page.evaluate("window.retryError='测试：已有任务运行中'")
        self.page.locator("#retryBtn").click()
        expect(self.page.locator("#startIssues")).to_have_text("测试：已有任务运行中")
        expect(self.page.locator("#retryBtn")).to_be_enabled()
        expect(self.page.locator("#storeResults tr")).to_have_count(2)
        expect(self.page.locator("#openFileBtn")).to_be_enabled()
        self.assertEqual(self.page.evaluate("window.testRetries"), [{
            "platform": "temu", "account_id": "ziniao-a", "manifest_path": "D:/results/manifest.json",
        }])
        self.assertEqual(self.errors, [])

    def test_confirmed_no_processing_amount_is_not_shown_as_failed_collection(self):
        self.open("lazada", restore=True, setup="""
          window.testResult.stores[0].values.processing = null;
          window.testResult.stores[0].evidence_exemptions = {processing:'withdrawal_success'};
          window.testResult.stores[0].notes = ['最新一笔提现已成功'];
          window.testResult.stores[1].values.processing = null;
          window.testResult.stores[1].evidence_exemptions = {processing:'no_withdrawal'};
        """)
        rows = self.page.locator("#storeResults tr")
        for index, reason in ((0, "最新一笔提现已成功"), (1, "当前余额流水日期范围内无提现记录")):
            cell = rows.nth(index).locator(".balance-amount").last
            expect(cell).to_have_text("无需记录")
            self.assertIn(reason, cell.get_attribute("title"))
            self.assertNotIn("balance-missing", cell.get_attribute("class"))
        expect(rows.nth(0)).to_contain_text("最新一笔提现已成功")
        self.assertEqual(self.errors, [])

    def test_temu_global_site_has_a_readable_label(self):
        self.open(restore=True, setup="window.testResult.stores[0].country='GLOBAL';window.testResult.stores[0].currency='USD';")
        expect(self.page.locator("#storeResults tr").first.locator("td").nth(1)).to_have_text("全球 / USD")
        self.assertEqual(self.errors, [])

    def test_account_change_discards_old_result_and_late_task_response(self):
        self.open(restore=True)
        expect(self.page.locator("#retryBtn")).to_be_enabled()
        self.page.locator("#storeNames").fill("测试店铺 A")
        self.page.evaluate("window.delayStart=true")
        self.page.locator("#startBtn").click()
        self.page.wait_for_function("typeof window.resolveStart === 'function'")
        self.page.evaluate("HQYL.applyAccountState({accounts:window.testAccounts,active_account_ids:{ziniao:'ziniao-b'}})")
        self.page.evaluate("window.resolveStart()")
        expect(self.page.locator("#taskBadge")).to_have_text("待运行")
        expect(self.page.locator("#resultMessage")).to_contain_text("已切换账号")
        expect(self.page.locator("#retryBtn")).to_be_disabled()
        expect(self.page.locator("#openFileBtn")).to_be_disabled()
        expect(self.page.locator("#recordCount")).to_have_text("0")
        self.assertEqual(self.errors, [])

    def test_restore_previous_month_and_visible_start_failure(self):
        self.open()
        self.page.locator("#balanceMonth").fill("2026-01")
        self.page.locator("#resetMonthBtn").click()
        expect(self.page.locator("#balanceMonth")).to_have_value("2026-08")
        self.page.locator("#storeNames").fill("测试店铺")
        self.page.evaluate("window.startError='店铺名称不存在'")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#startIssues")).to_have_text("店铺名称不存在")
        expect(self.page.locator("#startBtn")).to_be_enabled()
        expect(self.page.locator("#taskBadge")).to_have_text("启动失败")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()

"""Local browser fixtures only. These tests never connect to a seller or CLI account."""
from __future__ import annotations

import functools
import json
import os
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services.temu_on_sale_gateway import CAPTURE_START, PAGE_STATE, EXPORT_DIALOG, TemuOnSaleGateway
from backend.services.temu_on_sale_login import LOGIN_READY

try:
    from playwright.sync_api import Error, expect, sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
MOCK_BRIDGE = r"""(() => {
  const store = {store_id:'test-1',store_name:'测试店铺 A',platform_name:'TEMU'};
  window.testStarted = [];
  window.testAccounts = [{id:'ziniao-1',vendor:'ziniao',name:'测试公司',username:'operator',extra:{company:'测试公司'}},{id:'ziniao-2',vendor:'ziniao',name:'另一公司',username:'other',extra:{company:'另一公司'}}];
  window.testResult = {run_id:'fixture',profile:'测试授权账号',status:'incomplete',manifest_path:'fixture/manifest.json',output_dir:'fixture',output_file:'fixture/部分结果.xlsx',store_count:2,completed_count:1,success_count:1,no_data_count:0,failed_count:1,row_count:10,is_complete:false,can_retry:true,completion_message:'部分结果：1/2 个店铺完成',stores:[{...store,status:'success',raw_row_count:10,row_count:10,raw_file:'fixture/a.xlsx'},{store_id:'test-2',store_name:'测试店铺 B',status:'failed',error:'下载超时'}]};
  const task = () => ({ok:true,id:'test-task',tool:'temu_on_sale_export',status:'running',logs:[],context:{account_id:'ziniao-1',manifest_path:'fixture/manifest.json',profile:'测试授权账号'}});
  window.pywebview = {api:{
    get_app_info: async () => ({ok:true,app:{name:'测试平台',version:'dev'},settings:{output_dir:'D:/test-output'},account_state:{accounts:window.noAccountFixture ? [] : window.testAccounts,active_account_ids:{ziniao:window.noAccountFixture ? '' : 'ziniao-1'}}}),
    get_sidebar_preferences: async () => ({ok:true,preferences:null}),
    save_sidebar_preferences: async () => ({ok:true}),
    get_latest_task_status: async () => ({ok:false,empty:true}),
    get_temu_on_sale_export_info: async payload => ({ok:true,profile:payload.account_id === 'ziniao-1' ? '测试授权账号' : '其他账号',client_path:'C:/ziniao.exe',webdriver_path:'C:/drivers',latest:window.restoreFixture && payload.account_id === 'ziniao-1' ? window.testResult : null}),
    list_temu_on_sale_stores: async payload => { window.testListPayload=payload; const result={ok:true,profile:'测试授权账号',stores:[store,{store_id:'test-2',store_name:'测试店铺 B',platform_name:'TEMU'}]}; return window.delayStores ? new Promise(resolve=>window.resolveStores=()=>resolve(result)) : result; },
    preview_temu_on_sale_stores: async payload => ({ok:true,stores:[store],issues:[],can_start:true,preview_token:'server-token',profile:'测试授权账号'}),
    start_temu_on_sale_export: async payload => {
      window.testStarted.push(payload);
      if (window.useRealStart) return await window.testRealStart(payload);
      if (window.startError) return {ok:false,error:window.startError};
      return window.delayStart ? new Promise(resolve=>window.resolveStart=()=>resolve(task())) : task();
    },
    retry_temu_on_sale_export: async payload => {window.testRetry = payload;return task();},
    get_task_status: async () => window.testTerminal ? {...task(),status:'success',result:window.testResult} : task(),
    get_temu_on_sale_export_progress: async () => window.pauseProgress ? new Promise(resolve => {window.resumeProgress=resolve;}) : ({ok:true,result:window.testResult}),
    choose_output_dir: async () => ({ok:true,path:'D:/chosen'}),
    open_path: async path => {window.testOpened = path;return {ok:true};},
  }};
})()"""


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class TemuFrontendTests(unittest.TestCase):
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

    def open(self, restore=False):
        self.context.add_init_script(MOCK_BRIDGE)
        if restore:
            self.context.add_init_script("window.restoreFixture = true")
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/temu-on-sale-export.html")
        expect(self.page.locator('[data-active-account-name][data-vendor="ziniao"]')).to_have_text("测试公司 · operator")
        expect(self.page.locator("#clientPath")).to_have_value("C:/ziniao.exe")

    def test_names_start_directly_without_store_selection_or_preview(self):
        self.open()
        expect(self.page.locator("#startBtn")).to_be_disabled()
        expect(self.page.locator("#refreshStoresBtn, #selectionMode, #previewBtn, #storeCandidates")).to_have_count(0)
        self.page.locator("#storeNames").fill("测试店铺 A")
        expect(self.page.locator("#startBtn")).to_be_enabled()
        self.page.locator("#storeNames").fill("测试店铺 B")
        expect(self.page.locator("#startBtn")).to_be_enabled()
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#storeNames")).to_be_disabled()
        expect(self.page.locator("#chooseOutputBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("window.testStarted"), [{"account_id":"ziniao-1","client_path":"C:/ziniao.exe","webdriver_path":"C:/drivers","store_names":"测试店铺 B","output_dir":"D:/test-output"}])
        self.assertIsNone(self.page.evaluate("window.testListPayload || null"))
        self.assertEqual(self.errors, [])

    def test_partial_result_retry_restore_without_reentering_names(self):
        self.open(restore=True)
        expect(self.page.locator("#retryBtn")).to_be_enabled()
        expect(self.page.locator("#resultMessage")).to_contain_text("部分结果")
        self.page.locator("#openSummaryBtn").click()
        self.assertEqual(self.page.evaluate("window.testOpened"), "fixture/部分结果.xlsx")
        self.page.locator("#retryBtn").click()
        self.assertEqual(self.page.evaluate("window.testRetry"), {"account_id":"ziniao-1","client_path":"C:/ziniao.exe","webdriver_path":"C:/drivers","manifest_path":"fixture/manifest.json"})
        self.assertEqual(self.errors, [])

    def test_account_switch_does_not_accept_late_start_result_for_old_account(self):
        self.open(restore=True)
        self.page.locator("#storeNames").fill("测试店铺 A")
        self.page.evaluate("window.delayStart=true")
        self.page.locator("#startBtn").click()
        self.page.wait_for_function("typeof window.resolveStart === 'function'")
        self.page.evaluate("HQYL.applyAccountState({accounts:window.testAccounts,active_account_ids:{ziniao:'ziniao-2'}})")
        self.page.evaluate("window.resolveStart()")
        expect(self.page.locator("#retryBtn")).to_be_disabled()
        expect(self.page.locator("#recordCount")).to_have_text("0")
        expect(self.page.locator("#connectionNotice")).to_contain_text("账号已切换")
        self.assertEqual(self.page.evaluate("window.testStarted[0].account_id"), "ziniao-1")
        self.assertEqual(self.errors, [])

    def test_unbound_account_opens_existing_ziniao_login_dialog(self):
        self.context.add_init_script(MOCK_BRIDGE)
        self.context.add_init_script("window.noAccountFixture=true")
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/pages/temu-on-sale-export.html")
        self.page.locator("#storeNames").fill("测试店铺 A")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#quickAccountTitle")).to_have_text("绑定紫鸟账号")
        expect(self.page.locator("#quickCompanyField")).to_be_visible()
        self.assertEqual(self.page.evaluate("window.testStarted"), [])

    def test_start_error_shows_reason_and_name_edit_can_start_again(self):
        self.open()
        self.page.locator("#storeNames").fill("错误名称")
        self.page.evaluate("window.startError='错误名称：未匹配到完整店铺名'")
        self.page.locator("#startBtn").click()
        expect(self.page.locator("#startIssues")).to_contain_text("未匹配到完整店铺名")
        expect(self.page.locator("#startBtn")).to_be_enabled()
        expect(self.page.locator("#logBox")).to_contain_text("未匹配到完整店铺名")
        self.page.locator("#storeNames").fill("测试店铺 A")
        expect(self.page.locator("#startIssues")).to_be_hidden()
        self.page.evaluate("window.startError=null")
        self.page.locator("#startBtn").click()
        self.assertEqual([item['store_names'] for item in self.page.evaluate("window.testStarted")], ['错误名称','测试店铺 A'])
        self.assertEqual(self.errors, [])

    def test_blank_names_or_output_disable_start_without_preview_requirement(self):
        self.open()
        self.page.locator("#storeNames").fill("测试店铺 A")
        self.page.locator("#exportOutputDir").fill("")
        expect(self.page.locator("#startBtn")).to_be_disabled()
        self.page.locator("#exportOutputDir").fill("D:/output")
        expect(self.page.locator("#startBtn")).to_be_enabled()
        self.page.locator("#storeNames").fill(" ")
        expect(self.page.locator("#startBtn")).to_be_disabled()

    def test_browser_names_submit_reaches_real_bridge_without_preparation(self):
        from backend import app_bridge
        from backend.config_store import AppSettings, BoundAccount
        from backend.services import temu_on_sale_export as service
        with tempfile.TemporaryDirectory() as directory:
            account = BoundAccount('ziniao-1','ziniao','测试公司','operator','fixture-password',{'company':'测试公司'})
            settings = AppSettings(accounts=[account], active_account_ids={'ziniao':account.id}, output_dir=directory)
            config = SimpleNamespace(local_data_dir=Path(directory), load=lambda:settings)
            with patch.object(app_bridge, 'ConfigStore', return_value=config):
                bridge = app_bridge.AppBridge()
            browser = Mock()
            browser.list_stores.return_value = [
                {'store_id':'11','store_name':'测试店铺 A','platform_name':'TEMU'},
                {'store_id':'22','store_name':'测试店铺 B','platform_name':'TEMU'}]
            bridge.tasks = Mock()
            bridge.tasks.start.side_effect = lambda title,runner,**kw: {
                'ok':True,'id':'actual-bridge-task','tool':kw['tool'],'context':kw['context'],
                'status':'running','logs':[]}
            self.page.expose_function('testRealStart', bridge.start_temu_on_sale_export)
            self.open()
            self.page.evaluate('window.useRealStart=true')
            self.page.locator('#storeNames').fill('测试店铺 B\n测试店铺 A\n测试店铺 B')
            self.page.locator('#exportOutputDir').fill(directory)
            expect(self.page.locator('#startBtn')).to_be_enabled()
            if os.environ.get('HQYL_TEMU_SCREENSHOT'):
                output = Path(os.environ['HQYL_TEMU_SCREENSHOT'])
                output.parent.mkdir(parents=True,exist_ok=True)
                self.page.screenshot(path=str(output),full_page=True)
            with patch.object(app_bridge, 'ZiniaoBrowser') as factory:
                factory.return_value.__enter__.return_value = browser
                self.page.locator('#startBtn').click()
                expect(self.page.locator('#connectionNotice')).to_contain_text('任务已启动')
            browser.list_stores.assert_called_once()
            browser.ready.assert_called_once()
            bridge.tasks.start.assert_called_once()
            manifest = bridge.tasks.start.call_args.kwargs['context']['manifest_path']
            self.assertEqual([item['store_id'] for item in service.load_batch(manifest)['stores']], ['22','11'])
            self.assertEqual(self.errors, [])
    def test_dom_unique_controls_selected_tab_and_filter_guard(self):
        self.page.set_content('''<main><nav><a href="#">商品列表</a></nav><section>
          <input placeholder="商品名称" value=""><select><option>全部</option><option>泰国站</option></select>
          <button>重置</button><button>查询</button></section>
          <div role="tab" aria-selected="true"><span>在售中 12</span></div>
          <button><span>下载查询结果12</span></button></main>''')
        state = json.loads(self.page.evaluate(PAGE_STATE))
        self.assertEqual(len(state["found"]["下载查询结果"]), 1)
        self.assertTrue(state["found"]["在售中"][0]["selected"])
        self.assertEqual(state["filters"][1]["text"], "全部")
        self.page.locator("select").select_option(label="泰国站")
        state = json.loads(self.page.evaluate(PAGE_STATE))
        self.assertEqual(state["filters"][1]["text"], "泰国站")

    def test_late_progress_cannot_replace_final_result(self):
        self.open()
        self.page.locator("#storeNames").fill("测试店铺 A")
        self.page.evaluate("window.pauseProgress = true")
        self.page.locator("#startBtn").click()
        self.page.wait_for_function("typeof window.resumeProgress === 'function'")
        self.page.evaluate("window.testResult = {...window.testResult,status:'complete',is_complete:true,can_retry:false}; window.testTerminal = true")
        expect(self.page.locator("#taskBadge")).to_have_text("已完成")
        self.page.evaluate("window.resumeProgress({ok:true,result:{...window.testResult,status:'running',is_complete:false,row_count:0}})")
        expect(self.page.locator("#outputFile")).to_have_text("汇总已生成")
        expect(self.page.locator("#recordCount")).to_have_text("10")

    def test_browser_file_capture_does_not_use_historical_downloads(self):
        self.page.set_content("<p>local export fixture</p>")
        self.page.evaluate("URL.createObjectURL(new Blob([new Uint8Array([80,75,3,4,99])]))")
        self.page.evaluate(CAPTURE_START.replace("__KEY__", '"fixtureCapture"'))
        self.page.evaluate("URL.createObjectURL(new Blob([new Uint8Array([80,75,3,4,10,20,30])]))")
        self.page.wait_for_function("window.fixtureCapture.files.length === 1")
        self.assertEqual(self.page.evaluate("[...window.fixtureCapture.files[0].bytes]"), [80,75,3,4,10,20,30])

    def test_ziniao_autofill_readiness_returns_only_booleans(self):
        self.page.set_content('<input placeholder="手机号码" value="fixture-account"><input type="password" value="fixture-secret">')
        result=self.page.evaluate('() => {'+LOGIN_READY+'}')
        self.assertEqual(result,{'filled':True,'challenge':False})
        self.assertNotIn('fixture-secret',json.dumps(result))
        self.page.locator('input[type=password]').fill('')
        self.assertFalse(self.page.evaluate('() => {'+LOGIN_READY+'}')['filled'])
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div role=dialog>安全验证</div>')")
        self.assertTrue(self.page.evaluate('() => {'+LOGIN_READY+'}')['challenge'])

    def test_observed_custom_todo_modal_locates_only_its_close_control(self):
        self.page.set_content('''<div data-testid="beast-core-modal-container">
          <div data-testid="beast-core-modal-inner"><div data-testid="beast-core-modal-body">
            <div class="modal-todo-alert_container__fixture"><div class="modal-todo-alert_title__fixture">您有以下待办任务需要尽快处理</div><button>去处理</button></div>
          </div></div><button id="todo-close" data-testid="beast-core-modal-icon-close">关闭</button>
        </div><button id="unrelated" data-testid="beast-core-modal-icon-close">其他关闭</button>''')
        state=json.loads(self.page.evaluate(EXPORT_DIALOG))
        self.assertEqual(len(state['notices']),1)
        self.assertEqual(self.page.locator(state['notices'][0]).get_attribute('id'),'todo-close')

    def test_observed_beast_menu_tabs_and_selected_site_tags(self):
        self.page.set_content('''<div class="account-info_mallInfo__fixture">herefn</div>
          <a href="#"><span><style>.fixture { color: black; }</style>商品管理</span></a>
          <div class="use-quick-field_tsx_card__fixture">热销款</div>
          <form><div data-testid="beast-core-select-header">
            <input readonly data-testid="beast-core-select-htmlInput" value="SKC">
          </div><div data-testid="beast-core-select-header" id="site">
            <input data-testid="beast-core-select-htmlInput" value="">
          </div><button>重置</button></form>
          <div data-testid="beast-core-tab-itemLabel-wrapper" class="TAB_active_5-123-0">
            <div data-testid="beast-core-tab-itemLabel">在售中 650</div></div>
          <button>下载查询结果650</button>''')
        state = json.loads(self.page.evaluate(PAGE_STATE))
        self.assertEqual(len(state["found"]["商品管理"]), 1)
        self.assertEqual(state["sellers"], ["herefn"])
        self.assertTrue(TemuOnSaleGateway.query_ready(state))
        self.page.locator("#site").evaluate("e => { const span=document.createElement('span'); span.textContent='泰国站'; e.append(span); }")
        self.assertFalse(TemuOnSaleGateway.filters_clear(json.loads(self.page.evaluate(PAGE_STATE))))


if __name__ == "__main__":
    unittest.main()

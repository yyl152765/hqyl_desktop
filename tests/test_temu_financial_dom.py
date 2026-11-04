"""Browser fixtures reproduce the supplied screenshots; no real store is used."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import SkipTest, TestCase
from unittest.mock import patch

from backend.services import temu_financial_gateway as gateway
from backend.services.temu_fee_refund_controls import _script
from tests.test_temu_financial_workbook import make_workbook


HTML = '''<!doctype html><meta charset="utf-8"><style>[hidden]{display:none!important} label{display:inline-block;margin:8px}</style>
<div class="account-info_mallInfo__fixture">Seller</div><div>账户资金</div><a>对账中心</a>
<div id="filters"><label><span>日期选择</span><input data-testid="beast-core-rangePicker-htmlInput" value="2026-10-01 ~ 2026-10-31"></label>
<label><span>资金流向</span><select><option>全部</option></select></label>
<label><span>账务类型</span><select><option>全部</option></select></label>
<label>收支金额(USD)<input><input></label>
<button id="query" onclick="setTimeout(()=>document.querySelector('td').textContent=String(++window.queries),20)">查询</button><button>重置</button></div>
<table><tbody><tr><td>旧数据</td></tr></tbody></table>
<button onclick="document.getElementById('modal').hidden=false">导出</button>
<button onclick="document.getElementById('history').hidden=false">导出历史</button>
<div role="dialog" id="modal" hidden><h2>导出</h2><div>导出内容</div>
<label><input type="radio" name="content" id="listOnly" checked><span>导出列表</span></label>
<label><input type="radio" name="content" id="withDetails"><span>导出列表 + 账务详情</span></label>
<button id="confirm">确认</button><button>取消</button></div>
<div role="dialog" id="history" hidden><h2>导出历史</h2><button>刷新</button>
<button aria-label="关闭" onclick="document.getElementById('history').hidden=true">×</button><div id="cards"></div></div>
<script>
window.queries=0;
function now(){let d=new Date(),p=x=>String(x).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());}
function card(stamp,id){return `<article><div><div>账务明细生成时间 ${stamp}</div><div>账务明细查询时间: 2026-10-01 00:00:00 ~ 2026-10-31 23:59:59</div><div>账务类型: 全部</div><div>资金流向: --</div><div>导出内容: 列表数据、财务明细</div></div><aside><button>下载账务明细(卖家中心)</button><a id="${id}">下载财务明细(全球)</a><button>下载财务明细(欧区)</button><button>下载财务明细(美国)</button></aside></article>`;}
document.getElementById('cards').innerHTML=card('2026-10-01 08:00:00','old-global');
document.getElementById('confirm').onclick=()=>{if(!document.getElementById('withDetails').checked)throw Error('wrong radio');document.getElementById('modal').hidden=true;document.getElementById('cards').insertAdjacentHTML('afterbegin',card(now(),'new-global'));};
</script>'''


class DOMBrowser:
    profile = 'p'
    auth_failed = False
    cleanup_failed = False

    def __init__(self, page):
        self.page, self.closed = page, False

    def open_store(self, *_, **__):
        pass

    def navigate(self, _, url):
        self.page.goto(url)

    def evaluate(self, _, script):
        try:
            result = self.page.evaluate(script)
        except Exception as exc:
            if 'Execution context was destroyed' in str(exc):
                raise gateway.ZiniaoPageNavigatingError('店铺页面正在跳转') from None
            raise
        return json.loads(result) if isinstance(result, str) else result

    def click(self, _, selector):
        self.page.locator(selector).click()

    def close_store(self, _):
        self.closed = True


class FinancialDOMTests(TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SkipTest('Playwright unavailable')
        executable = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        if not executable.is_file():
            raise SkipTest('Local Edge unavailable')
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=str(executable), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.route('https://agentseller.temu.com/**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
        self.page.route('https://seller.kuajingmaihuo.com/**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
        self.page.goto(gateway.BASE_URL)
        self.transport = DOMBrowser(self.page)

    def test_screenshot_workflow_uses_combined_radio_and_new_global_card(self):
        store = {'store_id': '1', 'store_name': '示例-Seller', 'platform_name': 'TEMU'}
        instance = gateway.TemuFinancialGateway('p', browser=self.transport, timeout=2)
        instance.current_stores = {'1': store}
        with TemporaryDirectory() as temporary:
            def downloaded(browser, identity, selector, directory, **_):
                self.assertEqual(self.page.locator(selector).get_attribute('id'), 'new-global')
                return make_workbook(Path(directory) / 'global.xlsx', {'明细': [['订单号'], ['001']]})
            with patch.object(gateway, 'download_xlsx', side_effect=downloaded) as download:
                result = instance.export(store, '2026-10-01', '2026-10-31', Path(temporary))
            self.assertEqual(result['evidence']['download_label'], gateway.GLOBAL_DOWNLOAD)
            download.assert_called_once()
        self.assertEqual(self.page.evaluate('window.queries'), 1)
        self.assertTrue(self.transport.closed)

    def test_closed_or_offscreen_history_is_not_treated_as_visible(self):
        self.page.evaluate("document.getElementById('history').hidden=false")
        state = self.transport.evaluate('1', _script(gateway.HISTORY_STATE))
        self.assertTrue(state['open'])
        self.assertEqual(len(state['rows']), 1)
        self.assertEqual(len(state['rows'][0]['downloads']), 4)
        self.page.evaluate("document.getElementById('history').style.transform='translateX(4000px)'")
        self.assertFalse(self.transport.evaluate('1', _script(gateway.HISTORY_STATE))['open'])

    def test_seller_follower_count_and_sidebar_history_do_not_confuse_controls(self):
        self.page.evaluate('''() => {
          document.querySelector('.account-info_mallInfo__fixture').innerHTML='Seller<div>12 人关注</div>';
          document.body.insertAdjacentHTML('afterbegin','<a href="/labor/reconciliation/export-history">导出历史</a>');
        }''')
        state = self.transport.evaluate('1', _script(gateway.PAGE_STATE, ['导出历史']))
        self.assertEqual(state['sellers'], ['Seller'])
        self.assertEqual(len(state['found']['导出历史']), 1)
        self.assertEqual(self.page.locator(state['found']['导出历史'][0]['selector']).evaluate('(e)=>e.tagName'), 'BUTTON')
        self.page.goto(gateway.RECONCILIATION_URL)
        instance = gateway.TemuFinancialGateway('p', browser=self.transport)
        self.assertEqual(instance.state('1')['sellers'], ['Seller'])

    def test_reconciliation_waits_for_name_in_the_initially_empty_visible_container(self):
        loading = HTML.replace('>Seller</div>', '><div class="account-info_mallName__fixture"></div></div>', 1)
        loading += '''<script>setTimeout(() => {
          document.querySelector('.account-info_mallName__fixture').textContent='Seller';
        }, 500);</script>'''
        self.page.route(gateway.RECONCILIATION_URL, lambda route: route.fulfill(body=loading, content_type='text/html'))
        instance = gateway.TemuFinancialGateway('p', browser=self.transport)
        seller, state = instance._open_reconciliation('1', {'store_name': '示例-Seller'})
        self.assertEqual(seller, 'Seller')
        self.assertEqual(state['sellers'], ['Seller'])
        self.assertEqual(self.page.evaluate('window.queries'), 0)

    def test_empty_identity_then_login_redirect_reports_expired_session(self):
        loading = HTML.replace('>Seller</div>', '><div class="account-info_mallName__fixture"></div></div>', 1)
        loading += '<script>setTimeout(() => { location.href="/login"; }, 500);</script>'
        self.page.route(gateway.RECONCILIATION_URL, lambda route: route.fulfill(body=loading, content_type='text/html'))
        instance = gateway.TemuFinancialGateway('p', browser=self.transport)
        with self.assertRaisesRegex(ValueError, '登录已失效'):
            instance._open_reconciliation('1', {'store_name': '示例-Seller'})

    def test_seller_before_financial_form_waits_without_toggling_sidebar(self):
        loading = HTML.replace('<div id="filters">', '<div id="filters" hidden>', 1)
        loading += '''<script>setTimeout(() => {
          document.getElementById('filters').hidden=false;
        }, 700);</script>'''
        self.page.route(gateway.RECONCILIATION_URL, lambda route: route.fulfill(body=loading, content_type='text/html'))
        instance = gateway.TemuFinancialGateway('p', browser=self.transport)
        with patch.object(self.transport, 'click', wraps=self.transport.click) as clicked:
            seller, state = instance._open_reconciliation('1', {'store_name': '示例-Seller'})
        self.assertEqual(seller, 'Seller')
        self.assertTrue(instance._looks_reconciliation(state))
        clicked.assert_not_called()

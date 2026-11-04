"""Full-page Balance evidence exposes DOM geometry and restores user layout."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend.services import lazada_balance_view as view
from test_temu_balance_overlays import SeleniumFixtureDriver
from test_balance_evidence import Identity, rendered_window
from backend.services.balance_evidence import capture_balance_page_evidence
from PIL import Image

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = """<!doctype html><html><head><style>
body{margin:0;font:16px Arial;color:#111;background:white}
header{height:70px;background:#ddd}main{margin-left:180px;padding:24px;width:900px}
[data-spm]{height:160px;background:#eee}.cap-type{margin-top:70px;height:80px}
table{width:100%;border-collapse:collapse}td,th{border:1px solid #ddd;height:54px}
.next-pagination{height:60px;padding-top:12px}
#unknown-cover{position:fixed;inset:0;background:#6668;z-index:100}
</style></head><body><header>Seller Center</header><main>
<div data-spm="total_balance_card"><h2>总计余额</h2><span>PHP 1234.56</span></div>
<div class="cap-type"><button>提现</button><input value="2026-04-29"><input value="2026-10-29"></div>
<div id="table-wrapper"><table><thead><tr><th>序号</th><th>时间</th><th>类型</th><th>方式</th><th>金额</th><th>状态</th></tr></thead>
<tbody></tbody></table></div><div class="next-pagination">1 2 3 共 30 条</div>
</main><script>
document.querySelector('tbody').innerHTML=Array.from({length:10},(_,i)=>'<tr><td>'+(i+1)+'</td><td>28 Oct 2026</td><td>提现</td><td>自动提现</td><td>-100.00</td><td>提现成功</td></tr>').join('');
window.clicks=0;document.addEventListener('click',()=>window.clicks++);
</script></body></html>"""


class LazadaBalanceViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest('Playwright unavailable')
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel='msedge')
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 800})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content(FIXTURE)
        self.driver = SeleniumFixtureDriver(self.page)

    def test_total_all_ten_rows_and_pagination_remain_at_original_scale(self):
        before = self.page.locator('body').inner_text()
        with view.balance_page_view(self.driver, timeout=1):
            self.assertTrue(self.driver.execute_script(view.VIEW_CHECK))
            self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '1')
            self.assertGreater(self.page.evaluate("document.querySelector('.next-pagination').getBoundingClientRect().bottom"), 800)
            self.assertEqual(self.page.locator('tbody tr').count(), 10)
            self.assertEqual(self.page.locator('body').inner_text(), before)
        self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '')
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_clipped_scroll_container_is_expanded_then_restored_even_if_capture_raises(self):
        self.page.evaluate("""()=>{
          document.documentElement.style.setProperty('zoom','0.9','important');
          const table=document.querySelector('#table-wrapper');
          table.style.cssText='height:180px;max-height:180px;overflow-y:auto';table.scrollTop=120;
        }""")
        before = self.page.locator('#table-wrapper').get_attribute('style')
        with self.assertRaisesRegex(RuntimeError, 'capture failure'):
            with view.balance_page_view(self.driver, timeout=1):
                self.assertTrue(self.driver.execute_script(view.VIEW_CHECK))
                self.assertEqual(self.page.evaluate("document.querySelector('#table-wrapper').scrollTop"), 0)
                raise RuntimeError('capture failure')
        self.assertEqual(self.page.locator('#table-wrapper').get_attribute('style'), before)
        self.assertEqual(self.page.evaluate("document.querySelector('#table-wrapper').scrollTop"), 120)
        self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '0.9')
        self.assertEqual(self.page.evaluate("document.documentElement.style.getPropertyPriority('zoom')"), 'important')

    def test_unknown_cover_rejects_picture_and_restores_zoom(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=unknown-cover>登录确认</div>')")
        with self.assertRaisesRegex(view.LazadaBalanceViewError, '被遮挡、裁剪'):
            with view.balance_page_view(self.driver, timeout=.4):
                self.fail('Covered balance must not be captured')
        self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '')
        self.assertEqual(self.page.locator('#unknown-cover').count(), 1)
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_horizontal_table_clip_is_expanded_and_restored(self):
        self.page.evaluate("""()=>{
          const wrapper=document.querySelector('#table-wrapper');
          wrapper.style.cssText='width:400px;max-width:400px;overflow-x:auto';
          document.querySelector('table').style.minWidth='900px';wrapper.scrollLeft=100;
        }""")
        before = self.page.locator('#table-wrapper').get_attribute('style')
        with view.balance_page_view(self.driver, timeout=1):
            self.assertTrue(self.driver.execute_script(view.VIEW_CHECK))
            self.assertTrue(self.page.evaluate("""()=>{
              const wrapper=document.querySelector('#table-wrapper').getBoundingClientRect();
              const table=document.querySelector('table').getBoundingClientRect();
              return table.left>=wrapper.left&&table.right<=wrapper.right;
            }"""))
        self.assertEqual(self.page.locator('#table-wrapper').get_attribute('style'), before)
        self.assertEqual(self.page.evaluate("document.querySelector('#table-wrapper').scrollLeft"), 100)

    def test_explicit_empty_table_can_be_included(self):
        self.page.locator('tbody').evaluate("e=>e.innerHTML='<tr><td colspan=6>暂无记录</td></tr>'")
        with view.balance_page_view(self.driver, timeout=1):
            self.assertTrue(self.driver.execute_script(view.VIEW_CHECK))
            self.assertIn('暂无记录', self.page.locator('table').inner_text())

    def test_ambiguous_table_is_rejected_without_layout_change(self):
        self.page.evaluate("document.querySelector('main').append(document.querySelector('table').cloneNode(true))")
        with self.assertRaisesRegex(view.LazadaBalanceViewError, '唯一确认'):
            with view.balance_page_view(self.driver, timeout=.4):
                self.fail('Ambiguous table must not be captured')
        self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '')

    def test_fixed_document_hidden_overflow_rejects_cdp_clipped_table(self):
        self.page.evaluate("""()=>{
          document.body.style.cssText='height:800px;overflow:hidden';
          document.documentElement.style.cssText='height:800px;overflow:hidden';
        }""")
        session = self.context.new_cdp_session(self.page)
        self.addCleanup(session.detach)
        self.assertEqual(session.send('Page.getLayoutMetrics')['cssContentSize']['height'], 800)
        self.assertGreater(self.page.evaluate("document.querySelector('table').getBoundingClientRect().bottom"), 800)
        self.assertFalse(self.driver.execute_script(view.VIEW_CHECK))
        with self.assertRaisesRegex(view.LazadaBalanceViewError, '被遮挡、裁剪'):
            with view.balance_page_view(self.driver, timeout=.4):
                self.fail('Document overflow clipping must not produce full-page evidence')
        self.assertEqual(self.page.evaluate('document.documentElement.style.zoom'), '')

    def test_real_browser_full_page_capture_exceeds_viewport_at_original_scale(self):
        session = self.context.new_cdp_session(self.page)
        self.addCleanup(session.detach)
        self.driver.execute_cdp_cmd = session.send
        self.driver.current_url = 'https://sellercenter.lazada.co.th/apps/balance'
        self.driver.current_window_handle = 'fixture-tab'
        identity = Identity()
        api = Mock()
        api.find_windows.return_value = [identity]
        api.identity.return_value = identity
        api.rect.return_value = SimpleNamespace(width=800, height=600)
        api.placement.return_value = SimpleNamespace(show_cmd=1)
        self.page.evaluate('window.scrollTo(0,500)')
        with tempfile.TemporaryDirectory() as folder:
            with view.balance_page_view(self.driver, timeout=1):
                height = self.page.evaluate('document.documentElement.scrollHeight')
                output = capture_balance_page_evidence(
                    self.driver, Path(folder) / 'balance.png', identity.title, 'Balance',
                    window_api=api, capture_window=lambda *args: rendered_window())
            with Image.open(output) as screenshot:
                self.assertEqual(screenshot.size, (1440, height))
                self.assertGreater(screenshot.height, 800)
        self.assertGreater(self.page.evaluate('scrollY'), 0)


if __name__ == '__main__':
    unittest.main()

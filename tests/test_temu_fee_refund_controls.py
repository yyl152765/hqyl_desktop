from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch
from datetime import date

from backend.services import temu_fee_refund_controls as controls
from backend.core.ziniao_browser import ZiniaoClickInterceptedError, ZiniaoBrowserError
from test_temu_balance_calendar import FIXTURE, Error, sync_playwright


class BrowserFixture:
    def __init__(self, page):
        self.page = page

    def evaluate(self, store_id, script):
        return json.loads(self.page.evaluate(script))

    def click(self, store_id, selector):
        self.page.locator(selector).click()


class FeeRefundControlsTests(unittest.TestCase):
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
        self.context = self.browser.new_context(viewport={'width': 1800, 'height': 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        # Only the requested field owns the range input. A second background
        # range must not confuse the export drawer's labelled control.
        self.page.set_content(FIXTURE)
        self.page.evaluate('''() => {
          const input=document.querySelector('input');
          const field=document.createElement('div');
          input.before(field);field.innerHTML='<label>售后单创建日期</label>';field.append(input);
          const other=document.createElement('div');other.innerHTML='<label>售后时间</label><input data-testid="beast-core-rangePicker-htmlInput" value="2026-01-01 ~ 2026-01-31">';document.body.append(other);
        }''')
        self.transport = BrowserFixture(self.page)

    def set_range(self, start, end, **kwargs):
        return controls.set_range(self.transport, 'store', date.fromisoformat(start), date.fromisoformat(end), label='售后单创建日期', timeout=.6, **kwargs)

    def test_arbitrary_cross_year_range_and_replaced_calendar_nodes(self):
        result = self.set_range('2025-12-21', '2026-02-06')
        self.assertEqual(result, '2025-12-21 ~ 2026-02-06')
        self.assertEqual(self.page.evaluate('window.clicks'), ['2025-12-21', '2026-02-06'])
        self.assertIsNone(self.page.evaluate('window.queryClicks'))
        self.assertEqual(self.page.locator('input').last.input_value(), '2026-01-01 ~ 2026-01-31')

    def test_footer_confirmation_is_clicked_before_readback(self):
        self.page.evaluate('''() => {
          const input=document.querySelector('input');const original=window.pick;
          window.pick=(y,m,d)=>{
            const old=input.value, second=Boolean(window.startDate);original(y,m,d);
            if(second){const next=input.value;input.value=old;
              const root=document.querySelector('[data-testid="beast-core-rangePicker-dropdown-contentRoot"]');root.hidden=false;
              const button=document.createElement('button');button.textContent='确认';
              button.onclick=()=>{window.confirmed=true;input.value=next;root.hidden=true};root.append(button);
            }
          };
        }''')
        self.assertEqual(self.set_range('2026-09-01', '2026-09-30'), '2026-09-01 ~ 2026-09-30')
        self.assertTrue(self.page.evaluate('window.confirmed'))

    def test_disabled_day_and_ambiguous_field_fail(self):
        self.page.evaluate('window.disabledDay=30')
        with self.assertRaisesRegex(ValueError, '不可用'):
            self.set_range('2026-09-01', '2026-09-30')
        self.page.evaluate('''() => {const field=document.createElement('div');field.innerHTML='<label>售后单创建日期</label><input data-testid="beast-core-rangePicker-htmlInput">';document.body.append(field)}''')
        with self.assertRaisesRegex(ValueError, '唯一定位'):
            controls.field_state(self.transport, 'store', '售后单创建日期')

    def test_dropdown_exact_value_ignores_similar_values(self):
        self.page.set_content('''<div><label>售后单状态</label><div data-testid="beast-core-select-header" onclick="document.querySelector('[role=listbox]').hidden=false">退款中</div></div>
        <div role="listbox" hidden><div role="option" onclick="document.querySelector('[data-testid=beast-core-select-header]').textContent='已退款';this.parentElement.hidden=true">已退款</div><div role="option">部分已退款</div></div>''')
        self.assertEqual(controls.select_field(self.transport, 'store', '售后单状态', '已退款', timeout=.5), '已退款')
        # An exact match does not open the dropdown or submit another action.
        self.assertEqual(controls.select_field(self.transport, 'store', '售后单状态', '已退款', timeout=.5), '已退款')
        self.assertTrue(self.page.locator('[role=listbox]').is_hidden())

    def test_only_known_message_panel_is_dismissed(self):
        self.page.set_content('''<div class="new-bell_header__x"><div>全部消息</div><svg data-testid="beast-core-icon-close" style="height:20px;width:20px" onclick="this.parentElement.remove()"></svg></div><button onclick="window.bad=true">去处理</button>''')
        self.assertTrue(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
        self.assertFalse(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
        self.assertIsNone(self.page.evaluate('window.bad'))

    def test_message_html_toggle_is_preferred_and_never_reopens_panel(self):
        self.page.set_content('''<div onclick="window.toggleCount=(window.toggleCount||0)+1;const p=document.querySelector('.new-bell_header__x');p.hidden=!p.hidden"><span>消息</span></div>
        <div class="new-bell_header__x"><div>全部消息</div><svg data-testid="beast-core-icon-close" style="height:20px;width:20px" onclick="window.bad=true"></svg></div>''')
        self.assertTrue(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
        self.assertFalse(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
        self.assertEqual(self.page.evaluate('window.toggleCount'), 1)
        self.assertIsNone(self.page.evaluate('window.bad'))

    def test_dropdown_reads_input_value_instead_of_mirrored_placeholder(self):
        self.page.set_content('''<div><label>售后单类型</label><div data-testid="beast-core-select-header"><input readonly value="退货退款"><span>请选择</span></div></div>''')
        self.assertEqual(controls.select_field(self.transport, 'store', '售后单类型', '退货退款', timeout=.5), '退货退款')
        self.page.locator('input').evaluate("e=>e.value=''")
        self.assertEqual(controls.field_state(self.transport, 'store', '售后单类型', 'select')['value'], '')

    def test_message_panel_covering_toolbar_uses_its_own_close(self):
        self.page.set_content('''<button style="position:fixed;right:10px;top:10px" onclick="window.bad=true">消息</button>
        <div style="position:fixed;inset:0;z-index:10;background:white">
          <div class="new-bell_header__x"><div>全部消息</div>
          <svg data-testid="beast-core-icon-close" style="width:24px;height:24px" onclick="this.parentElement.parentElement.remove()"></svg></div>
        </div>''')
        self.assertTrue(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
        self.assertIsNone(self.page.evaluate('window.bad'))
        self.assertFalse(controls.dismiss_overlays(self.transport, 'store', timeout=.5))

    def test_mounted_closed_drawer_is_not_toggled_open(self):
        for style in ('transform:translateX(2000px)', 'opacity:0'):
            with self.subTest(style=style):
                self.page.set_content(f'''<button onclick="window.bad=true">消息</button>
                <div style="{style}"><div class="new-bell_header__x"><div>全部消息</div>
                <svg data-testid="beast-core-icon-close" style="width:24px;height:24px"></svg></div></div>''')
                self.assertFalse(controls.dismiss_overlays(self.transport, 'store', timeout=.5))
                self.assertIsNone(self.page.evaluate('window.bad'))


class SafeClickTests(unittest.TestCase):
    def test_late_panel_retries_only_an_intercepted_click_with_fresh_selector(self):
        browser = Mock()
        browser.click.side_effect = [ZiniaoClickInterceptedError('blocked'), None]
        selector = Mock(side_effect=['#old', '#fresh'])
        with patch.object(controls, 'dismiss_overlays', side_effect=[False, True]):
            controls.click_control(browser, 'store', selector)
        self.assertEqual([call.args for call in browser.click.call_args_list], [('store', '#old'), ('store', '#fresh')])

    def test_unknown_click_result_is_never_repeated(self):
        browser = Mock()
        browser.click.side_effect = ZiniaoBrowserError('connection lost after click')
        with patch.object(controls, 'dismiss_overlays', return_value=False):
            with self.assertRaises(ZiniaoBrowserError):
                controls.click_control(browser, 'store', '#export')
        browser.click.assert_called_once()

    def test_unknown_blocker_is_not_bypassed(self):
        browser = Mock()
        browser.click.side_effect = ZiniaoClickInterceptedError('unknown modal')
        with patch.object(controls, 'dismiss_overlays', return_value=False):
            with self.assertRaises(ZiniaoClickInterceptedError):
                controls.click_control(browser, 'store', '#export')
        browser.click.assert_called_once()

"""Exercise the observed TEMU message-panel DOM using real browser input."""
from __future__ import annotations

import importlib
import unittest

from selenium.common.exceptions import ElementClickInterceptedException
from backend.services import temu_balance_calendar as calendar_helper
from test_temu_balance_calendar import FIXTURE as CALENDAR_FIXTURE

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


# Structural selectors match the real panel observed on 2026-09-30. Fixture-only
# attributes wire its UI behavior and counters without affecting production DOM.
PANEL_HTML = r"""
<div class="PP_popover_5-120-1" data-fixture-panel>
  <div class="PP_popoverTitle_5-120-1">
    <div class="new-bell_header__sABhU">
      <div class="new-bell_headerBox__fixture">
        <svg data-testid="beast-core-icon-bellIcon"></svg>
        <div>全部消息</div><span>99+</span>
        <svg data-testid="beast-core-icon-right"></svg>
      </div>
      <svg data-testid="beast-core-icon-close" data-fixture-close viewBox="0 0 24 24">
        <path d="M4 4L20 20M20 4L4 20" stroke="black"/>
      </svg>
    </div>
  </div>
  <div class="new-bell_messageItem__fixture"><button data-fixture-message>去处理</button></div>
  <div class="new-bell_messageFooter__fixture"><button data-fixture-message>查看详情</button><button data-fixture-message>标记已读</button></div>
</div>
"""

FIXTURE = r"""
<!doctype html><html><head><style>
body{font:16px Arial}[hidden]{display:none!important}
#action{position:fixed;right:80px;top:150px;width:120px;height:40px}
[data-fixture-panel],#unknown-overlay{position:fixed;right:0;top:0;width:500px;height:500px;background:white;z-index:100}
[data-fixture-close]{position:absolute;right:16px;top:16px;width:24px;height:24px}
</style></head><body>
<button id="action">重置</button><button id="unrelated-close">其他窗口关闭</button>
<script>
window.fixtureClicks={close:0,message:0,action:0,unrelated:0,trusted:[]};
if(document.querySelector('#action'))document.querySelector('#action').onclick=()=>window.fixtureClicks.action++;
if(document.querySelector('#unrelated-close'))document.querySelector('#unrelated-close').onclick=()=>window.fixtureClicks.unrelated++;
window.openPanel=html=>{
  document.body.insertAdjacentHTML('beforeend',html);
  document.querySelectorAll('[data-fixture-close]').forEach(close=>close.onclick=event=>{
    window.fixtureClicks.close++;
    window.fixtureClicks.trusted.push(event.isTrusted);
    event.currentTarget.closest('[data-fixture-panel]').remove();
  });
  document.querySelectorAll('[data-fixture-message]').forEach(button=>button.onclick=()=>window.fixtureClicks.message++);
};
</script></body></html>
"""


class Element:
    def __init__(self, locator):
        self.locator = locator

    def is_displayed(self):
        return self.locator.is_visible()

    def is_enabled(self):
        return self.locator.is_enabled()

    def get_attribute(self, name):
        return self.locator.get_attribute(name)

    def click(self):
        # Selenium reports a covered click immediately. Playwright otherwise
        # waits for the obstruction to disappear, hiding the retry condition.
        obstruction = self.locator.evaluate("""element=>{
          const rect=element.getBoundingClientRect();
          const top=document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2);
          return !top || !(top===element || element.contains(top));
        }""")
        if obstruction:
            raise ElementClickInterceptedException("Local DOM target is covered")
        self.locator.click()


class SeleniumFixtureDriver:
    def __init__(self, page):
        self.page = page
        self.executed_scripts = []
        self.after_execute = None

    def find_elements(self, by, selector):
        locator = self.page.locator(selector)
        return [Element(locator.nth(index)) for index in range(locator.count())]

    def execute_script(self, script, *args):
        self.executed_scripts.append(script)
        def pack_arg(value):
            if isinstance(value, Element):
                reference = value.locator.evaluate("""element=>{
                  if(!element.dataset.fixtureRef)element.dataset.fixtureRef='r'+(window.fixtureSequence=(window.fixtureSequence||0)+1);
                  return element.dataset.fixtureRef;
                }""")
                return {"__element": reference}
            return value

        value = self.page.evaluate(r"""payload=>{
          const args=payload.args.map(value=>value && value.__element
            ? document.querySelector('[data-fixture-ref="'+value.__element+'"]') : value);
          const result=Function(payload.script).apply(null,args);
          const pack=value=>{
            if(value instanceof Element){
              if(!value.dataset.fixtureRef)value.dataset.fixtureRef='r'+(window.fixtureSequence=(window.fixtureSequence||0)+1);
              return {__element:value.dataset.fixtureRef};
            }
            if(Array.isArray(value))return value.map(pack);
            if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,pack(item)]));
            return value;
          };
          return pack(result);
        }""", {"script": script, "args": [pack_arg(value) for value in args]})

        def unpack(value):
            if isinstance(value, dict):
                if "__element" in value:
                    return Element(self.page.locator(f'[data-fixture-ref="{value["__element"]}"]'))
                return {key: unpack(item) for key, item in value.items()}
            if isinstance(value, list):
                return [unpack(item) for item in value]
            return value

        result = unpack(value)
        if self.after_execute:
            self.after_execute(script)
        return result


class TemuBalanceOverlaysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright unavailable")
        cls.helper = importlib.import_module("backend.services.temu_balance_overlays")
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel="msedge")
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1800, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(2000)
        self.page.set_content(FIXTURE)
        self.driver = SeleniumFixtureDriver(self.page)

    def open_panel(self):
        self.page.evaluate("window.openPanel", PANEL_HTML)

    def counts(self):
        return self.page.evaluate("window.fixtureClicks")

    def setup_query(self, mode='changed'):
        style = self.page.locator('style').text_content()
        behavior = self.page.locator('script').text_content()
        self.page.set_content(CALENDAR_FIXTURE)
        self.page.add_style_tag(content=style + '\n#query{position:fixed;right:80px;top:150px;width:120px;height:40px}')
        self.page.evaluate(behavior)
        self.page.evaluate('mode=>window.queryMode=mode', mode)

    def assert_no_observers(self):
        self.assertEqual(self.page.evaluate("Object.keys(window).filter(key=>key.startsWith('__hqyl_balance_refresh_'))"), [])

    def test_absent_panel_leaves_other_close_controls_untouched(self):
        self.assertFalse(self.helper.dismiss_temu_message_panel(self.driver, timeout=.2))
        self.assertEqual(self.counts(), {"close": 0, "message": 0, "action": 0, "unrelated": 0, "trusted": []})

    def test_known_panel_closes_through_native_click_only(self):
        self.open_panel()
        self.assertTrue(self.helper.dismiss_temu_message_panel(self.driver, timeout=.4))
        self.assertEqual(self.page.locator('[data-fixture-panel]').count(), 0)
        self.assertEqual(self.counts(), {"close": 1, "message": 0, "action": 0, "unrelated": 0, "trusted": [True]})

    def test_duplicate_close_buttons_fail_without_clicking(self):
        self.open_panel()
        self.page.evaluate("""()=>{
          const close=document.querySelector('[data-fixture-close]');
          close.parentElement.append(close.cloneNode(true));
        }""")
        with self.assertRaisesRegex(ValueError, 'TEMU 消息面板关闭按钮无法唯一确认'):
            self.helper.dismiss_temu_message_panel(self.driver, timeout=.2)
        self.assertEqual(self.counts()['close'], 0)
        self.assertEqual(self.page.locator('[data-fixture-panel]').count(), 1)

    def test_other_title_and_nested_close_are_not_used(self):
        self.open_panel()
        self.page.evaluate("""()=>{
          const label=[...document.querySelectorAll('div')].find(node=>node.textContent==='全部消息');
          label.textContent='其他消息';
        }""")
        self.assertFalse(self.helper.dismiss_temu_message_panel(self.driver, timeout=.2))
        self.assertEqual(self.counts()['close'], 0)
        self.page.set_content(FIXTURE)
        self.open_panel()
        self.page.evaluate("""()=>{
          const close=document.querySelector('[data-fixture-close]');
          const wrapper=document.createElement('div');
          close.replaceWith(wrapper);wrapper.append(close);
        }""")
        with self.assertRaisesRegex(ValueError, 'TEMU 消息面板关闭按钮无法唯一确认'):
            self.helper.dismiss_temu_message_panel(self.driver, timeout=.2)
        self.assertEqual(self.counts()['close'], 0)

    def test_existing_panel_closes_before_action_and_preserves_result(self):
        self.open_panel()

        def action():
            self.driver.find_elements('css selector', '#action')[0].click()
            return 'completed'

        self.assertEqual(self.helper.click_with_message_panel_retry(self.driver, action), 'completed')
        self.assertEqual(self.counts(), {"close": 1, "message": 0, "action": 1, "unrelated": 0, "trusted": [True]})

    def test_late_panel_interception_closes_and_retries_action_once(self):
        attempts = []

        def action():
            attempts.append(True)
            if len(attempts) == 1:
                self.open_panel()
            self.driver.find_elements('css selector', '#action')[0].click()

        self.helper.click_with_message_panel_retry(self.driver, action)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.counts(), {"close": 1, "message": 0, "action": 1, "unrelated": 0, "trusted": [True]})

    def test_new_panel_on_second_action_is_not_retried_again(self):
        attempts = []

        def action():
            attempts.append(True)
            self.open_panel()
            self.driver.find_elements('css selector', '#action')[0].click()

        with self.assertRaises(ElementClickInterceptedException):
            self.helper.click_with_message_panel_retry(self.driver, action)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.counts(), {"close": 1, "message": 0, "action": 0, "unrelated": 0, "trusted": [True]})

    def test_unknown_cover_is_not_dismissed_or_retried(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=unknown-overlay><button>关闭</button></div>')")
        attempts = []

        def action():
            attempts.append(True)
            self.driver.find_elements('css selector', '#action')[0].click()

        with self.assertRaises(ElementClickInterceptedException):
            self.helper.click_with_message_panel_retry(self.driver, action)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.counts()['close'], 0)
        self.assertEqual(self.page.locator('#unknown-overlay').count(), 1)

    def test_other_action_errors_are_not_retried(self):
        attempts = []

        def action():
            attempts.append(True)
            raise RuntimeError('unrelated action failure')

        with self.assertRaisesRegex(RuntimeError, 'unrelated action failure'):
            self.helper.click_with_message_panel_retry(self.driver, action)
        self.assertEqual(len(attempts), 1)

    def test_query_late_panel_reinstalls_observer_and_sends_one_native_query(self):
        self.setup_query()
        installations = []

        def after_execute(script):
            if script == calendar_helper.REFRESH_INSTALL:
                installations.append(True)
                if len(installations) == 1:
                    self.open_panel()

        self.driver.after_execute = after_execute
        result = calendar_helper.query_and_wait_for_refresh(self.driver, '#summary', timeout=.8, settle_ms=50)
        self.assertEqual(result['text'], '0.00')
        self.assertEqual(self.page.evaluate('window.queryClicks'), 1)
        self.assertEqual(len(installations), 2)
        relevant = [script for script in self.driver.executed_scripts
                    if script in (calendar_helper.REFRESH_INSTALL, calendar_helper.REFRESH_CLEANUP)]
        self.assertEqual(relevant, [calendar_helper.REFRESH_CLEANUP, calendar_helper.REFRESH_INSTALL,
                                   calendar_helper.REFRESH_CLEANUP, calendar_helper.REFRESH_INSTALL,
                                   calendar_helper.REFRESH_CLEANUP])
        self.assertEqual(self.counts()['trusted'], [True])
        self.assertEqual(self.counts()['message'], 0)
        self.assert_no_observers()

    def test_query_unknown_cover_never_submits_and_cleans_observer(self):
        self.setup_query()
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=unknown-overlay><button>关闭</button></div>')")
        with self.assertRaises(ElementClickInterceptedException):
            calendar_helper.query_and_wait_for_refresh(self.driver, '#summary', timeout=.3, settle_ms=50)
        self.assertIsNone(self.page.evaluate('window.queryClicks'))
        self.assertEqual(self.driver.executed_scripts.count(calendar_helper.REFRESH_INSTALL), 1)
        self.assertEqual(self.counts()['close'], 0)
        self.assert_no_observers()

    def test_query_already_sent_but_silent_is_not_submitted_again(self):
        self.setup_query('silent')
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, '拒绝使用筛选前金额'):
            calendar_helper.query_and_wait_for_refresh(self.driver, '#summary', timeout=.3, settle_ms=50)
        self.assertEqual(self.page.evaluate('window.queryClicks'), 1)
        self.assertEqual(self.driver.executed_scripts.count(calendar_helper.REFRESH_INSTALL), 1)
        self.assert_no_observers()


if __name__ == '__main__':
    unittest.main()

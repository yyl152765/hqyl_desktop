"""Exercise the calendar and DOM observer against local interactive fixtures."""
from __future__ import annotations

import unittest

from backend.services import temu_balance_calendar as calendar_helper

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = r"""
<!doctype html><html><head><style>
body{font:16px Arial} [hidden]{display:none!important} td{padding:3px} svg{width:24px;height:24px;display:inline-block;background:#ccd}
.panes{display:flex;gap:20px} .RPR_tableWrapper{display:block} .outOfMonth{color:#999}
</style></head><body>
<input data-testid="beast-core-rangePicker-htmlInput" readonly value="2026-08-29 ~ 2026-09-28">
<div data-testid="beast-core-rangePicker-dropdown-contentRoot" hidden></div>
<button id="query">查询</button><section id="summary"><span id="amount">1270.99</span></section><div id="other"></div>
<script>
(() => {
window.baseYear=2026;window.baseMonth=8;window.startDate='';window.queryMode='changed';window.clicks=[];
const input=document.querySelector('input');const dropdown=document.querySelector('[data-testid="beast-core-rangePicker-dropdown-contentRoot"]');
const summary=document.querySelector('#summary');
function monthAt(offset){return new Date(window.baseYear,window.baseMonth-1+offset,1)}
function render(){
  dropdown.innerHTML='<div class="panes">'+[0,1].map(index=>{
    const date=monthAt(index);const year=date.getFullYear();const month=date.getMonth()+1;
    const count=new Date(year,month,0).getDate();
    return '<div><div data-testid="beast-core-monthRangePicker-year-header">'
      +'<svg data-testid="beast-core-icon-left" onclick="shift(-1)"></svg>'
      +'<input readonly value="'+year+'年"><span class="RPR_dateText_x">'+month+'月</span>'
      +'<svg data-testid="beast-core-icon-right" onclick="shift(1)"></svg></div>'
      +'<div class="RPR_tableWrapper_x"><table><tbody><tr><td class="outOfMonth"><div title="1">1</div></td>'
      +Array.from({length:count},(_,i)=>{const day=i+1;return '<td'+(window.disabledDay===day ? ' class="disabled" aria-disabled="true"' : '')+'><div title="'+day+'" onclick="pick('+year+','+month+','+day+')">'+day+'</div></td>'}).join('')
      +'</tr></tbody></table></div></div>'
  }).join('')+'</div>';
}
window.shift=function(delta){if(window.ignoreNavigation)return;const d=monthAt(delta);window.baseYear=d.getFullYear();window.baseMonth=d.getMonth()+1;render()};
window.pick=function(y,m,d){
  const value=y+'-'+String(m).padStart(2,'0')+'-'+String(d).padStart(2,'0');window.clicks.push(value);
  if(!window.startDate){window.startDate=value;render();return;}
  input.value=window.startDate+' ~ '+value;window.startDate='';dropdown.hidden=true;
};
input.addEventListener('click',()=>{dropdown.hidden=false;render()});
document.querySelector('#query').addEventListener('click',()=>{
  window.queryClicks=(window.queryClicks||0)+1;
  const mode=window.queryMode;
  if(mode==='silent')return;
  if(mode==='unrelated'){document.querySelector('#other').textContent='unrelated change';summary.className='selected';return;}
  if(mode==='rerender_same'){summary.innerHTML='<span id="amount">1270.99</span>';return;}
  summary.setAttribute('aria-busy','true');
  if(mode==='never_complete')return;
  setTimeout(()=>{
    if(mode==='error'){const alert=document.createElement('div');alert.role='alert';alert.textContent='查询失败，请重试';document.body.append(alert)}
    if(mode==='range_change')input.value='2026-07-01 ~ 2026-07-31';
    if(!['same_loading','error'].includes(mode))document.querySelector('#amount').textContent='0.00';
    summary.removeAttribute('aria-busy');
  },70);
});
})();
</script></body></html>
"""


class Element:
    def __init__(self, locator):
        self.locator = locator

    def is_displayed(self):
        return self.locator.is_visible()

    def get_attribute(self, name):
        return self.locator.input_value() if name == "value" else self.locator.get_attribute(name)

    def click(self):
        self.locator.click()


class SeleniumFixtureDriver:
    """Use the real DOM while adapting only Selenium's transport interface."""
    def __init__(self, page):
        self.page = page

    def find_elements(self, by, selector):
        return [Element(self.page.locator(selector).nth(index)) for index in range(self.page.locator(selector).count())]

    def execute_script(self, script, *args):
        value = self.page.evaluate(r"""payload=>{
          const result=Function(payload.script).apply(null,payload.args);
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
        }""", {"script": script, "args": list(args)})

        def unpack(value):
            if isinstance(value, dict):
                if "__element" in value:
                    return Element(self.page.locator(f'[data-fixture-ref="{value["__element"]}"]'))
                return {key: unpack(item) for key, item in value.items()}
            if isinstance(value, list):
                return [unpack(item) for item in value]
            return value

        return unpack(value)


class TemuBalanceCalendarTests(unittest.TestCase):
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

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1800, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(2000)
        self.page.set_content(FIXTURE)
        self.driver = SeleniumFixtureDriver(self.page)

    def query(self, mode, **kwargs):
        self.page.evaluate("mode=>window.queryMode=mode", mode)
        return calendar_helper.query_and_wait_for_refresh(self.driver, "#summary", timeout=.7, settle_ms=50, **kwargs)

    def test_selects_entire_month_ignoring_out_of_month_day_and_stale_elements(self):
        result = calendar_helper.set_calendar_month(self.driver, "2026-08", timeout=.4)
        self.assertEqual(result, {"month": "2026-08", "start_date": "2026-08-01", "end_date": "2026-08-31", "range_value": "2026-08-01 ~ 2026-08-31"})
        self.assertEqual(self.page.evaluate("window.clicks"), ["2026-08-01", "2026-08-31"])
        self.assertIsNone(self.page.evaluate("window.queryClicks"))

    def test_cross_year_navigation_and_leap_month_end(self):
        for month, end in (("2025-12", "31"), ("2024-02", "29"), ("2027-01", "31")):
            with self.subTest(month=month):
                self.page.set_content(FIXTURE)
                result = calendar_helper.set_calendar_month(self.driver, month, timeout=.5)
                self.assertEqual(result["end_date"], f"{month}-{end}")

    def test_disabled_month_end_and_failed_navigation_do_not_claim_success(self):
        self.page.evaluate("window.disabledDay=31")
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "禁用"):
            calendar_helper.set_calendar_month(self.driver, "2026-08", timeout=.25)
        self.page.set_content(FIXTURE)
        self.page.evaluate("window.ignoreNavigation=true")
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "未刷新年月"):
            calendar_helper.set_calendar_month(self.driver, "2025-12", timeout=.2)

    def test_changed_summary_is_returned_only_after_refresh_settles(self):
        result = self.query("changed")
        self.assertEqual(result["text"], "0.00")
        self.assertTrue(result["changed"])
        self.assertTrue(result["saw_loading"])
        self.assertGreater(result["mutations"], 0)
        self.assertEqual(self.page.evaluate("Object.keys(window).filter(key=>key.startsWith('__hqyl_balance_refresh_'))"), [])

    def test_same_value_requires_observed_loading_or_summary_replacement(self):
        for mode in ("same_loading", "rerender_same"):
            with self.subTest(mode=mode):
                self.page.set_content(FIXTURE)
                result = self.query(mode)
                self.assertEqual(result["text"], "1270.99")
                self.assertFalse(result["changed"])
                self.assertTrue(result["saw_loading"] or result["mutations"])

    def test_silent_query_unrelated_mutations_and_never_completed_loading_reject_old_value(self):
        for mode in ("silent", "unrelated", "never_complete"):
            with self.subTest(mode=mode):
                self.page.set_content(FIXTURE)
                with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "拒绝使用筛选前金额"):
                    self.query(mode)
                self.assertEqual(self.page.evaluate("Object.keys(window).filter(key=>key.startsWith('__hqyl_balance_refresh_'))"), [])

    def test_visible_query_error_or_changed_filter_prevents_stale_success(self):
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "查询返回错误"):
            self.query("error")
        self.page.set_content(FIXTURE)
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "日期范围发生变化"):
            self.query("range_change", expected_range="2026-08-29 ~ 2026-09-28")

    def test_ambiguous_panel_or_range_control_and_existing_loading_are_rejected(self):
        self.page.evaluate("document.body.append(document.querySelector('#summary').cloneNode(true))")
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "唯一确认"):
            self.query("changed")
        self.page.set_content(FIXTURE)
        self.page.evaluate("document.querySelector('#summary').setAttribute('aria-busy','true')")
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "仍在加载"):
            self.query("changed")
        self.assertIsNone(self.page.evaluate("window.queryClicks"))
        self.page.set_content(FIXTURE)
        self.page.evaluate("document.body.append(document.querySelector('input').cloneNode())")
        with self.assertRaisesRegex(calendar_helper.TemuCalendarError, "唯一确认"):
            calendar_helper.set_calendar_month(self.driver, "2026-08")


if __name__ == "__main__":
    unittest.main()

"""Lazada language selection on local documents with actual native reloads."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit
from selenium.common.exceptions import JavascriptException

from backend.services import lazada_balance_language as language
from test_temu_balance_overlays import SeleniumFixtureDriver

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
[hidden]{display:none!important}body{font:16px Arial}ul{padding:0;list-style:none}
#language_position{width:160px}li{padding:8px;cursor:pointer}
.a-l-language-popup-container{width:160px;border:1px solid gray}
#cover{position:fixed;inset:0;background:#9999;z-index:1000}
</style></head><body>
<ul id="language_position" role="listbox" class="a-l-language-selector"><li aria-haspopup="true" aria-expanded="false"></li></ul>
<ul role="menu" class="a-l-language-popup-container" hidden>
<li role="option" title="English" data-spm="d_lang_en">English</li>
<li role="option" title="Tiếng Việt" data-spm="d_lang_vi">Tiếng Việt</li>
<li role="option" title="简体中文" data-spm="d_lang_zh_cn"><span class="a-l-menu-item-text" aria-selected="false">简体中文</span></li>
</ul>
<div>菜单帮助示例：简体中文</div><button id="business">立即充值</button>
<script>
const options=__OPTIONS__;
const locale=__LOCALE__;
const root=document.querySelector('#language_position');
const trigger=root.querySelector('li');
const menu=document.querySelector('[role=menu]');
const label=locale==='zh_CN'?'简体中文':locale==='en_US'?'English':'Tiếng Việt';
root.dataset.more=locale;trigger.title=label;trigger.textContent=label;
if(options.noEntry){trigger.remove();root.textContent=label}
if(options.noChinese)menu.querySelector('[data-spm=d_lang_zh_cn]').remove();
if(options.duplicateRoot)document.body.append(root.cloneNode(true));
if(options.duplicateChinese)menu.append(menu.querySelector('[data-spm=d_lang_zh_cn]').cloneNode(true));
trigger.onclick=async event=>{
  await window.recordLanguageAction('trigger',event.isTrusted);
  trigger.setAttribute('aria-expanded','true');menu.hidden=false;
};
document.querySelector('#business').onclick=event=>window.recordLanguageAction('business',event.isTrusted);
const chinese=menu.querySelector('[data-spm=d_lang_zh_cn]');
if(chinese)chinese.onclick=async event=>{
  await window.recordLanguageAction('chinese',event.isTrusted);
  if(options.mode==='silent')return;
  if(options.mode==='no_reload'){
    root.dataset.more='zh_CN';trigger.title='简体中文';trigger.textContent='简体中文';menu.hidden=true;return;
  }
  const host=options.mode==='wrong_host'?'https://sellercenter.lazada.com.ph':location.origin;
  const path=options.mode==='wrong_path'?'/apps/balance/unexpected':location.pathname;
  location.href=host+path+'?locale=zh_CN'+location.hash;
};
if(options.cover){const cover=document.createElement('div');cover.id='cover';document.body.append(cover)}
</script></body></html>
"""

LOGIN_FIXTURE = r"""
<!doctype html><html><head><meta charset="utf-8"><style>
[hidden]{display:none!important}li,button{padding:12px}ul{width:160px;padding:0;list-style:none}
</style></head><body>
<button data-spm="d_switch_lang_btn" aria-haspopup="true"><span class="next-btn-helper"></span><span class="next-btn-helper"> </span></button>
<ul class="next-menu next-ver next-menu-selectable-single" role="listbox" aria-multiselectable="false" hidden>
<li role="option" title="English"><span class="next-menu-item-text" aria-selected="false">English</span></li>
<li role="option" title="Tiếng Việt"><span class="next-menu-item-text" aria-selected="false">Tiếng Việt</span></li>
<li role="option" title="简体中文"><span class="next-menu-item-text" aria-selected="false">简体中文</span></li>
</ul>
<form><input name="account" placeholder="Email"><input name="password" type="password"><button type="submit">登录</button></form>
<script>
const options=__OPTIONS__,locale=__LOCALE__;
const trigger=document.querySelector('[data-spm=d_switch_lang_btn]'),menu=document.querySelector('ul');
const label=locale==='zh_CN'?'简体中文':locale==='en_US'?'English':'Tiếng Việt';
trigger.querySelector('span').textContent=label;
if(options.duplicateLabel)trigger.querySelectorAll('span')[1].textContent='English';
document.querySelector('[name=password]').placeholder=locale==='zh_CN'?'密码':'Password';
menu.querySelector('[title="'+label+'"] span').setAttribute('aria-selected','true');
menu.querySelector('[title="'+label+'"]').classList.add('next-selected');
if(location.hostname.startsWith('gsp.'))menu.querySelector('[title="Tiếng Việt"]').remove();
if(options.noEntry)trigger.remove();
if(options.duplicateRoot)document.body.append(trigger.cloneNode(true));
if(options.duplicateChinese)menu.append(menu.querySelector('[title="简体中文"]').cloneNode(true));
if(options.noChinese)menu.querySelector('[title="简体中文"]').remove();
trigger.onclick=async event=>{await recordLanguageAction('trigger',event.isTrusted);menu.hidden=false};
const chinese=menu.querySelector('[title="简体中文"]');
if(chinese)chinese.onclick=async event=>{
  await recordLanguageAction('chinese',event.isTrusted);
  if(options.mode==='silent')return;
  location.href=location.origin+location.pathname+'?locale=zh_CN';
};
document.querySelector('form').onsubmit=event=>{event.preventDefault();recordLanguageAction('login',event.isTrusted)};
</script></body></html>
"""


class Driver(SeleniumFixtureDriver):
    @property
    def current_url(self):
        return self.page.url

    def execute_script(self, script, *args):
        try:
            return super().execute_script(script, *args)
        except Error as exc:
            if 'Execution context was destroyed' in str(exc):
                raise JavascriptException('document navigation in progress') from None
            raise


class LazadaBalanceLanguageTests(unittest.TestCase):
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
        self.context = self.browser.new_context(viewport={'width': 1500, 'height': 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.actions = []
        self.options = {}
        self.page.expose_binding('recordLanguageAction', lambda _source, name, trusted: self.actions.append((name, trusted)))

        def fulfill(route):
            parsed = urlsplit(route.request.url)
            locale = parse_qs(parsed.query).get('locale', ['vi_VN'])[0]
            fixture = LOGIN_FIXTURE if parsed.path in {'/apps/seller/login', '/page/login'} else FIXTURE
            body = fixture.replace('__OPTIONS__', json.dumps(self.options)).replace('__LOCALE__', json.dumps(locale))
            route.fulfill(status=200, content_type='text/html', body=body)

        # Every request is fulfilled locally; no live Seller Center is contacted.
        self.page.route('**/*', fulfill)
        self.driver = Driver(self.page)

    def open(self, locale='vi_VN', path='/apps/balance', **options):
        self.options = options
        self.page.goto('https://sellercenter.lazada.vn' + path + '?locale=' + locale)

    def ensure(self, timeout=1.5):
        return language.ensure_lazada_chinese(self.driver, timeout=timeout)

    def test_vietnamese_menu_native_selection_requires_real_reload(self):
        self.open()
        before = self.page.evaluate('performance.timeOrigin')
        result = self.ensure()
        self.assertEqual(result, {'language': 'zh_CN', 'changed': True, 'fallback': False, 'reason': ''})
        self.assertNotEqual(self.page.evaluate('performance.timeOrigin'), before)
        self.assertEqual(self.actions, [('trigger', True), ('chinese', True)])
        self.assertEqual(self.page.locator('#language_position').get_attribute('data-more'), 'zh_CN')

    def test_already_chinese_is_idempotent_even_when_ads_html_lang_is_english(self):
        self.open('zh_CN', '/sponsor/solutions/ads/productads/')
        self.assertEqual(self.page.locator('html').get_attribute('lang'), 'en')
        self.assertFalse(self.ensure()['changed'])
        self.assertEqual(self.ensure()['language'], 'zh_CN')
        self.assertEqual(self.actions, [])

    def test_english_switches_when_chinese_option_is_available(self):
        self.open('en_US')
        self.assertEqual(self.ensure()['language'], 'zh_CN')
        self.assertEqual(self.actions, [('trigger', True), ('chinese', True)])

    def test_explicit_english_can_fallback_only_when_entry_or_chinese_is_missing(self):
        for options in ({'noEntry': True}, {'noChinese': True}):
            with self.subTest(options=options):
                self.actions.clear()
                self.open('en_US', **options)
                result = self.ensure()
                self.assertEqual(result['language'], 'en')
                self.assertTrue(result['fallback'])
                self.assertFalse(result['changed'])
                self.assertTrue(result['reason'])
                self.assertNotIn(('chinese', True), self.actions)

    def test_unknown_language_cannot_fallback_or_use_body_chinese_text(self):
        for options in ({'noEntry': True}, {'noChinese': True}):
            with self.subTest(options=options):
                self.actions.clear()
                self.open(**options)
                with self.assertRaises(language.LazadaLanguageError):
                    self.ensure()
                self.assertFalse(any(name in {'chinese', 'business'} for name, _ in self.actions))

    def test_duplicate_current_control_or_chinese_option_is_rejected(self):
        for options in ({'duplicateRoot': True}, {'duplicateChinese': True}):
            with self.subTest(options=options):
                self.actions.clear()
                self.open(**options)
                with self.assertRaisesRegex(language.LazadaLanguageError, '唯一确认'):
                    self.ensure()
                self.assertFalse(any(name in {'chinese', 'business'} for name, _ in self.actions))

    def test_covered_language_control_never_uses_coordinate_click(self):
        self.open(cover=True)
        with self.assertRaisesRegex(language.LazadaLanguageError, '遮挡'):
            self.ensure()
        self.assertEqual(self.actions, [])

    def test_clicked_chinese_with_no_reload_or_no_effect_never_falls_back_to_english(self):
        for mode in ('silent', 'no_reload'):
            with self.subTest(mode=mode):
                self.actions.clear()
                self.open('en_US', mode=mode)
                with self.assertRaisesRegex(language.LazadaLanguageError, '未确认页面重载'):
                    self.ensure(timeout=.5)
                self.assertEqual(self.actions, [('trigger', True), ('chinese', True)])

    def test_language_reload_cannot_change_country_or_business_route(self):
        for mode in ('wrong_host', 'wrong_path'):
            with self.subTest(mode=mode):
                self.actions.clear()
                self.open(mode=mode)
                with self.assertRaisesRegex(language.LazadaLanguageError, '站点|受信任'):
                    self.ensure()
                self.assertFalse(any(name == 'business' for name, _ in self.actions))

    def test_unknown_host_is_rejected_before_any_menu_click(self):
        self.page.goto('https://sellercenter.lazada.vn.example.invalid/apps/balance')
        with self.assertRaisesRegex(language.LazadaLanguageError, '受信任'):
            self.ensure()
        self.assertEqual(self.actions, [])

    def test_seller_and_gsp_login_menus_reload_before_confirming_chinese(self):
        for host, path, locale in (('sellercenter.lazada.vn', '/apps/seller/login', 'vi_VN'),
                                   ('gsp.lazada-seller.cn', '/page/login', 'en_US'),
                                   ('gsp.lazada.com', '/page/login', 'en_US')):
            with self.subTest(host=host):
                self.actions.clear()
                self.page.goto(f'https://{host}{path}?locale={locale}')
                before = self.page.evaluate('performance.timeOrigin')
                result = language.ensure_lazada_login_chinese(self.driver, timeout=2)
                self.assertTrue(result['changed'])
                self.assertNotEqual(before, self.page.evaluate('performance.timeOrigin'))
                self.assertEqual(self.page.locator('.next-btn-helper').first.inner_text(), '简体中文')
                self.assertEqual(self.page.locator('.next-btn-helper').count(), 2)
                self.assertEqual(self.page.locator('[name=password]').get_attribute('placeholder'), '密码')
                self.assertEqual(self.actions, [('trigger', True), ('chinese', True)])
                self.assertFalse(language.ensure_lazada_login_chinese(self.driver)['changed'])

    def test_legacy_login_without_known_language_entry_keeps_form_untouched(self):
        self.options = {'noEntry': True}
        self.page.goto('https://gsp.lazada-seller.cn/page/login?locale=en_US')
        self.assertIsNone(language.ensure_lazada_login_chinese(self.driver))
        self.assertEqual(self.actions, [])

    def test_login_language_duplicates_reject_and_silent_selection_cannot_fallback(self):
        for options, message in (({'duplicateRoot': True}, '唯一确认'),
                                  ({'duplicateLabel': True}, '当前语言标签无法唯一确认'),
                                  ({'duplicateChinese': True}, '唯一确认'),
                                  ({'mode': 'silent'}, '未确认页面重载')):
            with self.subTest(options=options):
                self.actions.clear()
                self.options = options
                self.page.goto('https://gsp.lazada-seller.cn/page/login?locale=en_US')
                with self.assertRaisesRegex(language.LazadaLanguageError, message):
                    language.ensure_lazada_login_chinese(self.driver, timeout=.6)
                self.assertFalse(any(name == 'login' for name, _ in self.actions))


if __name__ == '__main__':
    unittest.main()

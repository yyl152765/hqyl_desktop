"""Store-scoped tab transitions and login failures, without a real browser."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.services import lazada_balance_login as login

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


class _Element:
    def __init__(self, text, click, href="", *, enabled=True):
        self.text, self._click, self.href, self.enabled = text, click, href, enabled

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def click(self):
        self._click()


class _Driver:
    def __init__(self, url):
        self.urls = {"original": url}
        self.current_window_handle = "original"
        self.switch_to = SimpleNamespace(window=self._switch)
        self.gets, self.scripts, self.clicks = [], [], []
        self.feedback = {"rejected": False, "challenge": False}
        self.filled = True
        self.on_get = None
        self.on_submit = lambda: self.urls.update({self.current_window_handle: "https://sellercenter.lazada.vn/"})
        self.on_link = lambda: None
        self.href = "https://sellercenter.lazada.vn/apps/seller/login"

    @property
    def window_handles(self):
        return list(self.urls)

    @property
    def current_url(self):
        return self.urls[self.current_window_handle]

    def _switch(self, handle):
        if handle not in self.urls:
            raise RuntimeError("closed")
        self.current_window_handle = handle

    def get(self, url):
        self.gets.append((self.current_window_handle, url))
        self.urls[self.current_window_handle] = url
        if self.on_get:
            self.on_get(url)

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if script == login.LOGIN_STATUS_SCRIPT:
            return dict(self.feedback)
        if script == login.PREFILLED_STATE_SCRIPT:
            return {"filled": self.filled() if callable(self.filled) else self.filled}
        raise AssertionError("unexpected script")

    def find_elements(self, by, selector):
        if selector.startswith("a["):
            def click_link():
                self.clicks.append((self.current_window_handle, "link"))
                self.on_link()
            return [_Element("登录", click_link, self.href)]
        def click_login():
            self.clicks.append((self.current_window_handle, "login"))
            self.on_submit()
        return [_Element("Login", click_login)]


class LazadaBalanceLoginTests(unittest.TestCase):
    target = "https://sellercenter.lazada.vn/portal/apps/finance/myIncome/index"

    def setUp(self):
        self.now = 0.0
        self.patch_clock = patch.object(login.time, "monotonic", side_effect=lambda: self.now)
        self.patch_sleep = patch.object(login.time, "sleep", side_effect=self._advance)
        self.patch_clock.start()
        self.patch_sleep.start()
        language = patch.object(login, 'ensure_lazada_login_chinese', return_value=None)
        self.language = language.start()
        self.addCleanup(language.stop)
        self.addCleanup(self.patch_clock.stop)
        self.addCleanup(self.patch_sleep.stop)

    def _advance(self, value):
        self.now += value

    def ensure(self, driver, state=None, target=None):
        return login.ensure_lazada_page(driver, target or self.target, state if state is not None else {}, timeout=1)

    def test_all_six_sites_get_requested_path_without_reading_credentials(self):
        for host in login.SELLER_HOSTS:
            with self.subTest(host=host):
                target = f"https://{host}/apps/balance"
                driver = _Driver(f"https://{host}/")
                self.assertEqual(self.ensure(driver, target=target), "original")
                self.assertEqual(driver.gets, [("original", target)])
                self.assertFalse(driver.scripts)
                self.assertFalse(driver.clicks)

    def test_exact_finance_tab_selected_without_reusing_stale_login(self):
        driver = _Driver("https://gsp.lazada.com/page/login")
        driver.urls["finance"] = self.target
        self.assertEqual(self.ensure(driver), "finance")
        self.assertEqual(driver.current_window_handle, "finance")
        self.assertFalse(driver.scripts)

    def test_same_domain_home_is_not_finance_success(self):
        driver = _Driver("https://sellercenter.lazada.vn/")
        driver.on_get = lambda _: driver.urls.update({"original": "https://sellercenter.lazada.vn/"})
        with self.assertRaisesRegex(login.LazadaLoginError, "指定资金页面"):
            self.ensure(driver)
        self.assertFalse(driver.clicks)

    def test_register_login_opens_new_tab_then_explicitly_returns_to_finance(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/register/index")
        driver.on_link = lambda: driver.urls.update({"login": driver.href})
        state = {}
        self.assertEqual(self.ensure(driver, state), "login")
        self.assertEqual(driver.clicks, [("original", "link"), ("login", "login")])
        self.assertEqual(driver.gets, [("login", self.target)])
        self.assertTrue(state["attempted"])
        self.assertEqual(state["business_handle"], "login")

    def test_gsp_both_origins_saved_login_and_cross_origin_home(self):
        for host in login.GSP_HOSTS:
            with self.subTest(host=host):
                driver = _Driver(f"https://{host}/page/login")
                driver.on_submit = lambda: driver.urls.update({"original": "https://gsp.lazada.com/portal/home/index"})
                self.assertEqual(self.ensure(driver), "original")
                self.assertEqual(driver.gets, [("original", self.target)])
                self.assertEqual(driver.clicks, [("original", "login")])
                filled_calls = [args for script, args in driver.scripts if script == login.PREFILLED_STATE_SCRIPT]
                self.assertEqual(filled_calls, [(True,)])

    def test_login_new_business_tab_and_closed_auth_tab(self):
        driver = _Driver("https://gsp.lazada.com/page/login")
        def submit():
            driver.urls["business"] = "https://gsp.lazada.com/portal/home/index"
            del driver.urls["original"]
        driver.on_submit = submit
        self.assertEqual(self.ensure(driver), "business")
        self.assertEqual(driver.gets, [("business", self.target)])

    def test_saved_autofill_may_arrive_after_page_load(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/seller/login")
        values = iter([False, True])
        driver.filled = lambda: next(values)
        self.ensure(driver)
        self.assertEqual(driver.clicks, [("original", "login")])

    def test_existing_password_error_blocks_all_fields_without_submit(self):
        driver = _Driver("https://gsp.lazada-seller.cn/page/login")
        driver.feedback["rejected"] = True
        state = {}
        for target in (self.target, "https://sellercenter.lazada.vn/apps/balance"):
            with self.assertRaisesRegex(login.LazadaLoginError, "密码错误"):
                self.ensure(driver, state, target)
        self.assertFalse(driver.clicks)

    def test_password_error_after_submit_never_retries(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/seller/login")
        driver.on_submit = lambda: driver.feedback.update(rejected=True)
        state = {}
        for _ in range(2):
            with self.assertRaisesRegex(login.LazadaLoginError, "密码错误"):
                self.ensure(driver, state)
        self.assertEqual(driver.clicks, [("original", "login")])

    def test_captcha_before_submit_is_not_clicked(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/seller/login")
        driver.feedback["challenge"] = True
        with self.assertRaisesRegex(login.LazadaLoginError, "验证码"):
            self.ensure(driver)
        self.assertFalse(driver.clicks)
        self.assertFalse(any(script == login.PREFILLED_STATE_SCRIPT for script, _ in driver.scripts))
        self.language.assert_not_called()

    def test_language_reload_rechecks_feedback_and_form_before_single_login(self):
        driver = _Driver('https://gsp.lazada-seller.cn/page/login')
        self.language.side_effect = [{'changed': True}, {'changed': False}]
        self.ensure(driver)
        self.assertEqual(driver.clicks, [('original', 'login')])
        calls = [script for script, _ in driver.scripts]
        self.assertEqual(calls[:3], [login.LOGIN_STATUS_SCRIPT, login.LOGIN_STATUS_SCRIPT, login.PREFILLED_STATE_SCRIPT])
        self.assertEqual(self.language.call_count, 2)

    def test_unconfirmed_language_blocks_login_without_prefill_or_repeat(self):
        driver = _Driver('https://gsp.lazada-seller.cn/page/login')
        self.language.side_effect = login.LazadaLanguageError('语言未确认页面重载')
        state = {}
        for _ in range(2):
            with self.assertRaisesRegex(login.LazadaLoginError, '语言未确认'):
                self.ensure(driver, state)
        self.assertEqual(self.language.call_count, 1)
        self.assertFalse(driver.clicks)
        self.assertFalse(any(script == login.PREFILLED_STATE_SCRIPT for script, _ in driver.scripts))

    def test_uncertain_submit_blocks_repeat_and_keeps_no_credential_data(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/seller/login")
        def submit():
            raise RuntimeError("upstream private response")
        driver.on_submit = submit
        state = {}
        for _ in range(2):
            with self.assertRaisesRegex(login.LazadaLoginError, "结果不确定") as caught:
                self.ensure(driver, state)
            self.assertNotIn("private", str(caught.exception))
        self.assertEqual(driver.clicks, [("original", "login")])
        self.assertEqual(set(state), {"attempted", "blocked"})

    def test_relogin_between_fields_keeps_one_submission_total(self):
        driver = _Driver("https://sellercenter.lazada.vn/apps/seller/login")
        state = {}
        self.ensure(driver, state)
        driver.urls["original"] = "https://sellercenter.lazada.vn/apps/seller/login"
        with self.assertRaisesRegex(login.LazadaLoginError, "登录未完成"):
            self.ensure(driver, state, "https://sellercenter.lazada.vn/apps/balance")
        self.assertEqual(driver.clicks, [("original", "login")])

    def test_untrusted_or_wrong_country_link_is_never_clicked(self):
        for href in ("https://sellercenter.lazada.vn.evil.example/apps/seller/login", "https://sellercenter.lazada.sg/apps/seller/login"):
            driver = _Driver("https://sellercenter.lazada.vn/apps/register/index")
            driver.href = href
            with self.assertRaisesRegex(login.LazadaLoginError, "受信任"):
                self.ensure(driver)
            self.assertFalse(driver.clicks)

    def test_target_host_scheme_and_port_are_strict(self):
        for url in ("http://sellercenter.lazada.vn/apps/balance", "https://sellercenter.lazada.vn.evil.test/apps/balance", "https://sellercenter.lazada.vn:8443/apps/balance", "https://user:secret@sellercenter.lazada.vn/apps/balance"):
            driver = _Driver(self.target)
            with self.assertRaisesRegex(login.LazadaLoginError, "不受支持"):
                self.ensure(driver, target=url)
            self.assertFalse(driver.gets)

    def test_redirect_to_other_country_fails_closed(self):
        driver = _Driver("https://sellercenter.lazada.vn/")
        driver.on_get = lambda _: driver.urls.update({"original": "https://sellercenter.lazada.sg/"})
        with self.assertRaisesRegex(login.LazadaLoginError, "其他国家"):
            self.ensure(driver)

    def test_explicit_balance_unavailable_remains_readable_for_caller(self):
        driver = _Driver("https://sellercenter.lazada.vn/")
        driver.on_get = lambda url: driver.urls.update({"original": url + "/unaccessable"})
        self.assertEqual(self.ensure(driver, target="https://sellercenter.lazada.vn/apps/balance"), "original")

    def test_ads_requires_overview_fragment(self):
        target = "https://sellercenter.lazada.vn/sponsor/solutions/ads/productads/#!/overview"
        driver = _Driver(target.replace("overview", "campaigns"))
        self.ensure(driver, target=target)
        self.assertEqual(driver.gets, [("original", target)])


class LazadaGspPrefilledDomTests(unittest.TestCase):
    """Run the real prefill script on synthetic forms; return booleans only."""

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
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def prefilled(self, html, *, gsp=True):
        self.page.set_content(html)
        result = self.page.evaluate('args=>Function(args.script).call(null,args.gsp)',
                                    {'script': login.PREFILLED_STATE_SCRIPT, 'gsp': gsp})
        self.assertEqual(set(result), {'filled'})
        self.assertIs(type(result['filled']), bool)
        return result['filled']

    def test_chinese_gsp_saved_name_fields_are_recognized(self):
        self.assertTrue(self.prefilled('''<form>
          <input type="text" name="account" placeholder="Email" value="fixture-account">
          <input type="password" name="password" placeholder="密码" value="fixture-password">
          <button class="login-button" type="submit">登录</button>
        </form>'''))

    def test_english_fallback_and_overlapping_union_matches_are_unique(self):
        for names in (False, True):
            with self.subTest(names=names):
                account_name = 'name="account"' if names else ''
                password_name = 'name="password"' if names else ''
                self.assertTrue(self.prefilled(f'''<form>
                  <input type="text" {account_name} placeholder="Email" value="fixture-account">
                  <input type="password" {password_name} placeholder="Password" value="fixture-password">
                </form>'''))

    def test_distinct_visible_candidates_from_either_selector_are_rejected(self):
        fields = '''<input name="account" value="fixture-account">
                    <input name="password" type="password" placeholder="密码" value="fixture-password">'''
        for extra in ('<input placeholder="Email" value="second-account">',
                      '<input type="password" placeholder="Password" value="second-password">',
                      '<input name="account" value="second-account">',
                      '<input name="password" type="password" value="second-password">'):
            with self.subTest(extra=extra):
                self.assertFalse(self.prefilled('<form>' + fields + extra + '</form>'))

    def test_hidden_duplicates_do_not_override_visible_unique_form(self):
        self.assertTrue(self.prefilled('''<form>
          <input name="account" placeholder="Email" value="fixture-account">
          <input name="password" type="password" placeholder="密码" value="fixture-password">
          <input name="account" placeholder="Email" value="hidden-account" style="display:none">
          <input name="password" type="password" placeholder="Password" value="hidden-password" style="visibility:hidden">
        </form>'''))
        self.assertFalse(self.prefilled('''<form>
          <input name="account" value="fixture-account">
          <input name="password" type="password" value="hidden-password" hidden>
        </form>'''))

    def test_separate_forms_empty_password_and_wrong_type_are_rejected(self):
        self.assertFalse(self.prefilled('''<form><input name="account" value="fixture-account"></form>
          <form><input name="password" type="password" placeholder="密码" value="fixture-password"></form>'''))
        for password in ('<input name="password" type="password" placeholder="密码">',
                         '<input name="password" type="text" placeholder="Password" value="fixture-password">'):
            with self.subTest(password=password):
                self.assertFalse(self.prefilled('<form><input name="account" value="fixture-account">' + password + '</form>'))

    def test_local_seller_login_keeps_name_only_selector_scope(self):
        self.assertTrue(self.prefilled('''<form><input name="account" value="fixture-account">
          <input name="password" type="password" placeholder="密码" value="fixture-password"></form>''', gsp=False))
        self.assertFalse(self.prefilled('''<form><input placeholder="Email" value="fixture-account">
          <input type="password" placeholder="Password" value="fixture-password"></form>''', gsp=False))


if __name__ == "__main__":
    unittest.main()

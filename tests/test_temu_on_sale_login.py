"""Login transitions with a synthetic driver: no accounts or network access."""
import unittest
from unittest.mock import Mock, patch, call

from selenium.common.exceptions import NoSuchWindowException, StaleElementReferenceException
from backend.services import temu_on_sale_login as login

GLOBAL = 'https://agentseller.temu.com/goods/list'
REGION = 'https://agentseller.temu.com/auth/authentication'
SSO = 'https://seller.kuajingmaihuo.com/settle/seller-login?redirectUrl=fixture'
HOME = 'https://seller.kuajingmaihuo.com/'
SITE_PICKER = 'https://seller.kuajingmaihuo.com/settle/site-main'


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Driver:
    def __init__(self):
        self.urls = {'main': REGION}
        self.active = 'main'
        self.logged_in = self.china_logged_in = self.phone_selected = False
        self.autofilled = self.key_ready = True
        self.key_error = self.challenge = self.sso_confirmation = False
        self.events = []
        self.switch_to = Mock()
        self.switch_to.window.side_effect = lambda h: setattr(self, 'active', h)
        self.region = Mock()
        self.region.click.side_effect = self.open_popup
        self.phone_tab = Mock()
        self.phone_tab.get_attribute.side_effect = lambda name: (
            'password-login_tabItemChecked__fixture' if self.phone_selected and name == 'class' else '')
        self.phone_tab.click.side_effect = self.select_phone
        self.phone_tabs = [self.phone_tab]
        self.agreement = Mock()
        self.agreement.find_element.return_value.is_selected.return_value = False
        self.labels = [self.agreement]
        self.checkboxes = [self.agreement.find_element.return_value]
        self.global_entry = Mock()
        self.global_entry.click.side_effect = self.enter_global
        self.global_entries = [self.global_entry]
        self.phone_button = Mock()
        self.phone_button.click.side_effect = self.phone_submit
        self.phone_buttons = [self.phone_button]
        self.confirm_button = Mock()
        self.confirm_button.click.side_effect = self.confirm
        self.confirm_buttons = [self.confirm_button]
        self.legacy_button = Mock()
        self.execute_script = Mock(side_effect=self.state)
        self.get = Mock(side_effect=self.navigate)
        self.refresh = Mock(side_effect=self.reload)

    @property
    def window_handles(self):
        return list(self.urls)

    @property
    def current_window_handle(self):
        if self.active not in self.urls:
            raise NoSuchWindowException('closed')
        return self.active

    @property
    def current_url(self):
        return self.urls[self.current_window_handle]

    def open_popup(self):
        self.urls['login'] = SSO

    def select_phone(self):
        self.events.append('phone_tab')
        self.phone_selected = True

    def state(self, script):
        assert script == login.LOGIN_READY
        return {'form': True, 'filled': self.autofilled, 'challenge': self.challenge,
                'confirmation': self.current_url == SSO and self.sso_confirmation,
                'key_error': self.key_error, 'key_ready': self.key_ready}

    def navigate(self, url):
        self.events.append(('get', url))
        self.urls[self.active] = url
        if url == login.PHONE_LOGIN_URL:
            self.phone_selected = False
        elif url == SSO and self.china_logged_in:
            self.sso_confirmation = True
        elif url == GLOBAL and self.china_logged_in:
            self.logged_in = True

    def reload(self):
        self.events.append('refresh')
        self.phone_selected = False

    def phone_submit(self):
        self.events.append('phone_submit')
        self.china_logged_in = True
        self.urls[self.active] = HOME

    def confirm(self):
        self.events.append('confirm')
        self.logged_in = True
        self.urls = {'main': GLOBAL}
        raise NoSuchWindowException('authorization popup closed')

    def enter_global(self):
        self.events.append('global_enter')
        self.urls[self.active] = GLOBAL
        self.logged_in = True

    def find_elements(self, by, selector):
        controls = {
            login.REGION_ENTRY: [self.region], login.PHONE_TAB: self.phone_tabs,
            login.PHONE_BUTTON: self.phone_buttons, login.LOGIN_BUTTON: [self.legacy_button],
            login.CONFIRM_BUTTON: self.confirm_buttons,
            login.GLOBAL_ENTRY: self.global_entries,
        }
        if selector in controls:
            return controls[selector]
        if selector.startswith('label:'):
            return self.labels
        if selector == 'input[type="checkbox"]':
            return self.checkboxes
        if selector == login.SELLER_MARKER:
            return [Mock()] if self.china_logged_in and self.current_url == HOME else []
        return [Mock()] if self.logged_in else []


class TemuLoginTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for name, function in [('monotonic', self.clock.now), ('sleep', self.clock.sleep)]:
            patcher = patch.object(login.time, name, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def direct(self):
        driver = Driver()
        driver.urls = {'login': login.PHONE_LOGIN_URL}
        driver.active = 'login'
        return driver

    def test_region_form_uses_phone_page_then_resumes_exact_sso_url(self):
        driver = Driver()
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        self.assertEqual(driver.active, 'main')
        driver.region.click.assert_called_once()
        self.assertEqual(driver.get.call_args_list, [call(login.PHONE_LOGIN_URL), call(SSO)])
        driver.phone_tab.click.assert_called_once()
        driver.phone_button.click.assert_called_once()
        driver.confirm_button.click.assert_called_once()
        driver.legacy_button.click.assert_not_called()
        self.assertLess(driver.events.index('phone_tab'), driver.events.index('phone_submit'))

    def test_legacy_public_key_error_recovers_via_correct_login_page(self):
        driver = Driver()
        original = driver.state
        driver.execute_script.side_effect = lambda script: {
            **original(script), 'key_error': driver.current_url == SSO and not driver.china_logged_in}
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        driver.get.assert_any_call(login.PHONE_LOGIN_URL)
        driver.refresh.assert_not_called()
        driver.legacy_button.click.assert_not_called()

    def test_direct_phone_login_returns_to_requested_business_page(self):
        driver = self.direct()
        self.assertEqual(login.ensure_temu_session(driver, business_url=GLOBAL), 'login')
        self.assertEqual(driver.current_url, GLOBAL)
        driver.get.assert_called_once_with(GLOBAL)
        driver.confirm_button.click.assert_not_called()

    def test_phone_login_site_picker_resumes_business_without_dashboard_marker(self):
        driver = self.direct()
        def submit():
            driver.china_logged_in = True
            driver.urls['login'] = SITE_PICKER
            driver.labels = []
            driver.checkboxes = []
        driver.phone_button.click.side_effect = submit
        self.assertEqual(login.ensure_temu_session(driver), 'login')
        self.assertEqual(driver.current_url, GLOBAL)
        driver.get.assert_called_once_with(GLOBAL)
        driver.phone_button.click.assert_called_once()
        driver.global_entry.click.assert_not_called()

    def test_independent_site_picker_with_consent_enters_global_once(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.agreement.click.side_effect = lambda: driver.events.append('consent')
        self.assertEqual(login.ensure_temu_session(driver), 'login')
        self.assertEqual(driver.current_url, GLOBAL)
        self.assertEqual(driver.events, ['consent', 'global_enter'])
        driver.agreement.click.assert_called_once()
        driver.global_entry.click.assert_called_once()
        driver.phone_button.click.assert_not_called()
        driver.get.assert_not_called()

    def test_independent_authorized_site_picker_restores_business_without_entry_click(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.china_logged_in = True
        driver.labels = []
        driver.checkboxes = []
        self.assertEqual(login.ensure_temu_session(driver), 'login')
        driver.get.assert_called_once_with(GLOBAL)
        driver.global_entry.click.assert_not_called()
        driver.phone_button.click.assert_not_called()

    def test_authorized_site_picker_restores_original_sso_url(self):
        driver = Driver()
        original_navigate = driver.navigate

        def submit():
            driver.china_logged_in = True
            driver.urls['login'] = SITE_PICKER
            driver.labels = []
            driver.checkboxes = []

        def navigate(url):
            original_navigate(url)
            if url == SSO and driver.china_logged_in:
                driver.labels = [driver.agreement]
                driver.checkboxes = [driver.agreement.find_element.return_value]

        driver.phone_button.click.side_effect = submit
        driver.get.side_effect = navigate
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        self.assertEqual(driver.get.call_args_list, [call(login.PHONE_LOGIN_URL), call(SSO)])
        driver.phone_button.click.assert_called_once()
        driver.global_entry.click.assert_not_called()
        driver.confirm_button.click.assert_called_once()

    def test_site_picker_waits_for_hidden_consent_instead_of_treating_it_as_authorized(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.agreement.is_displayed.side_effect = lambda: self.clock.value >= 1.5
        self.assertEqual(login.ensure_temu_session(driver, timeout=4), 'login')
        self.assertGreaterEqual(self.clock.value, 1.5)
        driver.agreement.click.assert_called_once()
        driver.global_entry.click.assert_called_once()
        driver.get.assert_not_called()

    def test_site_picker_hidden_consent_stays_bounded_without_click_or_navigation(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.agreement.is_displayed.return_value = False
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=2)
        driver.agreement.click.assert_not_called()
        driver.global_entry.click.assert_not_called()
        driver.get.assert_not_called()

    def test_persistent_authorized_site_picker_is_not_success_and_navigates_once(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.labels = []
        driver.checkboxes = []
        driver.get.side_effect = None  # The attempted route returns to site-main.
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=3)
        driver.get.assert_called_once_with(GLOBAL)
        driver.global_entry.click.assert_not_called()

    def test_persistent_site_picker_with_consent_is_submitted_only_once(self):
        driver = self.direct()
        driver.urls['login'] = SITE_PICKER
        driver.global_entry.click.side_effect = None
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=3)
        driver.agreement.click.assert_called_once()
        driver.global_entry.click.assert_called_once()
        driver.get.assert_not_called()

    def test_site_picker_ambiguous_global_entry_never_submits_or_navigates(self):
        for consent in (True, False):
            with self.subTest(consent=consent):
                driver = self.direct()
                driver.urls['login'] = SITE_PICKER
                driver.global_entries = [driver.global_entry, Mock()]
                if not consent:
                    driver.labels = []
                    driver.checkboxes = []
                with self.assertRaisesRegex(ValueError, '无法唯一确认'):
                    login.ensure_temu_session(driver)
                driver.agreement.click.assert_not_called()
                driver.global_entry.click.assert_not_called()
                driver.get.assert_not_called()

    def test_site_picker_with_challenge_does_not_resume_business(self):
        driver = self.direct()
        def submit():
            driver.urls['login'] = 'https://seller.kuajingmaihuo.com/settle/site-main'
            driver.challenge = True
        driver.phone_button.click.side_effect = submit
        with self.assertRaisesRegex(ValueError, '人工验证'):
            login.ensure_temu_session(driver)
        driver.get.assert_not_called()
        driver.global_entry.click.assert_not_called()

    def test_existing_business_session_skips_login(self):
        driver = Driver()
        driver.urls['main'] = GLOBAL
        driver.logged_in = True
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        driver.region.click.assert_not_called()
        driver.phone_button.click.assert_not_called()
        driver.switch_to.window.assert_not_called()

    def test_existing_sso_confirmation_does_not_need_phone_login_or_key(self):
        driver = Driver()
        driver.sso_confirmation = True
        driver.key_ready = False
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        driver.confirm_button.click.assert_called_once()
        driver.phone_button.click.assert_not_called()
        driver.get.assert_not_called()

    def test_selected_phone_and_consent_are_not_toggled(self):
        driver = self.direct()
        driver.phone_selected = True
        driver.agreement.find_element.return_value.is_selected.return_value = True
        login.ensure_temu_session(driver)
        driver.phone_tab.click.assert_not_called()
        driver.agreement.click.assert_not_called()
        driver.phone_button.click.assert_called_once()

    def test_delayed_autofill_waits_without_premature_refresh_or_submission(self):
        driver = self.direct()
        original = driver.state
        driver.execute_script.side_effect = lambda script: {
            **original(script), 'filled': self.clock.value >= 4}
        login.ensure_temu_session(driver)
        self.assertGreaterEqual(self.clock.value, 4)
        driver.refresh.assert_not_called()
        driver.phone_button.click.assert_called_once()

    def test_missing_autofill_refreshes_once_and_reselects_phone(self):
        driver = self.direct()
        driver.autofilled = False
        def reload():
            driver.reload()
            driver.autofilled = True
        driver.refresh.side_effect = reload
        login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        self.assertEqual(driver.phone_tab.click.call_count, 2)
        self.assertEqual(driver.events[:3], ['phone_tab', 'refresh', 'phone_tab'])
        driver.phone_button.click.assert_called_once()
        self.assertGreaterEqual(self.clock.value, login.AUTOFILL_WAIT)

    def test_phone_tab_click_waits_for_actual_selection_before_submit(self):
        driver = self.direct()
        driver.phone_tab.click.side_effect = None
        with self.assertRaisesRegex(ValueError, '选项未切换完成'):
            login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        driver.phone_button.click.assert_not_called()

    def test_missing_autofill_after_refresh_stops_with_specific_reason(self):
        driver = self.direct()
        driver.autofilled = False
        with self.assertRaisesRegex(ValueError, '账号或密码未自动填充.*刷新一次'):
            login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        driver.phone_button.click.assert_not_called()
        driver.agreement.click.assert_not_called()

    def test_public_key_initialization_is_awaited_even_when_autofilled(self):
        driver = self.direct()
        original = driver.state
        driver.execute_script.side_effect = lambda script: {
            **original(script), 'key_ready': self.clock.value >= 3}
        login.ensure_temu_session(driver)
        self.assertGreaterEqual(self.clock.value, 3)
        driver.refresh.assert_not_called()
        driver.phone_button.click.assert_called_once()

    def test_missing_public_key_initialization_is_bounded(self):
        driver = self.direct()
        driver.key_ready = False
        with self.assertRaisesRegex(ValueError, '公钥初始化未完成.*刷新一次'):
            login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        driver.phone_button.click.assert_not_called()

    def test_public_key_failure_before_submit_refreshes_once(self):
        driver = self.direct()
        driver.key_error = True
        def reload():
            driver.reload()
            driver.key_error = False
        driver.refresh.side_effect = reload
        login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        driver.phone_button.click.assert_called_once()

    def test_persistent_public_key_failure_stops_without_submission(self):
        driver = self.direct()
        driver.key_error = True
        with self.assertRaisesRegex(ValueError, '公钥.*刷新一次'):
            login.ensure_temu_session(driver)
        driver.refresh.assert_called_once()
        driver.phone_button.click.assert_not_called()

    def test_challenge_never_refreshes_or_submits(self):
        for direct in (True, False):
            with self.subTest(direct=direct):
                driver = self.direct() if direct else Driver()
                driver.challenge = True
                with self.assertRaisesRegex(ValueError, '人工验证'):
                    login.ensure_temu_session(driver)
                driver.refresh.assert_not_called()
                driver.phone_button.click.assert_not_called()
                driver.confirm_button.click.assert_not_called()

    def test_ambiguous_controls_never_submit(self):
        for control in ('phone_tabs', 'labels', 'phone_buttons', 'confirm_buttons'):
            with self.subTest(control=control):
                driver = self.direct() if control != 'confirm_buttons' else Driver()
                driver.sso_confirmation = control == 'confirm_buttons'
                setattr(driver, control, [Mock(), Mock()])
                with self.assertRaisesRegex(ValueError, '无法唯一确认'):
                    login.ensure_temu_session(driver)
                driver.phone_button.click.assert_not_called()
                driver.confirm_button.click.assert_not_called()

    def test_disabled_button_can_enable_after_consent(self):
        driver = self.direct()
        driver.phone_button.is_enabled.return_value = False
        driver.agreement.click.side_effect = lambda: setattr(driver.phone_button.is_enabled, 'return_value', True)
        login.ensure_temu_session(driver)
        driver.phone_button.click.assert_called_once()

    def test_phone_submit_unknown_result_is_not_refreshed_or_repeated(self):
        for error in (None, StaleElementReferenceException('changed')):
            with self.subTest(error=error):
                driver = self.direct()
                driver.phone_button.click.side_effect = error
                with self.assertRaisesRegex(ValueError, '未完成'):
                    login.ensure_temu_session(driver, timeout=3)
                driver.phone_button.click.assert_called_once()
                driver.refresh.assert_not_called()

    def test_key_error_after_submit_does_not_trigger_second_login(self):
        driver = self.direct()
        driver.phone_button.click.side_effect = lambda: setattr(driver, 'key_error', True)
        with self.assertRaisesRegex(ValueError, '提交后'):
            login.ensure_temu_session(driver)
        driver.phone_button.click.assert_called_once()
        driver.refresh.assert_not_called()

    def test_persistent_sso_confirmation_is_submitted_once(self):
        driver = Driver()
        driver.sso_confirmation = True
        driver.confirm_button.click.side_effect = None
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=3)
        driver.confirm_button.click.assert_called_once()

    def test_resumed_sso_waits_for_transient_form_to_become_confirmation(self):
        driver = Driver()
        original = driver.state
        resumed_reads = []

        def state(script):
            result = original(script)
            if driver.current_url == SSO and driver.china_logged_in:
                resumed_reads.append(self.clock.value)
                # The SPA briefly renders its form before restoring the session.
                result['confirmation'] = len(resumed_reads) > 2
            return result

        driver.execute_script.side_effect = state
        self.assertEqual(login.ensure_temu_session(driver, timeout=8), 'main')
        self.assertGreaterEqual(len(resumed_reads), 3)
        driver.phone_button.click.assert_called_once()
        driver.confirm_button.click.assert_called_once()
        driver.legacy_button.click.assert_not_called()
        self.assertEqual(driver.get.call_args_list, [call(login.PHONE_LOGIN_URL), call(SSO)])

    def test_resumed_sso_persistent_form_waits_boundedly_without_second_login(self):
        driver = Driver()
        original = driver.state
        resumed_reads = []

        def state(script):
            result = original(script)
            if driver.current_url == SSO and driver.china_logged_in:
                resumed_reads.append(self.clock.value)
                result['confirmation'] = False
            return result

        driver.execute_script.side_effect = state
        with self.assertRaisesRegex(ValueError, '授权|未完成'):
            login.ensure_temu_session(driver, timeout=8)
        self.assertGreater(len(resumed_reads), 1)
        self.assertLessEqual(self.clock.value, 8.5)
        driver.phone_button.click.assert_called_once()
        driver.confirm_button.click.assert_not_called()
        driver.legacy_button.click.assert_not_called()
        driver.refresh.assert_not_called()
        self.assertEqual(driver.get.call_args_list, [call(login.PHONE_LOGIN_URL), call(SSO)])

    def test_china_home_without_seller_identity_is_not_success(self):
        driver = self.direct()
        def submit():
            driver.urls['login'] = HOME
        driver.phone_button.click.side_effect = submit
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=3)
        driver.get.assert_not_called()

    def test_waiting_for_autofill_does_not_cycle_tabs(self):
        driver = Driver()
        driver.urls['login'] = login.PHONE_LOGIN_URL
        driver.autofilled = False
        with self.assertRaisesRegex(ValueError, '未完成'):
            login.ensure_temu_session(driver, timeout=3)
        self.assertEqual(driver.switch_to.window.call_args_list, [call('login')])

    def test_cdp_metadata_finds_existing_business_tab(self):
        driver = Driver()
        driver.urls = {'main': GLOBAL, 'login': SSO, 'unrelated': 'about:blank'}
        driver.active = 'login'
        driver.logged_in = True
        driver.execute_cdp_cmd = Mock(return_value={'targetInfos': [
            {'type': 'page', 'targetId': h, 'url': u} for h, u in driver.urls.items()]})
        self.assertEqual(login.ensure_temu_session(driver), 'main')
        driver.switch_to.window.assert_called_once_with('main')

    def test_cdp_stale_business_page_does_not_block_ready_tab(self):
        driver = Driver()
        driver.urls = {'stale': GLOBAL, 'ready': GLOBAL}
        driver.active = 'stale'
        driver.logged_in = True
        original = driver.find_elements

        def find_elements(by, selector):
            if driver.active == 'stale' and selector == '[class*="account-info_mallInfo__"]':
                return []
            return original(by, selector)

        driver.find_elements = find_elements
        driver.execute_cdp_cmd = Mock(side_effect=lambda *_: {'targetInfos': [
            {'type': 'page', 'targetId': h, 'url': u} for h, u in driver.urls.items()]})
        self.assertEqual(login.ensure_temu_session(driver, timeout=3), 'ready')
        driver.switch_to.window.assert_any_call('ready')
        driver.phone_button.click.assert_not_called()

    def test_cdp_stale_business_page_does_not_block_phone_login(self):
        driver = Driver()
        driver.urls = {'stale': GLOBAL, 'login': login.PHONE_LOGIN_URL}
        driver.active = 'stale'
        original = driver.find_elements

        def find_elements(by, selector):
            if driver.active == 'stale' and selector == '[class*="account-info_mallInfo__"]':
                return []
            return original(by, selector)

        driver.find_elements = find_elements
        driver.execute_cdp_cmd = Mock(side_effect=lambda *_: {'targetInfos': [
            {'type': 'page', 'targetId': h, 'url': u} for h, u in driver.urls.items()]})
        self.assertEqual(login.ensure_temu_session(driver, timeout=5), 'login')
        driver.phone_tab.click.assert_called_once()
        driver.phone_button.click.assert_called_once()
        driver.get.assert_called_once_with(GLOBAL)

    def test_success_in_new_business_tab_is_found_even_if_phone_page_stays_open(self):
        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                driver = self.direct()
                if metadata:
                    driver.execute_cdp_cmd = Mock(side_effect=lambda *_: {'targetInfos': [
                        {'type': 'page', 'targetId': h, 'url': u} for h, u in driver.urls.items()]})
                def submit():
                    driver.urls['new-business'] = GLOBAL
                    driver.logged_in = True
                driver.phone_button.click.side_effect = submit
                self.assertEqual(login.ensure_temu_session(driver), 'new-business')
                driver.phone_button.click.assert_called_once()
                driver.refresh.assert_not_called()

    def test_unknown_hosts_and_paths_are_not_clicked(self):
        for url in ('https://agentseller.temu.com.example.invalid/auth/authentication',
                    'https://seller.kuajingmaihuo.com.example.invalid/login',
                    'https://seller.kuajingmaihuo.com/unrelated'):
            with self.subTest(url=url):
                driver = self.direct()
                driver.urls['login'] = url
                with self.assertRaisesRegex(ValueError, '未完成'):
                    login.ensure_temu_session(driver, timeout=2)
                driver.phone_tab.click.assert_not_called()
                driver.phone_button.click.assert_not_called()
                driver.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import NoSuchWindowException
from backend.services.temu_on_sale_login import ensure_temu_session, REGION_ENTRY, LOGIN_BUTTON, CONFIRM_BUTTON


class Driver:
    def __init__(self):
        self.window_handles=['main']
        self.active='main'
        self.logged_in=False
        self.host='agentseller.temu.com'
        self.switch_to=Mock()
        self.switch_to.window.side_effect=lambda handle:setattr(self,'active',handle)
        self.region=Mock()
        self.region.click.side_effect=lambda:self.window_handles.append('login')
        self.agreement=Mock()
        self.agreement.find_element.return_value.is_selected.return_value=False
        self.button=Mock()
        self.button.click.side_effect=self.submit
        self.confirm_button=Mock()
        self.confirm_button.click.side_effect=self.submit
        self.login_buttons=[self.button]
        self.confirm_buttons=[]
        self.execute_script=Mock(return_value={'filled':True,'confirmation':False,'challenge':False})
        self.labels=[self.agreement]

    def show_confirmation(self):
        self.execute_script.return_value.update(filled=False,confirmation=True)
        self.login_buttons=[]
        self.confirm_buttons=[self.confirm_button]

    @property
    def current_url(self):
        return ('https://seller.kuajingmaihuo.com/settle/seller-login' if self.active=='login'
                else 'https://'+self.host+('/goods/list' if self.logged_in else '/auth/authentication'))

    def submit(self):
        self.logged_in=True
        self.window_handles=['main']
        raise NoSuchWindowException('authorization popup closed')

    def find_elements(self,by,selector):
        if selector==REGION_ENTRY: return [self.region]
        if selector==LOGIN_BUTTON: return self.login_buttons
        if selector==CONFIRM_BUTTON: return self.confirm_buttons
        if selector.startswith('label:'): return self.labels
        return [Mock()] if self.logged_in else []


class TemuLoginTests(unittest.TestCase):
    def setUp(self):
        sleep=patch('backend.services.temu_on_sale_login.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_region_popup_autofill_authorization_returns_to_main_tab(self):
        driver=Driver()
        self.assertEqual(ensure_temu_session(driver),'main')
        self.assertEqual(driver.active,'main')
        driver.region.click.assert_called_once()
        driver.agreement.click.assert_called_once()
        driver.button.click.assert_called_once()
        driver.execute_script.assert_called_once()

    def test_existing_session_skips_login_and_selected_agreement_is_not_toggled(self):
        driver=Driver()
        driver.logged_in=True
        ensure_temu_session(driver)
        driver.region.click.assert_not_called()
        driver.button.click.assert_not_called()
        driver=Driver()
        driver.agreement.find_element.return_value.is_selected.return_value=True
        ensure_temu_session(driver)
        driver.agreement.click.assert_not_called()
        driver.button.click.assert_called_once()

    def test_verification_or_ambiguous_login_never_submits(self):
        for reason in ('challenge','labels','buttons'):
            driver=Driver()
            if reason=='challenge': driver.execute_script.return_value['challenge']=True
            elif reason=='labels': driver.labels=[Mock(),Mock()]
            else: driver.login_buttons=[Mock(),Mock()]
            with self.assertRaises(ValueError): ensure_temu_session(driver)
            driver.agreement.click.assert_not_called()
            driver.button.click.assert_not_called()

    def test_existing_sso_confirmation_without_password_returns_to_main(self):
        for selected in (False,True):
            with self.subTest(selected=selected):
                driver=Driver()
                driver.show_confirmation()
                driver.agreement.find_element.return_value.is_selected.return_value=selected
                self.assertEqual(ensure_temu_session(driver),'main')
                self.assertEqual(driver.agreement.click.call_count,0 if selected else 1)
                driver.confirm_button.click.assert_called_once()
                driver.button.click.assert_not_called()

    def test_filled_login_can_transition_to_sso_confirmation(self):
        driver=Driver()
        driver.button.click.side_effect=driver.show_confirmation
        self.assertEqual(ensure_temu_session(driver),'main')
        driver.button.click.assert_called_once()
        driver.confirm_button.click.assert_called_once()

    def test_sso_verification_or_ambiguous_controls_never_submit(self):
        for reason in ('challenge','labels','buttons'):
            with self.subTest(reason=reason):
                driver=Driver()
                driver.show_confirmation()
                if reason=='challenge': driver.execute_script.return_value['challenge']=True
                elif reason=='labels': driver.labels=[Mock(),Mock()]
                else: driver.confirm_buttons=[driver.confirm_button,Mock()]
                with self.assertRaises(ValueError): ensure_temu_session(driver)
                driver.agreement.click.assert_not_called()
                driver.confirm_button.click.assert_not_called()
                driver.button.click.assert_not_called()

    def test_persistent_sso_confirmation_is_not_submitted_twice(self):
        driver=Driver()
        driver.window_handles=['login']
        driver.show_confirmation()
        driver.confirm_button.click.side_effect=None
        with patch('backend.services.temu_on_sale_login.time.monotonic',side_effect=[0,1,2,61]):
            with self.assertRaisesRegex(ValueError,'未完成'): ensure_temu_session(driver)
        driver.confirm_button.click.assert_called_once()

    def test_unknown_host_is_not_clicked(self):
        driver=Driver()
        driver.host='agentseller.temu.com.example.invalid'
        with patch('backend.services.temu_on_sale_login.time.monotonic',side_effect=[0,1,61]):
            with self.assertRaisesRegex(ValueError,'未完成'): ensure_temu_session(driver)
        driver.region.click.assert_not_called()
        driver.button.click.assert_not_called()

    def test_missing_autofill_does_not_submit_empty_login(self):
        driver=Driver()
        driver.window_handles=['login']
        driver.execute_script.return_value['filled']=False
        with patch('backend.services.temu_on_sale_login.time.monotonic',side_effect=[0,1,61]):
            with self.assertRaisesRegex(ValueError,'未完成'): ensure_temu_session(driver)
        driver.button.click.assert_not_called()

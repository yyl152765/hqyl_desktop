from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from selenium.common.exceptions import NoSuchWindowException, StaleElementReferenceException, TimeoutException, WebDriverException

from backend.core import ziniao_ip_check as ip_check


URL = 'https://check.example.invalid/ip?store=private-store-token'


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class ZiniaoIPCheckTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        timer = patch.object(ip_check, 'time', self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        self.driver = Mock(current_url=URL, current_window_handle='store-tab')
        self.success = Mock()
        self.success.is_displayed.return_value = True
        self.success.is_enabled.return_value = True
        self.driver.find_elements.return_value = [self.success]

    def check(self, **kwargs):
        ip_check.ensure_ziniao_ip_check(self.driver, URL, **kwargs)

    def assert_wait_reset(self):
        self.assertEqual(self.driver.implicitly_wait.call_args_list, [call(0), call(0)])

    def test_success_requires_exact_runtime_page_and_success_marker(self):
        self.check()
        self.driver.get.assert_called_once_with(URL)
        self.driver.find_elements.assert_called_once_with('xpath', ip_check.SUCCESS_XPATH)
        self.success.is_displayed.assert_called_once()
        self.success.is_enabled.assert_called_once()
        self.assert_wait_reset()

    def test_navigation_timeout_can_observe_delayed_success_without_reloading(self):
        def load(_url):
            self.clock.now += 45
            raise TimeoutException('sensitive-navigation-response')

        self.driver.get.side_effect = load
        self.driver.find_elements.side_effect = [[], [], [self.success]]
        self.check()
        self.assertEqual(self.clock.now, 45.5)
        self.assertEqual(self.driver.find_elements.call_count, 3)
        self.driver.get.assert_called_once_with(URL)
        self.assert_wait_reset()

    def test_deadline_includes_navigation_instead_of_another_minute(self):
        def load(_url):
            self.clock.now += 45
            raise TimeoutException('sensitive-navigation-response')

        self.driver.get.side_effect = load
        self.driver.find_elements.return_value = []
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '等待超时.*尚未确认通过.*代理线路'):
            self.check()
        self.assertEqual(self.clock.now, 60)
        self.driver.get.assert_called_once_with(URL)
        self.assert_wait_reset()

    def test_replaced_success_control_is_observed_again_without_navigation(self):
        for method in ('is_displayed', 'is_enabled'):
            with self.subTest(method=method):
                self.clock.now = 0
                self.driver.reset_mock()
                replaced = Mock()
                replaced.is_displayed.return_value = True
                getattr(replaced, method).side_effect = StaleElementReferenceException('private-status-response')
                self.driver.find_elements.side_effect = [[replaced], [self.success]]
                self.check()
                self.assertEqual(self.clock.now, .25)
                self.driver.get.assert_called_once_with(URL)
                self.assertEqual(self.driver.find_elements.call_count, 2)
                self.assert_wait_reset()

    def test_persistently_stale_marker_is_bounded_and_never_passes(self):
        self.success.is_displayed.side_effect = StaleElementReferenceException('private-status-response')
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '等待超时.*尚未确认通过') as caught:
            self.check(timeout=1)
        self.assertNotIn('private-status-response', str(caught.exception))
        self.assertEqual(self.clock.now, 1)
        self.driver.get.assert_called_once_with(URL)
        self.assert_wait_reset()

    def test_navigation_exhausting_deadline_cannot_accept_late_marker(self):
        self.driver.get.side_effect = lambda _: setattr(self.clock, 'now', 61)
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '等待超时'):
            self.check()
        self.driver.find_elements.assert_not_called()
        self.assert_wait_reset()

    def test_missing_hidden_or_disabled_marker_never_passes(self):
        for state in ('missing', 'hidden', 'disabled'):
            with self.subTest(state=state):
                self.clock.now = 0
                self.driver.reset_mock()
                self.success.is_displayed.return_value = state != 'hidden'
                self.success.is_enabled.return_value = state != 'disabled'
                self.driver.find_elements.return_value = [] if state == 'missing' else [self.success]
                with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '等待超时.*尚未确认通过'):
                    self.check(timeout=1)
                self.assertEqual(self.clock.now, 1)
                self.driver.get.assert_called_once_with(URL)
                self.assert_wait_reset()

    def test_multiple_visible_success_markers_are_rejected(self):
        duplicate = Mock()
        duplicate.is_displayed.return_value = True
        self.driver.find_elements.return_value = [self.success, duplicate]
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '标记不唯一'):
            self.check()
        self.success.is_enabled.assert_not_called()
        self.assert_wait_reset()

    def test_fragment_can_change_but_query_path_and_host_must_match(self):
        self.driver.current_url = URL + '#finished'
        self.check()
        for changed in ('https://other.example.invalid/ip?store=private-store-token',
                        'https://check.example.invalid/other?store=private-store-token',
                        'https://check.example.invalid/ip?store=another-store-token'):
            with self.subTest(url=changed.split('?')[0]):
                self.driver.reset_mock()
                self.driver.current_url = changed
                with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '页面或页签已变化'):
                    self.check()
                self.driver.find_elements.assert_not_called()
                self.assert_wait_reset()

    def test_navigation_timeout_on_another_page_is_rejected(self):
        self.driver.get.side_effect = TimeoutException('private-url-and-response')
        self.driver.current_url = 'https://example.invalid/seller'
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '页面或页签已变化'):
            self.check()
        self.driver.find_elements.assert_not_called()
        self.assert_wait_reset()

    def test_tab_change_during_navigation_is_rejected(self):
        self.driver.get.side_effect = lambda _: setattr(self.driver, 'current_window_handle', 'other-tab')
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '页面或页签已变化'):
            self.check()
        self.driver.find_elements.assert_not_called()
        self.assert_wait_reset()

    def test_page_or_tab_change_during_marker_probe_is_rejected(self):
        for attribute, changed in (('current_url', URL + '&wrong-store=true'),
                                   ('current_window_handle', 'other-tab')):
            with self.subTest(attribute=attribute):
                self.driver.reset_mock()
                self.driver.current_url = URL
                self.driver.current_window_handle = 'store-tab'
                def change():
                    setattr(self.driver, attribute, changed)
                    return True
                self.success.is_enabled.side_effect = change
                with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '页面或页签已变化'):
                    self.check()
                self.assert_wait_reset()

    def test_marker_arriving_after_deadline_is_not_accepted(self):
        def find(*_):
            self.clock.now += 60
            return [self.success]
        self.driver.find_elements.side_effect = find
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '等待超时'):
            self.check()
        self.assert_wait_reset()

    def test_other_driver_errors_do_not_probe_or_echo_url_and_exception(self):
        for error_type in (WebDriverException, NoSuchWindowException, RuntimeError):
            with self.subTest(error_type=error_type):
                self.driver.reset_mock()
                self.driver.get.side_effect = error_type(URL + ' private-password')
                with self.assertRaises(ip_check.ZiniaoIPCheckError) as caught:
                    self.check()
                message = str(caught.exception)
                self.assertIn(f'页面导航异常（{error_type.__name__}）', message)
                self.assertNotIn(URL, message)
                self.assertNotIn('private-password', message)
                self.assertNotIn('private-store-token', message)
                self.assertTrue(caught.exception.__suppress_context__)
                self.driver.find_elements.assert_not_called()
                self.assert_wait_reset()

    def test_failure_during_result_probe_is_classified_and_redacted(self):
        self.driver.find_elements.side_effect = WebDriverException(URL)
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '结果核验异常（WebDriverException）') as caught:
            self.check()
        self.assertNotIn('private-store-token', str(caught.exception))
        self.assert_wait_reset()

    def test_missing_detection_page_is_not_called_a_failed_proxy(self):
        for value in (None, '', ' ', 10):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '未返回有效的代理 IP 检测页面'):
                    ip_check.ensure_ziniao_ip_check(self.driver, value)
                self.driver.get.assert_not_called()
                self.driver.find_elements.assert_not_called()

    def test_reset_failure_after_success_cannot_be_ignored(self):
        self.driver.implicitly_wait.side_effect = [None, WebDriverException(URL)]
        with self.assertRaisesRegex(ip_check.ZiniaoIPCheckError, '连接恢复失败（WebDriverException）') as caught:
            self.check()
        self.assertNotIn('private-store-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

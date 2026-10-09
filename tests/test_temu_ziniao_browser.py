from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from backend.core.ziniao_browser import ZiniaoBrowser, ZiniaoBrowserError, account_profile


REQUEST = {'account_id':'account-1','company':'company','username':'operator','password':'private-test-value'}
ROW = {'browserId':'123','browserName':'泰国-testSeller','platform_name':'TEMU 东南亚','browserOauth':'opaque-test-oauth'}


class NativeAccountBrowserTests(unittest.TestCase):
    def make(self):
        runtime = Mock()
        runtime.get_browser_list.return_value = [dict(ROW)]
        runtime.close_store.return_value = {'statusCode':0}
        http = Mock()
        http.post.return_value.status_code = 200
        http.post.return_value.json.return_value = {'statusCode':0,'browserList':[dict(ROW)]}
        client = ZiniaoBrowser(dict(REQUEST), browser=runtime, http=http)
        login = patch('backend.services.temu_on_sale_login.ensure_temu_session')
        login.start()
        self.addCleanup(login.stop)
        probe = patch.object(client, '_port_open', return_value=False)
        probe.start()
        self.addCleanup(probe.stop)
        self.addCleanup(client.close)
        return client, runtime, http

    def test_account_identity_excludes_password_and_separates_accounts(self):
        value = account_profile(REQUEST)
        self.assertNotIn(REQUEST['password'], value)
        self.assertEqual(value, account_profile({**REQUEST,'password':'new-password'}))
        for field in ('account_id','company','username'):
            self.assertNotEqual(value, account_profile({**REQUEST,field:'other'}))

    def test_existing_account_protocol_uses_loopback_without_proxy_or_redirect(self):
        client, runtime, http = self.make()
        data = {key:REQUEST[key] for key in ('company','username','password')}
        data['action'] = 'getBrowserList'
        result = runtime.send_http(data, timeout=40)
        self.assertEqual(result['statusCode'],0)
        http.post.assert_called_once_with('http://127.0.0.1:16851', json=data, timeout=40, allow_redirects=False)
        self.assertFalse(http.trust_env)

    def test_login_failure_latches_and_never_echoes_response(self):
        client, runtime, http = self.make()
        http.post.return_value.json.return_value = {'statusCode':-10003,'err':REQUEST['password']}
        with self.assertRaises(ZiniaoBrowserError) as captured:
            runtime.send_http({'action':'getBrowserList'})
        self.assertTrue(captured.exception.auth)
        self.assertNotIn(REQUEST['password'],str(captured.exception))
        with self.assertRaises(ZiniaoBrowserError):
            runtime.send_http({'action':'startBrowser'})
        with self.assertRaises(ZiniaoBrowserError):
            client.list_stores()
        self.assertEqual(http.post.call_count,1)

    def test_redirect_or_transport_failure_does_not_expose_secrets(self):
        client, runtime, http = self.make()
        for status in (302,500):
            http.post.return_value.status_code = status
            with self.assertRaises(ZiniaoBrowserError):
                runtime.send_http(REQUEST)
        http.post.side_effect = RuntimeError(REQUEST['password'])
        with self.assertRaises(ZiniaoBrowserError) as captured:
            runtime.send_http(REQUEST)
        self.assertNotIn(REQUEST['password'],str(captured.exception))

    def test_ready_reuses_runtime_or_starts_without_killing_other_stores(self):
        client, runtime, _ = self.make()
        with patch.object(client,'_listening',return_value=True):
            client.ready()
        runtime.start_browser.assert_not_called()
        with patch.object(client,'_listening',side_effect=[False,True]):
            client.ready()
        runtime.start_browser.assert_called_once()
        runtime.kill_process.assert_not_called()
        runtime.load_super_browser.assert_not_called()

    def test_catalog_exposes_only_stable_id_name_and_platform(self):
        client, runtime, _ = self.make()
        result = client.list_stores()
        self.assertEqual(result,[{'store_id':'123','store_name':'泰国-testSeller','platform_name':'TEMU 东南亚'}])
        self.assertNotIn('opaque-test-oauth',json.dumps(result))
        for rows in ([{**ROW,'browserId':''}], [ROW,ROW], ['invalid']):
            runtime.get_browser_list.return_value = rows
            with self.assertRaises(ZiniaoBrowserError):
                client.list_stores()

    def opened(self):
        client, runtime, http = self.make()
        store = client.list_stores()[0]
        runtime.open_store.return_value = {'browserId':'123','debuggingPort':9222,'ipDetectionPage':'http://127.0.0.1/ip',
                                          'launcherPage':'https://agentseller.temu.com/'}
        driver = runtime.get_driver.return_value
        driver.current_window_handle = 'seller-tab'
        runtime.open_ip_check.return_value = True
        return client,runtime,driver,store

    def test_open_uses_selected_id_checks_ip_and_pins_page(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store,url='https://agentseller.temu.com/goods/list')
        runtime.open_store.assert_called_once_with('123')
        runtime.open_ip_check.assert_called_once()
        runtime.open_launcher_page.assert_called_once()
        driver.execute_script.return_value = '{"ok":true}'
        self.assertEqual(client.evaluate('123','JSON.stringify({ok:true})'),{'ok':True})
        driver.switch_to.window.assert_called_with('seller-tab')
        element = Mock()
        driver.find_elements.return_value = [element]
        client.click('123','#export')
        element.click.assert_called_once()
        client.close()
        driver.service.stop.assert_called_once()
        driver.quit.assert_not_called()
        runtime.close_store.assert_called_once_with('opaque-test-oauth')
        client._port_open.assert_called_once_with(9222)
        runtime.get_exit.assert_not_called()

    def test_wrong_store_identity_never_attaches_driver(self):
        client,runtime,driver,store = self.opened()
        runtime.open_store.return_value['browserId']='456'
        with self.assertRaisesRegex(ZiniaoBrowserError,'ID'):
            client.open_store(store)
        runtime.get_driver.assert_not_called()
        self.assertTrue(client.cleanup_failed)
        with self.assertRaisesRegex(ZiniaoBrowserError, '停止'):
            client.open_store(store)
        self.assertEqual(runtime.open_store.call_count, 1)

    def test_lost_open_response_blocks_next_store(self):
        client,runtime,driver,store = self.opened()
        runtime.open_store.side_effect = RuntimeError('connection lost')
        with self.assertRaises(ZiniaoBrowserError):
            client.open_store(store)
        with self.assertRaisesRegex(ZiniaoBrowserError, '停止'):
            client.open_store(store)
        self.assertEqual(runtime.open_store.call_count, 1)

    def test_ip_failure_detaches_only_own_connection(self):
        client,runtime,driver,store = self.opened()
        runtime.open_ip_check.return_value=False
        with self.assertRaisesRegex(ZiniaoBrowserError,'IP'):
            client.open_store(store)
        runtime.open_launcher_page.assert_not_called()
        driver.service.stop.assert_called_once()
        self.assertFalse(client.drivers)

    def test_page_failures_redacted_and_ambiguous_click_rejected(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        driver.execute_script.side_effect=RuntimeError(REQUEST['password'])
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client.evaluate('123','JSON.stringify({})')
        self.assertNotIn(REQUEST['password'],str(captured.exception))
        driver.find_elements.return_value=[Mock(),Mock()]
        with self.assertRaises(ZiniaoBrowserError):
            client.click('123','button')
        for element in driver.find_elements.return_value:
            element.click.assert_not_called()

    def test_only_one_store_can_be_open_and_close_precedes_next_open(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        runtime.reset_mock()
        client.open_store(store)
        methods = [call[0] for call in runtime.mock_calls]
        self.assertLess(methods.index('close_store'), methods.index('open_store'))
        self.assertEqual(len(client._opened),1)

    def test_click_waits_for_modal_animation_on_the_same_element_once(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        element.is_displayed.side_effect = [False, True]
        driver.find_elements.return_value = [element]
        with patch('backend.core.ziniao_browser.time.sleep'):
            client.click('123', '#close')
        driver.find_elements.assert_called_once()
        element.click.assert_called_once()

    def test_permanently_hidden_control_is_not_clicked(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        element.is_displayed.return_value = False
        driver.find_elements.return_value = [element]
        with patch('backend.core.ziniao_browser.time.monotonic',side_effect=[0,4]):
            with self.assertRaisesRegex(ZiniaoBrowserError,'不可操作'):
                client.click('123','#close')
        element.click.assert_not_called()

    def test_click_interception_is_distinct_from_uncertain_driver_failure(self):
        from selenium.common.exceptions import ElementClickInterceptedException
        from backend.core.ziniao_browser import ZiniaoClickInterceptedError
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element=Mock()
        driver.find_elements.return_value=[element]
        element.click.side_effect=ElementClickInterceptedException(REQUEST['password'])
        with self.assertRaises(ZiniaoClickInterceptedError) as captured:
            client.click('123','#reset')
        self.assertNotIn(REQUEST['password'],str(captured.exception))
        element.click.assert_called_once()

    def test_close_failure_blocks_any_further_store_open(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        runtime.close_store.return_value={'statusCode':1}
        with self.assertRaisesRegex(ZiniaoBrowserError,'暂停'):
            client.close_store('123')
        self.assertTrue(client.cleanup_failed)
        with self.assertRaisesRegex(ZiniaoBrowserError,'停止'):
            client.open_store(store)
        self.assertEqual(runtime.open_store.call_count,1)

    def test_open_and_close_failure_preserves_safe_phase_and_keeps_cleanup_latched(self):
        client,runtime,driver,store = self.opened()
        opening_secret = 'opening-secret https://example.invalid/?token=secret-token'
        runtime.get_driver.side_effect = RuntimeError(opening_secret)
        runtime.close_store.return_value = {'statusCode':1,'err':'closing-secret'}
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client.open_store(store)
        message = str(captured.exception)
        self.assertIn('驱动连接阶段（RuntimeError）',message)
        self.assertIn('未确认关闭',message)
        for secret in (opening_secret,'secret-token','closing-secret',ROW['browserOauth']):
            self.assertNotIn(secret,message)
        self.assertTrue(captured.exception.__suppress_context__)
        self.assertFalse(captured.exception.auth)
        self.assertTrue(client.cleanup_failed)
        self.assertIn('123',client._opened)
        next_store = {**store,'store_id':'456','store_name':'第二家'}
        with self.assertRaisesRegex(ZiniaoBrowserError,'停止'):
            client.open_store(next_store)
        # A later successful cleanup still cannot silently unlock this batch.
        runtime.close_store.return_value = {'statusCode':0}
        client.close_store('123')
        self.assertNotIn('123',client._opened)
        self.assertTrue(client.cleanup_failed)
        with self.assertRaisesRegex(ZiniaoBrowserError,'停止'):
            client.open_store(next_store)
        self.assertEqual(runtime.open_store.call_count,1)
        driver.quit.assert_not_called()

    def test_combined_failure_preserves_original_or_latched_auth_without_error_text(self):
        for original_auth, latched_auth in ((True,False),(False,True)):
            with self.subTest(original_auth=original_auth,latched_auth=latched_auth):
                client,runtime,driver,store = self.opened()
                client.auth_failed = latched_auth
                runtime.get_driver.side_effect = ZiniaoBrowserError(REQUEST['password'],auth=original_auth)
                runtime.close_store.side_effect = RuntimeError('closing-secret '+ROW['browserOauth'])
                with self.assertRaises(ZiniaoBrowserError) as captured:
                    client.open_store(store)
                self.assertTrue(captured.exception.auth)
                self.assertIn('驱动连接阶段（ZiniaoBrowserError）',str(captured.exception))
                for secret in (REQUEST['password'],'closing-secret',ROW['browserOauth']):
                    self.assertNotIn(secret,str(captured.exception))
                self.assertTrue(client.cleanup_failed)
                self.assertIn('123',client._opened)
                with self.assertRaisesRegex(ZiniaoBrowserError,'停止'):
                    client.open_store({**store,'store_id':'456','store_name':'第二家'})
                self.assertEqual(runtime.open_store.call_count,1)

    def test_open_error_is_unchanged_when_cleanup_succeeds(self):
        client,runtime,driver,store = self.opened()
        original = ZiniaoBrowserError('原有安全认证提示',auth=True)
        runtime.get_driver.side_effect = original
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client.open_store(store)
        self.assertIs(captured.exception,original)
        self.assertTrue(captured.exception.auth)
        self.assertFalse(client.cleanup_failed)
        self.assertFalse(client._opened)

    def test_close_ack_without_closed_browser_port_does_not_advance(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        client._port_open.return_value=True
        with patch('backend.core.ziniao_browser.time.monotonic',side_effect=[0,16]):
            with self.assertRaisesRegex(ZiniaoBrowserError,'暂停'):
                client.close_store('123')
        self.assertTrue(client.cleanup_failed)

    def test_port_probe_distinguishes_closed_windows_port_from_uncertain_failure(self):
        with patch('backend.core.ziniao_browser.socket.create_connection', side_effect=ConnectionRefusedError) as connect:
            self.assertFalse(ZiniaoBrowser._port_open(50027))
            self.assertGreaterEqual(connect.call_args.kwargs['timeout'], 3)
        for error in (TimeoutError, OSError):
            with self.subTest(error=error), patch('backend.core.ziniao_browser.socket.create_connection', side_effect=error):
                self.assertTrue(ZiniaoBrowser._port_open(50027))


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from backend.core.ziniao_browser import (
    ZiniaoBrowser, ZiniaoBrowserError, ZiniaoPageNavigatingError, ZiniaoTransportError, account_profile,
)
from backend.core.ziniao_ip_check import ZiniaoIPCheckError


REQUEST = {'account_id':'account-1','company':'company','username':'operator','password':'private-test-value'}
ROW = {'browserId':'123','browserName':'泰国-testSeller','platform_name':'TEMU 东南亚','browserOauth':'opaque-test-oauth'}


class NativeAccountBrowserTests(unittest.TestCase):
    def test_read_interrupted_by_navigation_is_typed_without_exposing_driver_details(self):
        client, runtime, driver, store = self.opened()
        client.open_store(store)
        for message in ('execution context was destroyed', 'cannot find context with specified id',
                        'document unloaded while waiting for result'):
            driver.execute_script.side_effect = RuntimeError(message + REQUEST['password'])
            with self.subTest(message=message), self.assertRaises(ZiniaoPageNavigatingError) as caught:
                client.evaluate('123', 'JSON.stringify({})')
            self.assertNotIn(REQUEST['password'], str(caught.exception))
        driver.execute_script.side_effect = RuntimeError('no such window')
        with self.assertRaises(ZiniaoBrowserError) as caught:
            client.evaluate('123', 'JSON.stringify({})')
        self.assertNotIsInstance(caught.exception, ZiniaoPageNavigatingError)

    def make(self):
        runtime = Mock()
        runtime.get_browser_list.return_value = [dict(ROW)]
        runtime.close_store.return_value = {'statusCode':0}
        http = Mock()
        http.post.return_value.status_code = 200
        http.post.return_value.json.return_value = {'statusCode':0,'browserList':[dict(ROW)]}
        client = ZiniaoBrowser(dict(REQUEST), browser=runtime, http=http)
        driver_check = patch('backend.core.ziniao_browser.ensure_ziniao_driver')
        client._test_driver_check = driver_check.start()
        self.addCleanup(driver_check.stop)
        login = patch('backend.services.temu_on_sale_login.ensure_temu_session')
        login.start()
        self.addCleanup(login.stop)
        ip_check = patch('backend.core.ziniao_browser.ensure_ziniao_ip_check')
        client._test_ip_check = ip_check.start()
        self.addCleanup(ip_check.stop)
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

    def test_native_requests_verify_port_owner_before_credentials_even_in_preview(self):
        from backend.core.ziniao_startup import ZiniaoStartupError
        client, runtime, http = self.make()
        client._native_runtime = True
        with patch('backend.core.ziniao_browser.ziniao_port_owned', side_effect=ZiniaoStartupError('其他程序占用')):
            with self.assertRaisesRegex(ZiniaoBrowserError, '其他程序'):
                client._send_http({**REQUEST, 'action': 'getBrowserList'})
        http.post.assert_not_called()
        with patch('backend.core.ziniao_browser.ziniao_port_owned', return_value=False):
            with self.assertRaises(ZiniaoTransportError):
                client._send_http({**REQUEST, 'action': 'getBrowserList'})
        http.post.assert_not_called()

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

    def test_real_batch_restarts_once_before_core_preparation_and_never_in_preview(self):
        client, runtime, http = self.make()
        events = []
        runtime.client_path = 'C:/fixture/ziniao.exe'
        runtime.start_browser.side_effect = lambda: events.append('start')
        with patch('backend.core.ziniao_browser.prepare_ziniao_client', side_effect=lambda *a, **k: events.append('stop')) as stop, \
                patch('backend.core.ziniao_browser.ziniao_port_owned', return_value=True), \
                patch.object(client, '_prepare_core', side_effect=lambda progress: events.append('core')):
            client.prepare_task(progress=events.append)
            client.prepare_task(progress=events.append)
        self.assertLess(events.index('stop'), events.index('start'))
        self.assertLess(events.index('start'), events.index('core'))
        stop.assert_called_once()
        runtime.start_browser.assert_called_once()
        payload = http.post.call_args.kwargs['json']
        self.assertEqual(payload['action'], 'getRunningInfo')
        self.assertNotIn('password', payload)

    def test_restart_refuses_unknown_listener_without_sending_credentials(self):
        from backend.core.ziniao_startup import ZiniaoStartupError
        client, runtime, http = self.make()
        with patch('backend.core.ziniao_browser.prepare_ziniao_client'), \
                patch('backend.core.ziniao_browser.ziniao_port_owned', side_effect=ZiniaoStartupError('端口被其他程序占用')):
            with self.assertRaisesRegex(ZiniaoBrowserError, '其他程序'):
                client.prepare_task()
        http.post.assert_not_called()
        self.assertFalse(client._task_prepared)

    def test_core_download_has_deadline_and_auth_failure_is_not_retried(self):
        client, runtime, http = self.make()
        http.post.return_value.json.return_value = {'statusCode': -10003, 'err': REQUEST['password']}
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client._prepare_core(None)
        self.assertTrue(captured.exception.auth)
        self.assertEqual(http.post.call_count, 1)
        self.assertNotIn(REQUEST['password'], str(captured.exception))
        client, runtime, http = self.make()
        http.post.return_value.json.return_value = {'statusCode': 1}
        with patch('backend.core.ziniao_browser.time.monotonic', side_effect=[0, 0, 0, 179, 181]), \
                patch('backend.core.ziniao_browser.time.sleep'):
            with self.assertRaisesRegex(ZiniaoBrowserError, '180 秒'):
                client._prepare_core(None)
        self.assertEqual(http.post.call_count, 1)

    def test_driver_check_failure_closes_store_before_any_selenium_attachment(self):
        from backend.core.ziniao_driver import ZiniaoDriverError
        client, runtime, driver, store = self.opened()
        client._test_driver_check.side_effect = ZiniaoDriverError('自动下载驱动失败，请检查网络')
        with self.assertRaisesRegex(ZiniaoBrowserError, '自动下载驱动失败'):
            client.open_store(store)
        runtime.get_driver.assert_not_called()
        runtime.close_store.assert_called_once()
        self.assertFalse(client.cleanup_failed)

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
        driver.execute_script.return_value = {'connected':True,'clear':True,'x':50,'y':60}
        return client,runtime,driver,store

    def test_open_uses_selected_id_checks_ip_and_pins_page(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store,url='https://agentseller.temu.com/goods/list')
        runtime.open_store.assert_called_once_with('123')
        client._test_ip_check.assert_called_once_with(driver, 'http://127.0.0.1/ip')
        runtime.open_ip_check.assert_not_called()
        runtime.open_launcher_page.assert_called_once()
        driver.execute_script.return_value = '{"ok":true}'
        self.assertEqual(client.evaluate('123','JSON.stringify({ok:true})'),{'ok':True})
        driver.switch_to.window.assert_not_called()
        driver.execute_script.return_value = {'connected':True,'clear':True,'x':50,'y':60}
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

    def test_page_reads_switch_only_when_the_current_tab_differs(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store,url='https://agentseller.temu.com/goods/list')
        driver.current_window_handle='another-tab'
        driver.switch_to.window.side_effect=lambda handle:setattr(driver,'current_window_handle',handle)
        driver.execute_script.return_value='{"ready":true}'
        client.evaluate('123','JSON.stringify({ready:true})')
        client.evaluate('123','JSON.stringify({ready:true})')
        driver.switch_to.window.assert_called_once_with('seller-tab')

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
        client._test_ip_check.side_effect = ZiniaoIPCheckError('代理 IP 检测等待超时，尚未确认通过')
        with self.assertRaisesRegex(ZiniaoBrowserError,'IP'):
            client.open_store(store)
        runtime.open_launcher_page.assert_not_called()
        driver.service.stop.assert_called_once()
        self.assertFalse(client.drivers)

    def test_missing_ip_page_is_distinct_and_never_starts_launcher(self):
        from backend.core.ziniao_ip_check import ensure_ziniao_ip_check
        client,runtime,driver,store = self.opened()
        runtime.open_store.return_value.pop('ipDetectionPage')
        client._test_ip_check.side_effect = ensure_ziniao_ip_check
        with self.assertRaisesRegex(ZiniaoBrowserError, '未返回有效的代理 IP 检测页面'):
            client.open_store(store)
        runtime.open_launcher_page.assert_not_called()
        driver.get.assert_not_called()
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
        element.is_displayed.side_effect = [False, True, True]
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

    def test_transient_mask_disappears_before_single_click_on_same_element(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.side_effect = [
            {'connected':True,'clear':False,'x':50,'y':60},
            {'connected':True,'clear':True,'x':50,'y':60},
            {'connected':True,'clear':True,'x':50,'y':60},
        ]
        with patch('backend.core.ziniao_browser.time.sleep'):
            client.click('123', '#export')
        driver.find_elements.assert_called_once()
        element.click.assert_called_once()
        self.assertEqual([call.args[1:] for call in driver.execute_script.call_args_list],
                         [(element, True), (element, False), (element, False)])

    def test_moving_center_must_settle_before_single_dispatch(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.side_effect = [
            {'connected':True,'clear':True,'x':50,'y':y} for y in (10, 30, 60, 60)
        ]
        with patch('backend.core.ziniao_browser.time.sleep'):
            client.click('123', '#export')
        self.assertEqual(driver.execute_script.call_count, 4)
        element.click.assert_called_once()

    def test_permanent_mask_returns_interception_without_dispatch(self):
        from backend.core.ziniao_browser import ZiniaoClickInterceptedError
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.return_value = {'connected':True,'clear':False,'x':50,'y':60}
        with patch('backend.core.ziniao_browser.time.monotonic',side_effect=[0,4]):
            with self.assertRaises(ZiniaoClickInterceptedError):
                client.click('123', '#export')
        element.click.assert_not_called()

    def test_disconnected_original_element_is_not_replaced_or_clicked(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        driver.find_elements.return_value = [element]
        driver.execute_script.return_value = {'connected':False}
        with self.assertRaisesRegex(ZiniaoBrowserError, '控件已变化'):
            client.click('123', '#export')
        driver.find_elements.assert_called_once()
        element.click.assert_not_called()

    def test_uncertain_result_after_click_never_repeats_dispatch(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        element = Mock()
        driver.find_elements.return_value = [element]
        element.click.side_effect = RuntimeError('connection lost after submission private-secret')
        with patch('backend.core.ziniao_browser.time.sleep'):
            with self.assertRaises(ZiniaoBrowserError) as captured:
                client.click('123', '#export')
        element.click.assert_called_once()
        self.assertNotIn('private-secret', str(captured.exception))
        from backend.core.ziniao_browser import ZiniaoClickInterceptedError
        self.assertNotIsInstance(captured.exception, ZiniaoClickInterceptedError)

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

    def test_unexpected_open_failure_keeps_safe_phase_when_cleanup_succeeds(self):
        from selenium.common.exceptions import TimeoutException
        for operation, phase, error_type in (
            ('get_driver', '驱动连接', RuntimeError),
            ('set_page_load_timeout', '驱动初始化', RuntimeError),
            ('get', '业务页面导航', TimeoutException),
        ):
            with self.subTest(operation=operation):
                client,runtime,driver,store = self.opened()
                target = runtime if operation == 'get_driver' else driver
                getattr(target, operation).side_effect = error_type(
                    REQUEST['password'] + ' https://example.invalid/?token=private-token')
                with self.assertRaises(ZiniaoBrowserError) as captured:
                    client.open_store(store, url='https://agentseller.temu.com/goods/list')
                message = str(captured.exception)
                self.assertIn(f'{phase}阶段（{error_type.__name__}）', message)
                for secret in (REQUEST['password'], 'private-token', ROW['browserOauth']):
                    self.assertNotIn(secret, message)
                self.assertTrue(captured.exception.__suppress_context__)
                self.assertFalse(captured.exception.auth)
                self.assertFalse(client.cleanup_failed)
                self.assertFalse(client._opened)
                self.assertFalse(client.drivers)
                runtime.close_store.assert_called_once_with(ROW['browserOauth'])
                driver.quit.assert_not_called()

    def test_driver_os_error_reports_code_and_core_without_exception_secrets(self):
        for winerror, hint in ((193, '可执行程序'), (216, '系统架构'), (5, '拒绝访问'),
                               (2, '不存在'), (32, '被占用'), (112, '空间不足'), (9999, '--version')):
            with self.subTest(winerror=winerror):
                client, runtime, driver, store = self.opened()
                runtime.open_store.return_value['core_version'] = '138.0.7204.101'
                error = OSError('private-error https://example.invalid/?token=private-token')
                error.winerror = winerror
                error.filename = 'C:/private-account/secret.exe'
                runtime.get_driver.side_effect = error
                with self.assertRaises(ZiniaoBrowserError) as captured:
                    client.open_store(store)
                message = str(captured.exception)
                self.assertIn(f'WinError {winerror}', message)
                self.assertIn('紫鸟内核 138.0.7204.101', message)
                self.assertIn('chromedriver138.exe', message)
                self.assertIn(hint, message)
                for secret in ('private-error', 'private-token', 'private-account', 'secret.exe', ROW['browserOauth']):
                    self.assertNotIn(secret, message)
                runtime.get_driver.assert_called_once()
                runtime.open_launcher_page.assert_not_called()
                runtime.close_store.assert_called_once_with(ROW['browserOauth'])
                self.assertFalse(client.cleanup_failed)

    def test_driver_os_code_survives_cleanup_failure_and_untrusted_metadata_is_omitted(self):
        client, runtime, driver, store = self.opened()
        runtime.open_store.return_value['core_version'] = '138\nsecret-token'
        error = OSError('private-error')
        error.winerror = 193
        runtime.get_driver.side_effect = error
        runtime.close_store.return_value = {'statusCode': 1}
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client.open_store(store)
        message = str(captured.exception)
        self.assertIn('WinError 193', message)
        self.assertIn('未确认关闭', message)
        self.assertNotIn('secret-token', message)
        self.assertNotIn('private-error', message)
        self.assertTrue(client.cleanup_failed)

    def test_driver_errno_without_winerror_and_opaque_codes_stay_safe(self):
        import errno
        for error, expected in ((OSError(errno.EACCES, 'private-error'), 'errno 13'),
                                (OSError('private-error'), '--version')):
            with self.subTest(expected=expected):
                client, runtime, driver, store = self.opened()
                error.filename = 'private-path'
                runtime.get_driver.side_effect = error
                with self.assertRaises(ZiniaoBrowserError) as captured:
                    client.open_store(store)
                self.assertIn(expected, str(captured.exception))
                self.assertNotIn('private-error', str(captured.exception))
                self.assertNotIn('private-path', str(captured.exception))

    def test_close_ack_without_closed_browser_port_does_not_advance(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        client._port_open.return_value=True
        with patch('backend.core.ziniao_browser.time.monotonic',side_effect=[0,16]):
            with self.assertRaisesRegex(ZiniaoBrowserError,'暂停'):
                client.close_store('123')
        self.assertTrue(client.cleanup_failed)

    def test_stop_response_lost_but_owned_port_closes_allows_next_store(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        runtime.close_store.side_effect = ZiniaoTransportError('transport-secret')
        client._port_open.side_effect = [True, False]
        with patch('backend.core.ziniao_browser.time.sleep'):
            client.close_store('123')
        runtime.close_store.assert_called_once_with(ROW['browserOauth'])
        self.assertEqual(client._port_open.call_args_list, [unittest.mock.call(9222)] * 2)
        self.assertFalse(client.cleanup_failed)
        self.assertFalse(client._opened)
        self.assertFalse(client.drivers)
        driver.quit.assert_not_called()
        # Only the authoritative closed-port result unlocks the next open.
        runtime.close_store.side_effect = None
        client._port_open.side_effect = None
        client.open_store(store)
        self.assertEqual(runtime.open_store.call_count, 2)

    def test_stop_timeout_uses_confirmation_without_resending_stop(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        http = client.http
        http.post.side_effect = TimeoutError('request-secret')
        runtime.close_store.side_effect = lambda handle: runtime.send_http(
            {'action': 'stopBrowser', 'browserOauth': handle}, timeout=8)
        client.close_store('123')
        self.assertEqual(http.post.call_count, 1)
        self.assertEqual(http.post.call_args.kwargs['timeout'], 8)
        client._port_open.assert_called_once_with(9222)
        self.assertFalse(client.cleanup_failed)

    def test_no_stop_response_without_closed_port_still_halts_batch(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        runtime.close_store.return_value = None
        client._port_open.return_value = True
        with patch('backend.core.ziniao_browser.time.monotonic',side_effect=[0,16]):
            with self.assertRaisesRegex(ZiniaoBrowserError,'等待店铺窗口退出'):
                client.close_store('123')
        self.assertTrue(client.cleanup_failed)
        self.assertIn('123', client._opened)
        with self.assertRaisesRegex(ZiniaoBrowserError, '停止'):
            client.open_store(store)
        self.assertEqual(runtime.open_store.call_count, 1)
        runtime.close_store.assert_called_once()

    def test_stop_auth_failure_is_not_recovered_by_closed_port(self):
        client,runtime,driver,store = self.opened()
        client.open_store(store)
        client.http.post.return_value.json.return_value = {'statusCode': -10003, 'err':'auth-secret'}
        runtime.close_store.side_effect = lambda handle: runtime.send_http(
            {'action':'stopBrowser', 'browserOauth':handle}, timeout=8)
        with self.assertRaises(ZiniaoBrowserError) as captured:
            client.close_store('123')
        self.assertTrue(client.auth_failed)
        self.assertTrue(client.cleanup_failed)
        self.assertTrue(captured.exception.auth)
        self.assertIn('关闭请求', str(captured.exception))
        self.assertNotIn('auth-secret', str(captured.exception))
        client._port_open.assert_not_called()

    def test_port_probe_distinguishes_closed_windows_port_from_uncertain_failure(self):
        with patch('backend.core.ziniao_browser.socket.create_connection', side_effect=ConnectionRefusedError) as connect:
            self.assertFalse(ZiniaoBrowser._port_open(50027))
            self.assertGreaterEqual(connect.call_args.kwargs['timeout'], 3)
        for error in (TimeoutError, OSError):
            with self.subTest(error=error), patch('backend.core.ziniao_browser.socket.create_connection', side_effect=error):
                self.assertTrue(ZiniaoBrowser._port_open(50027))


class NativeClickDomTests(unittest.TestCase):
    """Execute native-click hit testing against real local browser geometry."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright unavailable')
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel='msedge')
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def fixture(self):
        context = self.browser.new_context(viewport={'width':900,'height':600})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content('''<button id="export" style="margin-top:1500px"
          onclick="window.dispatches=(window.dispatches||0)+1">Export</button>
          <div id="mask" style="position:fixed;inset:0;z-index:10;background:transparent"></div>''')
        handle = page.query_selector('#export')
        element = Mock()
        element.is_displayed.side_effect = handle.is_visible
        element.is_enabled.side_effect = handle.is_enabled
        element.click.side_effect = handle.click
        driver = Mock()
        driver.find_elements.return_value = [element]

        def execute(script, target, scroll):
            self.assertIs(target, element)
            return page.evaluate('''([script,element,scroll]) =>
                (new Function(script)).apply(null,[element,scroll])''', [script, handle, scroll])

        driver.execute_script.side_effect = execute
        client = Mock()
        client._driver.return_value = driver
        return client, driver, element, page

    def test_transparent_transition_mask_disappears_and_dispatches_once(self):
        client,driver,element,page = self.fixture()
        page.evaluate("setTimeout(()=>document.querySelector('#mask').remove(),240)")
        ZiniaoBrowser.click(client, 'fixture', '#export')
        element.click.assert_called_once()
        driver.find_elements.assert_called_once()
        self.assertEqual(page.evaluate('window.dispatches'), 1)
        self.assertGreater(page.evaluate('scrollY'), 0)

    def test_unknown_permanent_mask_is_left_in_place_without_click(self):
        from backend.core.ziniao_browser import ZiniaoClickInterceptedError
        client,driver,element,page = self.fixture()
        # Keep the real clock: Playwright's asyncio transport also uses it.
        with self.assertRaises(ZiniaoClickInterceptedError):
            ZiniaoBrowser.click(client, 'fixture', '#export')
        element.click.assert_not_called()
        self.assertIsNone(page.evaluate('window.dispatches'))
        self.assertEqual(page.locator('#mask').count(), 1)


if __name__ == '__main__':
    unittest.main()

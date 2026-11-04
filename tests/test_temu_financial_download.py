from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from backend.services import temu_financial_download as download
from tests.test_temu_financial_workbook import make_workbook


class FinancialDownloadTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.driver = Mock()
        self.browser = SimpleNamespace(_driver=lambda _: self.driver)

    def test_native_download_is_validated_renamed_and_browser_settings_restored(self):
        def receive(*_):
            make_workbook(self.root / 'platform.xlsx', {'明细': [['ID'], ['001']]})
        with patch.object(download, 'click_control', side_effect=receive) as click:
            path = download.download_xlsx(self.browser, '1', '#global', self.root, timeout=0)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, '财务明细_全球.xlsx')
        click.assert_called_once_with(self.browser, '1', '#global')
        self.assertEqual(self.driver.execute_cdp_cmd.call_args.args[1], {'behavior': 'default'})

    def test_stale_directory_and_invalid_download_never_succeed(self):
        (self.root / 'old.xlsx').write_bytes(b'old')
        with patch.object(download, 'click_control') as click, self.assertRaisesRegex(ValueError, '非空'):
            download.download_xlsx(self.browser, '1', '#global', self.root, timeout=0)
        click.assert_not_called()
        (self.root / 'old.xlsx').unlink()
        with patch.object(download, 'click_control', side_effect=lambda *_: (self.root / 'bad.xlsx').write_bytes(b'not excel')):
            with self.assertRaises(ValueError):
                download.download_xlsx(self.browser, '1', '#global', self.root, timeout=0)

    def test_timeout_after_click_does_not_fallback_and_click_twice(self):
        with patch.object(download, 'click_control') as click, patch.object(download, 'capture_xlsx') as fallback:
            with self.assertRaisesRegex(ValueError, '超时'):
                download.download_xlsx(self.browser, '1', '#global', self.root, timeout=0)
        click.assert_called_once()
        fallback.assert_not_called()

    def test_capture_fallback_is_selected_only_before_download(self):
        self.driver.execute_cdp_cmd.side_effect = RuntimeError('unsupported transport')
        with patch.object(download, 'capture_xlsx', return_value=self.root / 'global.xlsx') as fallback:
            download.download_xlsx(self.browser, '1', '#global', self.root, timeout=0)
        fallback.assert_called_once()

    def authorization_driver(self, seller='Seller', *, region='1'):
        driver = Mock()
        driver.window_handles = ['original', 'download']
        driver.current_window_handle = 'original'
        url = 'https://seller.kuajingmaihuo.com/link-agent-seller?' + urlencode({
            'region': region, 'targetUrl': 'https://agentseller.temu.com/labor/bill-download-with-detail?params=sample',
        })
        driver.execute_cdp_cmd.return_value = {'targetInfos': [
            {'targetId': 'download', 'type': 'page', 'url': url},
        ]}
        driver.execute_script.return_value = {'text': f'即将前往 Seller Central (全球) 的 「{seller}」 店铺'}
        label, button, checkbox = Mock(), Mock(), Mock()
        label.text = '您授权您的账号ID和店铺名称在卖家中心各板块共享'
        label.is_displayed.return_value = button.is_displayed.return_value = button.is_enabled.return_value = True
        checkbox.is_selected.return_value = False
        label.find_element.return_value = checkbox
        driver.find_elements.side_effect = lambda by, _: [button] if by == 'xpath' else [label]
        return driver, label, button

    def test_only_new_same_store_global_download_authorization_is_confirmed_once(self):
        driver, label, button = self.authorization_driver()
        handled = set()
        download._continue_global_authorization(driver, {'original'}, handled, 'Seller')
        download._continue_global_authorization(driver, {'original'}, handled, 'Seller')
        driver.switch_to.window.assert_called_once_with('download')
        label.click.assert_called_once()
        button.click.assert_called_once()
        self.assertEqual(handled, {'download'})

    def test_wrong_store_or_region_and_preexisting_pages_are_never_authorized(self):
        for seller, region in [('Other', '1'), ('Seller', '2')]:
            with self.subTest(seller=seller, region=region):
                driver, label, button = self.authorization_driver(seller, region=region)
                with self.assertRaises(ValueError):
                    download._continue_global_authorization(driver, {'original'}, set(), 'Seller')
                label.click.assert_not_called()
                button.click.assert_not_called()
        driver, label, button = self.authorization_driver()
        download._continue_global_authorization(driver, {'original', 'download'}, set(), 'Seller')
        driver.switch_to.window.assert_not_called()
        button.click.assert_not_called()

    def test_ambiguous_authorization_result_is_not_resubmitted(self):
        driver, _, button = self.authorization_driver()
        handled = set()
        button.click.side_effect = RuntimeError('connection interrupted')
        with self.assertRaises(RuntimeError):
            download._continue_global_authorization(driver, {'original'}, handled, 'Seller')
        download._continue_global_authorization(driver, {'original'}, handled, 'Seller')
        button.click.assert_called_once()

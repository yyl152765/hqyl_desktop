from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.output_paths import directory_scope_key
from backend.services.temu_financial_export import load_batch
from launcher.main import resolve_entry_html


class FinancialBridgeTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = AppSettings(
            output_dir=str(self.root / 'unrelated'),
            accounts=[BoundAccount('a', 'ziniao', '示例公司', 'fixture-user', 'fixture-password', {'company': '示例公司'})],
            active_account_ids={'ziniao': 'a'})
        self.config = SimpleNamespace(local_data_dir=self.root, load=lambda: self.settings)
        with patch.object(app_bridge, 'ConfigStore', return_value=self.config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {'ok': True, 'id': 'fixture-task'}
        factory = patch.object(app_bridge, 'ZiniaoBrowser')
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.browser = self.factory.return_value.__enter__.return_value
        self.browser.list_stores.return_value = [{'store_id': '1', 'store_name': '紫鸟店铺甲', 'platform_name': 'TEMU'}]
        self.payload = {'account_id': 'a', 'store_names': '店铺甲', 'start_date': '2026-10-01',
                        'end_date': '2026-10-31', 'output_dir': str(self.root)}

    def test_bound_account_full_name_dates_and_isolated_directory_reach_task(self):
        scoped = str(self.root / 'financial-only')
        self.settings.tool_output_dirs[directory_scope_key('temu_financial_export')] = scoped
        self.assertEqual(self.bridge.get_temu_financial_export_info()['output_dir'], scoped)
        result = self.bridge.start_temu_financial_export({**self.payload, 'output_dir': ''})
        self.assertTrue(result['ok'], result)
        context = self.bridge.tasks.start.call_args.kwargs['context']
        self.assertEqual(context['account_id'], 'a')
        self.assertTrue(Path(context['manifest_path']).is_relative_to(Path(scoped)))
        record = load_batch(context['manifest_path'], context['profile'])
        self.assertEqual(record['stores'][0]['store_name'], '紫鸟店铺甲')
        self.assertNotIn('operator_name', record['stores'][0])
        self.assertEqual(record['start_date'], '2026-10-01')
        self.assertNotIn('fixture-password', Path(context['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(self.settings.output_dir, str(self.root / 'unrelated'))
        self.assertFalse(self.bridge.start_temu_fee_refund_export(self.payload)['ok'])

    def test_invalid_request_or_unmatched_shop_never_starts_task(self):
        self.assertFalse(self.bridge.start_temu_financial_export({**self.payload, 'end_date': 'invalid'})['ok'])
        self.factory.assert_not_called()
        self.assertFalse(self.bridge.start_temu_financial_export({**self.payload, 'store_names': '不存在'})['ok'])
        self.bridge.tasks.start.assert_not_called()

    def test_runner_failure_and_dispatch_failure_finalize_manifest_and_release_lock(self):
        self.assertTrue(self.bridge.start_temu_financial_export(self.payload)['ok'])
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(app_bridge, 'TemuFinancialGateway', side_effect=ValueError('浏览器失败')):
            result = runner(lambda *_: None)
        self.assertFalse(self.bridge._temu_export_active)
        self.assertTrue(all(s['status'] == 'failed' for s in result['stores']))
        self.bridge.tasks.start.return_value = {'ok': False, 'error': '启动失败'}
        self.assertFalse(self.bridge.start_temu_financial_export(self.payload)['ok'])
        context = self.bridge.tasks.start.call_args.kwargs['context']
        self.assertEqual(load_batch(context['manifest_path'])['status'], 'failed')
        self.assertFalse(self.bridge._temu_export_active)

    def test_progress_is_scoped_to_selected_account_and_frontend_entry_exists(self):
        self.assertTrue(self.bridge.start_temu_financial_export(self.payload)['ok'])
        context = self.bridge.tasks.start.call_args.kwargs['context']
        self.assertTrue(self.bridge.get_temu_financial_export_progress(context)['ok'])
        self.settings.accounts.append(BoundAccount('b', 'ziniao', '另一公司', 'other', 'password'))
        self.assertFalse(self.bridge.get_temu_financial_export_progress({**context, 'account_id': 'b'})['ok'])
        self.assertEqual(resolve_entry_html(['--page=temu-financial-export']).name, 'temu-financial-export.html')
        with patch.object(app_bridge, 'get_group_options', return_value=[]):
            self.assertTrue(any(tool['key'] == 'temu_financial_export' for tool in self.bridge.get_app_info()['tools']))

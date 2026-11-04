import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.services.temu_fee_refund_export import load_batch
from backend.output_paths import directory_scope_key

class FeeRefundBridgeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        account = BoundAccount('a', 'ziniao', '公司', 'user', 'secret', {'company':'公司'})
        settings = AppSettings(output_dir=str(self.root), accounts=[account], active_account_ids={'ziniao':'a'})
        self.settings = settings
        config = SimpleNamespace(local_data_dir=self.root, load=lambda: settings)
        with patch.object(app_bridge, 'ConfigStore', return_value=config): self.bridge = AppBridge()
        self.bridge.tasks = Mock(); self.bridge.tasks.start.return_value = {'ok':True, 'id':'x'}
        self.factory = patch.object(app_bridge, 'ZiniaoBrowser'); self.browser_class = self.factory.start(); self.addCleanup(self.factory.stop)
        self.browser = self.browser_class.return_value.__enter__.return_value
        self.browser.list_stores.return_value = [{'store_id':'1','store_name':'甲-店A','platform_name':'TEMU'}]
        self.payload = {'store_names':'店A','start_date':'2026-09-01','end_date':'2026-09-30','output_dir':str(self.root)}

    def test_info_and_start_use_only_temu_output_scope(self):
        scoped = str(self.root / 'temu-only')
        self.settings.tool_output_dirs[directory_scope_key('temu_fee_refund_export')] = scoped
        with patch.object(app_bridge, 'ziniao_runtime_paths', return_value={}):
            result = self.bridge.get_temu_fee_refund_info()
        self.assertEqual(result['output_dir'], scoped)
        self.assertTrue(self.bridge.start_temu_fee_refund_export({**self.payload, 'output_dir':''})['ok'])
        context = self.bridge.tasks.start.call_args.kwargs['context']
        self.assertTrue(Path(context['manifest_path']).is_relative_to(Path(scoped)))
        self.assertEqual(self.settings.output_dir, str(self.root))

    def test_bound_store_operator_and_date_are_persisted_without_password(self):
        self.assertTrue(self.bridge.start_temu_fee_refund_export(self.payload)['ok'])
        context = self.bridge.tasks.start.call_args.kwargs['context']
        profile = account_profile(self.bridge._payload_with_account({}, 'ziniao'))
        record = load_batch(context['manifest_path'], profile)
        self.assertEqual((record['stores'][0]['store_name'], record['stores'][0]['operator_name'], record['start_date']), ('甲-店A','甲','2026-09-01'))
        self.assertNotIn('secret', Path(context['manifest_path']).read_text(encoding='utf-8'))
        self.assertFalse(self.bridge.start_temu_fee_refund_export(self.payload)['ok'])

    def test_validation_and_unmatched_store_do_not_start_task(self):
        self.assertFalse(self.bridge.start_temu_fee_refund_export({**self.payload, 'store_names':''})['ok'])
        self.browser_class.assert_not_called()
        self.assertFalse(self.bridge.start_temu_fee_refund_export({**self.payload, 'store_names':'无此店'})['ok'])
        self.bridge.tasks.start.assert_not_called()

    def test_runner_releases_shared_export_lock_on_failure(self):
        self.assertTrue(self.bridge.start_temu_fee_refund_export(self.payload)['ok'])
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(app_bridge, 'TemuFeeRefundGateway', side_effect=ValueError('fail')):
            result = runner(lambda _:None)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(all(item['status'] == 'failed' for item in result['stores'][0]['exports'].values()))
        self.assertEqual(load_batch(result['manifest_path'], result['profile'])['status'], 'failed')
        self.assertFalse(self.bridge._temu_export_active)

    def test_dispatch_failure_finalizes_manifest(self):
        self.bridge.tasks.start.return_value = {'ok':False, 'error':'无法启动线程'}
        result = self.bridge.start_temu_fee_refund_export(self.payload)
        self.assertFalse(result['ok'])
        self.assertFalse(self.bridge._temu_export_active)
        context = self.bridge.tasks.start.call_args.kwargs['context']
        record = load_batch(context['manifest_path'], context['profile'])
        self.assertEqual(record['status'], 'failed')
        self.assertIn('无法启动线程', record['error'])

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.services import temu_on_sale_export as service


class TemuOnSaleBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = BoundAccount("ziniao-1", "ziniao", "测试公司", "operator", "test-password", {"company": "测试公司"})
        self.other = BoundAccount("ziniao-2", "ziniao", "其他公司", "other", "other-password", {"company": "其他公司"})
        settings = AppSettings(output_dir=str(self.root), accounts=[self.account, self.other], active_account_ids={"ziniao": self.account.id})
        self.settings = settings
        config = SimpleNamespace(local_data_dir=self.root, load=lambda: settings)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "task-1"}
        self.profile = account_profile(self.bridge._payload_with_account({}, "ziniao"))
        self.cli = Mock(profile=self.profile)
        self.cli.list_stores.return_value = [{"store_id": "1", "store_name": "东南亚店铺", "platform_name": "TEMU"}]
        self.patch = patch.object(app_bridge, "ZiniaoBrowser")
        self.factory = self.patch.start()
        self.factory.return_value.__enter__.return_value = self.cli
        self.addCleanup(self.patch.stop)
        paths = patch.object(app_bridge, "ziniao_runtime_paths", return_value={"client_path": "client.exe", "webdriver_path": "drivers"})
        paths.start()
        self.addCleanup(paths.stop)

    def preview(self):
        self.assertTrue(self.bridge.list_temu_on_sale_stores()["ok"])
        result = self.bridge.preview_temu_on_sale_stores({"store_names": "东南亚店铺\n东南亚店铺"})
        self.assertTrue(result["can_start"])
        return result["preview_token"]

    def test_preview_uses_selected_account_and_same_runtime_as_ads(self):
        self.assertTrue(self.preview())
        self.cli.ready.assert_called_once()
        self.cli.list_stores.assert_called_once()
        self.assertEqual(self.bridge.get_temu_on_sale_export_info()["profile"], self.profile)
        request = self.factory.call_args.args[0]
        self.assertEqual((request["account_id"], request["company"], request["username"], request["password"]),
                         (self.account.id, "测试公司", "operator", "test-password"))
        self.factory.return_value.__exit__.assert_called_once()

    def test_start_requires_preview_and_writable_directory(self):
        result = self.bridge.start_temu_on_sale_export({"store_ids": ["1"], "output_dir": str(self.root)})
        self.assertFalse(result["ok"])
        token = self.preview()
        occupied = self.root / "file"
        occupied.write_text("not a directory")
        for directory in ("", str(occupied)):
            self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": directory})["ok"])
        self.bridge.tasks.start.assert_not_called()

    def test_names_start_without_refresh_or_preview_uses_live_account_stores(self):
        self.cli.list_stores.return_value.append({'store_id':'2','store_name':'第二家店','platform_name':'TEMU'})
        self.bridge._temu_export_catalog = {'profile':'stale', 'stores':[]}
        payload = {'store_names':' 第二家店\n\n东南亚店铺\n第二家店 ', 'output_dir':str(self.root),
                   'store_ids':['spoof'], 'username':'spoof', 'password':'spoof'}
        result = self.bridge.start_temu_on_sale_export(payload)
        self.assertTrue(result['ok'], result)
        self.cli.ready.assert_called_once()
        self.cli.list_stores.assert_called_once()
        context = self.bridge.tasks.start.call_args.kwargs['context']
        record = service.load_batch(context['manifest_path'])
        self.assertEqual([item['store_id'] for item in record['stores']], ['2','1'])
        self.assertEqual(self.factory.call_args.args[0]['password'], self.account.password)
        self.assertEqual(context['account_id'], self.account.id)

    def test_names_start_reports_unmatched_ambiguous_and_non_temu_before_opening(self):
        for rows, name, message in [
            (self.cli.list_stores.return_value, '不存在的店', '未匹配'),
            ([{'store_id':str(i),'store_name':'重复店','platform_name':'TEMU'} for i in (1,2)], '重复店', '同名'),
            ([{'store_id':'1','store_name':'其他平台','platform_name':'Shopee'}], '其他平台', '平台信息')]:
            with self.subTest(name=name):
                self.cli.list_stores.return_value = rows
                result = self.bridge.start_temu_on_sale_export({'store_names':name,'output_dir':str(self.root)})
                self.assertFalse(result['ok'])
                self.assertIn(message, result['error'])
                self.assertFalse(self.bridge._temu_export_active)
        self.cli.open_store.assert_not_called()
        self.bridge.tasks.start.assert_not_called()
        self.assertFalse(list(self.root.glob('TEMU在售商品_*')))

    def test_names_start_login_failure_can_retry_without_stale_preview(self):
        payload = {'store_names':'东南亚店铺','output_dir':str(self.root)}
        self.cli.ready.side_effect = RuntimeError('自动化客户端未就绪')
        self.assertFalse(self.bridge.start_temu_on_sale_export(payload)['ok'])
        self.assertFalse(self.bridge._temu_export_active)
        self.cli.ready.side_effect = None
        self.assertTrue(self.bridge.start_temu_on_sale_export(payload)['ok'])
        self.bridge.tasks.start.assert_called_once()

    def test_empty_names_or_unwritable_output_do_not_contact_ziniao(self):
        occupied = self.root/'occupied'
        occupied.write_text('fixture')
        for names, output in [('',str(self.root)), (' \n ',str(self.root)), ('东南亚店铺',''), ('东南亚店铺',str(occupied))]:
            result = self.bridge.start_temu_on_sale_export({'store_names':names,'output_dir':output})
            self.assertFalse(result['ok'])
        self.factory.assert_not_called()
        self.bridge.tasks.start.assert_not_called()

    def test_fixed_server_selection_duplicate_start_and_no_credentials_in_context(self):
        token = self.preview()
        result = self.bridge.start_temu_on_sale_export({"preview_token": token, "store_names": "其他店", "output_dir": str(self.root)})
        self.assertTrue(result["ok"])
        context = self.bridge.tasks.start.call_args.kwargs["context"]
        record = service.load_batch(context["manifest_path"])
        self.assertEqual([store["store_name"] for store in record["stores"]], ["东南亚店铺"])
        self.assertEqual(set(context), {"manifest_path", "profile", "account_id"})
        self.assertNotIn("test-password", json.dumps(record) + json.dumps(context) + json.dumps(self.bridge._temu_export_catalog))
        self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})["ok"])
        self.assertFalse(self.bridge.retry_temu_on_sale_export({"manifest_path": context["manifest_path"]})["ok"])
        self.assertFalse(self.bridge.list_temu_on_sale_stores()["ok"])
        self.bridge.tasks.start.assert_called_once()

    def test_failed_refresh_clears_stale_preview(self):
        token = self.preview()
        self.cli.list_stores.side_effect = ValueError("auth failure")
        self.assertFalse(self.bridge.list_temu_on_sale_stores()["ok"])
        self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})["ok"])

    def test_changed_profile_or_expired_preview_cannot_start(self):
        token = self.preview()
        self.settings.active_account_ids["ziniao"] = self.other.id
        self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})["ok"])
        self.settings.active_account_ids["ziniao"] = self.account.id
        self.bridge._temu_export_previews[token]["created"] = time.monotonic() - 1000
        self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})["ok"])

    def test_runner_failure_releases_lock_and_manifest_recovers(self):
        token = self.preview()
        self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})
        runner = self.bridge.tasks.start.call_args.args[1]
        path = self.bridge.tasks.start.call_args.kwargs["context"]["manifest_path"]
        with patch.object(app_bridge, "TemuOnSaleGateway", side_effect=RuntimeError("模拟异常")):
            with self.assertRaises(RuntimeError):
                runner(Mock())
        self.assertFalse(self.bridge._temu_export_active)
        recovered = self.bridge.get_temu_on_sale_export_progress({"manifest_path": path})
        self.assertTrue(recovered["result"]["can_retry"])
        self.assertEqual(recovered["result"]["status"], "interrupted")
        self.assertEqual(self.bridge.get_temu_on_sale_export_info()["latest"]["manifest_path"], path)

    def test_dispatch_failure_releases_lock(self):
        token = self.preview()
        self.bridge.tasks.start.side_effect = RuntimeError("无法创建线程")
        self.assertFalse(self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})["ok"])
        self.assertFalse(self.bridge._temu_export_active)

    def test_no_bound_account_requests_existing_account_dialog(self):
        self.settings.accounts = []
        result = self.bridge.list_temu_on_sale_stores()
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "ziniao")
        self.assertEqual(self.bridge.get_temu_on_sale_export_info()["profile"], "")
        self.factory.assert_not_called()

    def test_frontend_credentials_cannot_override_bound_account(self):
        result = self.bridge.list_temu_on_sale_stores({"account_id": self.account.id, "username": "spoof", "password": "spoof", "company": "spoof"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.factory.call_args.args[0]["password"], self.account.password)
        self.assertEqual(self.factory.call_args.args[0]["company"], "测试公司")
        self.assertFalse(self.bridge.list_temu_on_sale_stores({"account_id": "unknown"})["ok"])

    def test_cross_account_progress_and_retry_and_cli_history_rejected(self):
        path = service.create_batch(self.root, self.profile, self.cli.list_stores.return_value)
        request = {"manifest_path": str(path), "account_id": self.other.id}
        self.assertFalse(self.bridge.get_temu_on_sale_export_progress(request)["ok"])
        self.assertFalse(self.bridge.retry_temu_on_sale_export(request)["ok"])
        legacy = service.create_batch(self.root, "旧CLI账号", self.cli.list_stores.return_value)
        self.assertFalse(self.bridge.retry_temu_on_sale_export({"manifest_path": str(legacy)})["ok"])
        self.bridge.tasks.start.assert_not_called()

    def test_runner_keeps_original_account_after_active_account_changes(self):
        token = self.preview()
        self.bridge.start_temu_on_sale_export({"preview_token": token, "output_dir": str(self.root)})
        runner = self.bridge.tasks.start.call_args.args[1]
        self.settings.active_account_ids["ziniao"] = self.other.id
        with patch.object(app_bridge.temu_export, "run_batch", return_value={"ok": True}), patch.object(app_bridge, "TemuOnSaleGateway"):
            runner(Mock())
        self.assertEqual(self.factory.call_args.args[0]["account_id"], self.account.id)
        self.assertEqual(self.factory.call_args.args[0]["password"], self.account.password)


if __name__ == "__main__":
    unittest.main()

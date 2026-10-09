from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount


class TemuShippingBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="temu-bridge-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = BoundAccount("mabang-a", "mabang", "测试", "bound-user", "bound-secret")
        self.settings = AppSettings(accounts=[self.account], active_account_ids={"mabang": self.account.id})
        config = SimpleNamespace(local_data_dir=self.root, load=lambda: self.settings)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "test-task"}
        self.source = self.root / "input.xlsx"
        self.source.write_bytes(b"placeholder; parser is mocked")

    def test_all_account_actions_require_bound_mabang_account(self):
        self.settings.accounts = []
        for action in (self.bridge.start_temu_shipping_preview, self.bridge.start_temu_shipping_batch, self.bridge.get_temu_shipping_checkpoint):
            result = action({"username": "override", "password": "override", "input_file": str(self.source)})
            self.assertFalse(result["ok"])
            self.assertTrue(result["requires_account"])
        self.bridge.tasks.start.assert_not_called()

    def test_preview_uses_bound_credentials_and_sanitized_task_context(self):
        result = self.bridge.start_temu_shipping_preview({
            "input_file": str(self.source), "username": "override", "password": "override",
        })
        self.assertTrue(result["ok"])
        args, kwargs = self.bridge.tasks.start.call_args
        self.assertEqual(kwargs["tool"], "temu_shipping_preview")
        self.assertEqual(kwargs["context"]["account_id"], "mabang-a")
        self.assertNotIn("password", kwargs["context"])
        self.assertNotIn("username", kwargs["context"])
        with patch.object(app_bridge, "preview_temu_shipping", return_value={"preview": True}) as preview:
            self.assertEqual(args[1](Mock()), {"preview": True})
        self.assertEqual(preview.call_args.kwargs["username"], "bound-user")
        self.assertEqual(preview.call_args.kwargs["password"], "bound-secret")
        self.assertEqual(self.bridge._temu_running_accounts, set())

    def test_repeated_click_and_account_alias_cannot_start_concurrently(self):
        self.settings.accounts.append(BoundAccount("alias", "mabang", "同账号", "bound-user", "bound-secret"))
        self.assertTrue(self.bridge.start_temu_shipping_preview({"input_file": str(self.source)})["ok"])
        result = self.bridge.start_temu_shipping_preview({"input_file": str(self.source), "account_id": "alias"})
        self.assertFalse(result["ok"])
        self.assertIn("正在运行", result["error"])
        self.bridge.tasks.start.assert_called_once()

    def test_start_failure_releases_account(self):
        self.bridge.tasks.start.side_effect = RuntimeError("cannot spawn")
        result = self.bridge.start_temu_shipping_preview({"input_file": str(self.source)})
        self.assertFalse(result["ok"])
        self.assertEqual(self.bridge._temu_running_accounts, set())
        self.bridge.tasks.start.side_effect = None
        self.assertTrue(self.bridge.start_temu_shipping_preview({"input_file": str(self.source)})["ok"])

    def test_runner_error_redacts_password_and_releases_lock(self):
        self.bridge.start_temu_shipping_preview({"input_file": str(self.source)})
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(app_bridge, "preview_temu_shipping", side_effect=RuntimeError("bad bound-secret")):
            with self.assertRaisesRegex(ValueError, "已隐藏") as caught:
                runner(Mock())
        self.assertNotIn("bound-secret", str(caught.exception))
        self.assertFalse(self.bridge._temu_running_accounts)

    def test_missing_or_wrong_file_is_rejected_before_start(self):
        for path in ("", str(self.root / "missing.xlsx"), str(self.root / "input.csv")):
            self.assertFalse(self.bridge.start_temu_shipping_preview({"input_file": path})["ok"])
        self.bridge.tasks.start.assert_not_called()

    def test_batch_uses_only_server_saved_run_and_account(self):
        store = Mock()
        store.load.return_value = {"input_file": str(self.source)}
        with patch.object(self.bridge, "_temu_shipping_store", return_value=store), patch.object(
            app_bridge, "temu_shipping_public_result", return_value={"can_apply": True},
        ):
            result = self.bridge.start_temu_shipping_batch({
                "run_id": "a" * 32, "input_file": "changed.xlsx", "rows": [{"weight": "999"}],
            })
        self.assertTrue(result["ok"])
        store.load.assert_called_once_with("a" * 32, app_bridge.temu_shipping_account_key("bound-user"))
        args, kwargs = self.bridge.tasks.start.call_args
        self.assertEqual(kwargs["context"]["input_file"], str(self.source))
        self.assertEqual(kwargs["tool"], "temu_shipping_batch")
        with patch.object(app_bridge, "run_temu_shipping_batch", return_value={"done": True}) as batch:
            args[1](Mock())
        self.assertEqual(batch.call_args.kwargs["run_id"], "a" * 32)
        self.assertNotIn("rows", batch.call_args.kwargs)
        self.assertNotIn("input_file", batch.call_args.kwargs)

    def test_insufficient_data_and_completed_run_never_start_batch(self):
        store = Mock()
        store.load.return_value = {"input_file": str(self.source)}
        with patch.object(self.bridge, "_temu_shipping_store", return_value=store):
            for message in ("Excel 缺少 19 组", "全部渠道已设置并核验"):
                with patch.object(app_bridge, "temu_shipping_public_result", return_value={"can_apply": False, "message": message}):
                    result = self.bridge.start_temu_shipping_batch({"run_id": "a" * 32})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], message)
        self.bridge.tasks.start.assert_not_called()

    def test_reading_checkpoint_is_scoped_by_bound_account(self):
        store = Mock()
        store.latest.return_value = None
        with patch.object(self.bridge, "_temu_shipping_store", return_value=store):
            self.assertEqual(self.bridge.get_temu_shipping_checkpoint({}), {"ok": True, "result": None})
        store.latest.assert_called_once_with(app_bridge.temu_shipping_account_key("bound-user"))

    def test_inspect_and_dialog_share_parser(self):
        workbook = {"input_file": str(self.source), "row_count": 80}
        with patch.object(app_bridge, "read_temu_shipping_workbook", return_value=workbook) as parser:
            response = self.bridge.inspect_temu_shipping_file({"input_file": str(self.source)})
        self.assertTrue(response["ok"])
        self.assertEqual(response["workbook"]["row_count"], 80)
        parser.assert_called_once_with(str(self.source))


if __name__ == "__main__":
    unittest.main()

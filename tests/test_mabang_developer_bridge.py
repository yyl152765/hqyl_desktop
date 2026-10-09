from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount, ConfigStore


class MabangDeveloperBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mabang-developer-bridge-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        store = ConfigStore(self.root / "settings.json")
        store.local_data_dir = self.root / "local"
        store.logs_dir = store.local_data_dir / "logs"
        self.enterContext(patch("backend.config_store._load_legacy_dingtalk_credentials", return_value={}))
        self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Bridge tests must not access the network"),
        ))
        store.save(AppSettings(output_dir=str(self.root / "exports")))
        with patch.object(app_bridge, "ConfigStore", return_value=store):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.started = {"ok": True, "id": "test-task"}
        self.bridge.tasks.start.return_value = self.started
        self.account = BoundAccount("mabang-a", "mabang", "测试账号", "bound-user", "bound-secret")
        self.other_account = BoundAccount("mabang-b", "mabang", "另一账号", "other-user", "other-secret")
        self.names = ["张三", "李四"]
        self.rows = [
            {"input_name": "张三", "employee_id": "11", "status": "ready", "can_apply": True},
            {"input_name": "李四", "employee_id": "12", "status": "unchanged", "can_apply": False},
        ]
        self.preview_result = {
            "rows": self.rows,
            "ready_count": 1,
            "unchanged_count": 1,
            "custom_service_metadata": {"kept": True},
        }
        self.validate_preview = self.enterContext(patch.object(
            app_bridge,
            "validate_mabang_developer_preview",
            wraps=app_bridge.validate_mabang_developer_preview,
        ))
        self.preview_service = self.enterContext(patch.object(
            app_bridge,
            "preview_mabang_developer_permissions",
            side_effect=lambda _query, _progress: copy.deepcopy(self.preview_result),
        ))
        self.build_job = self.enterContext(patch.object(
            app_bridge, "build_developer_permission_batch_job", return_value=sentinel.job,
        ))
        self.batch_result = {"success_count": 1, "failed_count": 0, "rows": [{"status": "success"}]}
        self.batch_service = self.enterContext(patch.object(
            app_bridge, "run_mabang_developer_permission_batch", return_value=self.batch_result,
        ))

    def bind_accounts(self) -> None:
        self.bridge.config_store.save({
            "accounts": [self.account, self.other_account],
            "active_account_ids": {"mabang": self.account.id},
        })

    def create_preview(self) -> str:
        started = self.bridge.start_mabang_developer_permission_preview({"employee_names": self.names})
        self.assertTrue(started["ok"])
        runner = self.bridge.tasks.start.call_args.args[1]
        result = runner(Mock())
        self.bridge.tasks.reset_mock()
        return result["preview_token"]

    def batch_payload(self, token: str, **changes) -> dict:
        return {"preview_token": token, "employee_names": list(self.names), **changes}

    def test_both_actions_require_bound_mabang_account_even_with_request_credentials(self) -> None:
        for action in (
            self.bridge.start_mabang_developer_permission_preview,
            self.bridge.start_mabang_developer_permission_batch,
        ):
            with self.subTest(action=action.__name__):
                result = action({
                    "username": "unbound-user", "password": "unbound-secret",
                    "employee_names": self.names, "preview_token": "invented",
                })
                self.assertFalse(result["ok"])
                self.assertTrue(result["requires_account"])
                self.assertEqual(result["vendor"], "mabang")
        self.bridge.tasks.start.assert_not_called()
        self.validate_preview.assert_not_called()
        self.build_job.assert_not_called()

    def test_unknown_or_wrong_vendor_account_cannot_use_bound_mabang_credentials(self) -> None:
        self.bind_accounts()
        self.bridge.config_store.save({"accounts": [
            self.account, BoundAccount("bs-a", "bigseller", "BS", "bs-user", "bs-secret"),
        ]})
        for account_id in ("unknown", "bs-a"):
            for action in (
                self.bridge.start_mabang_developer_permission_preview,
                self.bridge.start_mabang_developer_permission_batch,
            ):
                with self.subTest(account_id=account_id, action=action.__name__):
                    result = action({"account_id": account_id, "employee_names": self.names})
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["requires_account"])
        self.bridge.tasks.start.assert_not_called()

    def test_preview_uses_bound_credentials_preserves_result_and_returns_token(self) -> None:
        self.bind_accounts()
        started = self.bridge.start_mabang_developer_permission_preview({
            "employee_text": " 张三 \n李四\n张三 ",
            "username": "client-override", "password": "client-override",
        })
        self.assertIs(started, self.started)
        validated = self.validate_preview.call_args.args[0]
        self.assertEqual(validated["account_id"], self.account.id)
        self.assertEqual(validated["username"], self.account.username)
        self.assertEqual(validated["password"], self.account.password)
        task_call = self.bridge.tasks.start.call_args
        self.assertEqual(task_call.kwargs, {
            "tool": "mabang_developer_permission_preview",
            "context": {"account_id": self.account.id, "mode": "preview", "employee_names": self.names},
        })
        self.assertEqual(self.bridge._mabang_developer_previews, {})
        progress = Mock()
        result = task_call.args[1](progress)
        query = self.preview_service.call_args.args[0]
        self.preview_service.assert_called_once_with(query, progress)
        self.assertEqual(query.employee_names, tuple(self.names))
        token = result["preview_token"]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual({key: value for key, value in result.items() if key != "preview_token"}, self.preview_result)
        record = self.bridge._mabang_developer_previews[token]
        self.assertEqual(record["account_id"], self.account.id)
        self.assertEqual(record["employee_names"], self.names)
        self.assertEqual(record["rows"], self.rows)

    def test_invalid_preview_does_not_start_task(self) -> None:
        self.bind_accounts()
        result = self.bridge.start_mabang_developer_permission_preview({"employee_names": []})
        self.assertFalse(result["ok"])
        self.assertIn("员工", result["error"])
        self.bridge.tasks.start.assert_not_called()
        self.preview_service.assert_not_called()

    def test_batch_uses_server_rows_bound_credentials_and_preserves_service_result(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        server_rows = self.bridge._mabang_developer_previews[token]["rows"]
        result = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(
            token,
            rows=[{"input_name": "冒名员工", "employee_id": "999", "can_apply": True}],
            preview_rows=[{"employee_id": "999"}],
            username="client-override", password="client-override",
        ))
        self.assertIs(result, self.started)
        self.build_job.assert_called_once_with(
            username=self.account.username, password=self.account.password, preview_rows=server_rows,
        )
        self.assertIs(self.build_job.call_args.kwargs["preview_rows"], server_rows)
        self.assertNotIn(token, self.bridge._mabang_developer_previews)
        task_call = self.bridge.tasks.start.call_args
        self.assertEqual(task_call.kwargs, {
            "tool": "mabang_developer_permission_batch",
            "context": {"account_id": self.account.id, "mode": "batch", "employee_names": self.names},
        })
        progress = Mock()
        self.assertIs(task_call.args[1](progress), self.batch_result)
        self.batch_service.assert_called_once_with(sentinel.job, progress)

    def test_changed_employee_list_or_account_is_rejected_without_consuming_preview(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        cases = (
            ({"employee_names": ["张三"]}, "员工名单已变化"),
            ({"employee_names": list(reversed(self.names))}, "员工名单已变化"),
            ({"account_id": self.other_account.id}, "马帮账号已变化"),
        )
        for changes, error in cases:
            with self.subTest(changes=changes):
                result = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token, **changes))
                self.assertFalse(result["ok"])
                self.assertIn(error, result["error"])
                self.assertIn(token, self.bridge._mabang_developer_previews)
        self.bridge.tasks.start.assert_not_called()
        self.build_job.assert_not_called()

    def test_missing_unknown_and_expired_tokens_are_rejected(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        self.bridge._mabang_developer_previews[token]["created_at"] = time.time() - 1801
        for invalid_token, error in (("", "请先完成预览"), ("unknown", "预览已失效"), (token, "预览已失效")):
            with self.subTest(token=invalid_token):
                result = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(invalid_token))
                self.assertFalse(result["ok"])
                self.assertIn(error, result["error"])
        self.bridge.tasks.start.assert_not_called()
        self.build_job.assert_not_called()

    def test_successfully_queued_token_cannot_start_a_second_save(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        first = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token))
        second = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("预览已失效", second["error"])
        self.bridge.tasks.start.assert_called_once()
        self.build_job.assert_called_once()
        self.batch_service.assert_not_called()

    def test_task_start_failure_restores_token_and_allows_retry(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        record = self.bridge._mabang_developer_previews[token]
        for failure in ({"ok": False, "error": "队列繁忙"}, RuntimeError("线程启动失败")):
            with self.subTest(failure=failure):
                if isinstance(failure, Exception):
                    self.bridge.tasks.start.side_effect = failure
                else:
                    self.bridge.tasks.start.side_effect = None
                    self.bridge.tasks.start.return_value = failure
                result = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token))
                self.assertFalse(result["ok"])
                self.assertIs(self.bridge._mabang_developer_previews[token], record)
        self.bridge.tasks.start.side_effect = None
        self.bridge.tasks.start.return_value = self.started
        retried = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token))
        self.assertTrue(retried["ok"])
        self.assertNotIn(token, self.bridge._mabang_developer_previews)

    def test_invalid_server_preview_does_not_consume_token_or_start_task(self) -> None:
        self.bind_accounts()
        token = self.create_preview()
        self.build_job.side_effect = ValueError("员工预览快照不完整，请重新预览")
        result = self.bridge.start_mabang_developer_permission_batch(self.batch_payload(token))
        self.assertFalse(result["ok"])
        self.assertIn("快照不完整", result["error"])
        self.assertIn(token, self.bridge._mabang_developer_previews)
        self.bridge.tasks.start.assert_not_called()

    def test_developer_page_is_accessible_from_launcher_and_sidebar(self) -> None:
        from launcher.main import resolve_entry_html

        page = resolve_entry_html(["--page=mabang-developer-permission"])
        self.assertEqual(page.name, "mabang-developer-permission.html")
        self.assertTrue(page.is_file())
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        sidebar = (frontend / "assets" / "common.js").read_text(encoding="utf-8")
        self.assertIn("mabang-developer-permission.html", sidebar)
        self.assertIn("批量添加开发员", sidebar)
        script = (frontend / "assets" / "pages" / "mabang-developer-permission.js").read_text(encoding="utf-8")
        self.assertIn("start_mabang_developer_permission_preview", script)
        self.assertIn("start_mabang_developer_permission_batch", script)


if __name__ == "__main__":
    unittest.main()

"""Offline contracts for the Lazada ads page, account selection and task runner."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge, config_store
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount, DingTalkSettings, DingTalkUserBinding
from backend.services import lazada_ads_data as service


class LazadaAdsBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = BoundAccount("ziniao-a", "ziniao", "公司 A", "operator-a", "private-a", {"company": "公司 A"})
        self.other = BoundAccount("ziniao-b", "ziniao", "公司 B", "operator-b", "private-b", {"company": "公司 B"})
        self.settings = AppSettings(
            output_dir=str(self.root),
            accounts=[self.account, self.other],
            active_account_ids={"ziniao": self.account.id},
            dingtalk=DingTalkSettings(
                app_key="fixture-app-key", app_secret="fixture-app-secret",
                users=[DingTalkUserBinding("操作人甲", "fixture-user-a")],
            ),
        )
        config = SimpleNamespace(load=lambda: self.settings, local_data_dir=self.root)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-ads-task"}
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("offline test must not use network")).start()
        self.addCleanup(patch.stopall)

    def payload(self, **extra):
        return {
            "sheet_name": "26年1月", "target_date": "2026-01-02",
            "store_names": "店铺 A\n店铺 A\n店铺 B", **extra,
        }

    def assert_no_credentials(self, result) -> None:
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in (
            self.account.password, self.other.password,
            "fixture-app-key", "fixture-app-secret", "fixture-user-a", "fixture-user-b",
            "password", "dingtalk_app_key", "dingtalk_app_secret", "dingtalk_user_id",
        ):
            self.assertNotIn(secret, serialized)

    def test_info_defaults_to_yesterday_sheet_and_yesterday_without_credentials(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        with patch.object(app_bridge, "resolve_lazada_monthly_report_runtime_paths", return_value={"client_path": "fixture-client", "webdriver_path": "fixture-driver"}):
            result = self.bridge.get_lazada_ads_data_info()
        self.assertTrue(result["ok"])
        self.assertEqual(result["default_sheet_name"], f"{yesterday.year % 100:02d}年{yesterday.month}月")
        self.assertEqual(result["default_target_date"], yesterday.isoformat())
        self.assertEqual(result["operator_names"], ["操作人甲"])
        self.assertEqual(result["workbook_id"], service.DEFAULT_WORKBOOK_ID)
        self.assertTrue(result["dingtalk_configured"])
        self.assertEqual(result["output_dir"], str(self.root))
        self.assertEqual(result["client_path"], "fixture-client")
        self.assertEqual(result["webdriver_path"], "fixture-driver")
        self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()
        self.network.assert_not_called()

    def test_info_loads_without_bound_accounts_dingtalk_or_installed_runtime(self) -> None:
        self.settings.accounts = []
        self.settings.active_account_ids = {}
        self.settings.dingtalk = DingTalkSettings()
        with patch.object(app_bridge, "resolve_lazada_monthly_report_runtime_paths", side_effect=RuntimeError("未安装客户端")):
            result = self.bridge.get_lazada_ads_data_info()
        self.assertTrue(result["ok"])
        self.assertFalse(result["dingtalk_configured"])
        self.assertEqual(result["operator_names"], [])
        self.assertEqual((result["client_path"], result["webdriver_path"]), ("", ""))
        self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()

    def test_start_uses_selected_bound_credentials_and_passes_validated_job_to_runner(self) -> None:
        payload = self.payload(
            account_id=self.other.id, username="forged", password="forged", company="forged",
            dingtalk_operator_name="操作人甲", dingtalk_app_key="forged", dingtalk_app_secret="forged",
            dingtalk_user_id="forged",
        )
        with patch.object(service, "validate_lazada_ads_payload", wraps=service.validate_lazada_ads_payload) as validate:
            result = self.bridge.start_lazada_ads_data(payload)
        self.assertTrue(result["ok"], result)
        request = validate.call_args.args[0]
        self.assertEqual((request["username"], request["password"], request["company"]), ("operator-b", "private-b", "公司 B"))
        self.assertEqual((request["dingtalk_app_key"], request["dingtalk_app_secret"], request["dingtalk_user_id"]), ("fixture-app-key", "fixture-app-secret", "fixture-user-a"))
        self.assertEqual(payload["password"], "forged", "Bridge must not mutate the caller's payload")
        call = self.bridge.tasks.start.call_args
        self.assertEqual(call.kwargs["tool"], "lazada_ads_data")
        self.assertEqual(call.kwargs["context"], {
            "account_id": self.other.id, "sheet_name": "26年1月", "target_date": "2026-01-02",
            "store_count": 2, "output_root": str(self.root),
        })
        self.assert_no_credentials(call.kwargs["context"])
        progress = Mock()
        expected = {"success_count": 2, "failed_count": 0, "output_dir": str(self.root)}
        with patch.object(service, "run_lazada_ads_data", return_value=expected) as run:
            actual = call.args[1](progress)
        self.assertIs(actual, expected)
        self.assertIs(run.call_args.args[1], progress)
        job = run.call_args.args[0]
        self.assertEqual(tuple(job.store_names), ("店铺 A", "店铺 B"))
        self.assertEqual(job.sheet_name, "26年1月")
        self.assertEqual(str(job.target_date), "2026-01-02")
        self.assertEqual(str(job.output_root), str(self.root))
        self.network.assert_not_called()

    def test_one_operator_is_selected_automatically_and_active_account_is_used(self) -> None:
        with patch.object(service, "validate_lazada_ads_payload", wraps=service.validate_lazada_ads_payload) as validate:
            result = self.bridge.start_lazada_ads_data(self.payload())
        self.assertTrue(result["ok"], result)
        request = validate.call_args.args[0]
        self.assertEqual(request["dingtalk_user_id"], "fixture-user-a")
        self.assertEqual(request["account_id"], self.account.id)

    def test_start_defaults_missing_empty_and_whitespace_sheet_and_date(self) -> None:
        today = date.today()
        for empty_fields in ({}, {"sheet_name": "", "target_date": ""},
                             {"sheet_name": " \t\n ", "target_date": " \t\n "}):
            with self.subTest(empty_fields=empty_fields):
                self.bridge.tasks.start.reset_mock()
                result = self.bridge.start_lazada_ads_data({"store_names": "店铺 A", **empty_fields})
                self.assertTrue(result["ok"], result)
                call = self.bridge.tasks.start.call_args
                self.assertEqual(call.kwargs["context"]["sheet_name"], service.default_sheet_name(today))
                self.assertEqual(call.kwargs["context"]["target_date"], service.default_target_date(today))
                with patch.object(service, "run_lazada_ads_data", return_value={}) as run:
                    call.args[1](Mock())
                job = run.call_args.args[0]
                self.assertEqual(job.sheet_name, service.default_sheet_name(today))
                self.assertEqual(job.target_date, service.default_target_date(today))
        self.network.assert_not_called()

    def test_multiple_operators_require_explicit_selection(self) -> None:
        self.settings.dingtalk.users.append(DingTalkUserBinding("操作人乙", "fixture-user-b"))
        result = self.bridge.start_lazada_ads_data(self.payload())
        self.assertFalse(result["ok"])
        self.assertIn("操作人", result["error"])
        self.bridge.tasks.start.assert_not_called()
        with patch.object(service, "validate_lazada_ads_payload", wraps=service.validate_lazada_ads_payload) as validate:
            result = self.bridge.start_lazada_ads_data(self.payload(dingtalk_operator_name="操作人乙"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(validate.call_args.args[0]["dingtalk_user_id"], "fixture-user-b")

    def test_unknown_operator_or_no_users_prevents_task_creation(self) -> None:
        for users, operator in ((self.settings.dingtalk.users, "未知操作人"), ([], "")):
            with self.subTest(operator=operator):
                self.settings.dingtalk.users = users
                result = self.bridge.start_lazada_ads_data(self.payload(dingtalk_operator_name=operator, dingtalk_user_id="forged"))
                self.assertFalse(result["ok"])
                self.assertIn("操作人", result["error"])
        self.bridge.tasks.start.assert_not_called()

    def test_unconfigured_dingtalk_cannot_be_replaced_by_payload_secrets(self) -> None:
        for key, secret in (("", "fixture-app-secret"), ("fixture-app-key", ""), ("", "")):
            with self.subTest(key_configured=bool(key), secret_configured=bool(secret)):
                self.settings.dingtalk.app_key = key
                self.settings.dingtalk.app_secret = secret
                result = self.bridge.start_lazada_ads_data(self.payload(dingtalk_app_key="forged", dingtalk_app_secret="forged"))
                self.assertFalse(result["ok"])
                self.assertIn("钉钉", result["error"])
        self.bridge.tasks.start.assert_not_called()

    def test_missing_or_unknown_bound_account_prevents_task_creation(self) -> None:
        for account_id in ("unknown-account", self.account.id):
            with self.subTest(account_id=account_id):
                if account_id == self.account.id:
                    self.settings.accounts = []
                    self.settings.active_account_ids = {}
                result = self.bridge.start_lazada_ads_data(self.payload(account_id=account_id, username="forged", password="forged"))
                self.assertFalse(result["ok"])
                self.assertTrue(result["requires_account"])
                self.assertEqual(result["vendor"], "ziniao")
        self.bridge.tasks.start.assert_not_called()

    def test_invalid_store_names_or_date_prevents_task_creation(self) -> None:
        for extra in ({"store_names": " \n "}, {"target_date": "2026-02-30"}, {"target_date": "not-a-date"}):
            with self.subTest(extra=extra):
                result = self.bridge.start_lazada_ads_data(self.payload(**extra))
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
        self.bridge.tasks.start.assert_not_called()
        self.network.assert_not_called()


class LazadaAdsPreferencesBridgeTests(unittest.TestCase):
    """Persist page defaults through the real store, exclusively in a temp dir."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config_store, "_base_local_data_dir", return_value=self.root / "local-data"))
        self.stack.enter_context(patch.object(config_store, "desktop_path", return_value=self.root / "desktop"))
        self.stack.enter_context(patch.object(config_store, "_load_legacy_dingtalk_credentials", return_value={}))
        self.stack.enter_context(patch.object(config_store, "_load_default_dingtalk_user_bindings", return_value=[]))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("preferences must not use network")))
        self.path = self.root / "settings.json"
        self.config = config_store.ConfigStore(self.path)
        self.config.save(AppSettings(output_dir=str(self.root / "output"), rows_per_page=200))
        with patch.object(app_bridge, "ConfigStore", return_value=self.config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-ads-task"}
        self.discovery = self.stack.enter_context(patch.object(
            app_bridge, "resolve_lazada_monthly_report_runtime_paths",
            return_value={"client_path": "detected-ziniao.exe", "webdriver_path": ""},
        ))

    def reload(self) -> AppSettings:
        return config_store.ConfigStore(self.path).load()

    def save_initial_preferences(self) -> None:
        result = self.bridge.save_lazada_ads_preferences({
            "client_path": "saved-ziniao.exe", "workbook_id": "saved-workbook",
        })
        self.assertTrue(result["ok"], result)

    def test_both_preferences_roundtrip_without_accounts_or_dingtalk_configuration(self) -> None:
        result = self.bridge.save_lazada_ads_preferences({
            "client_path": "  D:/fixture/ziniao.exe  ",
            "workbook_id": "https://alidocs.dingtalk.com/i/nodes/fixture-workbook?utm_scene=person_space",
        })
        self.assertTrue(result["ok"], result)
        settings = self.reload()
        self.assertEqual(settings.lazada_ads_client_path, "D:/fixture/ziniao.exe")
        self.assertEqual(settings.lazada_ads_workbook_id, "fixture-workbook")
        self.assertEqual(settings.accounts, [])
        self.assertEqual(settings.dingtalk.app_key, "")
        self.assertEqual(settings.dingtalk.app_secret, "")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["lazada_ads_client_path"], "D:/fixture/ziniao.exe")
        self.assertEqual(saved["lazada_ads_workbook_id"], "fixture-workbook")
        self.assertNotIn("client_path", saved)
        self.assertNotIn("workbook_id", saved)
        self.bridge.tasks.start.assert_not_called()

    def test_single_field_updates_preserve_the_other_preference(self) -> None:
        self.save_initial_preferences()
        result = self.bridge.save_lazada_ads_preferences({"client_path": "next-ziniao.exe"})
        self.assertTrue(result["ok"], result)
        settings = self.reload()
        self.assertEqual((settings.lazada_ads_client_path, settings.lazada_ads_workbook_id), ("next-ziniao.exe", "saved-workbook"))
        result = self.bridge.save_lazada_ads_preferences({"workbook_id": "next-workbook"})
        self.assertTrue(result["ok"], result)
        settings = self.reload()
        self.assertEqual((settings.lazada_ads_client_path, settings.lazada_ads_workbook_id), ("next-ziniao.exe", "next-workbook"))

    def test_extra_payload_keys_cannot_change_accounts_secrets_or_unrelated_settings(self) -> None:
        before = json.loads(self.path.read_text(encoding="utf-8"))
        result = self.bridge.save_lazada_ads_preferences({
            "client_path": "selected-ziniao.exe", "workbook_id": "selected-workbook",
            "password": "forged-password", "username": "forged-user",
            "dingtalk": {"app_key": "forged-app", "app_secret": "forged-secret"},
            "accounts": [{"id": "forged", "vendor": "ziniao", "username": "forged-user", "password": "forged-password"}],
            "output_dir": "forged-output", "rows_per_page": 999,
            "sheet_name": "fixed-sheet", "target_date": "2000-01-01",
            "lazada_ads_client_path": "forged-direct-field", "lazada_ads_workbook_id": "forged-direct-id",
        })
        self.assertTrue(result["ok"], result)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after, {**before, "lazada_ads_client_path": "selected-ziniao.exe", "lazada_ads_workbook_id": "selected-workbook"})
        self.assertNotIn("forged", json.dumps(result))
        self.bridge.tasks.start.assert_not_called()

    def test_invalid_workbook_id_does_not_write_either_preference(self) -> None:
        self.save_initial_preferences()
        before = self.path.read_bytes()
        for workbook in ("https://alidocs.dingtalk.com/i/p/shared", "not a workbook id"):
            with self.subTest(workbook=workbook), patch.object(self.config, "save", wraps=self.config.save) as save:
                result = self.bridge.save_lazada_ads_preferences({"client_path": "would-change.exe", "workbook_id": workbook})
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                save.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_clearing_each_preference_restores_its_default_without_clearing_the_other(self) -> None:
        self.save_initial_preferences()
        result = self.bridge.save_lazada_ads_preferences({"client_path": " \t "})
        self.assertTrue(result["ok"], result)
        settings = self.reload()
        self.assertEqual((settings.lazada_ads_client_path, settings.lazada_ads_workbook_id), ("", "saved-workbook"))
        info = self.bridge.get_lazada_ads_data_info()
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["client_path"], "detected-ziniao.exe")
        self.assertEqual(info["workbook_id"], "saved-workbook")
        result = self.bridge.save_lazada_ads_preferences({"workbook_id": " \t "})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.reload().lazada_ads_workbook_id, "")
        info = self.bridge.get_lazada_ads_data_info()
        self.assertEqual(info["client_path"], "detected-ziniao.exe")
        self.assertEqual(info["workbook_id"], service.DEFAULT_WORKBOOK_ID)

    def test_saved_preferences_take_precedence_while_sheet_and_date_follow_yesterday(self) -> None:
        self.save_initial_preferences()
        with patch.object(service, "date", wraps=date) as dates:
            # 月初也要落到上个月的表：11/01 的默认采集日是 10/31，Sheet 仍为 26年10月。
            for today, sheet in ((date(2026, 10, 31), "26年10月"), (date(2026, 11, 1), "26年10月")):
                with self.subTest(today=today):
                    dates.today.return_value = today
                    info = self.bridge.get_lazada_ads_data_info()
                    self.assertTrue(info["ok"], info)
                    self.assertEqual(info["client_path"], "saved-ziniao.exe")
                    self.assertEqual(info["workbook_id"], "saved-workbook")
                    self.assertEqual(info["default_sheet_name"], sheet)
                    self.assertEqual(info["default_target_date"], (today - timedelta(days=1)).isoformat())

    def test_info_distinguishes_saved_preferences_from_displayed_defaults(self) -> None:
        info = self.bridge.get_lazada_ads_data_info()
        self.assertEqual(info["saved_preferences"], {"client_path": "", "workbook_id": ""})
        self.assertEqual(info["client_path"], "detected-ziniao.exe")
        self.assertEqual(info["workbook_id"], service.DEFAULT_WORKBOOK_ID)
        self.save_initial_preferences()
        info = self.bridge.get_lazada_ads_data_info()
        self.assertEqual(info["saved_preferences"], {
            "client_path": "saved-ziniao.exe", "workbook_id": "saved-workbook",
        })

    def test_saved_preferences_remain_available_if_installation_discovery_fails(self) -> None:
        self.save_initial_preferences()
        self.discovery.side_effect = RuntimeError("installation unavailable")
        info = self.bridge.get_lazada_ads_data_info()
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["client_path"], "saved-ziniao.exe")
        self.assertEqual(info["workbook_id"], "saved-workbook")

    def test_start_inherits_saved_preferences_only_when_payload_fields_are_omitted(self) -> None:
        self.save_initial_preferences()
        self.config.save({
            "accounts": [BoundAccount("fixture-ziniao", "ziniao", "fixture-company", "fixture-user", "fixture-password")],
            "active_account_ids": {"ziniao": "fixture-ziniao"},
            "dingtalk": {"app_key": "fixture-app", "app_secret": "fixture-secret",
                         "users": [{"name": "fixture-operator", "user_id": "fixture-user-id"}]},
        })
        for fields, expected in (
            ({}, ("saved-ziniao.exe", "saved-workbook")),
            ({"client_path": "selected-ziniao.exe", "workbook_id": "selected-workbook"}, ("selected-ziniao.exe", "selected-workbook")),
            ({"client_path": "", "workbook_id": ""}, ("", service.DEFAULT_WORKBOOK_ID)),
        ):
            with self.subTest(fields=fields):
                result = self.bridge.start_lazada_ads_data({"store_names": "fixture-shop", **fields})
                self.assertTrue(result["ok"], result)
                runner = self.bridge.tasks.start.call_args.args[1]
                with patch.object(service, "run_lazada_ads_data", return_value={}) as run:
                    runner(Mock())
                query = run.call_args.args[0]
                self.assertEqual((query.client_path, query.workbook_id), expected)


if __name__ == "__main__":
    unittest.main()

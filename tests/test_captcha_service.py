from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend.app_bridge import AppBridge
from backend.config_store import ConfigStore
from backend.services.captcha_service import CaptchaServiceError, query_ttshitu_account_info


class CaptchaServiceTests(unittest.TestCase):
    @patch("backend.services.captcha_service.requests.get")
    def test_query_ttshitu_account_info_uses_official_endpoint(self, mock_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {"success": True, "data": {"balance": "12.50"}}
        mock_get.return_value = response

        data = query_ttshitu_account_info("demo-user", "demo-pass")

        self.assertEqual(data, {"balance": "12.50"})
        mock_get.assert_called_once_with(
            "http://api.ttshitu.com/queryAccountInfo.json",
            params={"username": "demo-user", "password": "demo-pass"},
            timeout=30,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("backend.services.captcha_service.requests.get")
    def test_query_ttshitu_account_info_reports_provider_error(self, mock_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {"success": False, "message": "账号或密码错误"}
        mock_get.return_value = response

        with self.assertRaisesRegex(CaptchaServiceError, "账号或密码错误"):
            query_ttshitu_account_info("demo-user", "wrong-pass")

    @patch("backend.services.captcha_service.requests.get")
    def test_query_ttshitu_account_info_reports_connection_error(self, mock_get: Mock) -> None:
        mock_get.side_effect = requests.Timeout("timed out")

        with self.assertRaisesRegex(CaptchaServiceError, "连接超时"):
            query_ttshitu_account_info("demo-user", "demo-pass")

    @patch("backend.app_bridge.query_ttshitu_account_info")
    def test_bridge_uses_saved_credentials_without_exposing_password(self, query: Mock) -> None:
        query.return_value = {"balance": "8.00"}
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = AppBridge()
            bridge.config_store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = bridge.save_settings(
                {"captcha_username": "saved-user", "captcha_password": "saved-pass"}
            )
            result = bridge.query_captcha_balance({})

        self.assertTrue(saved["settings"]["captcha_password_configured"])
        self.assertNotIn("captcha_password", saved["settings"])
        query.assert_called_once_with("saved-user", "saved-pass")
        self.assertEqual(result, {"ok": True, "data": {"balance": "8.00"}})

    def test_bridge_masks_dingtalk_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = AppBridge()
            bridge.config_store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = bridge.save_settings(
                {
                    "dingtalk": {
                        "app_key": "ding-app-key",
                        "app_secret": "ding-app-secret",
                        "users": [{"name": "王小妹", "user_id": "user-id"}],
                    }
                }
            )

        dingtalk = saved["settings"]["dingtalk"]
        self.assertTrue(dingtalk["app_key_configured"])
        self.assertEqual(dingtalk["app_key_masked"], "din****key")
        self.assertTrue(dingtalk["app_secret_configured"])
        self.assertNotIn("app_secret", dingtalk)
        self.assertEqual(dingtalk["user_count"], 1)
        self.assertEqual(dingtalk["operator_names"], ["王小妹"])
        self.assertNotIn("users", dingtalk)

    def test_sample_config_rejects_unknown_dingtalk_operator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"APPDATA": temp_dir}):
                bridge = AppBridge()
                bridge.config_store = ConfigStore(Path(temp_dir) / "settings.json")
                bridge.config_store.save(
                    {
                        "dingtalk": {
                            "users": [{"name": "王小妹", "user_id": "user-id"}],
                        }
                    }
                )
                result = bridge.save_sample_registration_config(
                    {
                        "target_doc_id": "doc-id",
                        "enable_target_update": True,
                        "dingtalk_operator_name": "不存在的人",
                        "groups": [{"leader": "A", "target_sheet": "A", "stores": ["store-1"]}],
                    }
                )

        self.assertFalse(result["ok"])
        self.assertIn("不在钉钉用户配置表", result["error"])


if __name__ == "__main__":
    unittest.main()

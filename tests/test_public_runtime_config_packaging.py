from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from backend.config_store import AppSettings, DingTalkSettings
from backend.services.shopee_ads_recharge import (
    desktop_dingtalk_doc_credentials,
    validate_sheet_dingtalk_credentials,
)
from scripts.build_public_runtime_configs import (
    BIGSELLER_CONFIG_NAME,
    SITE_CONFIG_NAMES,
    build_public_bigseller_config,
    build_public_process_config,
    generate_public_bigseller_config,
    generate_public_runtime_configs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_CONFIG_DIR = (
    PROJECT_ROOT.parent / "superbrowser_process" / "main" / "shopee" / "config"
)
BIGSELLER_CONFIG = (
    PROJECT_ROOT.parent / "mabang_process" / "vietnam" / "config" / BIGSELLER_CONFIG_NAME
)


class PublicRuntimeConfigPackagingTests(unittest.TestCase):
    def test_public_config_keeps_metadata_and_drops_credentials(self):
        public = build_public_process_config(
            {
                "ziniao": {"user_info": {"username": "user", "password": "password"}},
                "dingtalk": {
                    "agent": {"access_token": "token", "secret": "agent-secret"},
                    "doc_config": {
                        "app_key": "app-key",
                        "app_secret": "app-secret",
                        "config_doc_id": "doc-id",
                        "config_sheet": "Sheet1",
                    },
                },
                "task": ["my_shopee_ads_recharge"],
                "shopee": {
                    "ads_wallet_url": "https://seller.example/ads",
                    "security_verify": {
                        "enabled": True,
                        "base_url": "https://verify.example",
                        "phone": "test-phone",
                        "user_id": "test-user-id",
                        "user_name": "test-user-name",
                        "requester": "test-requester",
                        "token": "test-token",
                        "secret": "test-secret",
                    },
                },
            }
        )

        self.assertNotIn("ziniao", public)
        self.assertNotIn("agent", public["dingtalk"])
        self.assertNotIn("app_key", public["dingtalk"]["doc_config"])
        self.assertNotIn("app_secret", public["dingtalk"]["doc_config"])
        self.assertEqual(public["dingtalk"]["doc_config"]["config_doc_id"], "doc-id")
        self.assertEqual(public["task"], ["my_shopee_ads_recharge"])
        self.assertEqual(
            public["shopee"]["security_verify"],
            {"enabled": True, "base_url": "https://verify.example"},
        )

    def test_all_generated_site_configs_are_public_only(self):
        with TemporaryDirectory() as temp_dir:
            generated = generate_public_runtime_configs(
                SUPERBROWSER_CONFIG_DIR,
                Path(temp_dir),
            )

            self.assertEqual(len(generated), len(SITE_CONFIG_NAMES))
            for path in generated:
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                self.assertNotIn("ziniao", payload, path.name)
                self.assertNotIn("agent", payload.get("dingtalk", {}), path.name)
                doc_config = payload.get("dingtalk", {}).get("doc_config", {})
                for key in ("app_key", "app_secret", "access_token", "secret"):
                    self.assertNotIn(key, doc_config, path.name)
                security_verify = payload.get("shopee", {}).get("security_verify", {})
                for key in ("phone", "user_id", "user_name", "requester", "token", "secret"):
                    self.assertNotIn(key, security_verify, path.name)

    def test_desktop_dingtalk_credentials_are_loaded_from_app_settings(self):
        settings = AppSettings(
            dingtalk=DingTalkSettings(
                app_key="desktop-app-key",
                app_secret="desktop-app-secret",
            )
        )

        self.assertEqual(
            desktop_dingtalk_doc_credentials(settings),
            {
                "app_key": "desktop-app-key",
                "app_secret": "desktop-app-secret",
            },
        )

    def test_sheet_recharge_rejects_missing_desktop_dingtalk_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "设置 > 钉钉应用配置"):
            validate_sheet_dingtalk_credentials(
                "sheet",
                {"app_key": "", "app_secret": ""},
            )

    def test_custom_recharge_does_not_require_desktop_dingtalk_credentials(self):
        validate_sheet_dingtalk_credentials(
            "custom",
            {"app_key": "", "app_secret": ""},
        )

    def test_public_bigseller_config_drops_all_runtime_credentials(self):
        public = build_public_bigseller_config(
            {
                "process_config": {
                    "process_name": "BigSeller同步",
                    "agent_config": {"access_token": "token", "secret": "secret"},
                    "bigseller_account": {"username": "user", "password": "password"},
                    "captcha_service": {
                        "provider": "ttshitu",
                        "url": "https://captcha.example/predict",
                        "username": "captcha-user",
                        "password": "captcha-password",
                        "max_attempts": 8,
                    },
                    "sync": {"page_size": 100},
                }
            }
        )

        process_config = public["process_config"]
        self.assertNotIn("agent_config", process_config)
        self.assertNotIn("bigseller_account", process_config)
        self.assertNotIn("username", process_config["captcha_service"])
        self.assertNotIn("password", process_config["captcha_service"])
        self.assertEqual(process_config["captcha_service"]["provider"], "ttshitu")
        self.assertEqual(process_config["sync"]["page_size"], 100)

    def test_generated_bigseller_config_is_public_only(self):
        with TemporaryDirectory() as temp_dir:
            target = generate_public_bigseller_config(BIGSELLER_CONFIG, Path(temp_dir))
            payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            process_config = payload.get("process_config", {})

            self.assertNotIn("agent_config", process_config)
            self.assertNotIn("bigseller_account", process_config)
            self.assertNotIn("bigseller_accounts", process_config)
            self.assertNotIn("username", process_config.get("captcha_service", {}))
            self.assertNotIn("password", process_config.get("captcha_service", {}))


if __name__ == "__main__":
    unittest.main()

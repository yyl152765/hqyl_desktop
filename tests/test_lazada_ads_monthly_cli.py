"""Tests for ZiniaoMonthlyCli contract, store matching, recovery, and lifecycle."""
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly_cli import (
    ZiniaoMonthlyCli,
    normalize_store_name,
)


class TestLazadaAdsMonthlyCli(unittest.TestCase):
    def setUp(self):
        identity = patch.object(ZiniaoMonthlyCli, 'identity_fingerprint', return_value='offline-test')
        identity.start()
        self.addCleanup(identity.stop)

    def test_normalize_store_name(self):
        self.assertEqual(normalize_store_name("  Store   A  "), "Store A")
        # NFKC full-width characters
        self.assertEqual(normalize_store_name("Ｓｔｏｒｅ　１"), "Store 1")

    def test_store_matching_exact_and_ambiguity_c08(self):
        cli = ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        cli.profile = "default"

        stores = [
            {"store_id": "101", "store_name": "ABC Store", "platform_name": "lazada", "site": "TH"},
            {"store_id": "102", "store_name": "XYZ Shop", "platform_name": "lazada", "site": "MY"},
            {"store_id": "103", "store_name": "ABC Store", "platform_name": "lazada", "site": "PH"},
        ]

        # 1. Exact unique match
        matched = cli.match_store("XYZ Shop", stores)
        self.assertEqual(matched["store_id"], "102")

        # 2. Normalized match with irregular spacing
        matched = cli.match_store("  XYZ   Shop  ", stores)
        self.assertEqual(matched["store_id"], "102")

        # 3. Not found
        with self.assertRaises(ZiniaoCliError) as ctx:
            cli.match_store("Nonexistent Shop", stores)
        self.assertIn("未找到店铺", str(ctx.exception))

        # 4. Ambiguous match (2 stores have same name "ABC Store")
        with self.assertRaises(ZiniaoCliError) as ctx:
            cli.match_store("ABC Store", stores)
        self.assertIn("存在歧义", str(ctx.exception))

    @patch("backend.services.lazada_ads_monthly_cli.cli_executable", return_value="dummy_ziniao.exe")
    def test_profile_switch_assertion_c07(self, mock_exe):
        cli = ZiniaoMonthlyCli(profile="profile-orig")

        with patch.object(cli, "current_profile", return_value="profile-changed"):
            with self.assertRaises(ZiniaoCliError) as ctx:
                cli.assert_profile()
            self.assertTrue(ctx.exception.auth)
            self.assertIn("已从 profile-orig 切换为 profile-changed", str(ctx.exception))
            self.assertTrue(cli.auth_failed)

    @patch("backend.services.lazada_ads_monthly_cli.cli_executable", return_value="dummy_ziniao.exe")
    def test_doctor_login_recovery_c05(self, mock_exe):
        cli = ZiniaoMonthlyCli(profile="test-profile")

        doctor_logged_out = subprocess.CompletedProcess(
            args=["doctor"],
            returncode=1,
            stdout="✓ API Key 有效\n✓ ZClaw Bridge 连通正常\n✗ 客户端未登录 CLIENT_LOGGED_OUT",
            stderr="",
        )
        login_success = subprocess.CompletedProcess(
            args=["zclaw", "login"],
            returncode=0,
            stdout="登录成功",
            stderr="",
        )
        doctor_healthy = subprocess.CompletedProcess(
            args=["doctor"],
            returncode=0,
            stdout="✓ API Key 有效\n✓ ZClaw Bridge 连通正常\n✓ 客户端登录用户: seller_a",
            stderr="",
        )

        with patch.object(cli, "current_profile", return_value="test-profile"):
            with patch.object(cli, "_process", side_effect=[doctor_logged_out, login_success, doctor_healthy]) as mock_proc:
                cli.ready()
                # Verify that zclaw login was called
                self.assertEqual(mock_proc.call_count, 3)
                mock_proc.assert_any_call(["zclaw", "login"], timeout=45)

    @patch("backend.services.lazada_ads_monthly_cli.cli_executable", return_value="dummy_ziniao.exe")
    def test_window_lifecycle_reused_vs_owned(self, mock_exe):
        cli = ZiniaoMonthlyCli(profile="test-profile")

        with patch.object(cli, "current_profile", return_value="test-profile"):
            # 1. Open new window -> not reused
            with patch.object(cli, "run", side_effect=[
                {"items": [], "total": 0},
                {"reused": False, "storeId": "1001", "name": "Store 1"},
                {"items": [{"storeId": "1001"}], "total": 1},
                {"data": {"targetId": "target_123"}},
                {"ok": True},  # close
                {"items": [], "total": 0},
            ]) as mock_run:
                cli.open_store("1001", "Store 1", entry_url="https://sellercenter.lazada.co.th")
                self.assertIn("1001", cli.owned_windows)
                self.assertEqual(cli.targets["1001"], "target_123")

                # Close store
                cli.close_store("1001")
                self.assertNotIn("1001", cli.owned_windows)
                self.assertNotIn("1001", cli.targets)
                mock_run.assert_any_call(["store", "close", "--id", "1001"], timeout=30)

            # 2. Reused window -> should NOT be closed
            with patch.object(cli, "run", side_effect=[
                {"items": [{"storeId": "2002"}], "total": 1},
                {"reused": True, "storeId": "2002", "name": "Store 2"},
                {"items": [{"storeId": "2002"}], "total": 1},
                {"data": {"targetId": "target_456"}},
            ]) as mock_run_reused:
                cli.open_store("2002", "Store 2", entry_url="https://sellercenter.lazada.co.th")
                self.assertNotIn("2002", cli.owned_windows)

                # Attempt close
                cli.close_store("2002")
                # Ensure store close was not called
                for call_args in mock_run_reused.call_args_list:
                    self.assertNotIn(["store", "close", "--id", "2002"], call_args)


if __name__ == "__main__":
    unittest.main()

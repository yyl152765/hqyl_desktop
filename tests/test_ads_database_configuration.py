"""Offline built-in database regression tests; never starts a shop or payment."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import psycopg2
from backend import ads_database as database
from backend.app_bridge import AppBridge
from backend.services import shopee_ads_recharge as service


class AdsDatabaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"HQYL_GUARD_DB_PASSWORD": "", "PGPASSWORD": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.guard = SimpleNamespace(BUILTIN_DB_PASSWORD="fixture-builtin", GUARD_DB_CONFIG={"host":"fixture-host"}, GUARD_DB_FALLBACK_HOST="fixture-ip")

    def test_no_environment_uses_builtin_for_both_consumers(self):
        with patch.object(database, "_guard_module", return_value=self.guard):
            self.assertEqual(database.resolve_password(), "fixture-builtin")
            self.assertEqual(database.resolve_password(False), "fixture-builtin")

    def test_legacy_local_password_file_is_not_read(self):
        with patch.object(database, "_guard_module", return_value=self.guard), patch("pathlib.Path.read_text", side_effect=AssertionError("Local credential file read")):
            self.assertEqual(database.resolve_password(), "fixture-builtin")

    def test_optional_environment_override_and_empty_specific_fallback(self):
        with patch.dict(os.environ, {"HQYL_GUARD_DB_PASSWORD": "guard-fixture", "PGPASSWORD": "sync-fixture"}):
            self.assertEqual(database.resolve_password(), "guard-fixture")
            self.assertEqual(database.resolve_password(False), "sync-fixture")
        with patch.dict(os.environ, {"HQYL_GUARD_DB_PASSWORD": "", "PGPASSWORD": "sync-fixture"}):
            self.assertEqual(database.resolve_password(), "sync-fixture")

    def test_readonly_validation_closes_connection(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(psycopg2, "connect", return_value=connection) as connect:
            database.validate_connection("fixture-secret")
        self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
        cursor.execute.assert_called_once_with("SELECT 1")
        connection.close.assert_called_once()

    def test_auth_failure_never_retries_and_never_returns_driver_secret(self):
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("password authentication failed SECRET")) as connect:
            with self.assertRaisesRegex(ValueError, "连接失败") as error:
                database.validate_connection("SECRET")
        self.assertNotIn("SECRET", str(error.exception))
        connect.assert_called_once()

    def test_dns_failure_retries_only_the_configured_fallback(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(psycopg2, "connect", side_effect=[psycopg2.OperationalError("could not translate host name"), connection]) as connect:
            database.validate_connection("fixture-secret")
        self.assertEqual([call.kwargs["host"] for call in connect.call_args_list], ["fixture-host", "fixture-ip"])
        self.assertTrue(all("default_transaction_read_only=on" in call.kwargs["options"] for call in connect.call_args_list))
        connection.close.assert_called_once()

    def test_query_failure_closes_connection(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.DatabaseError("SECRET")
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(psycopg2, "connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "连接失败"):
                database.validate_connection("fixture-secret")
        connection.close.assert_called_once()

    def test_missing_builtin_blocks_before_loading_any_store(self):
        self.guard.BUILTIN_DB_PASSWORD = ""
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(service, "get_site_module") as load:
            with self.assertRaisesRegex(ValueError, "连接配置不完整"):
                service.run_shopee_ads_recharge(SimpleNamespace(site_code="id"))
        load.assert_not_called()

    def test_failed_validation_blocks_before_loading_any_store(self):
        with patch.object(database, "_guard_module", return_value=self.guard), patch.object(database, "validate_connection", side_effect=ValueError("连接失败")), patch.object(service, "get_site_module") as load:
            with self.assertRaisesRegex(ValueError, "连接失败"):
                service.run_shopee_ads_recharge(SimpleNamespace(site_code="id"))
        load.assert_not_called()

    def test_bridge_no_longer_exposes_database_configuration(self):
        self.assertFalse(hasattr(AppBridge, "get_ads_database_settings"))
        self.assertFalse(hasattr(AppBridge, "save_ads_database_settings"))

    def test_both_imported_consumers_receive_builtin_without_configuration(self):
        report = database.self_check()
        self.assertTrue(report["builtin_credential"])
        self.assertTrue(report["guard_and_sync_injection"])
        self.assertTrue(report["works_without_user_configuration"])
        self.assertEqual(report["real_database_connections"], 0)


if __name__ == "__main__":
    unittest.main()

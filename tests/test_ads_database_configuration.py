"""Offline tests for migration and the recharge startup gate."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import psycopg2
from backend import ads_database as database
from backend.services import shopee_ads_recharge as service

class AdsDatabaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"HQYL_GUARD_DB_HOST": "fixture.invalid", "HQYL_GUARD_DB_USER": "fixture",
                                     "HQYL_GUARD_DB_PASSWORD": "configured"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_requires_correct_target(self):
        for key, value in (("HOST", "rpa.whhqyl.com.cn"), ("HOST", "47.112.20.68"), ("NAME", "rpa_db"), ("NAME", "wh_rpa"),
                           ("PORT", "65536"), ("SCHEMA", "bad;syntax")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {"HQYL_GUARD_DB_" + key: value}):
                with self.assertRaises(ValueError):
                    database.connection_parameters("configured")

    def test_absent_password_stops_before_stores(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(service, "get_site_module") as load:
            with self.assertRaisesRegex(ValueError, "连接配置不完整"):
                service.run_shopee_ads_recharge(SimpleNamespace(site_code="id"))
        load.assert_not_called()

    def test_validation_checks_target_tables_readonly_and_closes(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        for row, valid in ((("hrms", True), True), (("hrms", False), False), (("other", True), False)):
            cursor.fetchone.side_effect = [row, (True,)]
            with patch.object(psycopg2, "connect", return_value=connection) as connect:
                if valid:
                    database.validate_connection("configured")
                else:
                    with self.assertRaises(ValueError):
                        database.validate_connection("configured")
            self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
            self.assertTrue(any(len(c.args) == 2 and "tbl_fin_ads_shop_browser" in c.args[1][0]
                                for c in cursor.execute.call_args_list))
        self.assertEqual(connection.close.call_count, 3)

    def test_no_credential_leak_or_legacy_retry(self):
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("private-fixture")) as connect:
            with self.assertRaisesRegex(ValueError, "连接失败") as err:
                database.validate_connection("configured")
        self.assertNotIn("private-fixture", str(err.exception))
        connect.assert_called_once()

    def test_both_recharge_and_shop_sync_use_provisioned_database(self):
        report = database.self_check()
        self.assertFalse(report["builtin_credential"])
        self.assertTrue(report["guard_and_sync_injection"])
        self.assertEqual(report["real_database_connections"], 0)

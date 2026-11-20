"""Offline tests for the desktop tax flow; the tax client is never launched."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings
from backend.services import tax_declaration_service
from launcher.main import resolve_entry_html
from openpyxl import Workbook


class TaxDeclarationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tax-bridge-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workbook = self.root / "synthetic-accounts.xlsx"
        self.client = self.root / "EPEvenue_SH.exe"
        self.fake_password = "OFFLINE_TEST_PASSWORD_DO_NOT_RETURN"
        book = Workbook()
        book.active.append(["", "单位名称", "", "申报密码"])
        book.active.append(["", "合成测试单位", "", self.fake_password])
        book.save(self.workbook)
        book.close()
        self.client.touch()
        config = SimpleNamespace(load=lambda: AppSettings(output_dir=str(self.root)), local_data_dir=self.root)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()

    def payload(self, **changes):
        return {
            "workbook": str(self.workbook),
            "app_path": str(self.client),
            "export_start_month": "2026-01",
            "export_end_month": "2026-10",
            **changes,
        }

    def mock_tasks(self):
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-task"}
        return self.bridge.tasks

    def test_info_and_tool_catalog_expose_the_local_page_without_starting_work(self) -> None:
        tasks = self.mock_tasks()
        with patch.object(app_bridge, "get_group_options", return_value=[]):
            catalog = self.bridge.get_app_info()
        tool = next(item for item in catalog["tools"] if item["key"] == "tax_declaration")
        self.assertEqual(tool["vendor"], "local")
        self.assertEqual(resolve_entry_html(["--page=tax-declaration"]).name, "tax-declaration.html")

        with patch.dict(os.environ, {"TAX_APP_PATH": ""}):
            info = self.bridge.get_tax_declaration_info()
        self.assertTrue(info["ok"])
        self.assertEqual(Path(info["app_path"]).name, "EPEvenue_SH.exe")
        self.assertTrue(info["workbook"].endswith(".xlsx"))
        self.assertRegex(info["export_start_month"], r"^\d{4}-01$")
        self.assertRegex(info["export_end_month"], r"^\d{4}-\d{2}$")
        self.assertNotIn(self.fake_password, json.dumps(info))
        tasks.start.assert_not_called()

    def test_default_mode_is_export_only_and_sets_exclusive_task(self) -> None:
        tasks = self.mock_tasks()
        started = self.bridge.start_tax_declaration(self.payload(max_rows=2))
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["task_id"], "fixture-task")
        task = tasks.start.call_args
        self.assertEqual(task.kwargs["tool"], "tax_declaration")
        self.assertIs(task.kwargs["exclusive"], True)
        self.assertNotIn(self.fake_password, json.dumps(task.kwargs.get("context", {})))

        progress = Mock()
        fixture = {"is_complete": True, "total": 1}
        with patch.object(app_bridge.tax_declaration_service, "run_tax_declaration", return_value=fixture) as run:
            self.assertEqual(task.args[1](progress), fixture)
        settings = run.call_args.args[0]
        self.assertTrue(settings.export_records_only)
        self.assertEqual(settings.workbook, self.workbook.resolve())
        self.assertEqual(settings.app_path, self.client.resolve())
        self.assertEqual(settings.max_rows, 2)
        self.assertEqual((settings.export_start_month, settings.export_end_month), ("2026-01", "2026-10"))
        self.assertIs(run.call_args.args[1], progress)

    def test_real_declaration_requires_an_explicit_boolean_true(self) -> None:
        tasks = self.mock_tasks()
        for value in (None, False, 1, "true", "yes"):
            with self.subTest(value=value):
                rejected = self.bridge.start_tax_declaration(self.payload(mode="declare", confirm_declaration=value))
                self.assertFalse(rejected["ok"], rejected)
                self.assertTrue(rejected["message"])
        tasks.start.assert_not_called()

        started = self.bridge.start_tax_declaration(self.payload(mode="declare", confirm_declaration=True))
        self.assertTrue(started["ok"], started)
        with patch.object(app_bridge.tax_declaration_service, "run_tax_declaration", return_value={}) as run:
            tasks.start.call_args.args[1](Mock())
        self.assertFalse(run.call_args.args[0].export_records_only)

    def test_invalid_paths_dates_modes_and_limits_never_start_a_task(self) -> None:
        tasks = self.mock_tasks()
        other_exe = self.root / "other.exe"
        other_exe.touch()
        for changes in (
            {"workbook": str(self.root / "missing.xlsx")},
            {"workbook": str(self.client)},
            {"app_path": str(self.root / "missing" / "EPEvenue_SH.exe")},
            {"app_path": str(other_exe)},
            {"export_end_month": "2026-13"},
            {"export_start_month": "2026-11", "export_end_month": "2026-10"},
            {"mode": "unknown"},
            {"max_rows": 0},
            {"max_rows": -1},
        ):
            with self.subTest(changes=changes):
                rejected = self.bridge.start_tax_declaration(self.payload(**changes))
                self.assertFalse(rejected["ok"], rejected)
                self.assertTrue(rejected["message"])
        tasks.start.assert_not_called()

    def test_task_manager_blocks_a_second_launch_and_polls_final_result(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        fixture = {"is_complete": True, "total": 1, "failed": 0}

        def offline_runner(settings, progress):
            self.assertTrue(settings.export_records_only)
            progress("仅使用合成测试数据")
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release offline worker")
            return fixture

        with patch.object(app_bridge.tax_declaration_service, "run_tax_declaration", side_effect=offline_runner) as run:
            started = self.bridge.start_tax_declaration(self.payload())
            try:
                self.assertTrue(started["ok"], started)
                self.assertTrue(entered.wait(5))
                running = self.bridge.get_task_status(started["task_id"])
                self.assertEqual(running["status"], "running")
                self.assertFalse(self.bridge.start_tax_declaration(self.payload())["ok"])
            finally:
                release.set()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                finished = self.bridge.get_task_status(started["task_id"])
                if finished["status"] in {"success", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(finished["status"], "success", finished)
            self.assertEqual(finished["result"], fixture)
            self.assertTrue(any("合成测试数据" in line for line in finished["logs"]))
            self.assertNotIn(self.fake_password, json.dumps(finished))
            run.assert_called_once()

    def test_runner_keeps_export_failure_visible_without_reconfiguring_desktop_logging(self) -> None:
        settings = tax_declaration_service.validate_request(self.payload(), self.root)
        settings = replace(settings, result_file=self.root / "run.xlsx", log_file=self.root / "run.log")
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        logs = []

        def execute_offline(job, *, configure_run_logging, on_progress):
            self.assertEqual(job, settings)
            self.assertFalse(configure_run_logging)
            on_progress(1, 1)
            return {
                "output_file": str(job.result_file), "total": 1, "successful": 1,
                "failed": 0, "skipped": 0,
                "results": [{"status": "已经申报过了", "detail": "申报记录导出失败：离线测试"}],
            }

        with (
            patch.object(tax_declaration_service, "_ensure_client_closed") as check_client,
            patch.object(tax_declaration_service.tax_declaration, "execute", side_effect=execute_offline) as execute,
        ):
            result = tax_declaration_service.run_tax_declaration(settings, logs.append)
        check_client.assert_called_once_with()
        execute.assert_called_once()
        self.assertEqual(root_logger.handlers, original_handlers)
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["export_failed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(Path(result["log_file"]), settings.log_file)
        self.assertIn("已处理 1/1 家公司", logs)

    @unittest.skipUnless(os.name == "nt", "Windows 税务客户端进程检查")
    def test_open_tax_client_blocks_worker_without_killing_or_launching_it(self) -> None:
        settings = tax_declaration_service.validate_request(self.payload(), self.root)
        settings = replace(settings, result_file=self.root / "run.xlsx", log_file=self.root / "run.log")
        for image in ("EPPortalITS.exe", "EPEvenue_SH.exe"):
            with (
                self.subTest(image=image),
                patch.object(tax_declaration_service.subprocess, "run", return_value=SimpleNamespace(
                    stdout=f'"other.exe","111","Console","1","2 K"\n"{image}","222","Console","1","3 K"\n',
                )) as process_list,
                patch.object(tax_declaration_service.tax_declaration, "execute") as execute,
            ):
                with self.assertRaisesRegex(tax_declaration_service.tax_declaration.AutomationError, "税务客户端已在运行"):
                    tax_declaration_service.run_tax_declaration(settings, lambda _message: None)
                execute.assert_not_called()
                process_list.assert_called_once()
                self.assertEqual(process_list.call_args.args[0][0], "tasklist")


if __name__ == "__main__":
    unittest.main()

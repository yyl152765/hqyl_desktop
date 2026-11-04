from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services import lazada_withdrawal_statistics as service


def job(root: Path, stores=("店铺A",)):
    return service.LazadaWithdrawalStatisticsQuery(
        company="fixture", username="user", password="private-password", country="MY",
        start_date="2026-09-01", end_date="2026-09-30", store_names=stores,
        output_root=str(root),
        dingtalk_app_key="fixture-app", dingtalk_app_secret="fixture-secret", dingtalk_user_id="fixture-user",
    )


class WithdrawalUnavailableTests(unittest.TestCase):
    def process(self, root: Path, *, unavailable=True, screenshot_error=False, country="MY"):
        driver = Mock()
        driver.current_url = (
            f"https://{service.COUNTRIES[country]['domain']}/apps/balance/unaccessable"
            "?token=private-token#private-fragment"
        )

        def save_screenshot(path):
            if screenshot_error:
                raise RuntimeError("fixture screenshot error")
            Path(path).write_bytes(b"fixture screenshot")
            return True

        driver.save_screenshot.side_effect = save_screenshot
        withdrawal = Mock()
        withdrawal.open_lazada_finance_page.return_value = False
        withdrawal._is_lazada_balance_unavailable.return_value = unavailable
        logger = Mock(spec=logging.Logger)
        with (
            patch.object(service, "_reference_modules", return_value=(None, withdrawal, None, Mock())),
            patch.object(service.time, "sleep"),
            patch.object(service, "_upload_file") as upload,
        ):
            result = service._process_ph_my(driver, job(root), "店铺A", root, logger)
        upload.assert_not_called()
        withdrawal.set_trade_date_range.assert_not_called()
        return result, driver, logger

    def test_platform_restriction_preserves_diagnostics_without_creating_empty_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _, logger = self.process(root)
            self.assertEqual(result.status, "failed")
            self.assertTrue(result.balance_page_unavailable)
            self.assertEqual(result.collection_method, "income_statement")
            self.assertIn("有效下载目录", result.message)
            self.assertFalse(result.no_data)
            self.assertFalse(result.filter_verified)
            self.assertFalse(result.date_verified)
            self.assertEqual(result.local_files, [])
            self.assertEqual(result.drive_paths, [])
            self.assertEqual(result.uploaded_count, 0)
            self.assertTrue(result.finished_at)
            self.assertEqual(list(root.glob("*.xlsx")), [])
            self.assertTrue(Path(result.diagnostic_screenshot).is_file())
            self.assertEqual(
                result.page_url,
                "https://sellercenter.lazada.com.my/apps/balance/unaccessable",
            )
            self.assertNotIn("private-", str(result))
            self.assertIn("店铺A", str(logger.warning.call_args))

    def test_screenshot_failure_does_not_hide_platform_restriction(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, _ = self.process(Path(directory), screenshot_error=True)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.balance_page_unavailable)
        self.assertIn("有效下载目录", result.message)
        self.assertEqual(result.diagnostic_screenshot, "")
        self.assertTrue(result.finished_at)

    def test_generic_navigation_failure_is_not_classified_as_platform_restriction(self):
        with tempfile.TemporaryDirectory() as directory:
            result, driver, _ = self.process(Path(directory), unavailable=False)
        self.assertEqual(result.status, "failed")
        self.assertIn("无法进入", result.message)
        driver.save_screenshot.assert_not_called()

    def test_foreign_country_restriction_is_navigation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result, driver, _ = self.process(Path(directory), country="PH")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.page_url, "")
        driver.save_screenshot.assert_not_called()


class WithdrawalConcurrentLoggingTests(unittest.TestCase):
    def close_logger(self, logger):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_shared_navigation_logs_keep_store_context_across_workers_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = []
            logger, log_file = service._make_logger(root, progress.append)
            barrier = threading.Barrier(2)
            task = job(root, ("店铺A", "店铺B"))
            loader = Mock()
            loader.open_store.side_effect = lambda store_id: {"browserId": store_id}
            loader.get_driver.side_effect = lambda opened: Mock(store=opened["browserId"])

            def process(driver, current_job, name, store_dir, current_logger, **_kwargs):
                barrier.wait(timeout=5)
                # Shared reference-module loggers emit on the same worker thread.
                logger.warning("page marker %s", name)
                return service.StoreResult(
                    current_job.country, name, name, current_job.start_date, current_job.end_date,
                    status="page_unavailable", message="平台未开放",
                )

            def worker(name):
                result = service._run_one_browser(
                    loader, {"browserId": name, "browserName": name}, task, root, logger,
                )
                logger.info("worker context cleared")
                return result

            try:
                with (
                    patch.object(service, "_process_ph_my", side_effect=process),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    results = list(pool.map(worker, task.store_names))
                logger.info("task summary")
                content = log_file.read_text(encoding="utf-8")
            finally:
                self.close_logger(logger)

            self.assertEqual([item.status for item in results], ["page_unavailable"] * 2)
            self.assertEqual(loader.close_store.call_count, 2)
            for name in task.store_names:
                self.assertIn(f"WARNING - [{name}] page marker {name}", content)
                self.assertIn(f"WARNING - [{name}] page marker {name}", progress)
            cleared = [line for line in progress if "context cleared" in line or "task summary" in line]
            self.assertEqual(len(cleared), 3)
            self.assertTrue(all("[店铺" not in line for line in cleared))

    def test_startup_failure_also_clears_context_and_closes_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = []
            logger, _ = service._make_logger(root, progress.append)
            loader = Mock()
            loader.open_store.side_effect = RuntimeError("fixture startup failure")
            try:
                with patch.object(service.time, "sleep"):
                    result = service._run_one_browser(
                        loader, {"browserId": "a", "browserName": "店铺A"}, job(root), root, logger,
                    )
                logger.info("outside worker")
            finally:
                self.close_logger(logger)
        self.assertEqual(result.status, "failed")
        self.assertEqual(loader.open_store.call_count, 2)
        self.assertEqual(loader.close_store.call_count, 2)
        self.assertEqual(progress[-1], "INFO - outside worker")


class WithdrawalSummaryTests(unittest.TestCase):
    def test_unavailable_is_reported_separately_and_never_makes_task_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            statuses = {
                "成功店铺": "success", "空流水": "no_data", "受限店铺": "page_unavailable",
                "异常店铺": "failed", "部分店铺": "partial_success",
            }
            browsers = [{"browserName": name} for name in statuses]
            loader = Mock()
            loader.load_super_browser.return_value = browsers
            operator = SimpleNamespace(
                load_runtime_configs=Mock(return_value=({}, {})),
                LazadaWithdrawalSettings=Mock(),
                _merge_settings_into_config=Mock(side_effect=lambda config, settings: config),
                _filter_lazada_stores=Mock(return_value=(browsers, ["未匹配店铺"])),
            )
            reference = (operator, Mock(), Mock(return_value=loader), None)

            def run_one(_loader, browser, current_job, _run_dir, _logger):
                name = browser["browserName"]
                return service.StoreResult(
                    current_job.country, name, name, current_job.start_date, current_job.end_date,
                    status=statuses[name],
                )

            with (
                patch.object(service, "_reference_modules", return_value=reference),
                patch.object(service, "_run_one_browser", side_effect=run_one),
                patch.object(service, "_make_logger", return_value=(Mock(), root / "run.log")),
            ):
                result = service.run_lazada_withdrawal_statistics(
                    job(root, (*statuses, "未匹配店铺")),
                )
            saved = json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))
        self.assertFalse(result["success"])
        self.assertEqual(result["success_store_count"], 2)
        self.assertEqual(result["partial_store_count"], 1)
        self.assertEqual(result["unavailable_store_count"], 1)
        self.assertEqual(result["error_store_count"], 2)
        # Existing consumers still see all unsuccessful stores in failed_store_count.
        self.assertEqual(result["failed_store_count"], 3)
        self.assertEqual(result["processed_store_count"], 6)
        self.assertEqual(result["matched_store_count"], 5)
        self.assertIn("平台未开放 1，失败/未匹配 2", result["message"])
        self.assertEqual(saved, result)
        self.assertNotIn("private-password", json.dumps(result))
        loader.get_exit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import logging
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook

from backend.services import lazada_withdrawal_statistics as service


def make_job(root: Path, country: str = "MY"):
    return service.LazadaWithdrawalStatisticsQuery(
        company="fixture", username="fixture", password="private-password",
        country=country, start_date="2026-09-01", end_date="2026-09-30",
        store_names=("测试店铺",), output_root=str(root),
    )


def statement_row(number: str, start: str = "2026-09-07", end: str = "2026-09-13"):
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    return {
        "statement_number": number,
        "period": f"{first:%d %b %Y} - {last:%d %b %Y}",
        "period_range": (first, last),
        "element": object(),
    }


def write_statement(path: Path, number: str, period: str):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Income Overview"
    sheet.append(["Billing Period", "Statement Number", "Transaction Date", "Fee Name"])
    sheet.append([period, number, "13 Sep 2026", "Product price credit"])
    workbook.save(path)
    workbook.close()


@contextmanager
def statement_runtime(root: Path, country: str, pages: list[list[dict]], *, download=None):
    downloads = root / "downloads"
    downloads.mkdir()
    output = root / "output"
    output.mkdir()
    driver = Mock()
    driver.current_url = f"https://{service.COUNTRIES[country]['domain']}/"
    driver.get.side_effect = lambda url: setattr(driver, "current_url", url)

    def save_screenshot(path):
        Path(path).write_bytes(b"fixture screenshot")
        return True

    driver.save_screenshot.side_effect = save_screenshot
    withdrawal = Mock()
    withdrawal.open_lazada_income_statement_page.return_value = True
    withdrawal.open_lazada_income_statement_tab.return_value = True
    withdrawal.collect_withdrawal_transaction_details.return_value = {
        "outcome": "confirmed_empty", "collection_complete": True, "empty_confirmed": True,
        "currency": {"PH": "PHP", "MY": "MYR", "TH": "THB"}[country],
        "transaction_records": [], "record_count": 0, "amount": "0.00",
    }
    withdrawal.validate_transaction_record_consistency.return_value = (True, "")
    withdrawal._is_login_url.side_effect = lambda url: "login" in url.casefold()
    withdrawal._parse_iso_date.side_effect = datetime.fromisoformat
    withdrawal._date_ranges_intersect.side_effect = lambda a, b, c, d: a <= d and c <= b
    withdrawal.wait_for_thailand_income_statement_rows.side_effect = [*pages, []]
    withdrawal.get_thailand_income_statement_rows.return_value = [row for page in pages for row in page]
    withdrawal.click_thailand_income_statement_next_page.side_effect = [
        *([True] * max(0, len(pages) - 1)), False,
    ]
    drive = Mock()
    drive.ensure_path.return_value = SimpleNamespace(id="fixture-folder")
    drive.upload_file.return_value = SimpleNamespace(id="fixture-file")
    logger = Mock(spec=logging.Logger)

    def download_valid(_driver, row, _downloads, destination, _withdrawal):
        path = destination / f"{row['statement_number']}.xlsx"
        write_statement(path, row["statement_number"], row["period"])
        return str(path), "fixture-export-id"

    with ExitStack() as stack:
        stack.enter_context(patch.object(service, "_reference_modules", return_value=(None, withdrawal, None, Mock())))
        stack.enter_context(patch.object(service.time, "sleep"))
        stack.enter_context(patch.object(service, "_create_drive_client", return_value=drive))
        download_mock = stack.enter_context(
            patch.object(service, "_download_statement_exact", side_effect=download or download_valid)
        )
        yield SimpleNamespace(
            root=root, job=make_job(root, country), driver=driver, withdrawal=withdrawal,
            downloads=downloads, output=output, drive=drive, logger=logger, download=download_mock,
        )


def process_statements(runtime):
    return service._process_income_statements(
        runtime.driver, runtime.job, "测试店铺", str(runtime.downloads), runtime.output, runtime.logger,
    )


class RestrictedBalanceFallbackTests(unittest.TestCase):
    def test_restrictions_archive_statements_without_claiming_withdrawal_success(self):
        for country in ("MY", "PH", "TH"):
            with self.subTest(country=country), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with statement_runtime(root, country, [[statement_row(f"{country}-2026-037")]]) as runtime:
                    unavailable_url = (
                        f"https://{service.COUNTRIES[country]['domain']}/apps/balance/unaccessable"
                        "?token=private-token#private-fragment"
                    )

                    def restricted(_util):
                        runtime.driver.current_url = unavailable_url
                        return False

                    runtime.withdrawal.open_lazada_finance_page.side_effect = restricted
                    runtime.withdrawal._is_lazada_balance_unavailable.return_value = True
                    result = service._process_ph_my(
                        runtime.driver, runtime.job, "测试店铺", runtime.output, runtime.logger,
                        browser_download_dir=str(runtime.downloads),
                    )

                    self.assertEqual(result.status, "page_unavailable")
                    self.assertEqual(result.statement_archive_status, "success")
                    self.assertIn("未取得提现流水", result.message)
                    self.assertFalse(result.no_data)
                    self.assertEqual(result.collection_method, "income_statement")
                    self.assertTrue(result.balance_page_unavailable)
                    self.assertEqual(result.country, country)
                    self.assertEqual(result.uploaded_count, 1)
                    self.assertEqual(result.downloaded_count, 1)
                    self.assertEqual(len(result.local_files), 1)
                    self.assertEqual(len(result.drive_paths), 1)
                    self.assertTrue(Path(result.local_files[0]).is_file())
                    self.assertTrue(Path(result.diagnostic_screenshot).is_file())
                    self.assertEqual(
                        result.page_url,
                        f"https://{service.COUNTRIES[country]['domain']}/apps/balance/unaccessable",
                    )
                    self.assertNotIn("private-", str(result))
                    self.assertTrue(result.finished_at)
                    self.assertEqual(runtime.driver._lazada_target_country, country)
                    self.assertTrue(all(
                        call.args[0] == f"https://{service.COUNTRIES[country]['domain']}/"
                        for call in runtime.driver.get.call_args_list
                    ))
                    runtime.drive.ensure_path.assert_called_once_with(
                        ["9月", "测试店铺"], parent_id=service.COUNTRIES[country]["drive_root_id"],
                    )
                    runtime.withdrawal.set_trade_date_range.assert_not_called()
                    runtime.withdrawal.open_lazada_withdrawal_page.assert_not_called()

    def test_normal_balance_flow_keeps_withdrawal_workbook_and_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                runtime.withdrawal.open_lazada_finance_page.return_value = True
                runtime.withdrawal.set_trade_date_range.return_value = True
                runtime.withdrawal._verify_balance_date_range.return_value = True
                with (
                    patch.object(service, "_process_income_statements") as fallback,
                    patch.object(service, "_dismiss_balance_update_modal"),
                    patch.object(service, "_dismiss_guided_tours"),
                    patch.object(service, "_scroll_to_balance_transactions"),
                    patch.object(service, "_select_withdrawal_filter", return_value=(True, {}, {})),
                    patch.object(service, "_transaction_table_state", return_value={"ready": True}),
                    patch.object(service, "_collect_withdrawal_transactions", return_value=([], 1)),
                ):
                    result = service._process_ph_my(
                        runtime.driver, runtime.job, "测试店铺", runtime.output, runtime.logger,
                        browser_download_dir=str(runtime.downloads),
                    )
                fallback.assert_not_called()
                self.assertEqual(result.status, "no_data")
                self.assertEqual(result.collection_method, "withdrawal_transactions")
                self.assertFalse(result.balance_page_unavailable)
                self.assertTrue(result.filter_verified)
                self.assertTrue(result.date_verified)
                self.assertEqual(result.uploaded_count, 1)
                self.assertEqual(Path(result.local_files[0]).name, "测试店铺-withdrawal.xlsx")
                runtime.download.assert_not_called()

    def test_generic_navigation_error_and_foreign_restriction_do_not_trigger_fallback(self):
        cases = [("MY", False), ("PH", True)]
        for visible_country, unavailable in cases:
            with self.subTest(visible_country=visible_country, unavailable=unavailable):
                with tempfile.TemporaryDirectory() as directory:
                    with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                        def navigation_failure(_util):
                            runtime.driver.current_url = (
                                f"https://{service.COUNTRIES[visible_country]['domain']}/apps/balance/unaccessable"
                            )
                            return False

                        runtime.withdrawal.open_lazada_finance_page.side_effect = navigation_failure
                        runtime.withdrawal._is_lazada_balance_unavailable.return_value = unavailable
                        with patch.object(service, "_process_income_statements") as fallback:
                            result = service._process_ph_my(
                                runtime.driver, runtime.job, "测试店铺", runtime.output, runtime.logger,
                                browser_download_dir=str(runtime.downloads),
                            )
                        self.assertEqual(result.status, "failed")
                        self.assertFalse(result.balance_page_unavailable)
                        fallback.assert_not_called()
                        runtime.drive.upload_file.assert_not_called()
                        runtime.driver.save_screenshot.assert_not_called()

    def test_restriction_with_no_download_directory_is_failed_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                def restricted(_util):
                    runtime.driver.current_url = "https://sellercenter.lazada.com.my/apps/balance/unaccessable"
                    return False

                runtime.withdrawal.open_lazada_finance_page.side_effect = restricted
                runtime.withdrawal._is_lazada_balance_unavailable.return_value = True
                result = service._process_ph_my(
                    runtime.driver, runtime.job, "测试店铺", runtime.output, runtime.logger,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.collection_method, "income_statement")
                self.assertTrue(result.balance_page_unavailable)
                self.assertTrue(Path(result.diagnostic_screenshot).is_file())
                self.assertIn("下载目录", result.message)
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_empty_fallback_keeps_balance_and_income_diagnostics_separate_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                def restricted(_util):
                    runtime.driver.current_url = (
                        "https://sellercenter.lazada.com.my/apps/balance/unaccessable"
                        "?token=private-balance-token#private-fragment"
                    )
                    return False

                def income_page(_util):
                    runtime.driver.current_url = (
                        "https://fixture:private-userinfo@sellercenter.lazada.com.my"
                        "/portal/apps/finance/myIncome/index?token=private-income-token#private-fragment"
                    )
                    return True

                runtime.withdrawal.open_lazada_finance_page.side_effect = restricted
                runtime.withdrawal._is_lazada_balance_unavailable.return_value = True
                runtime.withdrawal.open_lazada_income_statement_page.side_effect = income_page
                result = service._process_ph_my(
                    runtime.driver, runtime.job, "测试店铺", runtime.output, runtime.logger,
                    browser_download_dir=str(runtime.downloads),
                )
                self.assertEqual(result.status, "page_unavailable")
                self.assertEqual(result.statement_archive_status, "no_matching_statement")
                self.assertFalse(result.no_data)
                self.assertTrue(result.balance_page_unavailable)
                self.assertEqual(
                    result.page_url, "https://sellercenter.lazada.com.my/apps/balance/unaccessable",
                )
                self.assertEqual(
                    result.income_page_url,
                    "https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index",
                )
                self.assertTrue(Path(result.diagnostic_screenshot).is_file())
                self.assertTrue(Path(result.income_diagnostic_screenshot).is_file())
                self.assertNotEqual(result.diagnostic_screenshot, result.income_diagnostic_screenshot)
                self.assertNotIn("private-", str(result))
                self.assertNotIn("private-", str(runtime.logger.mock_calls))
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()


class IncomeStatementCollectionTests(unittest.TestCase):
    def test_thailand_statement_archive_retains_navigation_and_drive_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "TH", [[statement_row("TH-2026-037")]]) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.collection_method, "income_statement")
                self.assertFalse(result.balance_page_unavailable)
                self.assertEqual(runtime.driver._lazada_target_country, "TH")
                runtime.driver.get.assert_called_once_with("https://sellercenter.lazada.co.th/")
                runtime.drive.ensure_path.assert_called_once_with(
                    ["9月", "测试店铺"], parent_id=service.COUNTRIES["TH"]["drive_root_id"],
                )

    def test_all_intersecting_periods_are_downloaded_across_pages(self):
        pages = [
            [
                statement_row("MY-after", "2026-10-05", "2026-10-11"),
                statement_row("MY-end-overlap", "2026-09-28", "2026-10-04"),
            ],
            [
                statement_row("MY-middle", "2026-09-14", "2026-09-20"),
                statement_row("MY-start-overlap", "2026-08-31", "2026-09-06"),
                statement_row("MY-before", "2026-08-24", "2026-08-30"),
            ],
        ]
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", pages) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.uploaded_count, 3)
                self.assertEqual(
                    [call.args[1]["statement_number"] for call in runtime.download.call_args_list],
                    ["MY-end-overlap", "MY-middle", "MY-start-overlap"],
                )
                self.assertEqual(runtime.withdrawal.wait_for_thailand_income_statement_rows.call_count, 2)
                runtime.withdrawal.click_thailand_income_statement_next_page.assert_called_once()
                self.assertFalse(result.no_data)
                info_messages = [call.args[0] % call.args[1:] for call in runtime.logger.info.call_args_list]
                self.assertTrue(any(
                    "第 1 页" in message and "本页 2 个账单" in message
                    and "目标日期匹配 1 个" in message and "2026-09-28~2026-10-11" in message
                    for message in info_messages
                ))
                self.assertTrue(any(
                    "第 2 页" in message and "本页 3 个账单" in message
                    and "目标日期匹配 2 个" in message and "2026-08-24~2026-09-20" in message
                    for message in info_messages
                ))

    def test_wrong_statement_identity_or_period_is_never_uploaded(self):
        for corrupt in ("number", "period"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                def download_wrong(_driver, row, _downloads, destination, _withdrawal):
                    path = destination / "wrong.xlsx"
                    write_statement(
                        path,
                        "MY-old-export" if corrupt == "number" else row["statement_number"],
                        "01 Jun 2026 - 07 Jun 2026" if corrupt == "period" else row["period"],
                    )
                    return str(path), "wrong-export-id"

                with statement_runtime(
                    Path(directory), "MY", [[statement_row("MY-requested")]], download=download_wrong,
                ) as runtime:
                    result = process_statements(runtime)
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.uploaded_count, 0)
                    self.assertEqual(result.downloaded_count, 0)
                    self.assertEqual(result.local_files, [])
                    self.assertEqual(len(result.failed_items), 1)
                    self.assertIn("不匹配", result.failed_items[0])
                    self.assertEqual(runtime.download.call_count, 2)
                    runtime.drive.upload_file.assert_not_called()

    def test_one_failed_download_keeps_partial_success(self):
        def download_partial(_driver, row, _downloads, destination, _withdrawal):
            if row["statement_number"] == "PH-failed":
                raise RuntimeError("fixture download failed")
            path = destination / f"{row['statement_number']}.xlsx"
            write_statement(path, row["statement_number"], row["period"])
            return str(path), "good-export-id"

        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(
                Path(directory), "PH", [[statement_row("PH-success"), statement_row("PH-failed")]],
                download=download_partial,
            ) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "partial_success")
                self.assertEqual(result.uploaded_count, 1)
                self.assertEqual(result.downloaded_count, 1)
                self.assertEqual(len(result.local_files), 1)
                self.assertEqual(len(result.failed_items), 1)
                runtime.drive.upload_file.assert_called_once()

    def test_all_failed_downloads_and_no_matching_periods_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(
                Path(directory), "MY", [[statement_row("MY-failed")]],
                download=Mock(side_effect=RuntimeError("fixture download failed")),
            ) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.uploaded_count, 0)
                self.assertEqual(len(result.failed_items), 1)
                runtime.drive.upload_file.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(
                Path(directory), "MY", [[statement_row("MY-before", "2026-08-24", "2026-08-30")]],
            ) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "no_matching_statement")
                self.assertFalse(result.no_data)
                self.assertEqual(result.uploaded_count, 0)
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_explicit_empty_statement_table_has_accurate_message_and_diagnostic_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "no_matching_statement")
                self.assertIn("收入账单列表为空", result.message)
                self.assertIn("页面已确认无数据", result.message)
                self.assertNotIn("日期范围内", result.message)
                self.assertFalse(result.no_data)
                self.assertTrue(Path(result.income_diagnostic_screenshot).is_file())
                self.assertEqual(result.income_page_url, "https://sellercenter.lazada.com.my/")
                self.assertEqual(result.page_url, "")
                self.assertEqual(result.diagnostic_screenshot, "")
                info_messages = [call.args[0] % call.args[1:] for call in runtime.logger.info.call_args_list]
                self.assertTrue(any("第 1 页" in message and "明确显示无数据" in message for message in info_messages))
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_existing_statements_outside_range_log_page_counts_and_iso_date_coverage(self):
        pages = [
            [statement_row("MY-after", "2026-10-05", "2026-10-11")],
            [statement_row("MY-before", "2026-08-24", "2026-08-30")],
        ]
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", pages) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "no_matching_statement")
                self.assertIn("已检查 2 页、2 个账单", result.message)
                self.assertIn("日期范围内没有匹配账单", result.message)
                self.assertNotIn("列表为空", result.message)
                self.assertTrue(Path(result.income_diagnostic_screenshot).is_file())
                info_messages = [call.args[0] % call.args[1:] for call in runtime.logger.info.call_args_list]
                for page, period in ((1, "2026-10-05~2026-10-11"), (2, "2026-08-24~2026-08-30")):
                    self.assertTrue(any(
                        f"第 {page} 页" in message and "本页 1 个账单" in message
                        and "目标日期匹配 0 个" in message and period in message
                        for message in info_messages
                    ))
                self.assertNotIn("MY-after", str(runtime.logger.mock_calls))
                self.assertNotIn("MY-before", str(runtime.logger.mock_calls))
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_diagnostic_screenshot_failure_does_not_change_no_match_or_expose_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[]]) as runtime:
                runtime.driver.save_screenshot.side_effect = RuntimeError("private-screenshot-detail")
                result = process_statements(runtime)
                self.assertEqual(result.status, "no_matching_statement")
                self.assertIn("收入账单列表为空", result.message)
                self.assertEqual(result.income_diagnostic_screenshot, "")
                self.assertEqual(result.failed_items, [])
                self.assertNotIn("private-", str(result))
                self.assertNotIn("private-", str(runtime.logger.mock_calls))
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_invalid_download_path_is_rejected_before_navigation_or_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with statement_runtime(root, "PH", [[statement_row("PH-requested")]]) as runtime:
                for path in ("", str(root / "missing"), str(root / "plain-file")):
                    if path.endswith("plain-file"):
                        Path(path).write_text("fixture", encoding="utf-8")
                    with self.subTest(path=path):
                        result = service._process_income_statements(
                            runtime.driver, runtime.job, "测试店铺", path, runtime.output, runtime.logger,
                        )
                        self.assertEqual(result.status, "failed")
                        self.assertIn("下载目录", result.message)
                runtime.driver.get.assert_not_called()
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_income_page_redirect_to_another_country_prevents_download_and_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[statement_row("MY-requested")]]) as runtime:
                def wrong_country(_util):
                    runtime.driver.current_url = "https://sellercenter.lazada.co.th/finance/income"
                    return True

                runtime.withdrawal.open_lazada_income_statement_page.side_effect = wrong_country
                result = process_statements(runtime)
                self.assertEqual(result.status, "failed")
                self.assertIn("国家", result.message)
                runtime.download.assert_not_called()
                runtime.drive.upload_file.assert_not_called()

    def test_repeated_pagination_page_is_partial_and_never_uploads_duplicate_statement(self):
        row = statement_row("MY-once")
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[row], [row]]) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "partial_success")
                self.assertEqual(result.uploaded_count, 1)
                self.assertEqual(result.downloaded_count, 1)
                self.assertEqual(len(result.local_files), 1)
                self.assertIn("未变化", result.message)
                self.assertTrue(result.failed_items)
                runtime.download.assert_called_once()
                runtime.drive.upload_file.assert_called_once()

    def test_statement_repeated_on_different_pages_is_uploaded_once(self):
        duplicate = statement_row("PH-duplicate")
        pages = [
            [duplicate],
            [duplicate, statement_row("PH-second"), statement_row("PH-old", "2026-08-24", "2026-08-30")],
        ]
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "PH", pages) as runtime:
                result = process_statements(runtime)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.uploaded_count, 2)
                self.assertEqual(
                    [call.args[1]["statement_number"] for call in runtime.download.call_args_list],
                    ["PH-duplicate", "PH-second"],
                )

    def test_navigation_failure_after_one_upload_keeps_partial_result(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "MY", [[statement_row("MY-done")]]) as runtime:
                runtime.withdrawal.click_thailand_income_statement_next_page.side_effect = RuntimeError(
                    "fixture next page unavailable"
                )
                result = process_statements(runtime)
                self.assertEqual(result.status, "partial_success")
                self.assertEqual(result.uploaded_count, 1)
                self.assertEqual(result.downloaded_count, 1)
                self.assertEqual(len(result.local_files), 1)
                self.assertEqual(len(result.drive_paths), 1)
                self.assertIn("fixture next page unavailable", result.message)
                self.assertTrue(result.failed_items)
                self.assertTrue(result.finished_at)

    def test_upload_retry_does_not_duplicate_validated_local_file_count(self):
        with tempfile.TemporaryDirectory() as directory:
            with statement_runtime(Path(directory), "PH", [[statement_row("PH-retried")]]) as runtime:
                runtime.drive.upload_file.side_effect = [
                    RuntimeError("fixture first upload failed"), SimpleNamespace(id="fixture-success"),
                ]
                result = process_statements(runtime)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.uploaded_count, 1)
                self.assertEqual(result.downloaded_count, 1)
                self.assertEqual(len(result.local_files), 1)
                self.assertEqual(len(result.drive_paths), 1)
                self.assertEqual(result.failed_items, [])
                self.assertEqual(runtime.download.call_count, 2)
                self.assertEqual(runtime.drive.upload_file.call_count, 2)

    def test_download_login_redirect_reports_expired_session_without_retry_or_upload(self):
        for prior_success in (False, True):
            with self.subTest(prior_success=prior_success), tempfile.TemporaryDirectory() as directory:
                rows = [statement_row("TH-login-expired"), statement_row("TH-never-attempted")]
                if prior_success:
                    rows.insert(0, statement_row("TH-already-uploaded"))

                def download_with_expired_session(driver, row, _downloads, destination, _withdrawal):
                    if row["statement_number"] == "TH-already-uploaded":
                        path = destination / "TH-already-uploaded.xlsx"
                        write_statement(path, row["statement_number"], row["period"])
                        return str(path), "fixture-uploaded-export-id"
                    driver.current_url = "https://sellercenter.lazada.co.th/apps/seller/login"
                    raise RuntimeError("fixture export menu unavailable")

                with statement_runtime(
                    Path(directory), "TH", [rows], download=download_with_expired_session,
                ) as runtime:
                    result = process_statements(runtime)
                    self.assertEqual(result.status, "partial_success" if prior_success else "failed")
                    self.assertIn("登录态失效", result.message)
                    self.assertNotIn("国家", result.message)
                    self.assertTrue(result.failed_items)
                    self.assertIn("登录态失效", result.failed_items[-1])
                    self.assertEqual(result.uploaded_count, int(prior_success))
                    self.assertEqual(result.downloaded_count, int(prior_success))
                    self.assertEqual(len(result.local_files), int(prior_success))
                    self.assertEqual(runtime.drive.upload_file.call_count, int(prior_success))
                    self.assertEqual(
                        [call.args[1]["statement_number"] for call in runtime.download.call_args_list],
                        ["TH-already-uploaded", "TH-login-expired"] if prior_success else ["TH-login-expired"],
                    )
                    runtime.withdrawal.click_thailand_income_statement_next_page.assert_not_called()


class BrowserDownloadPathPropagationTests(unittest.TestCase):
    def test_ph_my_worker_passes_browser_download_path_for_fallback(self):
        for country in ("PH", "MY"):
            with self.subTest(country=country), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = make_job(root, country)
                opened = {"browserId": "fixture-store", "downloadPath": "  C:/fixture-downloads  "}
                loader = Mock()
                loader.open_store.return_value = opened
                driver = Mock()
                loader.get_driver.return_value = driver
                expected = service.StoreResult(
                    country, "测试店铺", "测试店铺", task.start_date, task.end_date, status="success",
                )
                logger = Mock(spec=logging.Logger)
                with patch.object(service, "_process_ph_my", return_value=expected) as process:
                    result = service._run_one_browser(
                        loader, {"browserId": "fixture-store", "browserName": "测试店铺"}, task, root, logger,
                    )
                self.assertIs(result, expected)
                process.assert_called_once_with(
                    driver, task, "测试店铺", root / country / "测试店铺", logger,
                    browser_download_dir="C:/fixture-downloads",
                )
                loader.close_store.assert_called_once_with("fixture-store")


if __name__ == "__main__":
    unittest.main()

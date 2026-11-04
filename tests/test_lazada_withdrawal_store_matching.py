from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services import lazada_withdrawal_statistics as service


def query(root: Path, names: tuple[str, ...]):
    return service.LazadaWithdrawalStatisticsQuery(
        company="fixture", username="fixture", password="private-password", country="PH",
        start_date="2026-09-01", end_date="2026-09-30", store_names=names, output_root=str(root),
        dingtalk_app_key="fixture-app", dingtalk_app_secret="fixture-secret", dingtalk_user_id="fixture-user",
    )


class WithdrawalStoreMatchingIntegrationTests(unittest.TestCase):
    def test_runner_passes_selected_country_and_preserves_match_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"browserId": "profile-29", "browserName": "操作员-LZ菲律宾029-PH007-半运营"}
            catalog = [original]
            diagnostics = {
                "菲律宾4": {"reason": "ambiguous", "candidate_names": ["甲-LZ菲律宾004", "乙-LZmall菲律宾004"]},
                "菲律宾165": {"reason": "not_found", "candidate_names": []},
                original["browserName"]: {"reason": "duplicate", "candidate_names": [original["browserName"]]},
                "马来21": {"reason": "country_mismatch", "candidate_names": []},
            }

            def match(browsers, names, logger, *, diagnostics: dict, target_country):
                self.assertIs(browsers, catalog)
                self.assertEqual(target_country, "PH")
                self.assertEqual(names[0], "菲律宾29")
                diagnostics.update(match_diagnostics)
                return [{**original, "_withdrawal_requested_store_name": "菲律宾29"}], list(match_diagnostics)

            match_diagnostics = diagnostics
            loader = Mock()
            loader.load_super_browser.return_value = catalog
            operator = SimpleNamespace(
                load_runtime_configs=Mock(return_value=({}, {})),
                LazadaWithdrawalSettings=Mock(),
                _merge_settings_into_config=Mock(side_effect=lambda config, settings: config),
                _filter_lazada_stores=match,
            )

            def process(_loader, browser, job, _run_dir, _logger):
                return service.StoreResult(
                    job.country, browser["_withdrawal_requested_store_name"], browser["browserName"],
                    job.start_date, job.end_date, status="success",
                )

            with (
                patch.object(service, "_reference_modules", return_value=(operator, Mock(), Mock(return_value=loader), None)),
                patch.object(service, "_run_one_browser", side_effect=process) as worker,
                patch.object(service, "_make_logger", return_value=(Mock(), root / "run.log")),
            ):
                result = service.run_lazada_withdrawal_statistics(query(root, ("菲律宾29", *diagnostics)))

            worker.assert_called_once()
            loader.get_exit.assert_called_once_with()
            self.assertNotIn("_withdrawal_requested_store_name", original)
            self.assertEqual(result["matched_store_count"], 1)
            self.assertEqual(result["processed_store_count"], 5)
            self.assertFalse(result["success"])
            by_name = {item["requested_store_name"]: item for item in result["stores"]}
            self.assertEqual(by_name["菲律宾29"]["store_name"], original["browserName"])
            for name, diagnostic in diagnostics.items():
                with self.subTest(name=name):
                    item = by_name[name]
                    self.assertEqual(item["status"], "unmatched_store")
                    self.assertEqual(item["match_reason"], diagnostic["reason"])
                    self.assertEqual(item["candidate_store_names"], diagnostic["candidate_names"])
            self.assertIn("完整名称", by_name["菲律宾4"]["message"])
            self.assertIn("甲-LZ菲律宾004", by_name["菲律宾4"]["message"])
            self.assertIn("店铺授权", by_name["菲律宾165"]["message"])
            self.assertIn("重复", by_name[original["browserName"]]["message"])
            self.assertIn("所选菲律宾", by_name["马来21"]["message"])
            saved = json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))
            self.assertEqual(saved, result)
            self.assertNotIn("private-password", json.dumps(saved))

    def test_worker_retains_the_short_input_that_selected_the_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = "操作员-LZ菲律宾029-PH007-半运营"
            job = query(root, ("菲律宾29",))
            loader = Mock()
            loader.open_store.return_value = {"browserId": "profile-29", "downloadPath": str(root)}
            processed = service.StoreResult("PH", actual, actual, job.start_date, job.end_date, status="success")
            with patch.object(service, "_process_ph_my", return_value=processed):
                result = service._run_one_browser(
                    loader,
                    {"browserId": "profile-29", "browserName": actual, "_withdrawal_requested_store_name": "菲律宾29"},
                    job, root, Mock(),
                )
            self.assertEqual(result.requested_store_name, "菲律宾29")
            self.assertEqual(result.store_name, actual)
            loader.open_store.assert_called_once_with("profile-29")
            loader.close_store.assert_called_once_with("profile-29")

    def test_worker_ignores_private_alias_metadata_not_in_the_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = "操作员-LZ菲律宾029-PH007-半运营"
            job = query(root, (actual,))
            loader = Mock()
            loader.open_store.return_value = {"browserId": "profile-29"}
            processed = service.StoreResult("PH", actual, actual, job.start_date, job.end_date, status="success")
            with patch.object(service, "_process_ph_my", return_value=processed):
                result = service._run_one_browser(
                    loader,
                    {"browserId": "profile-29", "browserName": actual, "_withdrawal_requested_store_name": "错误输入"},
                    job, root, Mock(),
                )
            self.assertEqual(result.requested_store_name, actual)


if __name__ == "__main__":
    unittest.main()

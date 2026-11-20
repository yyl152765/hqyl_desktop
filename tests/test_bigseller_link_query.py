from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services import bigseller_link_query as service
from backend.services import bigseller_sku_benchmark as transport
from backend.services.bigseller_item_id_query import (
    BigSellerAuthenticationError, BigSellerPermissionError, BigSellerQueryError,
    validate_bigseller_item_id_query_payload,
)


def listing(**changes):
    return {"shopName": "最早店铺", "itemId": "001234567890123456789", "shopId": "internal-shop-id",
            "link": "https://shopee.ph/product/123/001234567890123456789",
            "platformCreateTime": "2020-07-16 17:48", "createTimeStr": "2025-05-29 11:11",
            "views": 0, "site": "PH", **changes}


def response(rows, total=None, status=200):
    page = {"rows": rows}
    if total is not None:
        page["totalSize"] = total
    value = Mock(status_code=status, headers={})
    value.json.return_value = {"code": 0, "data": {"page": page}}
    return value


class LinkQueryTests(unittest.TestCase):
    def job(self, directory, text="001\n002\n003"):
        return service.validate_bigseller_link_query_payload({
            "username": "private-account", "password": "fixture-password-1", "sku_text": text,
            "output_dir": directory, "captcha_username": "captcha", "captcha_password": "private-captcha",
        })

    def query(self, rows, total=None):
        with patch.object(service, "request_benchmark_page", return_value=(rows, total)):
            return service.query_first_listing(Mock(), Mock(), {}, "001", Mock())

    def test_uses_platform_time_ascending_and_same_first_listing_for_all_fields(self):
        session, util = Mock(), Mock()
        first = listing()
        second = listing(shopName="高浏览量店铺", itemId="999", views=9999,
                         createTimeStr="2018-01-01 00:00", platformCreateTime="2024-01-01 00:00")
        with patch.object(transport, "_post_listing", return_value=response([first, second], 200)) as post:
            result = service.query_first_listing(session, util, {}, "sku-child-A", Mock())
        query = post.call_args.args[3]
        self.assertEqual(query["orderBy"], "platform_create_time")
        self.assertIs(query["desc"], False)
        self.assertEqual((query["searchType"], query["searchContent"], query["inquireType"]), ("sku", "sku-child-A", 0))
        self.assertEqual((query["shopeeStatus"], query["status"], query["pageNo"]), ("live", "active", 1))
        self.assertNotIn("shopId", query)
        self.assertEqual(result["shop_name"], first["shopName"])
        self.assertEqual(result["product_url"], first["link"])
        self.assertEqual(result["listed_time"], "2020-07-16 17:48:00")
        self.assertEqual(result["item_id"], first["itemId"])
        self.assertEqual(result["listing_scope"], "live")
        post.assert_called_once()  # Server sorting makes later pages unnecessary.

    def test_live_empty_falls_back_to_unlisted_with_same_sku_and_earliest_sort(self):
        progress = Mock()
        with patch.object(service, "request_benchmark_page", side_effect=[([], 0), ([listing()], 3)]) as request:
            row = service.query_first_listing(Mock(), Mock(), {}, "sku-child-B2", progress)
        self.assertEqual(row["listing_scope"], "unlisted")
        self.assertEqual(row["status"], "matched")
        self.assertIn("在售无匹配", row["message"])
        live, unlisted = [call.kwargs["query"] for call in request.call_args_list]
        self.assertEqual(live["shopeeStatus"], "live")
        self.assertEqual(unlisted, {**live, "shopeeStatus": "unList"})
        self.assertEqual(unlisted["status"], "active")
        self.assertEqual(unlisted["searchContent"], "sku-child-B2")
        self.assertEqual(unlisted["orderBy"], "platform_create_time")
        self.assertIs(unlisted["desc"], False)
        self.assertIn("继续查询下架", progress.call_args.args[0])

    def test_earlier_other_country_does_not_override_selected_country(self):
        vietnam = listing(site="VN", shopName="越南店铺", itemId="101", platformCreateTime="2020-06-09 16:52")
        philippines = listing(shopName="菲律宾店铺", itemId="202", platformCreateTime="2020-08-17 18:10")
        later = listing(shopName="菲律宾较晚店铺", itemId="303", platformCreateTime="2020-09-01 17:48")
        with patch.object(service, "request_benchmark_page", return_value=([vietnam, philippines, later], 3)):
            row = service.query_first_listing(Mock(), Mock(), {}, "same-sku", Mock())
            other = service.query_first_listing(Mock(), Mock(), {}, "same-sku", Mock(), site="VN")
        self.assertEqual((row["shop_name"], row["item_id"], row["listed_time"], row["product_url"]),
                         ("菲律宾店铺", "202", "2020-08-17 18:10:00", philippines["link"]))
        self.assertEqual((other["shop_name"], other["item_id"]), ("越南店铺", "101"))

    def test_other_country_live_does_not_block_selected_country_unlisted(self):
        overseas = listing(site="VN", itemId="101")
        local = listing(itemId="202", platformCreateTime="2024-01-01 00:00")
        with patch.object(service, "request_benchmark_page", side_effect=[([overseas], 1), ([overseas, local], 2)]) as request:
            row = service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
        self.assertEqual((row["item_id"], row["listing_scope"]), ("202", "unlisted"))
        self.assertEqual([call.kwargs["query"]["shopeeStatus"] for call in request.call_args_list], ["live", "unList"])
        with patch.object(service, "request_benchmark_page", return_value=([overseas], 1)):
            empty = service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
        self.assertEqual(empty["status"], "not_found")
        self.assertIn("菲律宾", empty["message"])

    def test_country_match_on_later_page_preserves_sort_and_child_sku(self):
        first_page = [listing(site="VN", itemId=str(i)) for i in range(50)]
        local = listing(itemId="100", platformCreateTime="2024-01-01 00:00")
        for total in (51, None):
            with self.subTest(total=total), patch.object(service, "request_benchmark_page", side_effect=[(first_page, total), ([local], total)]) as request:
                row = service.query_first_listing(Mock(), Mock(), {}, "sku-child-B2", Mock())
            self.assertEqual(row["item_id"], "100")
            queries = [call.kwargs["query"] for call in request.call_args_list]
            self.assertEqual([q["pageNo"] for q in queries], [1, 2])
            self.assertEqual(queries[1], {**queries[0], "pageNo": 2})
            self.assertEqual(queries[1]["searchContent"], "sku-child-B2")
            self.assertNotIn("site", queries[1])  # BS silently ignores this parameter.

    def test_country_pagination_errors_never_fall_back_or_claim_not_found(self):
        first_page = [listing(site="VN", itemId=str(i)) for i in range(50)]
        for later_page, total in (([], 51), (first_page, 100), ([listing()], 52),
                                 ([listing(platformCreateTime="2010-01-01 00:00")], 51)):
            initial_total = 100 if total == 100 else 51
            with self.subTest(total=total, count=len(later_page)), \
                 patch.object(service, "request_benchmark_page", side_effect=[(first_page, initial_total), (later_page, total)]) as request:
                with self.assertRaises(BigSellerQueryError):
                    service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
                self.assertEqual(request.call_count, 2)
                self.assertTrue(all(call.kwargs["query"]["shopeeStatus"] == "live" for call in request.call_args_list))
        with patch.object(service, "MAX_PAGES", 1), patch.object(service, "request_benchmark_page", return_value=(first_page, 51)):
            with self.assertRaisesRegex(BigSellerQueryError, "超过查询上限"):
                service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())

    def test_unknown_country_is_failure_instead_of_guessing_from_shop_name(self):
        for site in (None, "", "ZZ", 1):
            with self.subTest(site=site), self.assertRaisesRegex(BigSellerQueryError, "国家/地区"):
                self.query([listing(site=site, shopName="菲律宾店铺")])

    def test_country_validation_defaults_to_philippines_and_rejects_invalid_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"username": "private-account", "password": "fixture-password-1", "sku_text": "sku", "output_dir": directory}
            self.assertEqual(service.validate_bigseller_link_query_payload(payload).site, "PH")
            job = service.validate_bigseller_link_query_payload({**payload, "site": " vn "})
            self.assertEqual(job.site, "VN")
            self.assertNotIn("private", repr(job))
            for site in ("all", "ZZ", " "):
                with self.subTest(site=site), self.assertRaises(ValueError):
                    service.validate_bigseller_link_query_payload({**payload, "site": site})

    def test_live_match_has_priority_even_if_unlisted_product_would_be_older(self):
        with patch.object(service, "request_benchmark_page", side_effect=[
            ([listing(platformCreateTime="2026-01-01 00:00", shopName="在售店铺")], 1),
            ([listing(platformCreateTime="2010-01-01 00:00", shopName="下架店铺")], 1),
        ]) as request:
            row = service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
        self.assertEqual((row["shop_name"], row["listing_scope"]), ("在售店铺", "live"))
        request.assert_called_once()

    def test_both_scopes_must_be_confirmed_empty_before_not_found(self):
        with patch.object(service, "request_benchmark_page", side_effect=[([], 0), ([], 0)]) as request:
            row = service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
        self.assertEqual(row["status"], "not_found")
        self.assertIn("在售及下架", row["message"])
        self.assertEqual(row["listing_scope"], "")
        self.assertEqual(request.call_count, 2)

    def test_live_errors_do_not_trigger_unlisted_fallback(self):
        errors = [BigSellerQueryError("响应格式异常"), BigSellerAuthenticationError("失效"),
                  BigSellerPermissionError("无权限"), transport.BenchmarkCooldownError("冷却", 60)]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 patch.object(service, "request_benchmark_page", side_effect=error) as request, \
                 self.assertRaises(type(error)):
                try:
                    service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
                finally:
                    request.assert_called_once()
        with patch.object(service, "request_benchmark_page", return_value=([listing(shopName="")], 1)) as request:
            with self.assertRaises(BigSellerQueryError):
                service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
            request.assert_called_once()

    def test_unlisted_errors_or_bad_first_row_are_not_not_found(self):
        for outcome in (BigSellerQueryError("接口失败"), ([], 5), ([listing(shopName=""), listing()], 2)):
            with self.subTest(outcome=outcome), \
                 patch.object(service, "request_benchmark_page", side_effect=[([], 0), outcome]) as request:
                with self.assertRaises(BigSellerQueryError):
                    service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
                self.assertEqual(request.call_count, 2)

    def test_ties_keep_first_listing_instead_of_resorting_by_id_or_shop(self):
        result = self.query([listing(itemId="999", shopName="Z"), listing(itemId="111", shopName="A")])
        self.assertEqual((result["shop_name"], result["item_id"]), ("Z", "999"))

    def test_empty_rows_only_count_as_not_found_if_total_is_consistent(self):
        self.assertEqual(self.query([], 0)["status"], "not_found")
        self.assertEqual(self.query([])["status"], "not_found")
        for rows, total in (([], 1), ([listing()], 0), ([listing(), listing()], 1)):
            with self.subTest(total=total, rows=rows), self.assertRaises(BigSellerQueryError):
                self.query(rows, total)

    def test_incomplete_first_listing_never_falls_back_to_later_product(self):
        for changes in ({"shopName": ""}, {"shopName": []}, {"shopName": "bad\x00name"},
                        {"itemId": None}, {"platformCreateTime": None}, {"platformCreateTime": "2025-99-99 12:00"}):
            with self.subTest(changes=changes), self.assertRaises(BigSellerQueryError):
                self.query([listing(**changes), listing()])

    def test_changed_sort_is_a_failure_not_a_wrong_earliest_shop(self):
        with self.assertRaisesRegex(BigSellerQueryError, "未按上架时间升序"):
            self.query([listing(platformCreateTime="2025-01-01 00:00"), listing()])

    def test_invalid_link_keeps_shop_and_never_uses_internal_shop_id_to_build_url(self):
        for link in (None, "", "javascript:alert(1)", "file:///C:/private", "https://shopee.ph.evil.test/a",
                     "https://user:secret@shopee.ph/a", "https://shopee.ph:9999/a", "https://shopee.ph/a\nb"):
            with self.subTest(link=link):
                row = self.query([listing(link=link)])
                self.assertEqual(row["status"], "matched")
                self.assertEqual(row["shop_name"], "最早店铺")
                self.assertEqual(row["product_url"], "")
                self.assertIn("未返回有效商品链接", row["message"])

    def test_business_rate_limit_uses_benchmark_backoff_and_closes_responses(self):
        limited = response([])
        limited.json.return_value = {"code": -1, "msg": "The request is too frequent. private-password"}
        success = response([listing()])
        with patch.object(transport, "_post_listing", side_effect=[limited, success]), \
             patch.object(transport, "_register_cooldown") as cooldown, \
             patch.object(transport, "wait_before_retry") as wait:
            row = service.query_first_listing(Mock(), Mock(), {}, "sku", Mock())
        self.assertEqual(row["status"], "matched")
        cooldown.assert_called_once_with(10.0)
        self.assertEqual(wait.call_args.args[0], 10.0)
        limited.close.assert_called_once()
        success.close.assert_called_once()

    def test_input_keeps_order_leading_zeroes_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            job = self.job(directory, "\ufeff001,001；SKU-a\nSKU-A\ttwo words")
            self.assertEqual(job.skus, ("001", "SKU-a", "SKU-A", "two words"))
            self.assertNotIn("private", repr(job))

    def test_link_capacity_accepts_50000_unique_skus_without_changing_item_id_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            skus = tuple(f"{index:06d}" for index in range(50000))
            job = self.job(directory, "\n".join((*skus, skus[0], skus[-1])))
            self.assertEqual(job.skus, skus)
            self.assertEqual(service.MAX_SKU_COUNT, 50000)
            with self.assertRaisesRegex(ValueError, "50000"):
                self.job(directory, "\n".join((*skus, "overflow")))
            with self.assertRaisesRegex(ValueError, "5000"):
                validate_bigseller_item_id_query_payload({
                    "username": "fixture-password-2", "password": "fixture-password-2", "sku_text": "\n".join(skus[:5001]),
                    "output_dir": directory, "max_sku_count": 50000,
                })

    def test_15001_skus_run_and_export_all_rows_with_limited_preview(self):
        skus = tuple(f"{index:06d}" for index in range(15001))
        def offline_result(_session, _util, _config, sku, _progress, *, site):
            return service.empty_row(sku, "离线容量测试：无匹配", "not_found")
        with tempfile.TemporaryDirectory() as directory:
            result, logs, _, _, query = self.run_job(directory, text="\n".join(skus), effects=offline_result)
            self.assertTrue(result["is_complete"])
            self.assertEqual((query.call_count, result["sku_count"], result["confirmed_count"], result["not_found_count"]), (15001,) * 4)
            self.assertEqual((len(result["rows"]), result["preview_limited"]), (500, True))
            self.assertTrue(any("[BS链接进度 15001/15001]" in line for line in logs))
            book = load_workbook(result["output_file"], read_only=True)
            try:
                exported = tuple(row[0] for row in book.active.iter_rows(min_row=2, max_col=1, values_only=True))
                self.assertEqual(exported, skus)
                self.assertEqual(book["查询说明"]["B5"].value, 15001)
                self.assertEqual(book["查询说明"]["B7"].value, 15001)
            finally:
                book.close()

    def run_job(self, directory, *, effects=None, login_effect=None, text="001\n002\n003"):
        session, logs = Mock(), []
        with patch.object(service, "load_bigseller_request_util", return_value=Mock()), \
             patch.object(service, "login_for_query", return_value=session, side_effect=login_effect) as login, \
             patch.object(service, "query_first_listing", side_effect=effects) as query:
            result = service.run_bigseller_link_query(self.job(directory, text), logs.append)
        return result, logs, session, login, query

    def test_permission_failure_preserves_success_and_marks_unexecuted_as_failed(self):
        good = {**self.query([listing()]), "sku": "001"}
        with tempfile.TemporaryDirectory() as directory:
            result, logs, session, _, query = self.run_job(
                directory, effects=[good, BigSellerPermissionError("账号无权读取商品列表")],
            )
            self.assertFalse(result["is_complete"])
            self.assertEqual((result["matched_count"], result["failed_count"], result["confirmed_count"]), (1, 2, 1))
            self.assertEqual(result["failed_skus"], ["002", "003"])
            self.assertIn("未执行", result["rows"][2]["message"])
            self.assertIn("结果不完整", result["output_file"])
            self.assertEqual(query.call_count, 2)
            session.close.assert_called_once()
            self.assertNotIn("fixture-password-1", " ".join(logs))

    def test_login_failure_is_incomplete_with_all_skus_in_export(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, _, _, query = self.run_job(directory, login_effect=BigSellerAuthenticationError("登录失败"))
            self.assertFalse(result["is_complete"])
            self.assertEqual(result["failed_count"], 3)
            self.assertEqual(result["confirmed_count"], 0)
            query.assert_not_called()
            book = load_workbook(result["output_file"])
            try:
                self.assertEqual(book.active.max_row, 4)
                self.assertTrue(all(row[6].value == "查询失败" for row in list(book.active)[1:]))
            finally:
                book.close()

    def test_reauth_once_then_stops_if_session_expires_again(self):
        good = self.query([listing()])
        with tempfile.TemporaryDirectory() as directory:
            result, _, session, login, query = self.run_job(directory, effects=[
                BigSellerAuthenticationError("失效"), good, BigSellerAuthenticationError("再次失效"),
            ])
            self.assertEqual(login.call_count, 2)
            self.assertEqual(query.call_count, 3)
            self.assertEqual(result["failed_count"], 2)
            self.assertEqual(session.close.call_count, 2)

    def test_cooldown_stops_batch_and_unexpected_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            result, logs, _, _, query = self.run_job(directory, effects=[transport.BenchmarkCooldownError("等待冷却", 80)])
            self.assertEqual(result["failed_count"], 3)
            query.assert_called_once()
            result, logs, _, _, _ = self.run_job(directory, effects=RuntimeError("password=private-password"))
            self.assertNotIn("fixture-password-1", json.dumps(result) + " ".join(logs))
            self.assertEqual(result["failed_count"], 3)

    def test_export_preserves_text_formulas_status_and_clickable_url(self):
        row = {**self.query([listing(shopName="=shop")]), "sku": "=formula"}
        with tempfile.TemporaryDirectory() as directory:
            output = service.export_link_results(Path(directory), [row, service.empty_row("0002", "未找到", "not_found")])
            book = load_workbook(output, data_only=False)
            try:
                sheet = book.active
                self.assertEqual(sheet["A2"].value, "=formula")
                self.assertEqual(sheet["A2"].data_type, "s")
                self.assertEqual(sheet["B2"].data_type, "s")
                self.assertEqual(sheet["E2"].value, "001234567890123456789")
                self.assertEqual(sheet["C2"].hyperlink.target, row["product_url"])
                self.assertEqual(sheet["A3"].value, "0002")
                self.assertEqual(sheet["F2"].value, "在售")
                self.assertEqual(sheet["G3"].value, "未找到商品")
            finally:
                book.close()

    def test_runner_reports_and_exports_live_and_unlisted_counts(self):
        live = self.query([listing()])
        unlisted = {**live, "sku": "002", "listing_scope": "unlisted"}
        with tempfile.TemporaryDirectory() as directory:
            result, *_ = self.run_job(directory, effects=[live, unlisted, service.empty_row("003", "均未找到", "not_found")])
            self.assertTrue(result["is_complete"])
            self.assertEqual((result["matched_count"], result["live_matched_count"], result["unlisted_matched_count"]), (2, 1, 1))
            book = load_workbook(result["output_file"])
            try:
                self.assertEqual([book.active.cell(i, 6).value for i in (2, 3, 4)], ["在售", "下架", None])
                self.assertEqual(book["查询说明"]["B9"].value, 1)
                self.assertEqual(book["查询说明"]["B10"].value, 1)
                self.assertEqual(book["查询说明"]["B11"].value, "菲律宾（PH）")
            finally:
                book.close()

    def test_selected_country_survives_reauthentication_and_export(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(service, "load_bigseller_request_util"), \
             patch.object(service, "login_for_query"), \
             patch.object(service, "query_first_listing", side_effect=[BigSellerAuthenticationError("失效"), service.empty_row("sku", "未找到", "not_found")]) as query:
            job = replace(self.job(directory, "sku"), site="VN")
            result = service.run_bigseller_link_query(job, Mock())
            self.assertTrue(result["is_complete"])
            self.assertEqual(result["site"], "VN")
            self.assertEqual([call.kwargs["site"] for call in query.call_args_list], ["VN", "VN"])
            self.assertIn("_VN_", result["output_file"])
            book = load_workbook(result["output_file"])
            try:
                self.assertEqual(book["查询说明"]["B11"].value, "越南（VN）")
            finally:
                book.close()

    def test_export_failure_keeps_preview_and_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service, "export_link_results", side_effect=OSError("fixture-password-1")):
            result, logs, *_ = self.run_job(directory, effects=[self.query([listing()])], text="001")
            self.assertFalse(result["is_complete"])
            self.assertEqual(result["output_file"], "")
            self.assertEqual(result["matched_count"], 1)
            self.assertIn("导出失败", result["completion_message"])
            self.assertNotIn("fixture-password-1", json.dumps(result) + " ".join(logs))

    def test_preview_limit_does_not_truncate_failed_skus_or_excel(self):
        with tempfile.TemporaryDirectory() as directory:
            result, *_ = self.run_job(directory, login_effect=BigSellerAuthenticationError("登录失败"),
                                     text="\n".join(f"{i:04}" for i in range(501)))
            self.assertEqual(len(result["rows"]), 500)
            self.assertEqual(len(result["failed_skus"]), 501)
            self.assertTrue(result["preview_limited"])
            book = load_workbook(result["output_file"])
            try:
                self.assertEqual(book.active.max_row, 502)
            finally:
                book.close()


class LinkBridgeTests(unittest.TestCase):
    def bridge(self, bound=True):
        bridge = AppBridge()
        bridge.config_store = Mock()
        bridge.config_store.load.return_value = AppSettings(
            accounts=[BoundAccount(id="bs", vendor="bigseller", name="BS", username="bound", password="fixture-password-1")] if bound else [],
            active_account_ids={"bigseller": "bs"} if bound else {},
            captcha_username="captcha", captcha_password="private-captcha",
        )
        bridge.tasks = Mock()
        return bridge

    def test_uses_bound_account_and_passes_only_sku_count_and_country_to_task_context(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge()
            with patch("backend.app_bridge.run_bigseller_link_query", return_value={"is_complete": True}) as run:
                bridge.start_bigseller_link_query({"sku_text": "001\n001", "output_dir": directory, "site": "VN", "username": "fixture-password-3", "password": "fixture-password-3"})
                call = bridge.tasks.start.call_args
                self.assertEqual(call.kwargs["tool"], "bigseller_link_query")
                self.assertEqual(call.kwargs["context"], {"sku_count": 1, "site": "VN"})
                self.assertNotIn("private", json.dumps(call.kwargs))
                call.args[1](Mock())
                self.assertEqual(run.call_args.args[0].username, "bound")
                self.assertEqual(run.call_args.args[0].captcha_password, "private-captcha")
                self.assertEqual(run.call_args.args[0].site, "VN")

    def test_missing_account_and_validation_failures_do_not_start_task(self):
        bridge = self.bridge(False)
        self.assertTrue(bridge.start_bigseller_link_query({})["requires_account"])
        bridge.tasks.start.assert_not_called()
        bridge = self.bridge()
        self.assertFalse(bridge.start_bigseller_link_query({"sku_text": ""})["ok"])
        with patch("backend.app_bridge.validate_bigseller_link_query_payload", side_effect=RuntimeError("fixture-password-1")):
            self.assertNotIn("fixture-password-1", bridge.start_bigseller_link_query({})["error"])
        bridge.tasks.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

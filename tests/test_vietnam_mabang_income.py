from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.core.mabang_client import MabangClient
from backend.core.mabang_order_client import OrderShopInfo
from backend.services.vietnam_income_collection import (
    REPORT_TYPES,
    create_collection_run,
    load_manifest,
    validate_collection_payload,
    write_manifest,
)
from backend.services.vietnam_mabang_income import (
    build_income_detail_form,
    build_income_summary_form,
    discover_shopee_vietnam_shops,
    normalize_income_rows,
    parse_income_pagination,
    parse_income_summary_table,
    validate_income_totals,
    validate_vietnam_mabang_income_payload,
)


class VietnamMabangIncomeTests(unittest.TestCase):
    def _created_manifest(self, temp_dir: str, *, sources_ready: bool) -> Path:
        request = validate_collection_payload(
            {
                "period": "2026-05",
                "output_dir": temp_dir,
                "stores": [{"name": "越南测试店"}],
            }
        )
        created = create_collection_run(request)
        manifest_path = Path(created["manifest_path"])
        if sources_ready:
            manifest = load_manifest(manifest_path)
            for report_type in REPORT_TYPES:
                output_file = manifest_path.parent / "raw" / f"{report_type}.csv"
                output_file.write_text("a\n1\n", encoding="utf-8")
                report = manifest["stores"][0]["reports"][report_type]
                report["status"] = "success"
                report["file"] = str(output_file.relative_to(manifest_path.parent))
            manifest["status"] = "sources_ready"
            write_manifest(manifest_path, manifest)
        return manifest_path

    def test_job_requires_completed_ziniao_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._created_manifest(temp_dir, sources_ready=False)
            with self.assertRaisesRegex(ValueError, "紫鸟四类数据"):
                validate_vietnam_mabang_income_payload(
                    {
                        "manifest_path": str(manifest_path),
                        "username": "13800000000",
                        "password": "secret",
                    }
                )

    def test_job_uses_bound_account_without_persisting_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._created_manifest(temp_dir, sources_ready=True)
            job = validate_vietnam_mabang_income_payload(
                {
                    "manifest_path": str(manifest_path),
                    "username": "13800000000",
                    "password": "secret",
                    "account_name": "马帮主账号",
                }
            )
            self.assertEqual(job.username, "13800000000")
            self.assertEqual(job.account_name, "马帮主账号")
            self.assertNotIn("secret", manifest_path.read_text(encoding="utf-8"))

    def test_detail_form_uses_paytime_rmb_and_dynamic_shop_ids(self) -> None:
        form = build_income_detail_form(
            ["101", "102"],
            "2026-05-01",
            "2026-05-31",
            page=3,
        )
        values = dict(form)
        self.assertEqual(values["paytimeTimeStart"], "2026-05-01 00:00:00")
        self.assertEqual(values["paytimeTimeEnd"], "2026-05-31 23:59:59")
        self.assertEqual(values["expresstimeTimeStart"], "")
        self.assertEqual(values["expresstimeTimeEnd"], "")
        self.assertEqual(values["moneyType"], "RMB")
        self.assertEqual(values["page"], "3")
        self.assertEqual([value for key, value in form if key == "shopIdMultiple[]"], ["101", "102"])
        self.assertFalse(any(key == "platformId[]" for key, _value in form))

    def test_summary_form_uses_same_paytime_rmb_scope(self) -> None:
        form = build_income_summary_form(["101", "102"], "2026-05-01", "2026-05-31")
        values = dict(form)
        self.assertEqual(values["timeType"], "paytime")
        self.assertEqual(values["dayDateStart"], "2026-05-01")
        self.assertEqual(values["dayDateEnd"], "2026-05-31")
        self.assertEqual(values["moneyType"], "RMB")
        self.assertEqual(values["platformId[]"], "17")
        self.assertEqual([value for key, value in form if key == "shopIdMultiple[]"], ["101", "102"])

    def test_pagination_parser_reads_total_pages_and_rows(self) -> None:
        page_html = "<div>每页 1000 条 共 3369 条 当前显示第 1-1000 条 1/4 页</div>"
        self.assertEqual(parse_income_pagination(page_html), (4, 3369))

    def test_normalize_income_rows_maps_required_finance_fields(self) -> None:
        rows = normalize_income_rows(
            [
                {
                    "platformOrder": "ORDER-1",
                    "shopName": "越南测试店",
                    "paidTime": "2026-05-02 12:00:00",
                    "itemTotal": "12.34",
                    "orderWeight": "500",
                    "logisticsChannel": "VN渠道",
                    "shippingCostNew": "4",
                    "packageFee": "1",
                    "quantityTotal": "2",
                }
            ]
        )
        self.assertEqual(rows[0]["订单编号"], "ORDER-1")
        self.assertEqual(rows[0]["收入-应收货款"], "12.34")
        self.assertEqual(rows[0]["物流渠道"], "VN渠道")

    def test_summary_parser_uses_item_total_as_receivable(self) -> None:
        html = """
        <table><tr>
          <td>1</td><td>越南测试店</td>
          <td data-field="totalNum">2</td>
          <td data-field="itemTotal">30.00</td>
          <td data-field="incomeTotal">99.00</td>
        </tr></table>
        """
        rows = parse_income_summary_table(html)
        self.assertEqual(rows[0]["订单数"], "2")
        self.assertEqual(rows[0]["应收货款"], "30.00")
        self.assertEqual(rows[0]["收入小计"], "99.00")

    def test_validation_compares_distinct_orders_and_item_total(self) -> None:
        detail_rows = [
            {"店铺名称": "越南测试店", "订单编号": "A", "收入-应收货款": "10.10"},
            {"店铺名称": "越南测试店", "订单编号": "B", "收入-应收货款": "19.90"},
        ]
        summary_rows = [
            {"店铺名称": "越南测试店", "订单数": "2", "应收货款": "30", "收入小计": "999"}
        ]
        validation = validate_income_totals(detail_rows, summary_rows)
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(validation["order_count_diff"], 0)
        self.assertEqual(validation["receivable_diff"], "0")

    def test_validation_reports_per_store_difference(self) -> None:
        validation = validate_income_totals(
            [{"店铺名称": "越南测试店", "订单编号": "A", "收入-应收货款": "10"}],
            [{"店铺名称": "越南测试店", "订单数": "2", "应收货款": "11"}],
        )
        self.assertEqual(validation["status"], "failed")
        self.assertEqual(validation["order_count_diff"], -1)
        self.assertEqual(validation["receivable_diff"], "-1")
        self.assertEqual(len(validation["store_mismatches"]), 1)

    @patch("backend.services.vietnam_mabang_income.list_order_shops")
    def test_store_discovery_filters_shopee_vietnam(self, mocked_list) -> None:
        mocked_list.return_value = [
            OrderShopInfo("1", "运营-越南001", "17", "Shopee"),
            OrderShopInfo("2", "运营-VN002", "17", "Shopee"),
            OrderShopInfo("3", "运营-越南Lazada", "7", "Lazada"),
            OrderShopInfo("4", "运营-菲律宾001", "17", "Shopee"),
        ]
        shops = discover_shopee_vietnam_shops(object())
        self.assertEqual([shop.shop_id for shop in shops], ["2", "1"])

    def test_core_client_encodes_repeated_form_fields(self) -> None:
        class FakeHttpClient:
            def __init__(self) -> None:
                self.content = ""
                self.url = ""
                self.kwargs = {}

            def post(self, url, **kwargs):
                self.url = str(url)
                self.kwargs = kwargs
                self.content = str(kwargs.get("content") or "")
                return httpx.Response(
                    200,
                    json={"success": True},
                    request=httpx.Request("POST", url),
                )

            def close(self) -> None:
                pass

        fake = FakeHttpClient()
        client = MabangClient("https://example.com", client=fake)
        client._logged_in = True
        client.post_form_json(
            "/index.php",
            form_data=[("shopIdMultiple[]", "101"), ("shopIdMultiple[]", "102")],
        )
        self.assertIn("shopIdMultiple%5B%5D=101", fake.content)
        self.assertIn("shopIdMultiple%5B%5D=102", fake.content)

        client.post_form_json(
            "https://example.com/index.php?mod=paypal.dosearchpaypalrefund",
            form_data=[("platformId", "17")],
        )
        self.assertIn("mod=paypal.dosearchpaypalrefund", fake.url)
        self.assertNotIn("params", fake.kwargs)


if __name__ == "__main__":
    unittest.main()

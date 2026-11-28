from __future__ import annotations

import unittest

from backend.core.mabang_client import MabangApiError
from backend.core.mabang_sales_report import (
    ModernOption,
    modern_filters_from_legacy_form,
    parse_filter_options_payload,
    rows_to_sales_csv,
)


class ModernSalesReportTests(unittest.TestCase):
    def test_filter_options_parse_categories_warehouses_and_people(self) -> None:
        payload = {
            "shopCategories": [
                {"id": 100, "name": "菲S-雷莹莹团队", "shopIds": [1], "children": [
                    {"id": 101, "name": "子分类", "shopIds": [2], "children": []},
                ]},
            ],
            "warehouseList": [{"stockWarehouseId": 330978, "stockWarehouseName": "泰国新仓TH01"}],
            "developerList": [{"developId": 0, "developName": "未分配开发员"}, {"developId": 111, "developName": "黄茵茵-开发>1"}],
            "saleList": [{"saleId": 0, "saleName": "未分配销售员"}, {"saleId": 222, "saleName": "张三"}],
        }

        options = parse_filter_options_payload(payload)

        self.assertEqual(options.categories, (ModernOption("100", "菲S-雷莹莹团队"), ModernOption("101", "子分类")))
        self.assertEqual(options.warehouses, (ModernOption("330978", "泰国新仓TH01"),))
        self.assertEqual([item.id for item in options.developers], ["0", "111"])
        self.assertEqual([item.id for item in options.salespeople], ["0", "222"])

    def test_legacy_form_maps_repeated_filters_and_us_currency(self) -> None:
        filters = modern_filters_from_legacy_form([
            ("timeStart", "2026-07-01 00:00:00"),
            ("timeEnd", "2026-07-31 23:59:59"),
            ("moneyType", "US"),
            ("platformOrShop", "country"),
            ("shopLabelIds[]", "1057656"),
            ("developid[]", "0"),
            ("developid[]", "111"),
        ], report_type="countryReports")

        self.assertEqual(filters["currency"], "USD")
        self.assertEqual(filters["shopLabelIds"], ["1057656"])
        self.assertEqual(filters["developId"], ["0", "111"])
        self.assertEqual(filters["type"], "countryCode")

    def test_rows_to_sales_csv_requires_full_41_column_mapping(self) -> None:
        row = {
            "stockSku": "SKU001",
            "nameCN": "商品",
            "nameEN": "Product",
            "itemId": "000123",
            "ncm": "NCM",
            "salesSku": "MAIN001",
            "salesSkuNewId": 99,
            "status": 3,
            "defaultCost": "1.20",
            "weight": "50",
            "stockQuantity": "10",
            "timeCreated": "2026-01-01",
            "sale": "张三",
            "develop": "李四",
            "orderNum": "2",
            "quantity": "4",
            "averageSellingPrice": "3.0000",
            "saleMoney": "12.0000",
            "shippingTotal": "1",
            "otherIncome": "0",
            "subsidyAmount": "0",
            "incomeTotal": "13",
            "itemTotalCost": "4",
            "shippingCostNew": "1",
            "packageFee": "0.5",
            "platformFee": "0.2",
            "paypalFee": "0.1",
            "headShipping": "0.3",
            "fbaMoney": "0",
            "vatTaxation": "0",
            "voucherPrice": "0",
            "otherMoney": "0",
            "otherExpend": "0",
            "evaluationFee": "0",
            "expendTotal": "6.1",
            "refundNum": "0",
            "itemRefundNum": "0",
            "refundMoney": None,
            "refundMoneyRMB": "0",
            "returnRate": "0",
            "gross": "6.9",
            "grossRate": "0.5308",
        }

        content = rows_to_sales_csv([row], expected_days=4, context="测试")
        text = content.decode("utf-8-sig")

        self.assertIn("item_id", text.splitlines()[0])
        self.assertIn("SKU001,商品,Product,000123", text)
        self.assertIn("1.00\t", text)
        self.assertIn("正常", text)

    def test_rows_to_sales_csv_rejects_missing_required_field(self) -> None:
        with self.assertRaisesRegex(MabangApiError, "缺少新版字段映射"):
            rows_to_sales_csv([{"stockSku": "SKU001", "quantity": "1"}], expected_days=1, context="测试")


if __name__ == "__main__":
    unittest.main()

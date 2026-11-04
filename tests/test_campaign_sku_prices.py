from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from backend.services.campaign_sku_prices import find_activity_sku_column, load_sku_prices, sku_value


class CampaignSkuPriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, rows, headers=("SKU", "价格"), extra=None, name="参考价格.xlsx"):
        book = Workbook()
        book.active.title = "价格数据"
        book.active.append(list(headers))
        for row in rows:
            book.active.append(row)
        if extra:
            extra(book)
        path = self.root / name
        book.save(path)
        book.close()
        return path

    def test_read_exact_skus_and_currency_without_changing_source(self):
        source = self.write([("  ABC-01  ", "PHP 1,000.00"), ("abc-01", 90), ("000123", 95)])
        before = source.read_bytes()
        result = load_sku_prices(source)
        self.assertEqual(result, {"ABC-01": Decimal("1000"), "abc-01": Decimal("90"), "000123": Decimal("95")})
        self.assertNotIn("ABC", result)
        self.assertEqual(source.read_bytes(), before)

    def test_multi_sheet_prices_merge_and_field_guide_is_not_data(self):
        def extra(book):
            guide = book.create_sheet("字段说明")
            guide.append(["字段", "说明"])
            guide.append(["SKU", "商家编码"])
            guide.append(["价格", "必须填入正数"])
            second = book.create_sheet("第二价格表")
            second.append(["Seller SKU", "Floor Price (USD)"])
            second.append(["b", 96])
            book.create_sheet("空表")
        source = self.write([("a", 95)], extra=extra)
        self.assertEqual(load_sku_prices(source), {"a": Decimal("95"), "b": Decimal("96")})

    def test_headers_can_be_within_first_ten_rows_and_reordered(self):
        def extra(book):
            sheet = book.active
            sheet.insert_rows(1, 2)
            sheet["A1"] = "本批 SKU 参考价格"
        source = self.write([(95, "sku1")], headers=("最低价格（元）", "卖家SKU"), extra=extra)
        self.assertEqual(load_sku_prices(source), {"sku1": Decimal("95")})

    def test_reference_prefers_seller_sku_over_generic_sku(self):
        source = self.write([("wrong-id", "seller-value", 95)], headers=("SKU", "卖家SKU", "参考价格"))
        self.assertEqual(load_sku_prices(source), {"seller-value": Decimal("95")})

    def test_same_prices_deduplicate_and_conflicting_prices_fail_with_original_locations(self):
        same = self.write([("a", 95), ("a", "95.00")])
        self.assertEqual(load_sku_prices(same), {"a": Decimal("95")})
        def extra(book):
            sheet = book.create_sheet("第二表")
            sheet.append(["SKU", "价格"])
            sheet.append(["a", 96])
        conflict = self.write([("a", 95)], extra=extra, name="冲突.xlsx")
        with self.assertRaisesRegex(ValueError, "价格冲突.*价格数据.*第 2 行.*第二表.*第 2 行"):
            load_sku_prices(conflict)

    def test_missing_or_invalid_rows_fail_instead_of_dropping_reference_data(self):
        for index, row in enumerate((("a", None), (None, 95), ("a", 0), ("a", -1), ("a", "NaN"),
                                      ("a", "0 < Price <= 95"), ("a", "=95"), ("=A1", 95),
                                      ("a", "95 and 96"), (None, None, "有数据的行"))):
            with self.subTest(row=row):
                source = self.write([row], name=f"异常{index}.xlsx")
                with self.assertRaisesRegex(ValueError, "价格数据.*第 2 行"):
                    load_sku_prices(source)

    def test_duplicate_or_missing_headers_fail(self):
        for index, headers in enumerate((("SKU", "SKU", "价格"), ("卖家SKU", "Seller SKU", "价格"),
                                         ("SKU", "价格", "最低价"), ("SKU", "其他字段"),
                                         ("Sku ID", "价格"), ("Shop SKU", "价格"), ("店铺sku", "价格"))):
            with self.subTest(headers=headers):
                source = self.write([], headers=headers, name=f"表头{index}.xlsx")
                with self.assertRaises(ValueError):
                    load_sku_prices(source)

    def test_malformed_additional_data_sheet_is_not_silently_ignored(self):
        def extra(book):
            sheet = book.create_sheet("缺少价格")
            sheet.append(["SKU", "备注"])
            sheet.append(["b", "不能漏掉"])
        source = self.write([("a", 95)], extra=extra)
        with self.assertRaisesRegex(ValueError, "缺少价格.*第 1 行.*缺少价格表头"):
            load_sku_prices(source)
        def misleading_title(book):
            sheet = book.create_sheet("说明")
            sheet.append(["未知编码", "未知底价"])
            sheet.append(["b", 96])
        source = self.write([("a", 95)], extra=misleading_title, name="说明中也有表格.xlsx")
        with self.assertRaisesRegex(ValueError, "说明.*未找到完整"):
            load_sku_prices(source)

    def test_header_only_blank_workbook_and_blank_rows(self):
        source = self.write([(None, None), ("  ", None), ("a", 95), (None, None)])
        self.assertEqual(load_sku_prices(source), {"a": Decimal("95")})
        for index, rows in enumerate(([], [(None, None)])):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "没有可用"):
                    load_sku_prices(self.write(rows, name=f"空{index}.xlsx"))

    def test_additional_sheet_with_header_after_row_ten_is_not_silently_skipped(self):
        for title in ("延迟表头", "说明"):
            with self.subTest(title=title):
                def extra(book):
                    sheet = book.create_sheet(title)
                    sheet["A11"] = "SKU"
                    sheet["B11"] = "价格"
                    sheet["A12"] = "b"
                    sheet["B12"] = 96
                source = self.write([("a", 95)], extra=extra, name=f"{title}.xlsx")
                with self.assertRaisesRegex(ValueError, f"{title}.*前10行未找到完整"):
                    load_sku_prices(source)

    def test_sku_representation_preserves_text_and_simple_leading_zero_format(self):
        book = Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        cases = ((" 00123 ", "General", "00123"), (123, "00000", "00123"),
                 (123.0, "General", "123"), (0, "General", "0"), ("Ab-c", "General", "Ab-c"))
        for index, (value, number_format, expected) in enumerate(cases, 1):
            cell = sheet.cell(index, 1, value)
            cell.number_format = number_format
            self.assertEqual(sku_value(cell), expected)
        self.assertEqual(sku_value(sheet["B1"]), "")

    def test_invalid_numeric_skus_formulas_dates_and_errors_are_rejected(self):
        book = Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        values = (True, date(2026, 11, 4), -1, 1.25, 1234567890123456, float("inf"), "=A1", "#N/A")
        for index, value in enumerate(values, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sku_value(sheet.cell(index, 1, value))

    def test_numeric_and_text_sku_match_while_text_zero_prefix_stays_distinct(self):
        source = self.write([(123, 95), ("123", 95), ("00123", 96)])
        self.assertEqual(load_sku_prices(source), {"123": Decimal("95"), "00123": Decimal("96")})

    def test_activity_column_priority_ignores_platform_sku_and_rejects_same_priority_ambiguity(self):
        book = Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(["Sku ID", "Shop SKU", "SKU", "Seller SKU", "价格"])
        self.assertEqual(find_activity_sku_column(sheet, 1), 4)
        sheet["D1"] = "商品名"
        self.assertEqual(find_activity_sku_column(sheet, 1), 3)
        sheet["C1"] = "店铺SKU"
        with self.assertRaisesRegex(ValueError, "缺少卖家SKU/SKU"):
            find_activity_sku_column(sheet, 1)
        sheet["A1"] = "卖家SKU"
        sheet["B1"] = "Seller SKU"
        with self.assertRaisesRegex(ValueError, "重复或有歧义"):
            find_activity_sku_column(sheet, 1)

    def test_reference_extension_size_dimensions_and_corrupt_file_limits(self):
        source = self.write([("a", 95)])
        for value in (None, "", self.root / "missing.xlsx", self.root / "wrong.xls"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_sku_prices(value)
        with patch("backend.services.campaign_sku_prices.MAX_FILE_BYTES", 1), self.assertRaisesRegex(ValueError, "50 MiB"):
            load_sku_prices(source)
        with patch("backend.services.campaign_sku_prices.MAX_ROWS", 1), self.assertRaisesRegex(ValueError, "超过"):
            load_sku_prices(source)
        with patch("backend.services.campaign_sku_prices.MAX_COLUMNS", 1), self.assertRaisesRegex(ValueError, "超过"):
            load_sku_prices(source)
        corrupt = self.root / "损坏.xlsx"
        corrupt.write_bytes(b"not a workbook")
        with self.assertRaisesRegex(ValueError, "无法读取"):
            load_sku_prices(corrupt)


if __name__ == "__main__":
    unittest.main()

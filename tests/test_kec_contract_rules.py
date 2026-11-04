"""Independent arithmetic and workbook checks for the confirmed KEC contract."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from backend.services import kec_reconciliation as kec


D = Decimal
STORAGE_HEADERS = [
    "仓库代码", "客户名称", "SKU", "品名", "入库单号", "箱号", "库位",
    "Unit Length(CM)", "Unit Width(CM)", "Unit Height(CM)", "Unit Billing CBM",
    "计费开始日期", "备注", "货物状态", "入库时间", "计费时间", "数量(PCS)",
    "Total CBM within period", "库龄", "单价", "金额",
]


def storage_record(*, bill_day: date, age: int, billed_amount: str, length=100):
    return [
        "FIXTURE-WH", "虚构客户", "FIXTURE-SKU", "合成商品", "FIXTURE-IN", "BOX", "BIN",
        length, 60, 100, 1, "", "", "正常", bill_day - timedelta(days=age - 1),
        bill_day, 2, 2, age, "1.5", billed_amount,
    ]


def columns(sheet):
    return {cell.value: cell.column for cell in sheet[1]}


def value(sheet, row, label):
    return sheet.cell(row=row, column=columns(sheet)[label]).value


def order(number, tracking, quantity):
    return SimpleNamespace(
        platform_order_id=number, tracking_number=tracking, item_total_quantity=quantity,
        item_kind_count=1, show_order_status_text="已发货", platform_order_status="",
        order_status="", shipping_time_text="2026-09-15 12:00:00", refund_flag="",
    )


class ConfirmedKecPriceTests(unittest.TestCase):
    def setUp(self):
        self.rules = kec.KecQuoteRules()
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("Offline contract test attempted HTTP"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_september_through_december_ignore_daily_order_volume(self):
        for month in (9, 10, 11, 12):
            for daily in (D("0"), D("2999.99"), D("3000"), D("5001")):
                for age, expected in ((30, "0"), (31, "0"), (60, "0"), (61, "1.5"), (181, "1.5")):
                    with self.subTest(month=month, daily=daily, age=age):
                        self.assertEqual(self.rules.storage_price_for_age(age, bill_date=date(2026, month, 1), avg_daily_orders=daily), D(expected))

    def test_other_months_apply_the_exact_3000_daily_boundary(self):
        cases = (
            (30, "2999.99", "0"), (31, "2999.99", "2"), (60, "2999.99", "2"),
            (120, "2999.99", "2"), (121, "2999.99", "3.5"), (180, "2999.99", "3.5"),
            (181, "2999.99", "5.1"), (60, "3000", "0"), (61, "3000", "1.5"),
            (181, "3000", "1.5"),
        )
        for age, daily, expected in cases:
            with self.subTest(age=age, daily=daily):
                self.assertEqual(self.rules.storage_price_for_age(age, bill_date=date(2026, 8, 31), avg_daily_orders=D(daily)), D(expected))
        self.assertEqual(self.rules.storage_price_for_age(31, bill_date=datetime(2026, 9, 1, 12)), D("0"))
        self.assertIsNone(self.rules.storage_price_for_age(None, bill_date=date(2026, 9, 1)))

    def test_outbound_tiers_do_not_round_daily_averages_into_a_lower_band(self):
        cases = (
            ("500", "1.8", "0.6"), ("500.01", "1.7", "0.6"),
            ("1000", "1.7", "0.6"), ("1000.01", "1.5", "0.6"),
            ("2000", "1.5", "0.6"), ("2000.01", "1.4", "0.5"),
            ("3000", "1.4", "0.5"), ("3000.01", "1.3", "0.5"),
            ("5000", "1.3", "0.5"), ("5000.01", "1.2", "0.5"),
        )
        for daily, first, additional in cases:
            with self.subTest(daily=daily):
                self.assertEqual(self.rules.outbound_base_fee_for_avg_daily_orders(D(daily)), D(first))
                self.assertEqual(self.rules.outbound_extra_piece_fee_for_avg_daily_orders(D(daily)), D(additional))

    def test_twentieth_piece_changes_only_that_and_subsequent_piece_prices(self):
        # The first ticket includes three pieces; 4..19 are sixteen normal extra pieces.
        cases = (
            ("2500", 3, "0"), ("2500", 4, "0.5"), ("2500", 19, "8"),
            ("2500", 20, "8.3"), ("2500", 21, "8.6"),
            ("2000", 19, "9.6"), ("2000", 20, "9.9"), ("2000", 21, "10.2"),
        )
        for daily, quantity, expected in cases:
            with self.subTest(daily=daily, quantity=quantity):
                self.assertEqual(self.rules.outbound_additional_fee_for_quantity(quantity, avg_daily_orders=D(daily)), D(expected))
        for quantity, expected in ((19, "9.4"), (20, "9.7"), (21, "10.0")):
            self.assertEqual(
                self.rules.outbound_base_fee_for_avg_daily_orders(D("2500"))
                + self.rules.outbound_additional_fee_for_quantity(quantity, avg_daily_orders=D("2500")),
                D(expected),
            )

    def test_return_ticket_includes_three_pieces(self):
        for quantity, expected in ((None, "0"), (0, "0"), (1, "2"), (2, "2"), (3, "2"), (4, "3"), (5, "4")):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.rules.return_fee_for_quantity(quantity), D(expected))

    def test_old_quote_keeps_unmodified_prices_but_cannot_override_new_contract(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-old-quote.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            for row, rate in ((5, 0), (6, 0.7), (7, 1.1), (8, 1.9), (9, 8.8), (10, 7.7), (11, 6.6), (12, 99), (24, 999)):
                sheet.cell(row=row, column=9, value=rate)
            sheet.cell(row=9, column=10, value=0.8)
            sheet.cell(row=12, column=10, value=88)
            sheet.cell(row=43, column=9, value=0.25)
            sheet.cell(row=43, column=11, value="ABF-FIXTURE")
            workbook.save(path)
            workbook.close()
            rules = kec.KecQuoteRules.from_workbook(path)
        self.assertEqual(rules.storage_price_for_age(31, bill_date=date(2026, 2, 1)), D("0.7"))
        self.assertEqual(rules.storage_price_for_age(61, bill_date=date(2026, 9, 1)), D("1.5"))
        self.assertEqual(rules.outbound_base_fee_for_avg_daily_orders(D("500")), D("8.8"))
        self.assertEqual(rules.outbound_extra_piece_fee_for_avg_daily_orders(D("2000")), D("0.8"))
        for daily, expected in (("2500", "1.4"), ("3001", "1.3"), ("5001", "1.2")):
            self.assertEqual(rules.outbound_base_fee_for_avg_daily_orders(D(daily)), D(expected))
        self.assertEqual(rules.outbound_extra_piece_fee_for_avg_daily_orders(D("2500")), D("0.5"))
        self.assertEqual(rules.outbound_additional_fee_for_quantity(20, avg_daily_orders=D("2000")), D("13.1"))
        self.assertEqual(rules.return_fee_for_quantity(3), D("2"))
        self.assertEqual(rules.return_fee_for_quantity(4), D("3"))
        self.assertEqual(rules.unloading_lcl_unit_price, D("27"))
        self.assertEqual(rules.misc_unit_rates["退仓费"], D("1.1"))
        self.assertEqual(rules.bubble_wrap_fee_for_code("ABF-FIXTURE"), D("0.25"))


class ConfirmedKecWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.service = kec.KecReconciliationService()
        self.rules = kec.KecQuoteRules()
        self.source = Workbook()
        self.output = Workbook()
        self.addCleanup(self.source.close)
        self.addCleanup(self.output.close)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("Offline workbook test attempted HTTP"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_storage_xlsx_uses_real_volume_and_keeps_inflated_bill_difference(self):
        sheet = self.source.active
        sheet.append(STORAGE_HEADERS)
        sheet.append(storage_record(bill_day=date(2026, 9, 1), age=61, billed_amount="3"))
        result = self.output.active
        self.service._write_storage_sheet(result, sheet, self.rules, avg_daily_orders=D("0"))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-result.xlsx"
            self.output.save(path)
            readback = load_workbook(path, data_only=True)
            try:
                stored = readback.active
                # 100*60*100 cm³ * 2 = 1.2 m³, at 1.5/day = 1.8, not 2*1.5=3.
                self.assertAlmostEqual(value(stored, 2, "核算基数(实际体积)"), 1.2)
                self.assertEqual(value(stored, 2, "账单 Total CBM"), 2)
                self.assertAlmostEqual(value(stored, 2, "核对金额"), 1.8)
                self.assertAlmostEqual(value(stored, 2, "金额差额"), 1.2)
                self.assertEqual(value(stored, 2, "核对结果"), "异常")
            finally:
                readback.close()

    def test_storage_writer_receives_daily_volume_and_missing_dimensions_stay_abnormal(self):
        sheet = self.source.active
        sheet.append(STORAGE_HEADERS)
        sheet.append(storage_record(bill_day=date(2026, 2, 1), age=61, billed_amount="2.4"))
        sheet.append(storage_record(bill_day=date(2026, 2, 1), age=61, billed_amount="0", length=None))
        result = self.output.active
        self.service._write_storage_sheet(result, sheet, self.rules, avg_daily_orders=D("3000"))
        self.assertAlmostEqual(value(result, 2, "核对金额"), 1.8)
        self.assertEqual(value(result, 3, "核对结果"), "异常")
        self.assertIsNone(value(result, 3, "核算基数(实际体积)"))

    def test_outbound_writer_uses_mabang_piece_count_and_preserves_packaging(self):
        sheet = self.source.active
        sheet.append([f"原字段-{i}" for i in range(29)])
        by_order = {}
        for index, (quantity, additional, total) in enumerate(((19, "8", "9.9"), (20, "8.3", "10.2"), (21, "8.6", "10.5"))):
            number, tracking = f"FIXTURE-ORDER-{index}", f"FIXTURE-TRACK-{index}"
            row = [None] * 29
            row[4:7] = ["2026-09-15 12:00:00", number, tracking]
            row[8] = 1 if quantity == 20 else quantity
            row[17], row[24], row[25], row[26], row[28] = "ABF1", "0.5", "1.4", additional, total
            sheet.append(row)
            by_order[number] = order(number, tracking, quantity)
        result = self.output.active
        self.service._write_outbound_sheet(result, sheet, self.rules, D("1.4"), by_order, {}, None, avg_daily_orders=D("2500"))
        for row_number, expected_extra, expected_total in ((2, 8, 9.9), (3, 8.3, 10.2), (4, 8.6, 10.5)):
            self.assertAlmostEqual(value(result, row_number, "核对续件费"), expected_extra)
            self.assertEqual(value(result, row_number, "核对包材费"), 0.5)
            self.assertAlmostEqual(value(result, row_number, "核对总金额"), expected_total)
        self.assertEqual(value(result, 3, "马帮商品总数量"), 20)
        self.assertNotEqual(value(result, 3, "核对结果"), "正常")

    def test_return_multi_sku_orders_are_charged_once_with_three_included_pieces(self):
        sheet = self.source.active
        headers = [f"原字段-{i}" for i in range(21)]
        for index, label in ((5, "ASN #"), (6, "PO Number"), (7, "Is Return"), (8, "SKU Code"), (10, "Actual QTY"), (20, "Amount")):
            headers[index] = label
        sheet.append(headers)
        lookups = {}
        for key, pieces, first_fee in (("THREE", (1, 2), "2"), ("FOUR", (1, 3), "3")):
            number, tracking = "FIXTURE-" + key, "FIXTURE-RETURN-" + key
            lookups[tracking] = SimpleNamespace(order=order(number, tracking, sum(pieces)), error_message="", has_return_hit=True, has_refund_hit=False)
            for index, quantity in enumerate(pieces):
                row = [None] * 21
                row[5], row[6], row[7], row[8], row[10], row[20] = tracking, number, "Y", f"SKU-{index}", quantity, first_fee if index == 0 else "0"
                sheet.append(row)
        _, group_quantities = self.service._read_return_identifiers(sheet)
        result = self.output.active
        stats = self.service._write_return_sheet(result, sheet, self.rules, group_quantities, lookups, None)
        self.assertEqual([value(result, row, "核对上架费") for row in range(2, 6)], [2, 0, 3, 0])
        self.assertEqual(stats["退件上架费_核对金额"], D("5"))

    def test_outbound_orders_with_unknown_dates_cannot_choose_a_daily_volume_tier(self):
        for invalid_date in (None, "", "not-a-date", "2026-02-30"):
            for invalid_first in (True, False):
                with self.subTest(invalid_date=invalid_date, invalid_first=invalid_first):
                    sheet = self.source.create_sheet()
                    sheet.append([f"原字段-{i}" for i in range(9)])
                    dates = [invalid_date, date(2026, 9, 15)]
                    if not invalid_first:
                        dates.reverse()
                    for index, handover_date in enumerate(dates):
                        row = [None] * 9
                        row[4:7] = [handover_date, f"FIXTURE-ORDER-{index}", f"FIXTURE-TRACK-{index}"]
                        sheet.append(row)
                    with self.assertRaises(ValueError):
                        self.service._read_outbound_identifiers(sheet)
        sheet = self.source.create_sheet()
        sheet.append([f"原字段-{i}" for i in range(9)])
        row = [None] * 9
        row[5:7] = ["FIXTURE-UNDATED", "FIXTURE-UNDATED-TRACK"]
        sheet.append(row)
        with self.assertRaises(ValueError):
            self.service._read_outbound_identifiers(sheet)

    def test_any_unknown_return_piece_count_keeps_the_entire_po_pending(self):
        for invalid_quantity in (None, "", "not-a-number", -1):
            for invalid_first in (True, False):
                with self.subTest(invalid_quantity=invalid_quantity, invalid_first=invalid_first):
                    sheet = self.source.create_sheet()
                    headers = [f"原字段-{i}" for i in range(21)]
                    for index, label in ((5, "ASN #"), (6, "PO Number"), (7, "Is Return"), (8, "SKU Code"), (10, "Actual QTY"), (20, "Amount")):
                        headers[index] = label
                    sheet.append(headers)
                    quantities = [invalid_quantity, 1]
                    if not invalid_first:
                        quantities.reverse()
                    for index, quantity in enumerate(quantities):
                        row = [None] * 21
                        row[5:9] = ["FIXTURE-RETURN-TRACK", "FIXTURE-RETURN-PO", "Y", f"SKU-{index}"]
                        row[10], row[20] = quantity, 0
                        sheet.append(row)
                    # A genuine zero in another PO remains a known quantity.
                    row = [None] * 21
                    row[5:9] = ["FIXTURE-ZERO-TRACK", "FIXTURE-ZERO-PO", "Y", "SKU-ZERO"]
                    row[10], row[20] = 0, 0
                    sheet.append(row)
                    _, quantities_by_po = self.service._read_return_identifiers(sheet)
                    self.assertIsNone(quantities_by_po["FIXTURE-RETURN-PO"])
                    self.assertEqual(quantities_by_po["FIXTURE-ZERO-PO"], D("0"))
                    lookups = {
                        tracking: SimpleNamespace(order=None, error_message="", has_return_hit=True, has_refund_hit=False)
                        for tracking in ("FIXTURE-RETURN-TRACK", "FIXTURE-ZERO-TRACK")
                    }
                    result = self.output.create_sheet()
                    stats = self.service._write_return_sheet(result, sheet, self.rules, quantities_by_po, lookups, None)
                    for row_number in (2, 3):
                        self.assertIsNone(value(result, row_number, "订单退货总数量"))
                        self.assertIsNone(value(result, row_number, "核对上架费"))
                        self.assertIsNone(value(result, row_number, "金额差额"))
                        self.assertEqual(value(result, row_number, "核对结果"), "待确认")
                    self.assertEqual(value(result, 4, "核对上架费"), 0)
                    self.assertEqual(value(result, 4, "核对结果"), "正常")
                    self.assertEqual(stats["退件上架费_待确认数"], 2)

    def test_a_non_return_flag_is_abnormal_even_when_lookup_and_amount_match(self):
        for return_flag in (None, "", "N"):
            with self.subTest(return_flag=return_flag):
                sheet = self.source.create_sheet()
                headers = [f"原字段-{i}" for i in range(21)]
                for index, label in ((5, "ASN #"), (6, "PO Number"), (7, "Is Return"), (8, "SKU Code"), (10, "Actual QTY"), (20, "Amount")):
                    headers[index] = label
                sheet.append(headers)
                row = [None] * 21
                row[5:9] = ["FIXTURE-FLAG-TRACK", "FIXTURE-FLAG-PO", return_flag, "SKU-FLAG"]
                row[10], row[20] = 3, 2
                sheet.append(row)
                _, quantities_by_po = self.service._read_return_identifiers(sheet)
                lookup = SimpleNamespace(order=None, error_message="", has_return_hit=True, has_refund_hit=False)
                result = self.output.create_sheet()
                stats = self.service._write_return_sheet(result, sheet, self.rules, quantities_by_po, {"FIXTURE-FLAG-TRACK": lookup}, None)
                self.assertEqual(value(result, 2, "核对上架费"), 2)
                self.assertEqual(value(result, 2, "金额差额"), 0)
                self.assertEqual(value(result, 2, "核对结果"), "异常")
                self.assertEqual(stats["退件上架费_异常数"], 1)

    def test_unloading_rounds_each_positive_cbm_up_and_return_to_warehouse_is_per_piece(self):
        unloading = self.source.active
        unloading.append(["日期", "批次", "说明", "方数", "备注", "卸货费"])
        for index, (cbm, billed) in enumerate((("0.2", "27"), ("1.2", "54"), ("2", "54"))):
            unloading.append(["", f"FIXTURE-BATCH-{index}", "", cbm, "", billed])
        result = self.output.active
        self.service._write_unloading_sheet(result, unloading, self.rules)
        self.assertEqual([value(result, row, "核对方数取整") for row in range(2, 5)], [1, 2, 2])
        self.assertEqual([value(result, row, "核对卸货费") for row in range(2, 5)], [27, 54, 54])
        misc = self.source.create_sheet("杂费")
        misc.append(["日期", "订单", "数量", "费用说明", "总费用"])
        misc.append(["", "FIXTURE-MISC", 3, "退仓费", "3.3"])
        misc.append(["", "FIXTURE-UNKNOWN", 1, "未约定服务", "8"])
        other = self.output.create_sheet("杂费核对")
        self.service._write_misc_sheet(other, misc, self.rules)
        self.assertAlmostEqual(value(other, 2, "核对费用"), 3.3)
        self.assertEqual(value(other, 3, "核对结果"), "待确认")

    def test_missing_or_zero_misc_quantity_cannot_silently_become_one_piece(self):
        misc = self.source.active
        misc.append(["日期", "订单", "数量", "费用说明", "总费用"])
        misc.append(["", "FIXTURE-MISSING", None, "退仓费", "1.1"])
        misc.append(["", "FIXTURE-ZERO", 0, "退仓费", "1.1"])
        result = self.output.active
        self.service._write_misc_sheet(result, misc, self.rules)
        self.assertNotEqual(value(result, 2, "核对结果"), "正常")
        self.assertNotEqual(value(result, 3, "核对结果"), "正常")


if __name__ == "__main__":
    unittest.main()

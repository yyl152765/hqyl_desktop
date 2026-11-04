"""Synthetic legacy/September quote layouts; no customer workbook is bundled."""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from backend.services.kec_reconciliation import KecQuoteRules


D = Decimal
ABF_RATES = ("0.5", "0.6", "0.9", "1.1", "1.6", "1.8", "2.7", "3.6", "4.3", "5", "6.4", "6.5")


def synthetic_quote(*, revised=True, labelled=True):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "合成仓库报价"
    if labelled:
        for column, label in (
            (2, "服务类型\nService Type"), (3, "费用项名称\nFee Name"),
            (4, "服务内容描述\nService Description"), (5, "日均单量"),
            (6, "报价分类\nClassification"), (7, "报价单位\nQuotation Unit"),
            (8, "报价币种\nCurrency"), (9, "报价金额\nRate"),
            (10, "续件费用/件\nFrom /pcs"),
            (11, "B2B续件费/件\n（订单超过20件）"), (12, "计费节点"),
        ):
            sheet.cell(4, column, label)
        if not revised:
            sheet.cell(4, 11, "计费节点\nBilling Triggering Timestamp")
            sheet.cell(4, 12).value = None
        sheet.cell(5, 3, "仓储费\nStorage fee")
        sheet.cell(5, 5, "日均单量<3000单")
    for row, age, rate in ((5, "≤30 天\n30 days", "免费\nfree"),
                           (6, "＞30天，≤120天\n>30 days - 120 days", 2),
                           (7, "＞120天，≤180天", 3.5), (8, "＞180天", 5.1)):
        sheet.cell(row, 9, rate)
        if labelled:
            sheet.cell(row, 6, age)
    first_outbound = 11 if revised else 9
    if revised:
        sheet.cell(9, 5, "日均单量3000单以上")
        sheet.cell(9, 6, "≤60 天\n60 days")
        sheet.cell(10, 6, ">60天")
        sheet.cell(9, 9, "免费\nfree")
        sheet.cell(10, 9, 1.5)
    tiers = (
        ("0-500单/天", 1.8, 0.6, 3), ("501-1000单/天", 1.7, 0.6, 3),
        ("1001-2000单/天", 1.5, 0.6, 3), ("2001-3000单/天", 1.4, 0.5, 4),
        ("3001-5000单/天", 1.3, 0.5, 5), ("5000单以上/天", 1.2, 0.5, 6),
    )
    for offset, (tier, rate, extra, included) in enumerate(tiers if revised else tiers[:3]):
        row = first_outbound + offset
        sheet.cell(row, 9, rate)
        if labelled:
            if offset == 0:
                sheet.cell(row, 3, "B2C订单操作费\nB2C order operation fee")
            sheet.cell(row, 5, tier)
            sheet.cell(row, 7, f"每订单（含{included}PCS)\nPer Order")
            sheet.cell(row, 10, extra)
            sheet.cell(row, 11, 0.3)
    if not labelled:
        sheet.cell(9, 10, 0.6)
    if revised:
        # These numeric prices occupy the old ABF range but are express bags.
        sheet.cell(17, 3, "2C退货处理费")
        for row in range(43, 48):
            sheet.cell(row, 9, 8 + row)
            sheet.cell(row, 6, "合成快递袋尺寸")
        first_abf, code_column = 48, 12
    else:
        first_abf, code_column = 43, 11
    for index, rate in enumerate(ABF_RATES):
        sheet.cell(first_abf + index, 9, float(rate))
        sheet.cell(first_abf + index, code_column, f"ABF{index + 1}")
    return workbook


class SeptemberKecQuoteTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        guard = patch("httpx.Client.send", side_effect=AssertionError("Quote parsing must remain offline"))
        guard.start()
        self.addCleanup(guard.stop)

    def parse(self, workbook, name="quote.xlsx"):
        path = self.directory / name
        workbook.save(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        rules = KecQuoteRules.from_workbook(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        return rules

    def workbook(self, **kwargs):
        workbook = synthetic_quote(**kwargs)
        self.addCleanup(workbook.close)
        return workbook

    def test_september_rows_and_moved_abf_codes_are_located_by_their_meaning(self):
        rules = self.parse(self.workbook())
        for daily, base in ((0, "1.8"), (501, "1.7"), (1001, "1.5"), (2500, "1.4"), (4000, "1.3"), (5001, "1.2")):
            self.assertEqual(rules.outbound_base_fee_for_avg_daily_orders(D(daily)), D(base))
        self.assertEqual(rules.storage_price_for_age(31, bill_date=date(2026, 2, 1)), D("2"))
        self.assertEqual(rules.storage_price_for_age(61, bill_date=date(2026, 9, 1)), D("1.5"))
        self.assertEqual(rules.bubble_wrap_rates, {f"ABF{i + 1}": D(rate) for i, rate in enumerate(ABF_RATES)})
        metadata = rules.quote_metadata
        self.assertEqual(metadata["layout"], "labelled_quote")
        self.assertEqual(metadata["header_row"], 4)
        self.assertEqual(metadata["sources"]["outbound_fee_0_500"], "I11")
        self.assertEqual(metadata["sources"]["outbound_fee_501_1000"], "I12")
        self.assertEqual(metadata["sources"]["outbound_extra_piece_fee"], ["J11", "J12", "J13"])
        self.assertEqual(metadata["sources"]["bubble_wrap_rates.ABF1"], {"code": "L48", "rate": "I48"})
        self.assertEqual(metadata["sources"]["bubble_wrap_rates.ABF12"], {"code": "L59", "rate": "I59"})

    def test_title_rows_and_inserted_columns_do_not_change_the_price_mapping(self):
        workbook = self.workbook()
        workbook.active.insert_rows(1, 3)
        workbook.active.insert_cols(1, 2)
        rules = self.parse(workbook)
        self.assertEqual(rules.outbound_fee_0_500, D("1.8"))
        self.assertEqual(rules.quote_metadata["header_row"], 7)
        self.assertEqual(rules.quote_metadata["sources"]["outbound_fee_0_500"], "K14")
        self.assertEqual(rules.quote_metadata["sources"]["bubble_wrap_rates.ABF1"], {"code": "N51", "rate": "K51"})

    def test_both_labelled_legacy_and_bare_legacy_grids_remain_supported(self):
        for labelled in (True, False):
            with self.subTest(labelled=labelled):
                rules = self.parse(self.workbook(revised=False, labelled=labelled), f"old-{labelled}.xlsx")
                self.assertEqual(rules.outbound_fee_0_500, D("1.8"))
                self.assertEqual(rules.outbound_extra_piece_fee, D("0.6"))
                self.assertEqual(rules.bubble_wrap_rates["ABF12"], D("6.5"))
                self.assertEqual(rules.quote_metadata["sources"]["bubble_wrap_rates.ABF1"], {"code": "K43", "rate": "I43"})

    def test_quote_included_four_five_six_pieces_never_changes_confirmed_three(self):
        workbook = self.workbook()
        for row in (14, 15, 16):
            workbook.active.cell(row, 9, 99)
            workbook.active.cell(row, 10, 88)
            workbook.active.cell(row, 11, 77)
        workbook.active.cell(10, 9, 66)
        rules = self.parse(workbook)
        self.assertEqual(rules.outbound_included_pieces, 3)
        for daily, base in ((2500, "1.4"), (4000, "1.3"), (5001, "1.2")):
            self.assertEqual(rules.outbound_base_fee_for_avg_daily_orders(D(daily)), D(base))
            self.assertEqual(rules.outbound_additional_fee_for_quantity(4, avg_daily_orders=D(daily)), D("0.5"))
            self.assertEqual(rules.outbound_additional_fee_for_quantity(20, avg_daily_orders=D(daily)), D("8.3"))
        self.assertEqual(rules.storage_price_for_age(61, bill_date=date(2026, 9, 1)), D("1.5"))
        conflicts = rules.quote_metadata["included_piece_conflicts"]
        self.assertEqual([(entry["coordinate"], entry["quoted_pieces"], entry["applied_pieces"]) for entry in conflicts],
                         [("G14", 4, 3), ("G15", 5, 3), ("G16", 6, 3)])
        observed = {entry["coordinate"]: entry for entry in rules.quote_metadata["contract_observations"]}
        self.assertEqual(observed["I14"]["quoted_value"], "99")
        self.assertEqual(observed["I14"]["applied_value"], "1.4")
        self.assertEqual(observed["K16"]["applied_value"], "0.3")

    def test_low_tier_quote_prices_still_override_unmodified_contract_fields(self):
        workbook = self.workbook()
        for row, rate in ((6, 0.7), (11, 1.9), (12, 1.8), (13, 1.6)):
            workbook.active.cell(row, 9, rate)
        for row in (11, 12, 13):
            workbook.active.cell(row, 10, 0.8)
        rules = self.parse(workbook)
        self.assertEqual(rules.storage_price_for_age(31, bill_date=date(2026, 2, 1)), D("0.7"))
        self.assertEqual(rules.outbound_base_fee_for_avg_daily_orders(D("500")), D("1.9"))
        self.assertEqual(rules.outbound_additional_fee_for_quantity(20, avg_daily_orders=D("2000")), D("13.1"))

    def test_missing_or_duplicate_outbound_tiers_cannot_fall_back_to_old_coordinates(self):
        for label in (None, "0-500单/天"):
            with self.subTest(label=label):
                workbook = self.workbook()
                workbook.active.cell(12, 5).value = label
                with self.assertRaisesRegex(ValueError, "分档"):
                    self.parse(workbook)

    def test_partial_headers_cannot_fall_back_to_a_plausible_numeric_grid(self):
        workbook = self.workbook()
        workbook.active.cell(4, 9).value = None
        with self.assertRaisesRegex(ValueError, "表头不完整"):
            self.parse(workbook)

    def test_explicit_free_is_allowed_but_non_numeric_prices_are_not_inferred(self):
        for invalid in ("免费30天之后另收2元", "1.8元/单", "实报实销+7%服务费", -1, "NaN", "Infinity", "=1+1", None):
            with self.subTest(invalid=invalid):
                workbook = self.workbook()
                workbook.active.cell(11, 9).value = invalid
                with self.assertRaisesRegex(ValueError, "有效费率"):
                    self.parse(workbook)

    def test_low_tier_extra_rates_must_be_complete_and_consistent(self):
        for invalid in (None, 0.7):
            with self.subTest(invalid=invalid):
                workbook = self.workbook()
                workbook.active.cell(12, 10).value = invalid
                with self.assertRaises(ValueError):
                    self.parse(workbook)

    def test_abf_rates_require_unique_complete_codes_and_same_row_numeric_prices(self):
        for column, invalid in ((12, None), (12, "ABF1"), (12, "ABF-UNKNOWN"), (9, None), (9, "另议3元")):
            with self.subTest(column=column, invalid=invalid):
                workbook = self.workbook()
                workbook.active.cell(52, column).value = invalid
                with self.assertRaisesRegex(ValueError, "气泡膜"):
                    self.parse(workbook)

    def test_abf_mentions_outside_the_code_column_cannot_supply_a_missing_price_tier(self):
        workbook = self.workbook()
        sheet = workbook.active
        sheet.cell(59, 12).value = None
        sheet.cell(43, 4, "ABF12")  # A bag's description must not become a rate code.
        with self.assertRaisesRegex(ValueError, "气泡膜"):
            self.parse(workbook)
        sheet.cell(59, 12, "ABF12")
        sheet.cell(48, 4, "ABF1")  # A repeated description is harmless.
        rules = self.parse(workbook)
        self.assertEqual(rules.bubble_wrap_rates["ABF12"], D("6.5"))
        self.assertEqual(rules.quote_metadata["sources"]["bubble_wrap_rates.ABF12"]["rate"], "I59")

    def test_revised_outbound_tiers_require_the_revised_storage_rows_as_well(self):
        workbook = self.workbook()
        for row in (9, 10):
            workbook.active.cell(row, 6).value = None
        with self.assertRaisesRegex(ValueError, "新版报价表缺少完整"):
            self.parse(workbook)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from backend.services.sample_registration import (
    DINGTALK_RANGE_CELL_LIMIT,
    _column_name,
    _read_existing_dingtalk_keys,
)


class DingTalkExistingRangeTests(unittest.TestCase):
    def test_column_name(self) -> None:
        self.assertEqual(_column_name(0), "A")
        self.assertEqual(_column_name(25), "Z")
        self.assertEqual(_column_name(26), "AA")

    def test_existing_keys_are_read_in_30000_cell_batches(self) -> None:
        calls: list[str] = []

        def reader(token, union_id, doc_id, sheet_id, cell_range, **kwargs):
            calls.append(cell_range)
            if cell_range == "A1:Z1":
                return [["寄样日期", "订单号", "店铺", "SKU", "中文名"]]
            if cell_range == "B2:D10001":
                return [["ORD-1", "店铺A", "SKU-1"]]
            if cell_range == "B10002:D12000":
                return [["ORD-2", "店铺B", "SKU-2"]]
            return []

        keys = _read_existing_dingtalk_keys(
            reader,
            "token",
            "union",
            "doc",
            "sheet",
            {"lastNonEmptyRow": 12000},
            app_key="app-key",
            app_secret="app-secret",
        )

        self.assertEqual(keys, {"ORD-1||SKU-1", "ORD-2||SKU-2"})
        self.assertEqual(calls, ["A1:Z1", "B2:D10001", "B10002:D12000"])
        for cell_range in calls[1:]:
            start, end = cell_range.split(":")
            start_col = "".join(ch for ch in start if ch.isalpha())
            end_col = "".join(ch for ch in end if ch.isalpha())
            start_row = int("".join(ch for ch in start if ch.isdigit()))
            end_row = int("".join(ch for ch in end if ch.isdigit()))
            width = ord(end_col) - ord(start_col) + 1
            self.assertLessEqual(width * (end_row - start_row + 1), DINGTALK_RANGE_CELL_LIMIT)

    def test_missing_header_returns_no_keys(self) -> None:
        calls: list[str] = []

        def reader(token, union_id, doc_id, sheet_id, cell_range, **kwargs):
            calls.append(cell_range)
            return [["寄样日期", "店铺"]]

        keys = _read_existing_dingtalk_keys(
            reader,
            "token",
            "union",
            "doc",
            "sheet",
            {"lastNonEmptyRow": 12000},
            app_key="app-key",
            app_secret="app-secret",
        )

        self.assertEqual(keys, set())
        self.assertEqual(calls, ["A1:Z1"])


if __name__ == "__main__":
    unittest.main()

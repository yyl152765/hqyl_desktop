from __future__ import annotations

import unittest
from pathlib import Path

from backend.services.purchase_log import (
    OUTPUT_COLUMNS,
    parse_datetime,
    parse_purchase_log_rows,
    parse_sku_text,
    validate_query_payload,
)


class PurchaseLogServiceTests(unittest.TestCase):
    def test_parse_sku_text_deduplicates_lines(self) -> None:
        self.assertEqual(parse_sku_text("A\n\nB\nA\n C "), ("A", "B", "C"))

    def test_parse_datetime_accepts_common_formats(self) -> None:
        self.assertEqual(parse_datetime("2026/06/18 09:30").strftime("%Y-%m-%d %H:%M"), "2026-06-18 09:30")

    def test_parse_purchase_log_rows_maps_columns(self) -> None:
        html = "<tr>" + "".join(f"<td>{column}</td>" for column in OUTPUT_COLUMNS) + "</tr>"
        rows = parse_purchase_log_rows(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["采购单号"], "采购单号")

    def test_validate_query_payload_requires_sku(self) -> None:
        with self.assertRaises(ValueError):
            validate_query_payload(
                {
                    "username": "u",
                    "password": "p",
                    "sku_text": "",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-18",
                    "output_dir": str(Path.cwd()),
                }
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl.worksheet._writer import ALL_TEMP_FILES

from tests import test_lz_warehouse_inventory as fixtures


class LzWarehouseInventoryFailureIsolationReviewTests(unittest.TestCase):
    def test_valid_legacy_excel_with_unwritable_xml_value_keeps_other_warehouse_results(self) -> None:
        fixture = fixtures.InventoryHttpFixture(0, {}, scopes={
            fixtures.WAREHOUSES[0][0]: (1, {1: fixtures._xlsx_bytes([
                ("04-1", "商品1", fixtures.WAREHOUSES[0][1], 0, "字段"),
            ])}),
            fixtures.WAREHOUSES[1][0]: (1, {1: fixtures._xls_bytes([
                ("05-1", "商品\x01名", fixtures.WAREHOUSES[1][1], 0, "字段"),
            ])}),
            fixtures.WAREHOUSES[2][0]: (1, {1: fixtures._xlsx_bytes([
                ("06-1", "商品3", fixtures.WAREHOUSES[2][1], 0, "字段"),
            ])}),
        })
        selected = tuple(name for _, name in fixtures.WAREHOUSES)
        temporary_xml_before = set(ALL_TEMP_FILES)
        with tempfile.TemporaryDirectory() as directory:
            result = fixtures.LzWarehouseInventoryHttpTests()._run(fixture, Path(directory), selected)
            self.assertFalse(result.is_complete)
            self.assertEqual([item.warehouse_name for item in result.exports], [selected[0], selected[2]])
            self.assertEqual([item["warehouse_name"] for item in result.failures], [selected[1]])
            self.assertNotIn("\x01", result.failures[0]["error"])
            self.assertNotIn("商品", result.failures[0]["error"])
            self.assertEqual(set(Path(directory).glob("*.xlsx")), set(result.output_files))
            self.assertEqual(len(result.output_files), 2)
            self.assertFalse(list(Path(directory).glob("*.tmp")))
        self.assertFalse(set(ALL_TEMP_FILES) - temporary_xml_before)


if __name__ == "__main__":
    unittest.main()

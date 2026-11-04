from __future__ import annotations

import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree

from openpyxl import load_workbook
from PIL import Image

from backend.services.balance_workbook import write_balance_workbook


class BalanceWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "proof.png"
        Image.new("RGB", (1600, 900), "white").save(self.image)

    def row(self, **changes):
        return {"store_name": "=HYPERLINK(\"https://example.invalid\")", "country": "TH", "currency": "THB",
                "status": "success", "captured_at": "2026-09-28T15:00:00+08:00", "notes": [], "field_errors": {},
                "values": {"total": "0.00", "pending": "-123.45", "income": "1.25", "balance": "0.00", "ads": "-2", "processing": None},
                "evidence": {field: str(self.image) for field in ("total", "pending", "income", "balance", "ads", "processing")},
                "evidence_exemptions": {"processing": "withdrawal_success"}, **changes}

    def test_temu_layout_money_images_and_literal_store_names(self):
        target = self.root / "temu.xlsx"
        write_balance_workbook("temu", [self.row()], target, month="2026-08")
        book = load_workbook(target)
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual([cell.value for cell in sheet[1]][:5], ["平台", "店铺", "账户总金额", "预估待结算销售额", "截图"])
        self.assertEqual(sheet["A2"].value, "temu")
        self.assertEqual(sheet["B2"].data_type, "s")
        self.assertEqual(sheet["C2"].value, 0)
        self.assertEqual(sheet["D2"].value, -123.45)
        self.assertEqual(sheet["K2"].value, "2026-08")
        self.assertIn(self.row()["store_name"], sheet["E2"].value)
        self.assertIn(self.row()["captured_at"], sheet["F2"].value)
        self.assertIn("非截图原文", sheet["E2"].value)
        self.assertEqual(sheet["E2"].data_type, "s")
        self.assertEqual(len(sheet._images), 2)
        self.assertEqual([image.anchor._from.col for image in sheet._images], [4, 5])
        for image in sheet._images:
            self.assertAlmostEqual(image.anchor.ext.cx / image.anchor.ext.cy, 1600 / 900)
            self.assertGreater(image.anchor._from.rowOff, 0)
        self.assertTrue(self.image.exists())
        with ZipFile(target) as archive:
            media = [name for name in archive.namelist() if name.startswith("xl/media/")]
            self.assertEqual(len(media), 2)
            for filename in media:
                self.assertEqual(archive.read(filename), self.image.read_bytes())

    def test_lazada_sheets_blank_zero_negative_and_no_cross_currency_totals(self):
        target = self.root / "lazada.xlsx"
        rows = [self.row(), self.row(store_name="另一个站点", country="PH", currency="PHP", values={"income": "12", "balance": "0", "ads": "0", "processing": "-8.1"}, evidence={field: str(self.image) for field in ("income", "balance", "ads", "processing")})]
        write_balance_workbook("lazada", rows, target)
        book = load_workbook(target)
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames, ["余额统计"])
        sheet = book.active
        self.assertEqual([cell.value for cell in sheet[1]], ["店铺名称", "Income", "Income截图", "Balance", "Balance截图", "Ads", "Ads截图", "processing", "processing截图", "站点", "币种", "状态", "采集时间", "说明"])
        self.assertEqual(sheet.max_row, 3)
        self.assertEqual(sheet["B2"].value, 1.25)
        self.assertEqual(sheet["D2"].value, 0)
        self.assertEqual(sheet["F2"].value, -2)
        self.assertIsNone(sheet["H2"].value)
        self.assertEqual(sheet["H3"].value, -8.1)
        self.assertEqual(sheet["K2"].value, "THB")
        self.assertEqual(sheet["K3"].value, "PHP")
        self.assertEqual(sheet["L2"].value, "成功")
        self.assertEqual(sheet["A2"].data_type, "s")
        self.assertEqual(len(sheet._images), 8)
        self.assertEqual([(image.anchor._from.row, image.anchor._from.col) for image in sheet._images], [(1, 2), (1, 4), (1, 6), (1, 8), (2, 2), (2, 4), (2, 6), (2, 8)])
        self.assertEqual(sheet.auto_filter.ref, "A1:N3")
        self.assertIsNone(sheet["I3"].value)
        self.assertIsNone(sheet["I2"].value)

    def test_lazada_images_are_embedded_and_survive_moving_workbook_without_pngs(self):
        original_bytes = self.image.read_bytes()
        target = self.root / "lazada.xlsx"
        write_balance_workbook("lazada", [self.row()], target)
        moved = self.root / "only_workbook" / "shared.xlsx"
        moved.parent.mkdir()
        shutil.copyfile(target, moved)
        self.image.unlink()
        target.unlink()
        with ZipFile(moved) as archive:
            media = [name for name in archive.namelist() if name.startswith("xl/media/")]
            self.assertEqual(len(media), 4, "Each metric image is embedded once, not duplicated on other sheets")
            for filename in media:
                self.assertEqual(archive.read(filename), original_bytes)
            drawing = ElementTree.fromstring(archive.read("xl/drawings/drawing1.xml"))
            ns = {"d": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"}
            anchors = drawing.findall("d:oneCellAnchor/d:from", ns)
            self.assertEqual([(int(a.find("d:col", ns).text), int(a.find("d:row", ns).text)) for a in anchors], [(2, 1), (4, 1), (6, 1), (8, 1)])
            for name in archive.namelist():
                if name.startswith("xl/drawings/") and name.endswith(".rels"):
                    self.assertNotIn(b'TargetMode="External"', archive.read(name))
        book = load_workbook(moved)
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual(len(sheet._images), 4)
        self.assertLessEqual(sheet.row_dimensions[2].height, 156)
        for column in ("C", "E", "G", "I"):
            self.assertEqual(sheet.column_dimensions[column].width, 47)
            self.assertIsNone(sheet[f"{column}2"].value)
            self.assertIsNone(sheet[f"{column}2"].hyperlink)
        for image in sheet._images:
            self.assertAlmostEqual(image.anchor.ext.cx / image.anchor.ext.cy, 1600 / 900)
            self.assertLessEqual(image.anchor.ext.cx, 320 * 9525)
            self.assertLessEqual(image.anchor.ext.cy, 200 * 9525)

    def test_lazada_missing_evidence_is_explicit_and_valid_exemption_is_retained(self):
        target = self.root / "partial_lazada.xlsx"
        unavailable = self.row(evidence={"income": str(self.image), "ads": str(self.image), "processing": str(self.image)},
                               evidence_exemptions={"balance": "cross_border_unavailable", "processing": "no_withdrawal"})
        failed = self.row(status="failed", values={}, evidence={}, field_errors={"income": "=缺少页面截图"})
        write_balance_workbook("lazada", [unavailable, failed], target)
        book = load_workbook(target)
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual(sheet["D2"].value, 0)
        self.assertIsNone(sheet["E2"].value)
        self.assertIn("跨境店余额不可用", sheet["N2"].value)
        self.assertIn("无提现记录", sheet["N2"].value)
        self.assertEqual(sheet["L2"].value, "成功")
        self.assertEqual(sheet["L3"].value, "失败")
        self.assertIsNone(sheet["B3"].value)
        self.assertIsNone(sheet["C3"].value)
        self.assertIn("缺少页面截图", sheet["N3"].value)
        self.assertEqual(sheet["N3"].data_type, "s")
        self.assertEqual(len(sheet._images), 3)
        self.assertIn((1, 8), [(image.anchor._from.row, image.anchor._from.col) for image in sheet._images])

    def test_lazada_blank_processing_requires_status_image_for_success(self):
        for reason in ("withdrawal_success", "no_withdrawal"):
            with self.subTest(reason=reason):
                target = self.root / f"processing_{reason}.xlsx"
                store = self.row(evidence_exemptions={"processing": reason})
                write_balance_workbook("lazada", [store], target)
                book = load_workbook(target)
                try:
                    self.assertEqual(book.sheetnames, ["余额统计"])
                    sheet = book.active
                    self.assertIsNone(sheet["H2"].value)
                    self.assertIsNone(sheet["I2"].value)
                    self.assertEqual(sheet["L2"].value, "成功")
                    self.assertEqual([image.anchor._from.col for image in sheet._images], [2, 4, 6, 8])
                finally:
                    book.close()
                previous = target.read_bytes()
                del store["evidence"]["processing"]
                with self.assertRaisesRegex(ValueError, "processing 缺少截图"):
                    write_balance_workbook("lazada", [store], target)
                self.assertEqual(target.read_bytes(), previous)

    def test_lazada_missing_or_corrupt_image_never_replaces_previous_report(self):
        target = self.root / "keep_lazada.xlsx"
        target.write_bytes(b"existing workbook")
        bad = self.root / "corrupt.png"
        bad.write_bytes(b"not an image")
        for evidence in ({}, {"income": str(self.root / "missing.png")}, {"income": str(bad)}):
            with self.subTest(evidence=evidence):
                with self.assertRaises((ValueError, OSError)):
                    write_balance_workbook("lazada", [self.row(evidence=evidence)], target)
                self.assertEqual(target.read_bytes(), b"existing workbook")
                self.assertEqual(list(self.root.glob(".*.xlsx")), [])

    def test_failed_store_blank_amounts_errors_and_precision_retained(self):
        target = self.root / "partial.xlsx"
        write_balance_workbook("temu", [self.row(status="failed", values={}, evidence={}, message="=危险公式"), self.row(values={"total": "1234567890123456.78", "pending": "0"})], target)
        book = load_workbook(target)
        self.addCleanup(book.close)
        sheet = book.active
        self.assertIsNone(sheet["C2"].value)
        self.assertEqual(sheet["L2"].data_type, "s")
        self.assertEqual(sheet["I2"].value, "失败")
        self.assertEqual(sheet["C3"].value, "1234567890123456.78")
        self.assertEqual(sheet["C3"].data_type, "s")

    def test_atomic_export_failure_keeps_previous_workbook(self):
        target = self.root / "keep.xlsx"
        target.write_bytes(b"existing workbook")
        with patch("backend.services.balance_workbook.Workbook.save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_balance_workbook("temu", [self.row()], target)
        self.assertEqual(target.read_bytes(), b"existing workbook")
        self.assertEqual(list(self.root.glob(".*.xlsx")), [])


if __name__ == "__main__":
    unittest.main()

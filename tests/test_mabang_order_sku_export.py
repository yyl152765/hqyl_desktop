from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook

from backend.services import mabang_order_sku_export as service


def write_xlsx(path: Path, rows: list[list[object]], sheet_name: str = "订单号") -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def make_order(order_id: str, order_no: str, **overrides: object) -> SimpleNamespace:
    raw = {
        "id": order_id,
        "platformOrderId": order_no,
        "shopIdText": "示例店铺",
        "platformIdText": "Shopee",
        "orderStatus": "4",
        "paidTime": "2026-11-05 10:00",
        "createDate": "2026-11-05 09:59",
        "expressTime": "--",
        "buyerName": "买家",
        "orderWeight": 210,
        "order_status_image_html": '<span class="order-pendingState">配货中</span>',
    }
    raw.update(overrides)
    return SimpleNamespace(order_id=order_id, platform_order_id=order_no, raw=raw, table_base="2", order_by_item_for_ary="id asc")


def make_item(order_id: str, order_no: str, sku: str, quantity: str = "1", price: str = "10", name: str = "示例商品") -> SimpleNamespace:
    return SimpleNamespace(
        order_id=order_id,
        platform_order_id=order_no,
        sku=sku,
        product_name=name,
        quantity=quantity,
        sell_price=price,
        shop_name="示例店铺",
        pay_time="2026-11-05 10:00",
        create_time="2026-11-05 09:59",
        buyer_name="买家",
        store_manager="店长A",
        weight="210",
    )


class OrderNumberParsingTests(unittest.TestCase):
    def test_xlsx_header_dedup_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "订单编号.xlsx"
            write_xlsx(
                path,
                [
                    ["订单编号"],
                    ["261105GEBJ0W71"],
                    ["261105GE99KNYV"],
                    [""],
                    ["261105GEBJ0W71"],
                    ["重复标题"],
                    ["261105GE97NM37 "],
                ],
            )
            preview = service.parse_order_number_file(path)
            self.assertEqual(
                preview.numbers,
                ("261105GEBJ0W71", "261105GE99KNYV", "261105GE97NM37"),
            )
            self.assertTrue(preview.header_detected)
            self.assertEqual(preview.column_name, "订单编号")
            self.assertEqual(preview.duplicate_count, 1)
            self.assertEqual(preview.invalid_count, 1)

    def test_csv_without_header_and_multiple_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "订单编号.csv"
            path.write_text("261105GEBJ0W71,261105GE99KNYV\n261105GE97NM37；261105GEBJ0W71\n", encoding="utf-8")
            preview = service.parse_order_number_file(path)
            self.assertEqual(
                preview.numbers,
                ("261105GEBJ0W71", "261105GE99KNYV", "261105GE97NM37"),
            )
            self.assertFalse(preview.header_detected)
            self.assertEqual(preview.duplicate_count, 1)

    def test_second_column_is_used_when_first_column_is_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "订单编号.xlsx"
            write_xlsx(path, [["序号", "订单号"], [1, "261105GEBJ0W71"], [2, "261105GE99KNYV"]])
            preview = service.parse_order_number_file(path)
            self.assertEqual(preview.numbers, ("261105GEBJ0W71", "261105GE99KNYV"))
            self.assertEqual(preview.column_name, "订单号")


class PayloadValidationTests(unittest.TestCase):
    def base_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "username": "demo",
            "password": "secret",
            "order_text": "261105GEBJ0W71\n261105GE99KNYV\n261105GEBJ0W71",
            "output_dir": "C:/Preview",
            "fields": ["order_no", "product_name"],
        }
        payload.update(overrides)
        return payload

    def test_sku_field_is_forced(self) -> None:
        job = service.validate_order_sku_export_payload(self.base_payload())
        self.assertEqual(job.order_numbers, ("261105GEBJ0W71", "261105GE99KNYV"))
        self.assertIn("sku", job.fields)

    def test_unknown_fields_are_ignored(self) -> None:
        job = service.validate_order_sku_export_payload(self.base_payload(fields=["order_no", "not_a_field"]))
        self.assertEqual(job.fields, ("sku", "order_no"))

    def test_missing_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            service.validate_order_sku_export_payload(self.base_payload(order_text=""))

    def test_missing_output_dir_raises(self) -> None:
        with self.assertRaises(ValueError):
            service.validate_order_sku_export_payload(self.base_payload(output_dir=""))


class AggregationTests(unittest.TestCase):
    def test_same_order_same_sku_is_merged(self) -> None:
        orders = [make_order("1", "ORDER-A")]
        items_map = {
            "1": [
                make_item("1", "ORDER-A", "SKU-1", quantity="2", price="5"),
                make_item("1", "ORDER-A", "SKU-1", quantity="3", price="5"),
            ]
        }
        rows = service.aggregate_detail_rows(orders, items_map)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 5)
        self.assertAlmostEqual(rows[0].amount, 25.0)
        self.assertEqual(rows[0].order_status, "配货中")

    def test_unique_sku_across_orders(self) -> None:
        orders = [make_order("1", "ORDER-A"), make_order("2", "ORDER-B")]
        items_map = {
            "1": [make_item("1", "ORDER-A", "SKU-1", quantity="1", price="5")],
            "2": [make_item("2", "ORDER-B", "SKU-1", quantity="2", price="5")],
        }
        rows = service.aggregate_detail_rows(orders, items_map)
        unique_rows = service.build_unique_rows(rows)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(unique_rows), 1)
        self.assertEqual(unique_rows[0].sku, "SKU-1")
        self.assertEqual(unique_rows[0].order_count, 2)
        self.assertEqual(unique_rows[0].total_quantity, 3)

    def test_blank_sku_is_skipped(self) -> None:
        orders = [make_order("1", "ORDER-A")]
        items_map = {"1": [make_item("1", "ORDER-A", ""), make_item("1", "ORDER-A", "SKU-2")]}
        rows = service.aggregate_detail_rows(orders, items_map)
        self.assertEqual([row.sku for row in rows], ["SKU-2"])


class WorkbookTests(unittest.TestCase):
    def test_workbook_sheets_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "马帮订单SKU汇总_test.xlsx"
            rows = [
                service.OrderSkuRow(order_no="ORDER-A", sku="SKU-1", product_name="商品一", quantity=2, unit_price=5, amount=10),
                service.OrderSkuRow(order_no="ORDER-B", sku="SKU-1", product_name="商品一", quantity=1, unit_price=5, amount=5),
                service.OrderSkuRow(order_no="ORDER-B", sku="SKU-2", product_name="商品二", quantity=1, unit_price=8, amount=8),
            ]
            unique_rows = service.build_unique_rows(rows)
            job = service.OrderSkuExportJob(
                username="demo",
                password="secret",
                order_numbers=("ORDER-A", "ORDER-B", "ORDER-C"),
                output_dir=Path(temp_dir),
            )
            service.build_workbook(
                rows=rows,
                unique_rows=unique_rows,
                not_found=["ORDER-C"],
                job=job,
                output_file=output_file,
            )

            workbook = load_workbook(output_file)
            self.assertEqual(workbook.sheetnames, ["唯一SKU", "订单明细", "未查询到订单"])

            sku_sheet = workbook["唯一SKU"]
            self.assertEqual([cell.value for cell in sku_sheet[1]][:4], ["SKU", "商品中文名称", "出现订单数", "商品总数量"])
            self.assertEqual(sku_sheet.max_row, 3)
            self.assertEqual(sku_sheet["A2"].value, "SKU-1")
            self.assertEqual(sku_sheet["C2"].value, 2)
            self.assertEqual(sku_sheet["D2"].value, "3")

            detail_headers = [cell.value for cell in workbook["订单明细"][1]]
            self.assertEqual(detail_headers, [service.FIELD_LABELS[key] for key in service.DEFAULT_FIELDS])
            self.assertEqual(workbook["未查询到订单"]["A2"].value, "ORDER-C")

    def test_detail_sheet_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "only_unique.xlsx"
            job = service.OrderSkuExportJob(
                username="demo",
                password="secret",
                order_numbers=("ORDER-A",),
                output_dir=Path(temp_dir),
                include_detail=False,
            )
            rows = [service.OrderSkuRow(order_no="ORDER-A", sku="SKU-1", quantity=1)]
            service.build_workbook(
                rows=rows,
                unique_rows=service.build_unique_rows(rows),
                not_found=[],
                job=job,
                output_file=output_file,
            )
            workbook = load_workbook(output_file)
            self.assertEqual(workbook.sheetnames, ["唯一SKU", "未查询到订单"])
            self.assertEqual(workbook["唯一SKU"].max_column, 4)


if __name__ == "__main__":
    unittest.main()

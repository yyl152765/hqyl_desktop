from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import openpyxl

from backend.app_bridge import AccountRequiredError, AppBridge
from backend.core.mabang_client import MabangApiError, MabangExportFile
from backend.services.group_sales_report import TARGET_SALES_HEADERS
from backend.services.warehouse_sales_export import (
    MonthExport,
    WarehouseSalesExportJob,
    build_report_form,
    build_workbook,
    count_detail_rows,
    default_month_range,
    export_intersection_csv,
    export_month,
    month_bounds,
    month_sequence,
    parse_report_options,
    require_full_sales_columns,
    validate_export_payload,
)
from tests.sales_report_fixtures import (
    ClassicClient,
    sales_csv,
    sales_row,
)

OPTIONS_HTML = """
<html><body>
  <label><input type="checkbox" name="shopLabelIds[]" value="1042621"><span>泰国TK</span></label>
  <label><input type="checkbox" name="shopLabelIds[]" value="1038954"><span>lazada泰国</span></label>
  <label><input type="checkbox" name="shopLabelIds[]" value="1042621"><span>重复分类</span></label>
  <label><input type="checkbox" name="shopLabelIds[]" value=""><span>全部</span></label>
  <label><input type="checkbox" name="stockWarehouseId[]" value="330978"><span>泰国新仓TH01</span></label>
  <label><input type="checkbox" name="stockWarehouseId[]" value="1101002"><span>泰国TH05仓</span></label>
</body></html>
"""

QUANTITY_INDEX = TARGET_SALES_HEADERS.index("销售数量")
ORDER_INCOME_INDEX = TARGET_SALES_HEADERS.index("收入-订单金额")


def _sales_row(sku: str, quantity: str = "10", income: str = "100.00") -> list[str]:
    values = ["0"] * len(TARGET_SALES_HEADERS)
    values[0] = sku
    values[1] = f"商品{sku}"
    values[QUANTITY_INDEX] = quantity
    values[ORDER_INCOME_INDEX] = income
    return values


def _csv_content(rows: int) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(TARGET_SALES_HEADERS)
    for index in range(rows):
        writer.writerow(_sales_row(f"hqsT{index:05d}"))
    writer.writerow(["合计"] + [""] * (len(TARGET_SALES_HEADERS) - 1))
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


class FakeClient(ClassicClient):
    """Constructed export client:每页最多返回 ``page_cap`` 行，模拟马帮截断。"""

    def __init__(self, *, page_cap: int, total: int) -> None:
        super().__init__([sales_row(f"hqsT{i:05d}", quantity="10", income="100.00", 毛利="0") for i in range(total)])
        self.page_cap = page_cap
        self.total = total
        self.export_calls = 0
        self.total_queries = 0

    def download_product_sales_report_csv(self, **_kwargs) -> MabangExportFile:
        self.export_calls += 1
        return MabangExportFile(
            file_name="stockSale.csv",
            file_type="csv",
            file_url="https://example.invalid/stockSale.csv",
            content=_csv_content(self.page_cap),
        )

    def post_form_json(self, *_args, **_kwargs) -> dict:
        self.total_queries += 1
        return {"success": True, "pageHtml": f"<div>共 {self.total} 条</div>"}


class ParseOptionTests(unittest.TestCase):
    def test_parses_categories_and_warehouses_without_duplicates(self) -> None:
        categories, warehouses = parse_report_options(OPTIONS_HTML)

        self.assertEqual([item.id for item in categories], ["1042621", "1038954"])
        self.assertEqual([item.name for item in categories], ["泰国TK", "lazada泰国"])
        self.assertEqual([item.id for item in warehouses], ["330978", "1101002"])
        self.assertEqual(warehouses[0].name, "泰国新仓TH01")

    def test_raises_when_page_has_no_filter_controls(self) -> None:
        with self.assertRaises(MabangApiError):
            parse_report_options("<html><body>登录页</body></html>")


class MonthSequenceTests(unittest.TestCase):
    def test_expands_consecutive_months_across_years(self) -> None:
        self.assertEqual(
            month_sequence("2025-10", "2026-01"),
            ((2025, 10), (2025, 11), (2025, 12), (2026, 1)),
        )

    def test_single_month_range(self) -> None:
        self.assertEqual(month_sequence("2026-10", "2026-10"), ((2026, 10),))

    def test_rejects_reversed_range_and_bad_format(self) -> None:
        with self.assertRaises(ValueError):
            month_sequence("2026-10", "2025-10")
        with self.assertRaises(ValueError):
            month_sequence("2026-13", "2026-12")
        with self.assertRaises(ValueError):
            month_sequence("2026/10", "2026-12")

    def test_month_bounds_handle_december_and_leap_february(self) -> None:
        start, end, stamp = month_bounds(2025, 12)
        self.assertEqual((start, end, stamp), (date(2025, 12, 1), date(2025, 12, 31), "202512"))
        start, end, _ = month_bounds(2028, 2)
        self.assertEqual((start, end), (date(2028, 2, 1), date(2028, 2, 29)))

    def test_default_month_range_is_recent_six_months(self) -> None:
        self.assertEqual(
            default_month_range(date(2026, 11, 10)),
            {"start_month": "2026-06", "end_month": "2026-11"},
        )
        self.assertEqual(
            default_month_range(date(2026, 2, 1)),
            {"start_month": "2025-09", "end_month": "2026-02"},
        )


class PayloadValidationTests(unittest.TestCase):
    def _payload(self, **overrides) -> dict:
        payload = {
            "username": "synthetic-user",
            "password": "secret",
            "category_ids": ["1042621"],
            "warehouse_ids": [],
            "start_month": "2026-06",
            "end_month": "2026-11",
            "output_dir": "C:/out",
        }
        payload.update(overrides)
        return payload

    def test_builds_job_with_months(self) -> None:
        job = validate_export_payload(self._payload())

        self.assertEqual(job.category_ids, ("1042621",))
        self.assertEqual(len(job.months), 6)
        self.assertEqual(job.months[0], (2026, 6))

    def test_requires_account_password_and_selection(self) -> None:
        cases = (
            ({"username": ""}, "请输入马帮账号"),
            ({"password": ""}, "请输入马帮密码"),
            ({"category_ids": [], "warehouse_ids": []}, "请至少选择一个自定义分类或一个仓库"),
            ({"output_dir": ""}, "请选择输出目录"),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as captured:
                    validate_export_payload(self._payload(**overrides))
                self.assertIn(message, str(captured.exception))

    def test_rejects_more_than_sixty_months(self) -> None:
        with self.assertRaises(ValueError):
            validate_export_payload(self._payload(start_month="2020-01", end_month="2026-11"))

    def test_deduplicates_repeated_ids(self) -> None:
        job = validate_export_payload(self._payload(category_ids=["1042621", "1042621"]))
        self.assertEqual(job.category_ids, ("1042621",))


class ReportFormTests(unittest.TestCase):
    def test_form_switches_to_warehouse_report_and_repeats_filters(self) -> None:
        form = build_report_form(
            ["1042621", "1038954"],
            ["330978"],
            date(2026, 4, 1),
            date(2026, 4, 30),
        )
        mapping = dict((key, value) for key, value in form if key not in {"shopLabelIds[]", "stockWarehouseId[]"})

        self.assertEqual(mapping["platformOrShop"], "warehouse")
        self.assertEqual(mapping["table"], "warehouse")
        self.assertEqual(mapping["timeStart"], "2026-04-01 00:00:00")
        self.assertEqual(mapping["timeEnd"], "2026-04-30 23:59:59")
        self.assertEqual(mapping["timeKey"], "payTime")
        self.assertEqual([value for key, value in form if key == "shopLabelIds[]"], ["1042621", "1038954"])
        self.assertEqual([value for key, value in form if key == "stockWarehouseId[]"], ["330978"])

    def test_count_detail_rows_skips_total_row(self) -> None:
        self.assertEqual(count_detail_rows(_csv_content(5)), 5)
        self.assertEqual(count_detail_rows(b""), 0)


class ExportIntegrityTests(unittest.TestCase):
    def _job(self, output_dir: Path) -> WarehouseSalesExportJob:
        return WarehouseSalesExportJob(
            username="synthetic-user",
            password="secret",
            category_ids=("1042621",),
            warehouse_ids=("330978",),
            start_month="2026-04",
            end_month="2026-04",
            output_dir=output_dir,
        )

    def test_full_export_checks_total_even_off_page_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeClient(page_cap=37, total=37)
            with patch("backend.services.warehouse_sales_export.time.sleep"):
                job = self._job(Path(temporary))
                from dataclasses import replace
                job = replace(job, warehouse_ids=())
                result = export_month(client, Path(temporary), job, 2026, 4)

            self.assertEqual(result.record_count, 37)
            self.assertEqual(client.total_queries, 1)
            self.assertTrue(result.csv_path.exists())

    def test_truncated_export_is_retried_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = FakeClient(page_cap=2000, total=5345)
            job = WarehouseSalesExportJob(
                username="synthetic-user", password="secret",
                category_ids=(), warehouse_ids=("330978",),
                start_month="2026-04", end_month="2026-04", output_dir=Path(temporary),
            )
            with patch("backend.services.warehouse_sales_export.time.sleep"):
                with self.assertRaises(MabangApiError) as captured:
                    export_month(client, Path(temporary), job, 2026, 4)

            self.assertIn("截断", str(captured.exception))
            self.assertEqual(client.export_calls, 4)
            self.assertEqual(client.total_queries, 4)
            self.assertFalse(any(Path(temporary).glob("*.csv")))


class PartialColumnTests(unittest.TestCase):
    def test_three_column_export_is_rejected(self) -> None:
        """实测分类+仓库时官方导出只回 3 列，必须拒绝而不是写出残缺表。"""
        content = "SKU,商品中文名,商品英文名\nhqkyy921A,笔,Pen\n".encode("utf-8-sig")
        with self.assertRaisesRegex(MabangApiError, "导出列不完整"):
            require_full_sales_columns(content, "2026-11 商品仓库报表")

    def test_full_export_passes_column_check(self) -> None:
        require_full_sales_columns(_csv_content(2), "ok")


class IntersectionExportTests(unittest.TestCase):
    def test_intersection_uses_both_filter_arrays_and_preserves_server_metrics(self):
        rows = [sales_row("sku-b", quantity="5", income="50", **{"平均销售价格": "", "毛利": "10", "毛利率%": "20"})]
        client = ClassicClient(rows, csv_content=sales_csv(rows, classic_layout=True))
        with tempfile.TemporaryDirectory() as temporary:
            job = WarehouseSalesExportJob("u", "p", ("c1", "c2"), ("w1", "w2"), "2026-04", "2026-04", Path(temporary))
            content = export_intersection_csv(client, job, date(2026, 4, 1), date(2026, 4, 30), 30)
        body = next(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(body["销售数量"], "5")
        self.assertEqual(body["平均单天销量"], "0.17\t")
        self.assertEqual(body["平均销售价格"], "10.0000\t")
        self.assertEqual(body["毛利"], "10")
        self.assertEqual(body["毛利率%"], "20")
        self.assertEqual([v for k, v in client.calls[0] if k == "shopLabelIds[]"], ["c1", "c2"])
        self.assertEqual([v for k, v in client.calls[0] if k == "stockWarehouseId[]"], ["w1", "w2"])

    def test_empty_intersection_is_valid(self):
        client = ClassicClient([])
        job = WarehouseSalesExportJob("u", "p", ("c1",), ("w1",), "2026-04", "2026-04", Path("."))
        content = export_intersection_csv(client, job, date(2026, 4, 1), date(2026, 4, 30), 30)
        self.assertEqual(count_detail_rows(content), 0)
        self.assertEqual(client.export_calls, 0)


class WorkbookTests(unittest.TestCase):
    def test_each_month_becomes_its_own_sheet_plus_note_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            months = []
            for index, label in enumerate(("2026-06", "2026-07")):
                path = run_dir / f"{label}.csv"
                path.write_bytes(_csv_content(index + 2))
                months.append(MonthExport(label=label, record_count=index + 2, csv_path=path))
            output = run_dir / "汇总.xlsx"
            job = WarehouseSalesExportJob(
                username="synthetic-user",
                password="secret",
                category_ids=("1042621",),
                warehouse_ids=("330978",),
                start_month="2026-06",
                end_month="2026-07",
                output_dir=run_dir,
            )

            total = build_workbook(
                run_dir=run_dir,
                output_path=output,
                job=job,
                months=months,
                category_names=("泰国TK",),
                warehouse_names=("泰国新仓TH01",),
            )

            self.assertEqual(total, 5)
            workbook = openpyxl.load_workbook(output, read_only=True)
            try:
                self.assertEqual(workbook.sheetnames, ["导出说明", "2026-06", "2026-07"])
                rows = list(workbook["2026-07"].iter_rows(values_only=True))
            finally:
                workbook.close()
            self.assertEqual(rows[0][0], "SKU")
            self.assertEqual(len(rows), 4)  # 表头 + 3 行明细（合计行不写入）
            self.assertEqual(rows[1][0], "hqsT00000")


class BridgeTests(unittest.TestCase):
    def _bridge(self) -> AppBridge:
        bridge = AppBridge.__new__(AppBridge)
        bridge.config_store = Mock()
        bridge.tasks = Mock()
        bridge.tasks.start.return_value = {"ok": True, "task_id": "task-1"}
        return bridge

    def test_starts_task_with_validated_filters(self) -> None:
        bridge = self._bridge()
        payload = {
            "account_id": "acc-1",
            "username": "synthetic-user",
            "password": "secret",
            "category_ids": ["1042621"],
            "warehouse_ids": ["330978"],
            "start_month": "2026-06",
            "end_month": "2026-08",
            "output_dir": "C:/out",
        }
        with patch.object(AppBridge, "_payload_with_account", return_value=payload):
            result = bridge.start_warehouse_sales_export({"account_id": "acc-1"})

        self.assertTrue(result["ok"])
        args, kwargs = bridge.tasks.start.call_args
        self.assertEqual(args[0], "商品仓库报表按月导出")
        self.assertEqual(kwargs["tool"], "warehouse_sales_export")

    def test_missing_account_is_reported_as_requires_account(self) -> None:
        bridge = self._bridge()
        with patch.object(
            AppBridge,
            "_payload_with_account",
            side_effect=AccountRequiredError("mabang", "请先绑定并选择马帮账号"),
        ):
            result = bridge.start_warehouse_sales_export({})

        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "mabang")
        bridge.tasks.start.assert_not_called()

    def test_invalid_selection_is_rejected_before_starting_task(self) -> None:
        bridge = self._bridge()
        payload = {
            "username": "synthetic-user",
            "password": "secret",
            "category_ids": [],
            "warehouse_ids": [],
            "start_month": "2026-06",
            "end_month": "2026-08",
            "output_dir": "C:/out",
        }
        with patch.object(AppBridge, "_payload_with_account", return_value=payload):
            result = bridge.start_warehouse_sales_export({})

        self.assertFalse(result["ok"])
        self.assertIn("至少选择一个", result["error"])
        bridge.tasks.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from backend.services import temu_activity_export as export
from backend.services.temu_activity_workbook import (
    QUOTE_SHEET,
    STOCK_SHEET,
    WorkbookFormatError,
    foreign_sites,
    inspect_export,
)


QUOTE_HEADER = ["活动类型（活动主题）", "SPU ID", "SKC ID", "SKC 货号", "SKU ID", "SKU 货号",
                "颜色属性", "尺码属性", "站点", "活动申报价格", "币种"]
STOCK_HEADER = ["活动类型（活动主题）", "SPU ID", "活动库存"]
QUOTE_ROWS = [
    ["限时秒杀-国庆", "SPU-1", "SKC-1", "SKC-CODE-1", "SKUID000000000001", "SKU-1", "红", "M", "菲律宾站", "1.82", "USD"],
    ["限时秒杀-国庆", "SPU-1", "SKC-1", "SKC-CODE-1", "SKUID000000000002", "SKU-2", "红", "L", "菲律宾站", "2.10", "USD"],
    ["清仓-季末", "SPU-2", "SKC-2", "SKC-CODE-2", "SKUID000000000003", "SKU-3", "蓝", "M", "菲律宾站", "", "USD"],
]


def build_template(path: Path, *, rows=None, site="菲律宾站", sheet_name=QUOTE_SHEET, stock: bool = True) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(QUOTE_HEADER)
    for row in rows if rows is not None else QUOTE_ROWS:
        sheet.append([site if index == 8 else value for index, value in enumerate(row)])
    if stock:
        stock_sheet = workbook.create_sheet(STOCK_SHEET)
        stock_sheet.append(STOCK_HEADER)
        stock_sheet.append(["限时秒杀-国庆", "SPU-1", 100])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


class WorkbookTests(unittest.TestCase):
    def test_counts_are_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = build_template(Path(root) / "模板.xlsx")
            report = inspect_export(path)
        self.assertEqual(report["quote_rows"], 3)
        self.assertEqual(report["stock_rows"], 1)
        self.assertEqual(report["spu_count"], 2)
        self.assertEqual(report["sku_id_count"], 3)
        self.assertEqual(report["sku_code_count"], 3)
        self.assertEqual(report["topic_count"], 2)
        self.assertEqual(report["clearance_rows"], 1)
        self.assertEqual(report["blank_price_rows"], 1)
        self.assertEqual(report["sites"], {"菲律宾站": 3})
        self.assertEqual(report["currencies"], ["USD"])
        self.assertEqual(len(report["sha256"]), 64)
        self.assertEqual(report["sheet_names"], [QUOTE_SHEET, STOCK_SHEET])

    def test_identifier_is_read_as_text(self) -> None:
        rows = [["限时秒杀-国庆", "SPU-1", "SKC-1", "SKC-CODE-1", 123456789012345678, "SKU-1", "红", "M", "菲律宾站", "1.82", "USD"]]
        with tempfile.TemporaryDirectory() as root:
            path = build_template(Path(root) / "模板.xlsx", rows=rows)
            report = inspect_export(path)
        self.assertEqual(report["sku_id_count"], 1)

    def test_missing_stock_sheet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = build_template(Path(root) / "模板.xlsx", stock=False)
            with self.assertRaises(WorkbookFormatError) as error:
                inspect_export(path)
        self.assertIn(STOCK_SHEET, str(error.exception))

    def test_changed_header_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = build_template(Path(root) / "模板.xlsx", sheet_name=QUOTE_SHEET)
            from openpyxl import load_workbook

            workbook = load_workbook(path)
            workbook[QUOTE_SHEET]["J1"] = "申报价格"
            workbook.save(path)
            with self.assertRaises(WorkbookFormatError) as error:
                inspect_export(path)
        self.assertIn("表头", str(error.exception))

    def test_foreign_sites_are_detected(self) -> None:
        self.assertEqual(foreign_sites({"菲律宾站": 3}, ["PH"]), [])
        self.assertEqual(foreign_sites({"泰国站": 3}, ["PH"]), ["泰国站"])


class RequestTests(unittest.TestCase):
    def test_sites_and_date_are_validated(self) -> None:
        options = export.validate_request({"store_names": "店铺A", "sites": ["PH", "MY"]})
        # Validated into stable codes; the date defaults to Beijing today.
        self.assertEqual(options["sites"], ["PH", "MY"])
        self.assertEqual(len(options["business_date"]), 10)

    def test_duplicate_and_unknown_sites_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "站点不能重复"):
            export.validate_request({"store_names": "店铺A", "sites": ["PH", "PH"]})
        with self.assertRaisesRegex(ValueError, "不支持的站点"):
            export.validate_request({"store_names": "店铺A", "sites": ["ID"]})
        with self.assertRaisesRegex(ValueError, "请至少勾选一个站点"):
            export.validate_request({"store_names": "店铺A", "sites": []})

    def test_future_business_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能晚于北京时间当天"):
            export.validate_request({"store_names": "店铺A", "sites": ["PH"], "business_date": "2999-01-01"})

    def test_store_names_are_deduplicated_case_insensitively(self) -> None:
        options = export.validate_request({"store_names": "Shop-A\nshop-a\nShop-B", "sites": ["PH"]})
        self.assertEqual(options["store_names"], ["Shop-A", "Shop-B"])


class FakeGateway:
    profile = "ziniao-account:test"

    def __init__(self, *, fail: tuple[str, ...] = (), halted: bool = False):
        self.fail = set(fail)
        self.halted = halted
        self.halt_reason = "紫鸟认证已失效，请恢复后重试"
        self.exported: list[str] = []

    def preflight(self, stores):
        return None

    def export(self, store, raw_dir, *, sites):
        if store["store_name"] in self.fail:
            raise ValueError("未找到「选择商品」，请核对活动报名页结构")
        self.exported.append(store["store_name"])
        path = build_template(raw_dir / "活动报名模板.xlsx", site="菲律宾站" if "PH" in sites else "泰国站")
        report = inspect_export(path)
        return {"path": str(path), "evidence": {"quote_rows": report["quote_rows"]}, "report": report}


def store(store_id: str, name: str) -> dict:
    return {"store_id": store_id, "store_name": name, "platform_name": "TEMU"}


class StoreMatchTests(unittest.TestCase):
    CATALOG = [
        {"store_id": "1", "store_name": "甲-店A", "platform_name": "TEMU"},
        {"store_id": "2", "store_name": "甲-店AB", "platform_name": "TEMU"},
        {"store_id": "3", "store_name": "甲-店C", "platform_name": "Shopee"},
    ]

    def test_full_name_wins_over_partial_name(self) -> None:
        result = export.resolve_stores(["甲-店A"], self.CATALOG)
        self.assertTrue(result["can_start"])
        self.assertEqual([store["store_id"] for store in result["stores"]], ["1"])

    def test_ambiguous_partial_name_is_reported(self) -> None:
        result = export.resolve_stores(["店A"], self.CATALOG)
        self.assertFalse(result["can_start"])
        self.assertIn("匹配到多个店铺", result["issues"][0]["error"])

    def test_unknown_and_non_temu_stores_are_reported(self) -> None:
        self.assertIn("未匹配到店铺", export.resolve_stores(["店Z"], self.CATALOG)["issues"][0]["error"])
        self.assertIn("TEMU", export.resolve_stores(["店C"], self.CATALOG)["issues"][0]["error"])

    def test_same_store_twice_is_rejected(self) -> None:
        result = export.resolve_stores(["甲-店A", "甲-店A"], self.CATALOG)
        self.assertFalse(result["can_start"])
        self.assertIn("同一店铺", result["issues"][0]["error"])


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.output = Path(self.root.name)
        self.stores = [store("1001", "寰球-店铺A"), store("1002", "寰球-店铺B")]

    def make_batch(self, stores=None) -> Path:
        return export.create_batch(self.output, FakeGateway.profile, stores or self.stores, ["PH"], "2026-11-03")

    def test_success_keeps_raw_file_and_named_copy(self) -> None:
        path = self.make_batch()
        result = export.run_batch(path, FakeGateway())
        self.assertTrue(result["is_complete"])
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["quote_rows"], 6)
        self.assertEqual(result["stock_rows"], 2)
        self.assertEqual(result["site_rows"], {"菲律宾站": 6})
        for item in result["stores"]:
            self.assertEqual(item["status"], "success")
            self.assertTrue(Path(item["raw_file"]).is_file())
            self.assertTrue(Path(item["result_file"]).is_file())
            self.assertEqual(item["quote_rows"], 3)
            self.assertEqual(item["topic_count"], 2)

    def test_one_failed_store_does_not_stop_the_batch(self) -> None:
        path = self.make_batch()
        result = export.run_batch(path, FakeGateway(fail=("寰球-店铺A",)))
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("选择商品", result["stores"][0]["error"])
        self.assertEqual(result["stores"][1]["status"], "success")

    def test_store_names_that_sanitize_identically_keep_separate_results(self) -> None:
        path = self.make_batch([store("1001", "店铺:A"), store("1002", "店铺?A")])
        result = export.run_batch(path, FakeGateway())
        self.assertTrue(result["is_complete"])
        files = [Path(item["result_file"]) for item in result["stores"]]
        self.assertEqual(len(set(files)), 2)
        self.assertEqual(len(list((path.parent / "results").glob("*.xlsx"))), 2)

    def test_rerun_skips_stores_that_already_succeeded(self) -> None:
        path = self.make_batch()
        export.run_batch(path, FakeGateway(fail=("寰球-店铺B",)))
        gateway = FakeGateway()
        result = export.run_batch(path, gateway)
        self.assertEqual(gateway.exported, ["寰球-店铺B"])
        self.assertTrue(result["is_complete"])
        self.assertEqual([item["attempts"] for item in result["stores"]], [1, 2])

    def test_halted_gateway_stops_remaining_stores(self) -> None:
        path = self.make_batch()
        gateway = FakeGateway(halted=True)
        result = export.run_batch(path, gateway)
        self.assertEqual(result["failed_count"], 2)
        self.assertIn("紫鸟认证已失效", result["stores"][1]["error"])

    def test_batch_rejects_another_account(self) -> None:
        path = self.make_batch()
        with self.assertRaisesRegex(ValueError, "不一致"):
            export.load_batch(path, "ziniao-account:other")

    def test_batch_rejects_paths_outside_the_run_directory(self) -> None:
        path = self.make_batch()
        record = export.load_batch(path)
        record["stores"][0]["raw_file"] = "../escape.xlsx"
        path.write_text(__import__("json").dumps(record, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "超出本次导出目录"):
            export.load_batch(path)

    def test_fail_batch_marks_pending_stores(self) -> None:
        path = self.make_batch()
        result = export.fail_batch(path, FakeGateway.profile, "任务未能启动")
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["failed_count"], 2)
        self.assertIn("任务未能启动", result["stores"][0]["error"])


if __name__ == "__main__":
    unittest.main()

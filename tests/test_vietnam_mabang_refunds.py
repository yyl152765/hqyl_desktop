from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.services.vietnam_income_collection import (
    REPORT_TYPES,
    create_collection_run,
    load_manifest,
    validate_collection_payload,
    write_manifest,
)
from backend.services.vietnam_mabang_income import _ensure_mabang_section
from backend.services.vietnam_mabang_refunds import (
    REFUND_EXPORT_COLUMNS,
    RefundSegment,
    build_refund_search_form,
    merge_refund_rows,
    parse_refund_csv,
    parse_refund_total,
    refund_dedupe_key,
    refund_segments,
    validate_vietnam_mabang_refund_payload,
)


class VietnamMabangRefundTests(unittest.TestCase):
    def _income_ready_manifest(self, temp_dir: str) -> Path:
        request = validate_collection_payload(
            {
                "period": "2026-05",
                "output_dir": temp_dir,
                "stores": [{"name": "越南测试店"}],
            }
        )
        created = create_collection_run(request)
        manifest_path = Path(created["manifest_path"])
        manifest = load_manifest(manifest_path)
        for report_type in REPORT_TYPES:
            output_file = manifest_path.parent / "raw" / f"{report_type}.csv"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("a\n1\n", encoding="utf-8")
            report = manifest["stores"][0]["reports"][report_type]
            report.update(status="success", file=str(output_file.relative_to(manifest_path.parent)))
        mabang = _ensure_mabang_section(manifest, "马帮主账号")
        for key in ("income_detail", "income_summary"):
            output_file = manifest_path.parent / "raw" / "mabang" / f"{key}.csv"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("a\n1\n", encoding="utf-8")
            mabang["reports"][key].update(
                status="success",
                file=str(output_file.relative_to(manifest_path.parent)),
            )
        mabang["validation"].update(status="passed", order_count_diff=0, receivable_diff="0")
        mabang["status"] = "income_ready"
        manifest["status"] = "mabang_income_ready"
        write_manifest(manifest_path, manifest)
        return manifest_path

    def test_job_requires_passed_income_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = validate_collection_payload(
                {"period": "2026-05", "output_dir": temp_dir, "stores": [{"name": "越南测试店"}]}
            )
            manifest_path = Path(create_collection_run(request)["manifest_path"])
            with self.assertRaisesRegex(ValueError, "收支明细与汇总校验"):
                validate_vietnam_mabang_refund_payload(
                    {"manifest_path": str(manifest_path), "username": "13800000000", "password": "secret"}
                )

    def test_job_uses_bound_account_without_persisting_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._income_ready_manifest(temp_dir)
            job = validate_vietnam_mabang_refund_payload(
                {
                    "manifest_path": str(manifest_path),
                    "username": "13800000000",
                    "password": "secret",
                    "account_name": "马帮主账号",
                }
            )
            self.assertEqual(job.username, "13800000000")
            self.assertEqual(job.account_name, "马帮主账号")
            self.assertNotIn("secret", manifest_path.read_text(encoding="utf-8"))

    def test_segments_cover_whole_month_and_leap_february(self) -> None:
        segments = refund_segments("2028-02-01", "2028-02-29")
        self.assertEqual(
            [(item.start_date, item.end_date) for item in segments],
            [("2028-02-01", "2028-02-10"), ("2028-02-11", "2028-02-20"), ("2028-02-21", "2028-02-29")],
        )

    def test_refund_form_uses_verified_advanced_search_filters(self) -> None:
        segment = RefundSegment("refund_part_01_10", "01-10", "2026-05-01", "2026-05-10")
        form = build_refund_search_form(segment)
        values = dict(form)
        self.assertEqual(values["platformId"], "17")
        self.assertEqual(values["countryCodes[]"], "VN")
        self.assertEqual(values["orderStatus[]"], "3")
        self.assertEqual(values["refundTimeStart"], "2026-05-01 00:00:00")
        self.assertEqual(values["refundTimeEnd"], "2026-05-10 23:59:59")

    def test_refund_total_parser_reads_page_count(self) -> None:
        html = "<div>每页 20 条 共 6,282 条 当前显示第 1-20 条 1/315 页</div>"
        self.assertEqual(parse_refund_total(html), 6282)

    def test_csv_parser_keeps_rmb_and_refund_fields(self) -> None:
        header = ",".join(REFUND_EXPORT_COLUMNS)
        values = {column: "" for column in REFUND_EXPORT_COLUMNS}
        values.update(
            {
                "订单号": "ORDER-1",
                "订单状态": "已发货",
                "创建退款时订单状态": "已发货",
                "平台": "Shopee",
                "店铺": "越南测试店",
                "国家": "越南",
                "退款单编号": "TKD-1",
                "退款金额": "100000",
                "退款币种": "VND",
                "退款金额RMB": "28.50",
                "退款时间": "2026-05-01 12:00:00(UTC+8)",
                "退款原因": "CHANGE_MIND",
            }
        )
        import csv
        import io

        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(REFUND_EXPORT_COLUMNS))
        writer.writeheader()
        writer.writerow(values)
        columns, rows = parse_refund_csv(b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8"))
        self.assertEqual(columns[0], header.split(",")[0])
        self.assertEqual(rows[0]["退款金额RMB"], "28.50")

    def test_dedupe_prefers_refund_number(self) -> None:
        first = {"订单号": "ORDER-1", "退款单编号": "\tTKD-1", "退款时间": "2026-05-01", "退款金额RMB": "10"}
        duplicate = dict(first, 退款原因="OTHER")
        different = dict(first, 退款单编号="TKD-2")
        segment = RefundSegment("refund_part_01_10", "01-10", "2026-05-01", "2026-05-10")
        merged, duplicate_count = merge_refund_rows([(segment, [first, duplicate, different])])
        self.assertEqual(len(merged), 2)
        self.assertEqual(duplicate_count, 1)
        self.assertEqual(refund_dedupe_key(first), "refund:TKD-1")

    def test_same_order_with_two_real_refunds_is_not_removed(self) -> None:
        first = {"订单号": "ORDER-1", "退款单编号": "", "退款时间": "2026-05-01", "退款金额RMB": "10", "退款原因": "A"}
        second = dict(first, 退款时间="2026-05-02", 退款金额RMB="20", 退款原因="B")
        segment = RefundSegment("refund_part_01_10", "01-10", "2026-05-01", "2026-05-10")
        merged, duplicate_count = merge_refund_rows([(segment, [first, second])])
        self.assertEqual(len(merged), 2)
        self.assertEqual(duplicate_count, 0)


if __name__ == "__main__":
    unittest.main()

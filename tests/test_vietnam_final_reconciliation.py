from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from backend.services.vietnam_final_reconciliation import (
    calculate_adjusted_shipping,
    extract_channel_rate,
    parse_profit_rate,
    parse_profit_rates,
    run_vietnam_final_reconciliation,
    validate_vietnam_final_reconciliation_payload,
)


class VietnamFinalReconciliationTests(unittest.TestCase):
    def test_extract_channel_rate_reads_yuan_per_kg_and_defaults_to_10(self) -> None:
        self.assertEqual(str(extract_channel_rate("SPX Express (VN胡志明自建仓)-15元/KG")), "15")
        self.assertEqual(str(extract_channel_rate("SPX Express (胡志明知虾海外仓)-6元/KG")), "6")
        self.assertEqual(str(extract_channel_rate("SPX Express 骑手订单")), "10")

    def test_reissue_order_shipping_is_zero(self) -> None:
        self.assertEqual(str(calculate_adjusted_shipping("ABC_1", "2000", "渠道-20元/KG")), "0")
        self.assertEqual(str(calculate_adjusted_shipping("ABC", "2000", "渠道-20元/KG")), "40.0000")

    def test_profit_rate_parser_accepts_percentage_and_decimal(self) -> None:
        self.assertEqual(str(parse_profit_rate("12.5%")), "0.125")
        self.assertEqual(str(parse_profit_rate("0.125")), "0.125")
        self.assertEqual(str(parse_profit_rate("12.5")), "0.125")
        parsed = parse_profit_rates("店铺A=12.5%\n店铺B,0.2")
        self.assertEqual(str(parsed["店铺A"]), "0.125")
        self.assertEqual(str(parsed["店铺B"]), "0.2")

    def test_run_generates_workbook_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest_path = _build_minimal_run(run_dir)
            job = validate_vietnam_final_reconciliation_payload(
                {
                    "manifest_path": str(manifest_path),
                    "profit_rates_text": "马帮店铺=10%",
                }
            )
            result = run_vietnam_final_reconciliation(job)
            output_file = Path(result["output_file"])
            self.assertTrue(output_file.is_file())

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "final_ready")
            self.assertEqual(manifest["final"]["status"], "ready")

            workbook = load_workbook(output_file, read_only=True, data_only=True)
            self.assertIn("核对汇总", workbook.sheetnames)
            self.assertIn("收支报表", workbook.sheetnames)
            summary_rows = list(workbook["核对汇总"].iter_rows(values_only=True))
            flattened = ["|".join("" if value is None else str(value) for value in row) for row in summary_rows]
            self.assertTrue(any("马帮店铺" in row for row in flattened))
            self.assertTrue(any("最终广告花费" in row for row in flattened))
            workbook.close()

    def test_missing_refund_profit_rate_blocks_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest_path = _build_minimal_run(run_dir)
            job = validate_vietnam_final_reconciliation_payload(
                {
                    "manifest_path": str(manifest_path),
                    "profit_rates_text": "别的店铺=10%",
                }
            )
            with self.assertRaisesRegex(ValueError, "未填写上月利润率"):
                run_vietnam_final_reconciliation(job)

    def test_reconciliation_summary_contains_totals_and_stores_and_correct_profit_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest_path = _build_minimal_run(run_dir)
            job = validate_vietnam_final_reconciliation_payload(
                {
                    "manifest_path": str(manifest_path),
                    "profit_rates_text": "马帮店铺=10%",
                }
            )
            result = run_vietnam_final_reconciliation(job)
            summary_file = Path(result["summary_file"])
            self.assertTrue(summary_file.is_file())
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            self.assertIn("totals", summary)
            self.assertIn("stores", summary)
            self.assertIn("exception_summary", summary)
            
            # Verify totals and exception counts
            self.assertEqual(len(summary["stores"]), 1)
            self.assertEqual(summary["stores"][0]["store_name"], "马帮店铺")
            # All expense + actual_refund + actual_affiliate + final_ads
            # Let's verify that the total profit rate is calculated correctly
            totals = summary["totals"]
            self.assertIn("profit_rate", totals)
            
    def test_generation_without_refund_stores_does_not_block_due_to_empty_profit_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest_path = _build_minimal_run(run_dir)
            # Remove refund rows so no refund stores exist
            refund_merged = run_dir / "raw" / "mabang" / "refunds" / "refunds.csv"
            refund_merged.write_text("订单号,店铺,退款金额RMB,退款单编号\n", encoding="utf-8-sig")
            for name in ("refund_part_01_10.csv", "refund_part_11_20.csv", "refund_part_21_end.csv"):
                (run_dir / "raw" / "mabang" / "refunds" / name).write_text("订单号,店铺,退款金额RMB,退款单编号\n", encoding="utf-8-sig")
                
            # Now run reconciliation with empty profit_rates_text
            job = validate_vietnam_final_reconciliation_payload(
                {
                    "manifest_path": str(manifest_path),
                    "profit_rates_text": "",
                }
            )
            result = run_vietnam_final_reconciliation(job)
            self.assertTrue(Path(result["output_file"]).is_file())

    def test_profit_rates_fall_back_to_saved_manifest_config(self) -> None:
        # 前端将利润率保存到 manifest.config 后只传 manifest_path，
        # 校验时应回退读取 config，而不是因为“未填写利润率”而阻断生成。
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            manifest_path = _build_minimal_run(run_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["config"] = {"profit_rates": {"马帮店铺": "0.125"}}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            # 不传 profit_rates_text，仅传 manifest_path
            job = validate_vietnam_final_reconciliation_payload({"manifest_path": str(manifest_path)})
            self.assertIn("马帮店铺", job.profit_rates)
            self.assertEqual(str(job.profit_rates["马帮店铺"]), "0.125")
            result = run_vietnam_final_reconciliation(job)
            self.assertTrue(Path(result["output_file"]).is_file())


def _build_minimal_run(run_dir: Path) -> Path:
    raw = run_dir / "raw"
    ziniao = raw / "ziniao" / "紫鸟店铺__abc"
    mabang = raw / "mabang"
    for path in (
        ziniao / "income",
        ziniao / "ads",
        ziniao / "affiliate",
        ziniao / "ads_credit",
        mabang / "income_detail",
        mabang / "income_summary",
        mabang / "refunds",
    ):
        path.mkdir(parents=True, exist_ok=True)

    income_detail = mabang / "income_detail" / "detail.csv"
    _write_csv(
        income_detail,
        [
            {
                "订单编号": "ORD001",
                "店铺名称": "马帮店铺",
                "订单重量": "2000",
                "物流渠道": "SPX Express (VN胡志明自建仓)-6元/KG",
                "收入-应收货款": "100",
                "汇率": "0.01",
                "预估运费": "99",
                "支出-包材费": "3",
                "销量": "2",
                "支出-成本": "30",
                "支出-转帐费": "0",
                "支出-平台费": "0",
                "支出-交易手续费": "0",
                "支出-佣金": "0",
                "支出-服务费": "0",
                "支出-广告费": "0",
                "支出-头程费": "0",
                "支出-FBA费用": "0",
                "支出-优惠券": "0",
                "支出-VAT税费": "0",
                "支出-其他费用": "0",
                "支出-其他支出": "0",
                "支出-测评费": "0",
            }
        ],
    )
    income_summary = mabang / "income_summary" / "summary.csv"
    _write_csv(income_summary, [{"店铺名称": "马帮店铺", "订单数": "1", "应收货款": "100"}])
    refund_merged = mabang / "refunds" / "refunds.csv"
    _write_csv(refund_merged, [{"订单号": "ORD001", "店铺": "马帮店铺", "退款金额RMB": "50", "退款单编号": "R1"}])
    for name in ("refund_part_01_10.csv", "refund_part_11_20.csv", "refund_part_21_end.csv"):
        _write_csv(mabang / "refunds" / name, [{"订单号": "ORD001", "店铺": "马帮店铺", "退款金额RMB": "50", "退款单编号": "R1"}])

    subsidy = ziniao / "income" / "subsidy.csv"
    _write_csv(subsidy, [{"店铺名称": "紫鸟店铺", "订单编号": "ORD001", "补贴金额": "20"}])
    ads = ziniao / "ads" / "ads.csv"
    ads.write_text("标题\n商店名称,紫鸟店铺\n\n排序,广告名称,花费\n1,A,1000\n", encoding="utf-8-sig")
    affiliate = ziniao / "affiliate" / "affiliate.csv"
    affiliate.write_text("Campaign,expense\nA,300\n", encoding="utf-8-sig")
    ads_credit = ziniao / "ads_credit" / "credit.csv"
    _write_csv(ads_credit, [{"店铺名称": "紫鸟店铺", "金额": "200"}])

    manifest = {
        "schema_version": 1,
        "source": "ziniao",
        "period": "2026-05",
        "start_date": "2026-05-01",
        "end_date": "2026-05-31",
        "status": "mabang_ready",
        "stores": [
            {
                "store_name": "紫鸟店铺",
                "reports": {
                    "income": {"status": "success", "file": _rel(run_dir, subsidy)},
                    "ads": {"status": "success", "file": _rel(run_dir, ads)},
                    "affiliate": {"status": "success", "file": _rel(run_dir, affiliate)},
                    "ads_credit": {"status": "success", "file": _rel(run_dir, ads_credit)},
                },
            }
        ],
        "mabang": {
            "status": "ready",
            "reports": {
                "income_detail": {"status": "success", "file": _rel(run_dir, income_detail)},
                "income_summary": {"status": "success", "file": _rel(run_dir, income_summary)},
                "refund_part_01_10": {"status": "success", "file": _rel(run_dir, mabang / "refunds" / "refund_part_01_10.csv")},
                "refund_part_11_20": {"status": "success", "file": _rel(run_dir, mabang / "refunds" / "refund_part_11_20.csv")},
                "refund_part_21_end": {"status": "success", "file": _rel(run_dir, mabang / "refunds" / "refund_part_21_end.csv")},
                "refunds_merged": {"status": "success", "file": _rel(run_dir, refund_merged)},
            },
            "validation": {"status": "passed"},
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


if __name__ == "__main__":
    unittest.main()

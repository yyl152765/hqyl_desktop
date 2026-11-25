from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.services.shopee_id_invoice_gateway import InvoiceExportError, validate_pdf
from backend.services import shopee_id_monthly_invoice as service


def make_valid_pdf(path: Path, label: str = "sample-invoice") -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        b"%PDF-1.7\n"
        + f"1 0 obj << /Title ({label}) >> endobj\n".encode("ascii")
        + b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    path.write_bytes(content)
    return {
        "path": str(path),
        "filename": path.name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


class FakeInvoiceGateway:
    def __init__(self, profile: str, plans: dict[str, object]) -> None:
        self.profile = profile
        self.plans = plans
        self.auth_failed = False
        self.halted = False
        self.halt_reason = "紫鸟认证已失效，请恢复后重试"
        self.exported_stores: list[str] = []

    def preflight(self, stores: list[dict]) -> None:
        return None

    def export(self, store: dict, month: str, store_dir: Path) -> dict:
        store_id = store["store_id"]
        self.exported_stores.append(store_id)
        plan = self.plans[store_id]
        if callable(plan):
            return plan(store, month, Path(store_dir))
        if isinstance(plan, Exception):
            raise plan
        return dict(plan)


class ShopeeIdMonthlyInvoiceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.today = date(2026, 11, 25)

    def test_default_and_validated_month_reject_current_future_and_malformed(self) -> None:
        self.assertEqual(service.default_month(self.today), "2026-10")
        self.assertEqual(service.validate_month("2026-10", today=self.today), "2026-10")
        self.assertEqual(service.validate_month("2026-01", today=self.today), "2026-01")
        for invalid in ("0000-01", "2026-11", "2026-12", "2027-01", "2026-00", "2026-13", "2026/10", "2026-1", ""):
            with self.subTest(month=invalid), self.assertRaises(ValueError):
                service.validate_month(invalid, today=self.today)

    def test_platform_country_wins_over_misleading_store_name(self) -> None:
        wrong_site = {"store_id": "101", "store_name": "运营甲-印尼046", "platform_name": "Shopee-泰国"}
        self.assertFalse(service.match_stores(["印尼046"], [wrong_site])["can_start"])
        correct_site = {"store_id": "102", "store_name": "测试品牌店", "platform_name": "Shopee-印尼-本土"}
        self.assertTrue(service.match_stores(["测试品牌店"], [correct_site])["can_start"])

    def test_store_matching_supports_exact_name_and_unique_indonesia_shorthand(self) -> None:
        catalog = [
            {"store_id": "101", "store_name": "运营甲-SP印尼046-ID007-本土", "platform_name": "Shopee"},
            {"store_id": "102", "store_name": "运营乙-SP印尼0460-本土", "platform_name": "Shopee"},
            {"store_id": "103", "store_name": "运营丙-Shopee印尼本土008", "platform_name": "虾皮"},
        ]
        matched = service.match_stores(["印尼046", "运营乙-SP印尼0460-本土", "印尼8"], catalog)
        self.assertTrue(matched["can_start"], matched)
        self.assertEqual(matched["issues"], [])
        self.assertEqual(
            [(item["store_id"], item["requested_name"]) for item in matched["stores"]],
            [
                ("101", "印尼046"),
                ("102", "运营乙-SP印尼0460-本土"),
                ("103", "印尼8"),
            ],
        )

    def test_shorthand_046_never_matches_0460_or_other_platforms_or_countries(self) -> None:
        catalog = [
            {"store_id": "201", "store_name": "运营乙-SP印尼0460-本土", "platform_name": "Shopee"},
            {"store_id": "202", "store_name": "运营丙-SP泰国046-本土", "platform_name": "Shopee"},
            {"store_id": "203", "store_name": "运营丁-LZ印尼046-本土", "platform_name": "Lazada"},
            {"store_id": "204", "store_name": "运营戊-TEMU印尼046", "platform_name": "TEMU"},
        ]
        result = service.match_stores(["印尼046"], catalog)
        self.assertFalse(result["can_start"])
        self.assertEqual(result["stores"], [])
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("Shopee", result["issues"][0]["error"])

        only_0460 = [{"store_id": "201", "store_name": "运营乙-SP印尼0460-本土", "platform_name": "Shopee"}]
        res_0460 = service.match_stores(["印尼046"], only_0460)
        self.assertFalse(res_0460["can_start"])
        self.assertEqual(res_0460["stores"], [])
        self.assertIn("未找到", res_0460["issues"][0]["error"])

        for wrong_country in ("泰国046", "菲律宾29", "越南12", "马来05", "运营丙-SP泰国046-本土"):
            with self.subTest(query=wrong_country):
                country_res = service.match_stores([wrong_country], catalog)
                self.assertFalse(country_res["can_start"])
                self.assertIn("印尼", country_res["issues"][0]["error"])

        pure_number = service.match_stores(["046"], only_0460)
        self.assertFalse(pure_number["can_start"])
        self.assertIn("印尼", pure_number["issues"][0]["error"])

    def test_matching_ambiguity_and_duplicate_resolution_fail_closed(self) -> None:
        ambiguous_catalog = [
            {"store_id": "301", "store_name": "运营甲-SP印尼046-A店", "platform_name": "Shopee"},
            {"store_id": "302", "store_name": "运营乙-SP印尼046-B店", "platform_name": "Shopee"},
        ]
        ambiguous = service.match_stores(["印尼046"], ambiguous_catalog)
        self.assertFalse(ambiguous["can_start"])
        self.assertEqual(ambiguous["stores"], [])
        self.assertIn("多个 Shopee 印尼店铺", ambiguous["issues"][0]["error"])
        self.assertEqual(
            ambiguous["issues"][0]["candidates"],
            ["运营甲-SP印尼046-A店", "运营乙-SP印尼046-B店"],
        )

        single_catalog = [ambiguous_catalog[0]]
        duplicate_target = service.match_stores(["印尼046", "运营甲-SP印尼046-A店"], single_catalog)
        self.assertFalse(duplicate_target["can_start"])
        self.assertEqual(len(duplicate_target["issues"]), 1)
        self.assertIn("同一店铺", duplicate_target["issues"][0]["error"])

    def test_run_batch_handles_three_row_six_pdf_store_and_explicit_no_data_store(self) -> None:
        stores = [
            {"store_id": "1", "store_name": "运营甲-SP印尼046", "requested_name": "印尼046", "platform_name": "Shopee"},
            {"store_id": "2", "store_name": "运营乙-SP印尼047", "requested_name": "印尼047", "platform_name": "Shopee"},
        ]
        manifest_path = service.create_batch(self.root, "ziniao-account:fixture", "2026-10", stores)

        def export_store_1(_store: dict, _month: str, attempt_dir: Path) -> dict:
            descriptions = ("Affiliate M.S.", "Shopee invoices", "SVS invoices")
            files = []
            rows = []
            for idx, desc in enumerate(descriptions, 1):
                m_path = attempt_dir / "Master_Invoice" / f"row_{idx:02d}" / f"{desc} - Master (2026-10).pdf"
                t_path = attempt_dir / "Tax_Invoice" / f"row_{idx:02d}" / f"{desc} - Tax #0{idx} (2026-10).pdf"
                m_info = make_valid_pdf(m_path, f"master-{idx}")
                t_info = make_valid_pdf(t_path, f"tax-{idx}")
                files.append({
                    "row_index": idx,
                    "description": f"{desc} October 2026",
                    "amount": "Rp 100.000",
                    "date": "October 2026",
                    "invoice_type": "master_invoice",
                    "invoice_label": "Master Invoice",
                    **m_info,
                })
                files.append({
                    "row_index": idx,
                    "description": f"{desc} October 2026",
                    "amount": "Rp 100.000",
                    "date": "October 2026",
                    "invoice_type": "tax_invoice",
                    "invoice_label": "Tax Invoice",
                    **t_info,
                })
                rows.append({"row_index": idx, "description": f"{desc} October 2026", "amount": "Rp 100.000", "date": "October 2026"})
            return {
                "no_data": False,
                "row_count": 3,
                "file_count": 6,
                "master_invoice_count": 3,
                "tax_invoice_count": 3,
                "files": files,
                "rows": rows,
                "evidence": {"selected_month": "October 2026", "row_count": 3},
            }

        def export_store_2(_store: dict, _month: str, _attempt_dir: Path) -> dict:
            return {
                "no_data": True,
                "row_count": 0,
                "file_count": 0,
                "files": [],
                "rows": [],
                "evidence": {"selected_month": "October 2026", "empty_text": "No receipt found"},
            }

        gateway = FakeInvoiceGateway("ziniao-account:fixture", {"1": export_store_1, "2": export_store_2})
        logs: list[str] = []
        result = service.run_batch(manifest_path, gateway, logs.append)

        self.assertTrue(result["is_complete"])
        self.assertFalse(result["can_retry"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["store_count"], 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["no_data_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["file_count"], 6)
        self.assertEqual(result["master_invoice_count"], 3)
        self.assertEqual(result["tax_invoice_count"], 3)
        self.assertEqual(len(result["output_files"]), 6)
        for file_path in result["output_files"]:
            verified = validate_pdf(file_path)
            self.assertTrue(verified["filename"].endswith(".pdf"))

    def test_partial_download_failure_preserves_valid_pdfs_and_retry_reuses_completed_stores(self) -> None:
        stores = [
            {"store_id": "1", "store_name": "运营甲-SP印尼046", "requested_name": "印尼046", "platform_name": "Shopee"},
            {"store_id": "2", "store_name": "运营乙-SP印尼047", "requested_name": "印尼047", "platform_name": "Shopee"},
        ]
        manifest_path = service.create_batch(self.root, "ziniao-account:fixture", "2026-10", stores)

        def export_store_1_ok(_store: dict, _month: str, attempt_dir: Path) -> dict:
            m_info = make_valid_pdf(attempt_dir / "Master_Invoice" / "row_01" / "Shopee Master.pdf", "m1")
            t_info = make_valid_pdf(attempt_dir / "Tax_Invoice" / "row_01" / "Shopee Tax.pdf", "t1")
            return {
                "no_data": False,
                "row_count": 1,
                "file_count": 2,
                "master_invoice_count": 1,
                "tax_invoice_count": 1,
                "files": [
                    {"row_index": 1, "description": "Shopee invoices October 2026", "date": "October 2026", "invoice_type": "master_invoice", "invoice_label": "Master Invoice", **m_info},
                    {"row_index": 1, "description": "Shopee invoices October 2026", "date": "October 2026", "invoice_type": "tax_invoice", "invoice_label": "Tax Invoice", **t_info},
                ],
                "rows": [{"row_index": 1, "description": "Shopee invoices October 2026", "date": "October 2026"}],
                "evidence": {"selected_month": "October 2026", "row_count": 1},
            }

        def export_store_2_partial_fail(_store: dict, _month: str, attempt_dir: Path) -> dict:
            m_info = make_valid_pdf(attempt_dir / "Master_Invoice" / "row_01" / "Partial Master.pdf", "partial-m1")
            partial = [{
                "row_index": 1,
                "description": "SVS invoices October 2026",
                "date": "October 2026",
                "invoice_type": "master_invoice",
                "invoice_label": "Master Invoice",
                **m_info,
            }]
            raise InvoiceExportError("等待第 1 行 Tax Invoice 下载完成超时", partial_files=partial)

        gateway_1 = FakeInvoiceGateway(
            "ziniao-account:fixture",
            {"1": export_store_1_ok, "2": export_store_2_partial_fail},
        )
        first_run = service.run_batch(manifest_path, gateway_1)
        self.assertFalse(first_run["is_complete"])
        self.assertTrue(first_run["can_retry"])
        self.assertEqual(first_run["status"], "incomplete")
        self.assertEqual(first_run["success_count"], 1)
        self.assertEqual(first_run["failed_count"], 1)

        failed_store = first_run["stores"][1]
        self.assertEqual(failed_store["status"], "failed")
        self.assertEqual(failed_store["file_count"], 1)
        self.assertEqual(failed_store["master_invoice_count"], 1)
        self.assertEqual(failed_store["tax_invoice_count"], 0)
        self.assertTrue(Path(failed_store["files"][0]["path"]).is_file())
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved_manifest["stores"][1]["files"]), 1)

        # Retry: store 1 should be reused without re-exporting, store 2 should succeed.
        gateway_2 = FakeInvoiceGateway(
            "ziniao-account:fixture",
            {"1": AssertionError("Store 1 must not be re-exported"), "2": export_store_1_ok},
        )
        second_run = service.run_batch(manifest_path, gateway_2)
        self.assertTrue(second_run["is_complete"])
        self.assertEqual(gateway_2.exported_stores, ["2"])
        self.assertEqual(second_run["success_count"], 2)
        self.assertEqual(second_run["file_count"], 4)


if __name__ == "__main__":
    unittest.main()

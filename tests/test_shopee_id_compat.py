"""Synthetic regressions for observed Shopee language and availability variants."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from openpyxl import load_workbook
from backend.services import shopee_id_income_report as income
from backend.services import shopee_id_monthly_invoice as service
from tests.shopee_id_fixtures import income_file
from tests.test_shopee_id_income_report import ReportBrowser, history_row
from tests.test_shopee_id_monthly_invoice import FakeInvoiceGateway, make_valid_pdf


def localized_workbook(folder):
    original = Path(income_file(folder)["path"])
    wb = load_workbook(original)
    for cell, text in {"A1": "Laporan Penghasilan", "A5": "Rincian Laporan",
                       "A6": "Username (Penjual)", "A7": "Dari", "A8": "ke"}.items():
        wb["Summary"][cell] = text
    wb["Adjustment"]["A3"], wb["Adjustment"]["C3"] = "Dari", "ke"
    wb["Income"].title = "Penghasilan"
    wb["Penghasilan"]["A1"], wb["Penghasilan"]["K1"] = "Informasi Pesanan", "Rincian Jumlah Pelepasan Dana"
    wb["Seller Fee"]["A2"], wb["Seller Fee"]["B2"] = "No.", "No. Pesanan"
    target = original.with_name(original.name.replace("released", "sudah dilepas"))
    wb.save(target)
    wb.close()
    original.unlink()
    return target


class LocalizedBrowser(ReportBrowser):
    def evaluate(self, store_id, script):
        state = super().evaluate(store_id, script)
        state["history_grid"]["header_columns"] = list(income.INDONESIAN_HISTORY_HEADERS)
        state["income_page"]["active_tab_text"] = "Sudah Dilepas"
        for row in state["history_grid"]["rows"]:
            row["report_type"] = "sudah dilepas"
            row["report_name"] = row["report_name"].replace("released", "sudah dilepas")
            if row["action_text"] == "Processing":
                row["action_text"] = "Sedang Diproses"
        for item in state["income_page"]["exporter_container"]["items"]:
            item["name"] = item["name"].replace("released", "sudah dilepas")
        return state

    def click(self, store_id, selector):
        if selector.startswith("#download-"):
            self.clicks.append(selector)
            localized_workbook(self.download_dir)
        else:
            super().click(store_id, selector)


class ShopeeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_expired_shopee_login_is_diagnosed_without_cross_site_action(self):
        browser = Mock()
        browser.evaluate.return_value = {"url": "https://accounts.shopee.co.id/seller/login?next=private",
                                         "history_grid": {}, "income_page": {}}
        with self.assertRaisesRegex(ValueError, "登录已失效"):
            income._raw_income_state(browser, "synthetic")
        browser.click.assert_not_called()
        browser.evaluate.return_value["url"] = "https://accounts.shopee.co.id.evil.test/seller/login"
        with self.assertRaisesRegex(ValueError, "不是 Shopee 印尼卖家中心"):
            income._raw_income_state(browser, "synthetic")

    def test_adjustment_optional_but_present_period_and_required_details_still_checked(self):
        path = Path(income_file(self.root)["path"])
        wb = load_workbook(path)
        wb.remove(wb["Adjustment"])
        wb.save(path)
        self.assertFalse(income.validate_income_xlsx(path, "2026-10")["adjustment_sheet_present"])
        wb.remove(wb["Income"])
        wb.save(path)
        wb.close()
        with self.assertRaisesRegex(ValueError, "Income"):
            income.validate_income_xlsx(path, "2026-10")
        path = Path(income_file(self.root / "present")["path"])
        wb = load_workbook(path)
        wb["Adjustment"]["D3"] = "2026-10-15"
        wb.save(path)
        wb.close()
        with self.assertRaisesRegex(ValueError, "Adjustment 内部账期"):
            income.validate_income_xlsx(path, "2026-10")

    def test_localized_history_reuse_and_generation_export_at_most_once(self):
        for generate in (False, True):
            with self.subTest(generate=generate):
                browser = LocalizedBrowser([[history_row("2026-09" if generate else "2026-10")]])
                browser.ready_on_refresh = 2
                result = income.export_monthly_income_report(
                    browser, "synthetic", "2026-10", self.root / str(generate),
                    timeout=.3, generation_timeout=.5, download_timeout=.3,
                    poll_interval=.01, refresh_interval=.01)
                self.assertEqual(result["source"], "generated" if generate else "history")
                self.assertEqual(browser.clicks.count("#export"), int(generate))
                self.assertEqual(browser.clicks.count("#download-1"), 1)
                self.assertIn("sudah dilepas", result["filename"])

    def test_localized_filename_does_not_accept_other_site_halfmonth_or_unknown_translation(self):
        valid = "Income.sudah dilepas.id.20261001_20261031.xlsx"
        self.assertTrue(income.is_matching_monthly_income_filename(valid, "2026-10"))
        for name in (valid.replace(".id.", ".my."), valid.replace("20261001", "20261016"),
                     valid.replace("sudah dilepas", "unknown status"), valid.replace(".xlsx", " (1).xlsx")):
            self.assertFalse(income.is_matching_monthly_income_filename(name, "2026-10"))
        path = localized_workbook(self.root)
        wb = load_workbook(path)
        wb["Summary"]["B8"] = "2026-10-15"
        wb.save(path)
        wb.close()
        with self.assertRaisesRegex(ValueError, "内部账期"):
            income.validate_income_xlsx(path, "2026-10")

    def test_missing_tax_requires_explicit_supplement_and_failed_retry_skips_it(self):
        stores = [{"store_id": str(i), "store_name": f"测试-印尼00{i}", "platform_name": "Shopee"} for i in (1, 2)]
        manifest = service.create_batch(self.root, "synthetic", "2026-10", stores)
        missing = True
        def export(store, month, folder):
            files = [income_file(folder / "Income_Report", month)]
            unavailable = store["store_id"] == "1" and missing
            for kind in (("master_invoice",) if unavailable else service.REQUIRED_ACTIONS):
                files.append({**make_valid_pdf(folder / (kind + ".pdf")), "invoice_type": kind, "row_index": 1})
            return {"row_count": 1, "files": files, "evidence": {"unavailable_documents": [{
                "row_index": 1, "invoice_type": "tax_invoice", "reason": "not_provided",
                "observed_actions": ["Master Invoice", "Itemised Billing"],
            }] if unavailable else []}}
        gateway = FakeInvoiceGateway("synthetic", {"1": export, "2": export})
        result = service.run_batch(manifest, gateway)
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["status"], "complete_with_unavailable")
        self.assertEqual((result["success_count"], result["unavailable_count"], result["failed_count"]), (1, 1, 0))
        self.assertFalse(result["can_retry"])
        self.assertTrue(result["can_supplement"])
        self.assertEqual(result["stores"][0]["master_invoice_count"], 1)
        old_files = list(result["output_files"])
        missing = False
        gateway.exported_stores.clear()
        result = service.run_batch(manifest, gateway)
        self.assertEqual(gateway.exported_stores, [])
        self.assertFalse(result["is_complete"])
        result = service.run_batch(manifest, gateway, retry_mode="supplement")
        self.assertTrue(result["is_complete"])
        self.assertEqual(gateway.exported_stores, ["1"])
        self.assertTrue(all(Path(p).is_file() for p in old_files))

    def test_old_v2_success_is_preserved_and_only_failed_store_retried(self):
        stores = [{"store_id": str(i), "store_name": f"测试-印尼00{i}", "platform_name": "Shopee"} for i in (1, 2)]
        manifest = service.create_batch(self.root, "synthetic", "2026-10", stores)
        def export(store, month, folder):
            return {"row_count": 0, "files": [income_file(folder / "Income_Report", month)],
                    "evidence": {"empty_text": "No receipt found"}}
        service.run_batch(manifest, FakeInvoiceGateway("synthetic", {"1": export, "2": ValueError("old failure")}))
        old = json.loads(manifest.read_text(encoding="utf-8"))
        old["schema"] = service.PREVIOUS_SCHEMA
        manifest.write_text(json.dumps(old), encoding="utf-8")
        gateway = FakeInvoiceGateway("synthetic", {"1": export, "2": export})
        result = service.run_batch(manifest, gateway)
        self.assertTrue(result["is_complete"])
        self.assertEqual(gateway.exported_stores, ["2"])
        self.assertEqual(json.loads(manifest.with_name("manifest.v2.json").read_text(encoding="utf-8")), old)

    def test_missing_tax_without_valid_evidence_never_completes(self):
        files = [{"invoice_type": "income_report", "row_index": 0, "path": "income.xlsx"},
                 {"invoice_type": "master_invoice", "row_index": 1, "path": "master.pdf"}]
        entry = {"row_index": 1, "invoice_type": "tax_invoice", "reason": "not_provided", "observed_actions": ["Master Invoice"]}
        service._validate_completeness(files, 1, {"unavailable_documents": [entry]})
        bad = [[], [entry, entry], [{**entry, "row_index": 2}], [{**entry, "invoice_type": "master_invoice"}],
               [{**entry, "reason": "disabled"}], [{**entry, "observed_actions": ["Master Invoice", "Tax Invoice"]}]]
        for entries in bad:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                service._validate_completeness(copy.deepcopy(files), 1, {"unavailable_documents": entries})

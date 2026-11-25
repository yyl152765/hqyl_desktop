from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.output_paths import directory_scope_key
from backend.services.shopee_id_monthly_invoice import load_batch
from launcher.main import resolve_entry_html


ROOT = Path(__file__).resolve().parents[1]


class ShopeeIdMonthlyBridgeTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = BoundAccount(
            "ziniao-1",
            "ziniao",
            "测试公司",
            "operator",
            "private-secret-pwd",
            {"company": "测试公司"},
        )
        self.other = BoundAccount(
            "ziniao-2",
            "ziniao",
            "其他公司",
            "other-user",
            "other-secret-pwd",
            {"company": "其他公司"},
        )
        self.settings = AppSettings(
            output_dir=str(self.root / "global-output"),
            accounts=[self.account, self.other],
            active_account_ids={"ziniao": self.account.id},
        )
        self.saved_updates: list[dict] = []

        def save_config(updates: dict):
            self.saved_updates.append(updates)
            if "tool_output_dirs" in updates:
                self.settings.tool_output_dirs.update(updates["tool_output_dirs"])
            return self.settings

        self.config = SimpleNamespace(
            local_data_dir=self.root,
            load=lambda: self.settings,
            save=save_config,
        )
        with patch.object(app_bridge, "ConfigStore", return_value=self.config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "task-invoice-1"}

        self.profile = account_profile(self.bridge._payload_with_account({}, "ziniao"))
        self.browser = Mock(profile=self.profile)
        self.browser.list_stores.return_value = [
            {"store_id": "101", "store_name": "运营甲-SP印尼046-本土", "platform_name": "Shopee"},
            {"store_id": "102", "store_name": "运营乙-SP印尼047-本土", "platform_name": "Shopee"},
        ]
        factory_patch = patch.object(app_bridge, "ZiniaoBrowser")
        self.factory = factory_patch.start()
        self.factory.return_value.__enter__.return_value = self.browser
        self.addCleanup(factory_patch.stop)

        paths_patch = patch.object(
            app_bridge,
            "ziniao_runtime_paths",
            return_value={"client_path": "C:/ziniao/ziniao.exe", "webdriver_path": "C:/ziniao/drivers"},
        )
        paths_patch.start()
        self.addCleanup(paths_patch.stop)

    def test_production_modules_do_not_import_ziniao_cli_or_lazada_matchers(self) -> None:
        for rel in (
            "backend/services/shopee_id_monthly_invoice.py",
            "backend/services/shopee_id_invoice_gateway.py",
            "backend/services/shopee_id_income_report.py",
        ):
            source = (ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertFalse(
                any("ziniao_cli" in mod or "lazada" in mod for mod in imported),
                f"Forbidden import in {rel}: {imported}",
            )

    def test_bound_account_shorthand_exclusive_task_and_scoped_output_dir(self) -> None:
        scoped_dir = str(self.root / "shopee-id-invoices")
        self.settings.tool_output_dirs[directory_scope_key("shopee_id_monthly_invoice")] = scoped_dir
        info = self.bridge.get_shopee_id_monthly_invoice_info()
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["output_dir"], scoped_dir)
        self.assertEqual(info["profile"], self.profile)

        result = self.bridge.start_shopee_id_monthly_invoice({
            "account_id": self.account.id,
            "username": "spoofed-user",
            "password": "spoofed-password",
            "company": "spoofed-company",
            "month": "2026-10",
            "store_names": "印尼046\n运营乙-SP印尼047-本土",
            "output_dir": "",
        })
        self.assertTrue(result["ok"], result)
        self.factory.assert_not_called()
        self.assertFalse(self.bridge.start_shopee_id_monthly_invoice({})["ok"])
        self.assertFalse(self.bridge.preview_shopee_id_monthly_invoice_stores({})["ok"])
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(app_bridge.shopee_id_monthly_invoice, "run_batch", return_value={"is_complete": True}):
            runner(lambda _: None)
        self.browser.prepare_task.assert_called_once()
        self.browser.list_stores.assert_called_once()
        self.assertEqual(self.factory.call_args.args[0]["password"], "private-secret-pwd")
        self.assertEqual(self.factory.call_args.args[0]["company"], "测试公司")

        call_kwargs = self.bridge.tasks.start.call_args.kwargs
        self.assertEqual(call_kwargs["tool"], "shopee_id_monthly_invoice")
        self.assertTrue(call_kwargs["exclusive"])
        context = call_kwargs["context"]
        self.assertEqual(set(context), {"manifest_path", "profile", "account_id", "month"})
        self.assertEqual(context["account_id"], self.account.id)
        self.assertEqual(context["month"], "2026-10")
        self.assertTrue(Path(context["manifest_path"]).is_relative_to(Path(scoped_dir)))

        manifest_text = Path(context["manifest_path"]).read_text(encoding="utf-8")
        self.assertNotIn("private-secret-pwd", manifest_text)
        self.assertNotIn("private-secret-pwd", json.dumps(context))
        record = load_batch(context["manifest_path"], self.profile)
        self.assertEqual([s["store_id"] for s in record["stores"]], ["101", "102"])
        self.assertEqual(record["stores"][0]["requested_name"], "印尼046")
        self.assertEqual(self.settings.output_dir, str(self.root / "global-output"))

        self.assertFalse(self.bridge._shopee_id_invoice_active)

    def test_unbound_account_requests_ziniao_account_dialog_without_starting_browser(self) -> None:
        self.settings.accounts = []
        self.settings.active_account_ids = {}
        preview = self.bridge.preview_shopee_id_monthly_invoice_stores({
            "month": "2026-10",
            "store_names": "印尼046",
        })
        self.assertFalse(preview["ok"])
        self.assertTrue(preview["requires_account"])
        self.assertEqual(preview["vendor"], "ziniao")

        started = self.bridge.start_shopee_id_monthly_invoice({
            "month": "2026-10",
            "store_names": "印尼046",
            "output_dir": str(self.root),
        })
        self.assertFalse(started["ok"])
        self.assertTrue(started["requires_account"])
        self.assertEqual(started["vendor"], "ziniao")
        self.factory.assert_not_called()

    def test_invalid_payload_never_launches_and_store_mismatch_never_opens_store(self) -> None:
        occupied = self.root / "file.txt"
        occupied.write_text("occupied", encoding="utf-8")
        for bad_payload in (
            {"month": "2026-11", "store_names": "印尼046", "output_dir": str(self.root)},
            {"month": "2027-01", "store_names": "印尼046", "output_dir": str(self.root)},
            {"month": "2026-10", "store_names": "  \n ", "output_dir": str(self.root)},
            {"month": "2026-10", "store_names": "印尼046\n印尼046", "output_dir": str(self.root)},
            {"month": "2026-10", "store_names": "印尼046", "output_dir": str(occupied)},
        ):
            with self.subTest(payload=bad_payload):
                res = self.bridge.start_shopee_id_monthly_invoice(bad_payload)
                self.assertFalse(res["ok"])
        self.factory.assert_not_called()
        self.bridge.tasks.start.assert_not_called()

        for bad_name in ("印尼999", "泰国046", "046"):
            with self.subTest(store_name=bad_name):
                res = self.bridge.start_shopee_id_monthly_invoice({
                    "month": "2026-10",
                    "store_names": bad_name,
                    "output_dir": str(self.root),
                })
                self.assertTrue(res["ok"])
                runner = self.bridge.tasks.start.call_args.args[1]
                with self.assertRaises(ValueError):
                    runner(lambda _: None)
                self.assertFalse(self.bridge._shopee_id_invoice_active)
        self.browser.open_store.assert_not_called()

    def test_runner_and_dispatch_failures_redact_secrets_and_release_lock(self) -> None:
        res = self.bridge.start_shopee_id_monthly_invoice({
            "month": "2026-10",
            "store_names": "印尼046",
            "output_dir": str(self.root),
        })
        self.assertTrue(res["ok"], res)
        runner = self.bridge.tasks.start.call_args.args[1]
        context = self.bridge.tasks.start.call_args.kwargs["context"]

        with patch.object(
            app_bridge,
            "ShopeeIdInvoiceGateway",
            side_effect=RuntimeError("连接异常 private-secret-pwd"),
        ):
            failed_result = runner(lambda *_: None)
        manifest_path = context["manifest_path"]
        self.assertFalse(self.bridge._shopee_id_invoice_active)
        self.assertEqual(failed_result["status"], "failed")
        self.assertNotIn("private-secret-pwd", json.dumps(failed_result, ensure_ascii=False))
        self.assertNotIn("private-secret-pwd", Path(manifest_path).read_text(encoding="utf-8"))

        # Exclusive task rejection releases lock and fails batch cleanly.
        self.bridge.tasks.start.return_value = {"ok": False, "error": "已有任务正在运行"}
        self.factory.reset_mock()
        rejected = self.bridge.start_shopee_id_monthly_invoice({
            "month": "2026-10",
            "store_names": "印尼046",
            "output_dir": str(self.root),
        })
        self.assertFalse(rejected["ok"])
        self.assertFalse(self.bridge._shopee_id_invoice_active)
        self.factory.assert_not_called()

    def test_cross_account_progress_and_retry_are_rejected(self) -> None:
        res = self.bridge.start_shopee_id_monthly_invoice({
            "month": "2026-10",
            "store_names": "印尼046",
            "output_dir": str(self.root),
        })
        self.assertTrue(res["ok"])
        context = self.bridge.tasks.start.call_args.kwargs["context"]
        with patch.object(app_bridge.shopee_id_monthly_invoice, "run_batch", return_value={}):
            self.bridge.tasks.start.call_args.args[1](lambda _: None)

        self.assertTrue(self.bridge.get_shopee_id_monthly_invoice_progress(context)["ok"])
        self.assertFalse(
            self.bridge.get_shopee_id_monthly_invoice_progress(
                {**context, "account_id": self.other.id}
            )["ok"]
        )
        self.assertFalse(
            self.bridge.retry_shopee_id_monthly_invoice(
                {**context, "account_id": self.other.id}
            )["ok"]
        )

    def test_launcher_navigation_and_frontend_script_contract(self) -> None:
        self.assertEqual(
            resolve_entry_html(["--page=shopee-id-monthly-invoice"]).name,
            "shopee-id-monthly-invoice.html",
        )
        with patch.object(app_bridge, "get_group_options", return_value=[]):
            tools = self.bridge.get_app_info()["tools"]
        self.assertTrue(any(item["key"] == "shopee_id_monthly_invoice" for item in tools))

        for rel in ("frontend/assets/common.js", "frontend/assets/pages/dashboard.js"):
            self.assertIn("shopee-id-monthly-invoice.html", (ROOT / rel).read_text(encoding="utf-8"))

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const elements = new Map();
function element() {
  return {
    value: '', textContent: '', className: '', disabled: false, hidden: false,
    listeners: {}, childNodes: [],
    addEventListener(n, f) { this.listeners[n] = f; },
    appendChild(e) { this.childNodes.push(e); },
    replaceChildren() { this.childNodes = []; }
  };
}
const e = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
global.document = { createElement: element };
let page, account = 'ziniao-1', sentStart, sentPreview, savedDir;
global.HQYL = {
  $: e,
  activeAccount: () => ({ id: account }),
  boot: p => { page = p; },
  showToast() {},
  openAccountDialog() { throw new Error('unexpected account dialog'); },
  api: () => ({
    get_shopee_id_monthly_invoice_info: async () => ({
      ok: true, default_month: '2026-10', output_dir: 'D:/ShopeeIDInvoices',
      client_path: 'C:/ziniao.exe', webdriver_path: 'C:/drivers', profile: 'p1'
    }),
    save_scoped_output_dirs: async p => { savedDir = p; return { ok: true }; },
    preview_shopee_id_monthly_invoice_stores: async p => {
      sentPreview = p;
      return { ok: true, stores: [{ store_id: '101', store_name: '运营甲-SP印尼046', requested_name: '印尼046' }], issues: [] };
    },
    start_shopee_id_monthly_invoice: async p => {
      sentStart = p;
      return { ok: true, context: { account_id: p.account_id, profile: 'p1', manifest_path: 'D:/m/manifest.json' } };
    },
    open_path: async () => ({ ok: true }),
    get_shopee_id_monthly_invoice_progress: async () => ({ ok: true, result: { profile: 'p1', stores: [] } })
  }),
  startTask: async fn => { await fn(); }
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  await page.init();
  assert.equal(e('invoiceMonth').value, '2026-10');
  assert.equal(e('outputDir').value, 'D:/ShopeeIDInvoices');
  assert.equal(e('startBtn').disabled, true);
  e('storeNames').value = '印尼046';
  e('storeNames').listeners.input();
  assert.equal(e('startBtn').disabled, false);
  assert.equal(e('previewBtn').disabled, false);
  e('previewBtn').listeners.click();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(sentPreview.store_names, '印尼046');
  assert.equal(e('previewBox').hidden, false);
  e('exportForm').listeners.submit({ preventDefault() {} });
  await new Promise(r => setTimeout(r, 0));
  assert.equal(sentStart.month, '2026-10');
  assert.equal(sentStart.store_names, '印尼046');
  assert.deepEqual(savedDir, { tool: 'shopee_id_monthly_invoice', output_dir: 'D:/ShopeeIDInvoices' });
  page.applyResult({
    manifest_path: 'D:/m/manifest.json', profile: 'p1', month: '2026-10',
    run_id: 'run-1', status: 'incomplete', is_complete: false, can_retry: true,
    store_count: 2, completed_count: 1, no_data_count: 1, failed_count: 1,
    file_count: 3, income_report_count: 2, master_invoice_count: 1, tax_invoice_count: 0,
    output_dir: 'D:/m',
    stores: [
      { store_id: '101', store_name: '甲-SP印尼046', requested_name: '印尼046', status: 'success', row_count: 0, income_report_count: 1, master_invoice_count: 0, tax_invoice_count: 0, evidence: { empty_text: 'No receipt found' } },
      { store_id: '102', store_name: '乙-SP印尼047', requested_name: '印尼047', status: 'failed', error: 'Verify 弹窗未关闭', income_report_count: 1, master_invoice_count: 1, tax_invoice_count: 0 }
    ]
  });
  assert.equal(e('retryBtn').disabled, false);
  assert.equal(e('storeResults').childNodes.length, 2);
  assert.equal(e('recordCount').textContent, '3');
  assert.equal(e('invoiceBreakdown').textContent, '收入 Excel 2 · Master 1 · Tax 0');
  assert.equal(e('storeResults').childNodes[0].childNodes.length, 8);
  assert.match(e('storeResults').childNodes[0].childNodes[7].textContent, /当月无税票/);
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
        proc = subprocess.run(
            [node, "-e", script, str(ROOT / "frontend/assets/pages/shopee-id-monthly-invoice.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

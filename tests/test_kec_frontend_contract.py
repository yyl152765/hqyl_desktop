"""KEC page and bridge contracts with in-memory accounts and an offline JS VM."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services.kec_reconciliation import (
    DEFAULT_MABANG_BASE_URL,
    KecReconciliationJob,
    KecReconciliationResult,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class KecBridgeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = BoundAccount(
            id="kec-fixture-account", vendor="mabang", name="离线测试",
            username="fixture-user", password="fixture-platform-password",
        )
        settings = AppSettings(
            output_dir="C:/FixtureOutput", accounts=[self.account],
            active_account_ids={"mabang": self.account.id},
        )
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = SimpleNamespace(load=lambda: settings, save=Mock())
        self.bridge.tasks = SimpleNamespace(start=Mock(return_value={"ok": True, "id": "fixture-task"}))

    def test_info_supplies_domain_without_exposing_password(self) -> None:
        info = self.bridge.get_kec_reconciliation_info()
        self.assertTrue(info["ok"])
        self.assertEqual(info["default_base_url"], DEFAULT_MABANG_BASE_URL)
        self.assertIn("实际体积", info["basis"])
        self.assertNotIn(self.account.password, json.dumps(info, ensure_ascii=False))

    def test_start_injects_platform_credentials_and_restorable_nonsecret_context(self) -> None:
        custom_domain = "https://fixture.private.mabangerp.com:9443"
        job = KecReconciliationJob(
            username=self.account.username, password=self.account.password,
            input_file=Path("C:/Fixture/bill.xlsx"), quote_file=Path("C:/Fixture/quote.xlsx"),
            output_dir=Path("C:/FixtureOutput"), base_url=custom_domain,
        )
        expected = KecReconciliationResult(
            output_file=job.output_dir / "bill_核对结果.xlsx",
            avg_daily_orders=Decimal(2500), outbound_base_fee=Decimal("1.4"),
            summary={"仓储费_账单金额": Decimal(3), "仓储费_核对金额": Decimal("1.5")},
        )
        with patch("backend.app_bridge.validate_kec_reconciliation_payload", return_value=job) as validate, patch(
            "backend.app_bridge.run_kec_reconciliation", return_value=expected,
        ) as run:
            response = self.bridge.start_kec_reconciliation({
                "account_id": self.account.id, "input_file": str(job.input_file),
                "quote_file": str(job.quote_file), "output_dir": str(job.output_dir),
                "base_url": custom_domain, "username": "forged-user", "password": "forged-password",
            })
            self.assertTrue(response["ok"])
            request = validate.call_args.args[0]
            self.assertEqual(request["username"], self.account.username)
            self.assertEqual(request["password"], self.account.password)
            self.assertEqual(request["base_url"], custom_domain)
            context = self.bridge.tasks.start.call_args.kwargs["context"]
            self.assertEqual(context, {
                "account_id": self.account.id, "input_file": str(job.input_file),
                "quote_file": str(job.quote_file), "output_dir": str(job.output_dir),
                "base_url": custom_domain,
            })
            self.assertNotIn(self.account.password, json.dumps(context))
            self.assertNotIn("username", context)
            self.bridge.config_store.save.assert_called_once_with({"output_dir": str(job.output_dir)})
            self.assertEqual(self.bridge.tasks.start.call_args.kwargs["tool"], "kec_reconciliation")
            result = self.bridge.tasks.start.call_args.args[1](lambda _message: None)
            run.assert_called_once_with(job, unittest.mock.ANY)
            self.assertEqual(result["output_file"], str(expected.output_file))
            self.assertEqual(result["storage_difference"], 1.5)
            self.assertEqual(result["outbound_base_fee"], 1.4)

    def test_other_vendor_account_cannot_start_kec(self) -> None:
        self.bridge.config_store.load = lambda: AppSettings(
            accounts=[BoundAccount(id="wrong-vendor", vendor="ziniao", name="fixture", username="u", password="p")],
        )
        with patch("backend.app_bridge.validate_kec_reconciliation_payload") as validate:
            response = self.bridge.start_kec_reconciliation({"account_id": "wrong-vendor"})
        self.assertFalse(response["ok"])
        self.assertTrue(response["requires_account"])
        self.assertEqual(response["vendor"], "mabang")
        validate.assert_not_called()
        self.bridge.tasks.start.assert_not_called()


class KecFrontendMigrationTests(unittest.TestCase):
    def test_visible_rules_explain_quote_precedence_and_piece_boundary(self) -> None:
        html = (FRONTEND / "pages/kec-reconciliation.html").read_text(encoding="utf-8")
        self.assertIn('id="kecBaseUrl"', html)
        self.assertIn(".xlsx / .xlsm", html)
        self.assertIn("60 天内免租", html)
        self.assertIn("超过 60 天 1.5", html)
        self.assertIn("首票 1.4", html)
        self.assertIn("首票 1.3", html)
        self.assertIn("首票 1.2", html)
        self.assertIn("前 19 件", html)
        self.assertIn("第 20 件起，仅该部分每件 0.3", html)
        self.assertIn("2 元/单含 3 件", html)
        self.assertIn("1.1 元/件", html)
        self.assertIn("方数向上取整", html)
        self.assertIn("本页新条款优先", html)
        self.assertNotIn("其他费用口径不变", html)
        self.assertNotIn("提供报价表时按表内规则核对", html)

    def test_domain_validation_account_scoping_and_parameter_restore_offline(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the offline page contract")
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[1];
const html = fs.readFileSync(path.join(root, 'pages/kec-reconciliation.html'), 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, style: {}, handlers: {}, children: [],
  addEventListener(name, handler) { this.handlers[name] = handler; },
  appendChild(child) { this.children.push(child); },
}]));
const calls = [], notices = [], prompts = [], logs = [], opened = [];
let page, account = { id: 'fixture-account' };
const defaultDomain = 'https://default-fixture.private.mabangerp.com';
const bridge = {
  async get_kec_reconciliation_info() { return { ok: true, default_base_url: defaultDomain }; },
  async start_kec_reconciliation(payload) { calls.push(JSON.parse(JSON.stringify(payload))); return { ok: true }; },
};
const HQYL = {
  $: (id) => nodes[id], api: () => bridge, boot: (value) => { page = value; },
  activeAccount: () => account, showToast: (value) => notices.push(value), appendLog: (value) => logs.push(value),
  openAccountDialog: (vendor) => prompts.push(vendor), openOutput: (value) => opened.push(value),
  async startTask(factory) { return await factory(); },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/pages/kec-reconciliation.js'), 'utf8'), {
  HQYL, URL, document: { createElement() { return { textContent: '', style: {}, children: [], appendChild(c) { this.children.push(c); } }; } },
});
(async () => {
  await page.init({ settings: { output_dir: 'C:/GlobalOutput' } });
  assert.equal(nodes.kecBaseUrl.value, defaultDomain);
  assert.equal(page.taskContextFilter({ account_id: account.id }), true);
  assert.equal(page.taskContextFilter({ account_id: 'another-account' }), false);
  page.onTaskRestored({ context: { input_file: 'C:/Restored/bill.xlsm', quote_file: 'C:/Restored/quote.xlsx',
    output_dir: 'C:/RestoredOutput', base_url: 'https://restored.private.mabangerp.com:9443',
    username: 'must-not-restore', password: 'must-not-restore' } });
  assert.equal(nodes.kecBillFile.value, 'C:/Restored/bill.xlsm');
  assert.equal(nodes.kecQuoteFile.value, 'C:/Restored/quote.xlsx');
  assert.equal(nodes.kecOutputDir.value, 'C:/RestoredOutput');
  assert.equal(nodes.kecBaseUrl.value, 'https://restored.private.mabangerp.com:9443');
  assert.match(nodes.kecQuoteHint.textContent, /新条款优先/);

  const submit = () => nodes.kecReconciliationForm.handlers.submit({ preventDefault() {} });
  for (const invalid of ['file:///C:/fixture', 'ftp://fixture.invalid', 'https://user:pass@fixture.invalid',
    'https://fixture.invalid/path', 'https://fixture.invalid?query=1', 'https://fixture.invalid#fragment',
    'not-a-domain', 'https:///fixture.invalid']) {
    nodes.kecBaseUrl.value = invalid;
    await submit();
    assert.equal(calls.length, 0, invalid);
    assert.match(notices.at(-1), /马帮 http\(s\) 域名/);
  }
  nodes.kecBaseUrl.value = 'https://custom.private.mabangerp.com:9443/';
  account = null;
  await submit();
  assert.deepEqual(prompts, ['mabang']);
  account = { id: 'fixture-account' };
  await submit();
  assert.deepEqual(calls[0], { account_id: account.id, input_file: 'C:/Restored/bill.xlsm',
    quote_file: 'C:/Restored/quote.xlsx', output_dir: 'C:/RestoredOutput',
    base_url: 'https://custom.private.mabangerp.com:9443' });
  assert.match(logs.at(-1), /新条款优先/);
  assert.equal('password' in calls[0], false);
  assert.equal('username' in calls[0], false);

  nodes.kecBillFile.value = 'C:/Chosen/bill.xlsx';
  nodes.kecQuoteFile.value = 'C:/Chosen/quote.xlsx';
  nodes.kecOutputDir.value = 'C:/ChosenOutput';
  page.onTaskRestored({ context: { input_file: 'C:/Other/bill.xlsx', quote_file: 'C:/Other/quote.xlsx',
    output_dir: 'C:/OtherOutput', base_url: 'https://other.private.mabangerp.com' } });
  assert.equal(nodes.kecBillFile.value, 'C:/Chosen/bill.xlsx');
  assert.equal(nodes.kecQuoteFile.value, 'C:/Chosen/quote.xlsx');
  assert.equal(nodes.kecOutputDir.value, 'C:/ChosenOutput');
  assert.equal(nodes.kecBaseUrl.value, 'https://custom.private.mabangerp.com:9443/');
  page.setRunning(true);
  assert.equal(nodes.kecBaseUrl.disabled, true);
  page.setRunning(false);
  assert.equal(nodes.kecBaseUrl.disabled, false);
  nodes.kecBaseUrl.value = '';
  nodes.kecQuoteFile.value = '';
  await submit();
  assert.equal(calls[1].base_url, defaultDomain);
  assert.equal(calls[1].quote_file, '');
  assert.match(logs.at(-1), /内置报价和本页新条款/);

  page.applyResult({ output_file: 'C:/ChosenOutput/bill_核对结果.xlsx', avg_daily_orders: 2500,
    outbound_base_fee: 1.4, storage_billed_amount: 3, storage_expected_amount: 1.5,
    summary: { '仓储费_账单金额': 3, '仓储费_核对金额': 1.5 } });
  assert.equal(nodes.outputFile.textContent, 'bill_核对结果.xlsx');
  assert.equal(nodes.kecOpenFileBtn.disabled, false);
  await nodes.kecOpenFileBtn.handlers.click();
  assert.equal(opened.at(-1), 'C:/ChosenOutput/bill_核对结果.xlsx');
  page.resetResult();
  assert.equal(nodes.kecQuoteFile.value, '');
  assert.equal(nodes.kecBaseUrl.value, '');
  assert.equal(nodes.kecResult.hidden, true);
  console.log('KEC migration offline page contracts passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            [node, "-e", script, str(FRONTEND)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()

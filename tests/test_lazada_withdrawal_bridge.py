"""Offline coverage for saved DingTalk settings reaching withdrawal uploads."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount, DingTalkSettings, DingTalkUserBinding
from backend.services import lazada_withdrawal_statistics as service


ROOT = Path(__file__).resolve().parents[1]


class LazadaWithdrawalBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = BoundAccount(
            "fixture-account", "ziniao", "测试公司", "fixture-login", "fixture-password",
            {"company": "测试公司"},
        )
        self.settings = AppSettings(
            output_dir="fixture-output",
            accounts=[self.account],
            active_account_ids={"ziniao": self.account.id},
            dingtalk=DingTalkSettings(
                app_key="fixture-app-key", app_secret="fixture-app-secret",
                users=[DingTalkUserBinding("操作人甲", "fixture-user-a")],
            ),
        )
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = SimpleNamespace(load=lambda: self.settings)
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-withdrawal-task"}
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("offline test attempted network"))
        self.network = network.start()
        self.addCleanup(network.stop)

    def payload(self, **extra):
        return {
            "country": "MY", "start_date": "2026-09-01", "end_date": "2026-09-30",
            "store_names": "测试店铺", **extra,
        }

    def assert_no_credentials(self, result) -> None:
        serialized = json.dumps(result, ensure_ascii=False)
        for value in ("fixture-app-key", "fixture-app-secret", "fixture-user-a", "fixture-user-b", self.account.password):
            self.assertNotIn(value, serialized)

    def started_job(self):
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(app_bridge, "run_lazada_withdrawal_statistics", return_value={"success": True}) as run:
            runner(Mock())
        return run.call_args.args[0]

    def test_info_returns_status_names_and_unique_default_without_credentials(self) -> None:
        with patch.object(app_bridge, "resolve_lazada_withdrawal_runtime_paths", return_value={"client_path": "fixture-client", "webdriver_path": "fixture-driver"}):
            result = self.bridge.get_lazada_withdrawal_statistics_info()
        self.assertTrue(result["ok"])
        self.assertTrue(result["dingtalk_configured"])
        self.assertEqual(result["operator_names"], ["操作人甲"])
        self.assertEqual(result["default_dingtalk_operator_name"], "操作人甲")
        self.assertEqual(result["client_path"], "fixture-client")
        self.assertEqual(result["webdriver_path"], "fixture-driver")
        self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()
        self.network.assert_not_called()

    def test_info_loads_without_accounts_saved_dingtalk_or_installed_runtime(self) -> None:
        self.settings.accounts = []
        self.settings.active_account_ids = {}
        self.settings.dingtalk = DingTalkSettings()
        with patch.object(app_bridge, "resolve_lazada_withdrawal_runtime_paths", side_effect=RuntimeError("未安装紫鸟")):
            result = self.bridge.get_lazada_withdrawal_statistics_info()
        self.assertTrue(result["ok"])
        self.assertFalse(result["dingtalk_configured"])
        self.assertEqual(result["operator_names"], [])
        self.assertEqual(result["default_dingtalk_operator_name"], "")
        self.assertEqual((result["client_path"], result["webdriver_path"]), ("", ""))
        self.assertEqual(len(result["countries"]), 3)
        self.assertTrue(result["period"]["start_date"])
        self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()

    def test_info_multiple_operators_has_no_arbitrary_default(self) -> None:
        self.settings.dingtalk.users.append(DingTalkUserBinding("操作人乙", "fixture-user-b"))
        with patch.object(app_bridge, "resolve_lazada_withdrawal_runtime_paths", return_value={}):
            result = self.bridge.get_lazada_withdrawal_statistics_info()
        self.assertEqual(result["operator_names"], ["操作人甲", "操作人乙"])
        self.assertEqual(result["default_dingtalk_operator_name"], "")
        self.assert_no_credentials(result)

    def test_start_overrides_payload_credentials_with_saved_settings(self) -> None:
        payload = self.payload(
            username="forged", password="forged", company="forged",
            dingtalk_operator_name="操作人甲", dingtalk_app_key="forged",
            dingtalk_app_secret="forged", dingtalk_user_id="forged",
        )
        result = self.bridge.start_lazada_withdrawal_statistics(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.bridge.tasks.start.call_args.kwargs["tool"], "lazada_withdrawal_statistics")
        job = self.started_job()
        self.assertEqual((job.username, job.password, job.company), (self.account.username, self.account.password, "测试公司"))
        self.assertEqual((job.dingtalk_app_key, job.dingtalk_app_secret, job.dingtalk_user_id), ("fixture-app-key", "fixture-app-secret", "fixture-user-a"))
        self.assertEqual(job.output_root, "fixture-output")
        self.assertEqual(payload["dingtalk_app_secret"], "forged")
        self.assert_no_credentials(result)
        self.network.assert_not_called()

    def test_one_operator_is_selected_automatically(self) -> None:
        result = self.bridge.start_lazada_withdrawal_statistics(self.payload())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.started_job().dingtalk_user_id, "fixture-user-a")

    def test_saved_credentials_reach_drive_client_when_legacy_constants_are_empty(self) -> None:
        self.assertTrue(self.bridge.start_lazada_withdrawal_statistics(self.payload())["ok"])
        job = self.started_job()
        legacy = ModuleType("util.dingtalk_doc_api")
        legacy.APP_KEY = legacy.APP_SECRET = legacy.USER_ID = ""
        drive = ModuleType("util.dingtalk_drive_util")
        drive.DingDriveClient = Mock()
        package = ModuleType("util")
        package.__path__ = []
        with (
            patch.object(service, "_ensure_superbrowser_path"),
            patch.dict("sys.modules", {"util": package, "util.dingtalk_doc_api": legacy, "util.dingtalk_drive_util": drive}),
            patch.dict("os.environ", {
                "LAZADA_WITHDRAWAL_DINGTALK_APP_KEY": "",
                "LAZADA_WITHDRAWAL_DINGTALK_APP_SECRET": "",
                "LAZADA_WITHDRAWAL_DINGTALK_USER_ID": "",
            }),
        ):
            client = service._create_drive_client(job)
        self.assertIs(client, drive.DingDriveClient.return_value)
        self.assertEqual(drive.DingDriveClient.call_args.args[:3], ("fixture-app-key", "fixture-app-secret", "fixture-user-a"))
        self.network.assert_not_called()

    def test_multiple_operators_require_explicit_selection_before_start(self) -> None:
        self.settings.dingtalk.users.append(DingTalkUserBinding("操作人乙", "fixture-user-b"))
        result = self.bridge.start_lazada_withdrawal_statistics(self.payload())
        self.assertFalse(result["ok"])
        self.assertIn("操作人", result["error"])
        self.bridge.tasks.start.assert_not_called()
        result = self.bridge.start_lazada_withdrawal_statistics(self.payload(dingtalk_operator_name="操作人乙"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.started_job().dingtalk_user_id, "fixture-user-b")

    def test_unknown_operator_and_empty_user_id_cannot_be_replaced_by_payload(self) -> None:
        for users, operator in (
            (self.settings.dingtalk.users, "未知操作人"),
            ([], ""),
            ([DingTalkUserBinding("操作人甲", "")], "操作人甲"),
        ):
            with self.subTest(operator=operator, count=len(users)):
                self.settings.dingtalk.users = users
                result = self.bridge.start_lazada_withdrawal_statistics(self.payload(dingtalk_operator_name=operator, dingtalk_user_id="forged"))
                self.assertFalse(result["ok"])
                self.assertIn("操作人", result["error"])
                self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()

    def test_missing_settings_cannot_be_replaced_by_payload_or_environment(self) -> None:
        for key, secret in (("", "fixture-app-secret"), ("fixture-app-key", ""), (" \t", "fixture-app-secret")):
            with self.subTest(key_configured=bool(key.strip()), secret_configured=bool(secret)):
                self.settings.dingtalk.app_key = key
                self.settings.dingtalk.app_secret = secret
                with patch.dict("os.environ", {
                    "LAZADA_WITHDRAWAL_DINGTALK_APP_KEY": "forged-env-key",
                    "LAZADA_WITHDRAWAL_DINGTALK_APP_SECRET": "forged-env-secret",
                    "LAZADA_WITHDRAWAL_DINGTALK_USER_ID": "forged-env-user",
                }):
                    result = self.bridge.start_lazada_withdrawal_statistics(self.payload(dingtalk_app_key="forged", dingtalk_app_secret="forged"))
                self.assertFalse(result["ok"])
                self.assertIn("设置", result["error"])
                self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()

    def test_missing_or_unknown_bound_account_is_rejected_before_start(self) -> None:
        for account_id in ("unknown-account", self.account.id):
            with self.subTest(account_id=account_id):
                if account_id == self.account.id:
                    self.settings.accounts = []
                    self.settings.active_account_ids = {}
                result = self.bridge.start_lazada_withdrawal_statistics(self.payload(account_id=account_id, username="forged", password="forged"))
                self.assertFalse(result["ok"])
                self.assertTrue(result["requires_account"])
                self.assertEqual(result["vendor"], "ziniao")
        self.bridge.tasks.start.assert_not_called()


class LazadaWithdrawalOperatorFrontendTests(unittest.TestCase):
    def test_page_exposes_operator_selection_and_configuration_link(self) -> None:
        html = (ROOT / "frontend/pages/lazada-withdrawal-statistics.html").read_text(encoding="utf-8")
        self.assertIn('id="lazadaDingtalkOperator" required', html)
        self.assertIn('id="lazadaDingtalkStatus"', html)
        self.assertIn('href="settings.html?focus=dingtalk"', html)

    def test_unique_operator_default_and_explicit_multi_operator_reach_payload(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for frontend behavior checks")
        script = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) {
    const value = { value: "", options: [], listeners: {}, disabled: false,
      classList: { toggle() {} },
      appendChild(child) { this.options.push(child); },
      addEventListener(type, action) { this.listeners[type] = action; } };
    Object.defineProperty(value, "textContent", { set(text) { this.text = text; this.options = []; }, get() { return this.text || ""; } });
    elements.set(id, value);
  }
  return elements.get(id);
};
let page;
let requested;
let info = { ok: true, countries: [{ code: "MY", name: "马来西亚" }], period: {},
  dingtalk_configured: true, operator_names: ["甲"], default_dingtalk_operator_name: "甲" };
const api = { get_lazada_withdrawal_statistics_info: async () => info,
  start_lazada_withdrawal_statistics: async (payload) => { requested = payload; return { ok: true }; } };
const context = { document: { createElement: () => ({}) }, HQYL: {
  $: element, boot: (value) => { page = value; }, api: () => api,
  activeAccount: () => ({ id: "fixture-account", username: "fixture-user", extra: {} }),
  startTask: async (start) => start(), appendLog() {}, showToast() {},
} };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
(async () => {
  await page.init({ settings: {} });
  assert.equal(element("lazadaDingtalkOperator").value, "甲");
  element("lazadaWithdrawalForm").listeners.submit({ preventDefault() {} });
  await Promise.resolve();
  assert.equal(requested.dingtalk_operator_name, "甲");
  assert.equal(Object.keys(requested).filter((key) => /dingtalk_(app_key|app_secret|user_id)$/.test(key)).length, 0);
  info = { ...info, operator_names: ["乙", "丙"], default_dingtalk_operator_name: "" };
  await element("lazadaRefreshBtn").listeners.click();
  assert.equal(element("lazadaDingtalkOperator").value, "");
  element("lazadaDingtalkOperator").value = "丙";
  element("lazadaWithdrawalForm").listeners.submit({ preventDefault() {} });
  await Promise.resolve();
  assert.equal(requested.dingtalk_operator_name, "丙");
  page.setRunning(true);
  assert.equal(element("lazadaDingtalkOperator").disabled, true);
  info = { ...info, dingtalk_configured: false, operator_names: [] };
  await element("lazadaRefreshBtn").listeners.click();
  assert.match(element("lazadaDingtalkStatus").textContent, /设置/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "withdrawal-operators.cjs"
            harness.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [node, str(harness), str(ROOT / "frontend/assets/pages/lazada-withdrawal-statistics.js")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

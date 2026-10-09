"""Balance bridge, account boundary, concurrency and desktop entry contracts."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.services import balance_statistics
from launcher.main import resolve_entry_html


ROOT = Path(__file__).resolve().parents[1]


class BalanceStatisticsBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = BoundAccount("ziniao-a", "ziniao", "公司 A", "operator-a", "private-a", {"company": "公司 A"})
        self.other = BoundAccount("ziniao-b", "ziniao", "公司 B", "operator-b", "private-b", {"company": "公司 B"})
        self.settings = AppSettings(output_dir=str(self.root), accounts=[self.account, self.other], active_account_ids={"ziniao": self.account.id})
        config = SimpleNamespace(load=lambda: self.settings, local_data_dir=self.root)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-task"}
        self.browser_guard = patch.object(balance_statistics, "ZiniaoBrowser").start()
        self.addCleanup(patch.stopall)

    def payload(self, platform="temu", **extra):
        return {"platform": platform, "month": "2026-08", "store_names": "店铺 A\n店铺 A\n店铺 B", **extra}

    def manifest(self, platform="temu"):
        path = self.root / "manifest.json"
        profile = account_profile(self.bridge._payload_with_account({"account_id": self.account.id}, "ziniao"))
        path.write_text(json.dumps({
            "schema": balance_statistics.SCHEMA, "profile": profile, "platform": platform,
            "stores": [{"store_id": "fixture-store", "store_name": "店铺 A", "status": "failed", "evidence": {}}],
        }), encoding="utf-8")
        return path

    def test_info_uses_local_defaults_without_account_or_ziniao_and_tolerates_missing_runtime(self):
        self.settings.accounts = []
        self.settings.active_account_ids = {}
        with patch.object(app_bridge, "ziniao_runtime_paths", side_effect=RuntimeError("未安装客户端")), patch.object(app_bridge, "ZiniaoBrowser") as browser:
            result = self.bridge.get_balance_statistics_info({"platform": "lazada"})
        self.assertTrue(result["ok"])
        self.assertRegex(result["month"], r"^\d{4}-\d{2}$")
        self.assertEqual({country["code"] for country in result["countries"]}, {"AUTO", "PH", "MY", "TH", "ID", "VN", "SG"})
        self.assertEqual(result["output_dir"], str(self.root))
        self.assertEqual(result["client_path"], "")
        self.assertNotIn("password", json.dumps(result))
        browser.assert_not_called()
        self.browser_guard.assert_not_called()
        self.bridge.tasks.start.assert_not_called()

    def test_start_uses_bound_credentials_and_task_context_has_only_public_identity(self):
        payload = self.payload(account_id=self.other.id, username="forged", password="forged", company="forged")
        self.assertTrue(self.bridge.start_balance_statistics(payload)["ok"])
        call = self.bridge.tasks.start.call_args
        self.assertEqual(call.kwargs, {"tool": "temu_balance_statistics", "context": {"account_id": self.other.id, "platform": "temu"}})
        progress = Mock()
        with patch.object(balance_statistics, "run_balance_statistics", return_value={"is_complete": True}) as run:
            result = call.args[1](progress)
        request = run.call_args.args[0]
        self.assertEqual((request["username"], request["password"], request["company"]), ("operator-b", "private-b", "公司 B"))
        self.assertEqual(request["store_names"], ["店铺 A", "店铺 B"])
        self.assertEqual(request["output_root"], str(self.root))
        self.assertEqual(result, {"is_complete": True, "account_id": self.other.id})
        self.assertFalse(self.bridge._balance_statistics_active)
        self.browser_guard.assert_not_called()

    def test_invalid_request_and_missing_account_do_not_start_or_hold_the_gate(self):
        for payload in (self.payload(platform="unknown"), self.payload(store_names=""), self.payload(month="2026-99"), self.payload(manifest_path="other.json")):
            with self.subTest(payload=payload):
                self.assertFalse(self.bridge.start_balance_statistics(payload)["ok"])
                self.assertFalse(self.bridge._balance_statistics_active)
        self.settings.active_account_ids = {}
        self.settings.accounts = []
        result = self.bridge.start_balance_statistics(self.payload())
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "ziniao")
        self.bridge.tasks.start.assert_not_called()
        self.browser_guard.assert_not_called()

    def test_temu_and_lazada_share_one_gate_even_for_simultaneous_starts(self):
        gate = threading.Event()

        def launch(platform):
            gate.wait(2)
            return self.bridge.start_balance_statistics(self.payload(platform))

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(launch, platform) for platform in ("temu", "lazada")]
            gate.set()
            results = [task.result(3) for task in tasks]
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.bridge.tasks.start.assert_called_once()
        self.assertTrue(self.bridge._balance_statistics_active)
        self.assertIn("已有余额统计任务", next(result["error"] for result in results if not result["ok"]))
        self.assertFalse(self.bridge.retry_balance_statistics({"platform": "temu", "manifest_path": "not-opened.json"})["ok"])

    def test_runner_and_task_start_failures_release_gate(self):
        self.assertTrue(self.bridge.start_balance_statistics(self.payload())["ok"])
        runner = self.bridge.tasks.start.call_args.args[1]
        with patch.object(balance_statistics, "run_balance_statistics", side_effect=RuntimeError("采集失败")):
            with self.assertRaisesRegex(RuntimeError, "采集失败"):
                runner(Mock())
        self.assertFalse(self.bridge._balance_statistics_active)
        self.bridge.tasks.start.side_effect = RuntimeError("无法启动线程")
        self.assertFalse(self.bridge.start_balance_statistics(self.payload("lazada"))["ok"])
        self.assertFalse(self.bridge._balance_statistics_active)
        self.bridge.tasks.start.side_effect = None
        self.bridge.tasks.start.return_value = {"ok": False, "error": "任务已存在"}
        self.assertFalse(self.bridge.start_balance_statistics(self.payload("lazada"))["ok"])
        self.assertFalse(self.bridge._balance_statistics_active)

    def test_retry_rejects_wrong_account_and_platform_before_starting(self):
        path = self.manifest()
        for payload in (
            {"platform": "temu", "account_id": self.other.id, "manifest_path": str(path)},
            {"platform": "lazada", "manifest_path": str(path)},
            {"platform": "temu", "manifest_path": ""},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(self.bridge.retry_balance_statistics(payload)["ok"])
                self.assertFalse(self.bridge._balance_statistics_active)
        self.bridge.tasks.start.assert_not_called()
        self.browser_guard.assert_not_called()

    def test_retry_passes_original_manifest_and_selected_account_to_runner(self):
        path = self.manifest("lazada")
        self.assertTrue(self.bridge.retry_balance_statistics({"platform": "lazada", "manifest_path": str(path)})["ok"])
        call = self.bridge.tasks.start.call_args
        self.assertEqual(call.kwargs, {"tool": "lazada_balance_statistics", "context": {"account_id": self.account.id, "platform": "lazada", "manifest_path": str(path.resolve())}})
        with patch.object(balance_statistics, "retry_balance_statistics", return_value={"is_complete": False}) as retry:
            call.args[1](Mock())
        self.assertEqual(retry.call_args.args[0]["manifest_path"], str(path.resolve()))
        self.assertEqual(retry.call_args.args[0]["account_id"], self.account.id)
        self.assertFalse(self.bridge._balance_statistics_active)

    def test_both_modules_have_launcher_navigation_dashboard_and_packaging_entries(self):
        common = (ROOT / "frontend/assets/common.js").read_text(encoding="utf-8")
        dashboard = (ROOT / "frontend/assets/pages/dashboard.js").read_text(encoding="utf-8")
        spec = (ROOT / "HQYLAutomation.spec").read_text(encoding="utf-8")
        for platform in ("temu", "lazada"):
            with self.subTest(platform=platform):
                page = resolve_entry_html([f"--page={platform}-balance-statistics"])
                self.assertEqual(page.name, f"{platform}-balance-statistics.html")
                self.assertIn(f'id: "{platform}_balance_statistics", href: "{page.name}"', common)
                self.assertIn(f'{platform}_balance_statistics: {{ href: "{page.name}"', dashboard)
                self.assertIn(f'"backend.services.{platform}_balance_collector"', spec)
        self.assertIn('*collect_submodules("PIL")', spec)
        self.assertIn('"util.ziniao_window_position"', spec)
        self.assertIn("Pillow>=10.0", (ROOT / "requirements.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

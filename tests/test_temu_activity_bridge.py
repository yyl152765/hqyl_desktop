from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.core.ziniao_browser import account_profile
from backend.output_paths import directory_scope_key
from backend.services.temu_activity_export import load_batch


class ActivityExportBridgeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        account = BoundAccount("a", "ziniao", "公司", "user", "secret", {"company": "公司"})
        settings = AppSettings(output_dir=str(self.root), accounts=[account], active_account_ids={"ziniao": "a"})
        self.settings = settings
        config = SimpleNamespace(local_data_dir=self.root, load=lambda: settings)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "x"}
        self.factory = patch.object(app_bridge, "ZiniaoBrowser")
        self.browser_class = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.browser = self.browser_class.return_value.__enter__.return_value
        self.browser.list_stores.return_value = [{"store_id": "1", "store_name": "甲-店A", "platform_name": "TEMU"}]
        self.payload = {
            "store_names": "店A",
            "sites": ["PH"],
            "business_date": "2026-11-03",
            "output_dir": str(self.root),
        }

    def test_info_uses_the_activity_output_scope(self) -> None:
        scoped = str(self.root / "temu-activity-only")
        self.settings.tool_output_dirs[directory_scope_key("temu_activity_export")] = scoped
        result = self.bridge.get_temu_activity_export_info()
        self.assertTrue(result["ok"])
        self.assertEqual(result["output_dir"], scoped)
        self.assertEqual([site["code"] for site in result["sites"]], ["PH", "MY", "TH"])
        self.assertEqual(result["activity_types"], ["限时秒杀", "官方大促", "专题活动"])
        self.assertEqual(self.settings.output_dir, str(self.root))

    def test_start_persists_scope_without_credentials(self) -> None:
        self.assertTrue(self.bridge.start_temu_activity_export({**self.payload, "output_dir": ""})["ok"])
        context = self.bridge.tasks.start.call_args.kwargs["context"]
        manifest = Path(context["manifest_path"])
        self.assertTrue(manifest.is_file())
        profile = account_profile(self.bridge._payload_with_account({}, "ziniao"))
        record = load_batch(manifest, profile)
        self.assertEqual(record["sites"], ["PH"])
        self.assertEqual(record["business_date"], "2026-11-03")
        self.assertEqual(record["stores"][0]["store_name"], "甲-店A")
        self.assertNotIn("secret", manifest.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "temu_activity_export" / "latest.json").is_file())

    def test_validation_and_unknown_store_do_not_start_a_task(self) -> None:
        self.assertFalse(self.bridge.start_temu_activity_export({**self.payload, "store_names": ""})["ok"])
        self.assertFalse(self.bridge.start_temu_activity_export({**self.payload, "sites": []})["ok"])
        self.assertFalse(self.bridge.start_temu_activity_export({**self.payload, "store_names": "店B"})["ok"])
        self.browser_class.assert_called()
        self.bridge.tasks.start.assert_not_called()

    def test_second_start_is_rejected_while_a_task_runs(self) -> None:
        self.assertTrue(self.bridge.start_temu_activity_export(self.payload)["ok"])
        second = self.bridge.start_temu_activity_export(self.payload)
        self.assertFalse(second["ok"])
        self.assertIn("正在运行", second["error"])

    def test_retry_requires_a_batch_with_failures(self) -> None:
        self.assertTrue(self.bridge.start_temu_activity_export(self.payload)["ok"])
        manifest = self.bridge.tasks.start.call_args.kwargs["context"]["manifest_path"]
        self.bridge._temu_export_active = False
        result = self.bridge.retry_temu_activity_export({"manifest_path": manifest})
        self.assertTrue(result["ok"])
        self.assertEqual(self.bridge.tasks.start.call_args.kwargs["tool"], "temu_activity_export")

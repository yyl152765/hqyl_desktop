from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HTML = FRONTEND / "pages" / "temu-activity-export.html"
SCRIPT = FRONTEND / "assets" / "pages" / "temu-activity-export.js"


class TemuActivityExportFrontendTests(unittest.TestCase):
    def test_page_matches_the_temu_export_shell(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="copyLogBtn"', html)
        self.assertIn('class="surface log-surface"', html)
        self.assertIn("20260623-log-panel", html)
        self.assertIn("../assets/pages/temu-activity-export.js", html)
        self.assertIn('data-active-account-name data-vendor="ziniao"', html)
        self.assertIn('href="settings.html?focus=ziniao"', html)

    def test_form_asks_for_stores_sites_date_and_output(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        for element in ('id="storeNames"', 'id="sitePH"', 'id="siteMY"', 'id="siteTH"',
                        'id="businessDate"', 'id="outputDir"', 'id="startBtn"', 'id="inspectBtn"'):
            self.assertIn(element, html)
        self.assertEqual(html.count('name="permissionType"'), 0)
        self.assertIn("每行一个", html)
        self.assertIn("清仓", html)

    def test_every_referenced_id_exists(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        referenced = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', script))
        self.assertEqual(sorted(referenced - set(re.findall(r'\bid="([^"]+)"', html))), [])

    def test_script_declares_validation_scope_and_result_feedback(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('key: "temu_activity_export"', script)
        self.assertIn("start_temu_activity_export", script)
        self.assertIn("retry_temu_activity_export", script)
        self.assertIn("inspect_temu_activity_page", script)
        self.assertIn("get_temu_activity_export_progress", script)
        self.assertIn("店铺名不能重复", script)
        self.assertIn("请至少勾选一个本次站点", script)
        self.assertIn("运行日期不能晚于北京时间当天", script)
        self.assertIn("导出范围：", script)
        self.assertIn("site_rows", script)
        self.assertIn("HQYL.boot(page)", script)

    def test_entry_and_catalog_are_registered(self) -> None:
        common = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")
        dashboard = (FRONTEND / "assets" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        launcher = (ROOT / "launcher" / "main.py").read_text(encoding="utf-8")
        self.assertIn('id: "temu_activity_export", href: "temu-activity-export.html"', common)
        self.assertIn("temu_activity_export: { href: \"temu-activity-export.html\"", dashboard)
        self.assertIn('"temu_activity_export", name: "TEMU 活动报名导出"', dashboard)
        self.assertIn('"temu-activity-export": "temu-activity-export.html"', launcher)


if __name__ == "__main__":
    unittest.main()

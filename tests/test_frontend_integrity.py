from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PAGE_SCRIPTS = {
    FRONTEND / "pages" / "group-sales.html": FRONTEND / "assets" / "pages" / "group-sales.js",
    FRONTEND / "pages" / "purchase-log.html": FRONTEND / "assets" / "pages" / "purchase-log.js",
    FRONTEND / "pages" / "shopee-ads.html": FRONTEND / "assets" / "pages" / "shopee-ads.js",
    FRONTEND / "pages" / "bigseller-sync.html": FRONTEND / "assets" / "pages" / "bigseller-sync.js",
    FRONTEND / "pages" / "settings.html": FRONTEND / "assets" / "pages" / "settings.js",
}


class FrontendIntegrityTests(unittest.TestCase):
    def test_javascript_syntax(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript syntax check")

        for script in sorted((FRONTEND / "assets").rglob("*.js")):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [node, "--check", str(script)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_javascript_dom_ids_exist_in_html(self) -> None:
        for html_path, script_path in PAGE_SCRIPTS.items():
            with self.subTest(page=html_path.name):
                javascript = script_path.read_text(encoding="utf-8")
                html = html_path.read_text(encoding="utf-8")
                referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', javascript))
                html_ids = set(re.findall(r'\bid="([^"]+)"', html))
                self.assertEqual(sorted(referenced_ids - html_ids), [])

    def test_html_attributes_do_not_use_smart_quotes(self) -> None:
        for html_path in sorted(FRONTEND.rglob("*.html")):
            with self.subTest(page=html_path.name):
                html = html_path.read_text(encoding="utf-8")
                malformed_attributes = re.findall(
                    r"\b(?:id|class|type|style|viewBox|aria-[\w-]+)=[“”]",
                    html,
                )
                self.assertEqual(malformed_attributes, [])

    def test_hidden_attribute_is_enforced_globally(self) -> None:
        css = (FRONTEND / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important\s*;")

    def test_each_module_has_its_own_html_and_script(self) -> None:
        for html_path, script_path in PAGE_SCRIPTS.items():
            with self.subTest(page=html_path.name):
                self.assertTrue(html_path.is_file())
                self.assertTrue(script_path.is_file())
                html = html_path.read_text(encoding="utf-8")
                self.assertIn(f"../assets/pages/{script_path.name}", html)

    def test_all_task_log_panels_are_fixed_and_copyable(self) -> None:
        css = (FRONTEND / "assets" / "app.css").read_text(encoding="utf-8")
        common_javascript = (FRONTEND / "assets" / "common.js").read_text(encoding="utf-8")

        for html_path in PAGE_SCRIPTS:
            if html_path.name == "settings.html":
                continue
            with self.subTest(page=html_path.name):
                html = html_path.read_text(encoding="utf-8")
                self.assertIn('id="copyLogBtn"', html)
                self.assertIn('class="surface log-surface"', html)
                self.assertIn("20260623-log-panel", html)

        self.assertRegex(css, r"\.log-surface\s*\{[^}]*height:\s*clamp\(")
        self.assertRegex(css, r"\.log-box\s*\{[^}]*overflow-y:\s*scroll\s*;")
        self.assertIn('copyButton.addEventListener("click", copyLogs)', common_javascript)
        self.assertIn("navigator.clipboard?.writeText", common_javascript)
        self.assertIn("isLogPinnedToBottom", common_javascript)
        self.assertIn("pinnedToBottom ? box.scrollHeight : previousScrollTop", common_javascript)


if __name__ == "__main__":
    unittest.main()

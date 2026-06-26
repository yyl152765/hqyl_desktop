from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_shopee_ads_runtime_is_bundled_by_pyinstaller(self):
        spec = (PROJECT_ROOT / "HQYLAutomation.spec").read_text(encoding="utf-8")

        self.assertIn('["desktop_entry.py"]', spec)
        self.assertNotIn('["launcher/main.py"]', spec)
        self.assertIn('project_root.parent / "superbrowser_process"', spec)
        self.assertIn('str(superbrowser_process_root)', spec)
        self.assertIn('"main/shopee/config"', spec)
        self.assertIn('project_root / "backend" / "resources" / "user_map.yaml"', spec)
        self.assertNotIn('superbrowser_process_root / "config" / "user_map.yaml"', spec)
        self.assertIn('collect_submodules("selenium")', spec)

        for site_code in ("id", "th", "ph", "vn", "my"):
            module = f"main.shopee.{site_code}_shopee_ads_recharge_operator_service"
            self.assertIn(f'"{module}"', spec)

    def test_shopee_ads_packaging_dependencies_are_declared(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

        for package in ("selenium", "psycopg2-binary", "alibabacloud-dingtalk"):
            self.assertIn(package, requirements)


if __name__ == "__main__":
    unittest.main()

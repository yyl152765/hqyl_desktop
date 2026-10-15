from datetime import date
from pathlib import Path
import unittest

from backend.config_store import desktop_path
from backend.services import lazada_ads_data as ads
from backend.services import lazada_bill_detail as bills
from backend.services import lz_warehouse_inventory as inventory


class ServiceOutputDirectoryTests(unittest.TestCase):
    def payload(self):
        return {
            "username": "fixture-user", "password": "fixture-secret",
            "dingtalk_app_key": "fixture-key", "dingtalk_app_secret": "fixture-secret",
            "dingtalk_user_id": "fixture-user-id", "store_names": ["fixture-store"],
        }

    def test_ads_has_its_own_default_even_without_a_bridge(self):
        job = ads.validate_lazada_ads_payload(self.payload(), today=date(2026, 10, 15))
        self.assertEqual(Path(job.output_root), desktop_path() / "log" / "lz泰国广告数据")

    def test_bill_defaults_change_with_country_for_logs_and_screenshots(self):
        for code, profile in bills.COUNTRY_PROFILES.items():
            with self.subTest(country=code):
                job = bills.validate_lazada_bill_detail_payload(
                    {**self.payload(), "country": code}, today=date(2026, 10, 15),
                )
                self.assertEqual(Path(job.output_root), desktop_path() / "log" / f"Lazada{profile.name}账单明细")
                self.assertEqual(Path(job.screenshot_root), desktop_path() / "log" / f"Lazada{profile.name}账单明细截图")

    def test_inventory_has_its_own_default_without_inheriting_another_script(self):
        job = inventory.validate_lz_inventory_payload({
            "username": "fixture-user", "password": "fixture-secret",
            "warehouse_names": ["泰国TH04仓"],
        })
        self.assertEqual(job.output_dir, desktop_path() / "log" / "lz各仓库存")

    def test_country_screenshot_root_gets_only_one_date_subfolder(self):
        root = desktop_path() / "log" / "Lazada菲律宾账单明细截图"
        actual = bills.screenshot_folder(root, "菲律宾", "2026-09-01", "2026-09-30")
        self.assertEqual(actual, root / "2026-09-01到2026-09-30")
        self.assertEqual(str(actual).count("Lazada菲律宾账单明细截图"), 1)

    def test_explicit_directories_still_override_defaults(self):
        root = str(Path("fixture-exports") / "logs")
        screenshots = str(Path("fixture-exports") / "screenshots")
        job = bills.validate_lazada_bill_detail_payload({
            **self.payload(), "country": "PH", "output_root": root, "screenshot_root": screenshots,
        }, today=date(2026, 10, 15))
        self.assertEqual(job.output_root, root)
        self.assertEqual(job.screenshot_root, screenshots)
        self.assertEqual(bills.screenshot_folder(screenshots, "菲律宾", "2026-09-01", "2026-09-30"), Path(screenshots) / "2026-09-01到2026-09-30")


if __name__ == "__main__":
    unittest.main()

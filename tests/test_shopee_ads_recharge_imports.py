from pathlib import Path
import sys
import unittest

from backend.services import shopee_ads_recharge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"


class ShopeeAdsRechargeImportTests(unittest.TestCase):
    def test_launcher_main_does_not_shadow_superbrowser_main_package(self):
        original_path = sys.path[:]
        original_root = shopee_ads_recharge._SUPERBROWSER_ROOT
        original_main = sys.modules.pop("main", None)
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "launcher"))
            shopee_ads_recharge._SUPERBROWSER_ROOT = None

            shopee_ads_recharge._ensure_superbrowser_path()

            self.assertEqual(str(SUPERBROWSER_ROOT.resolve()), sys.path[0])
            self.assertNotIn("main", sys.modules)
        finally:
            sys.path[:] = original_path
            shopee_ads_recharge._SUPERBROWSER_ROOT = original_root
            sys.modules.pop("main", None)
            if original_main is not None:
                sys.modules["main"] = original_main


if __name__ == "__main__":
    unittest.main()

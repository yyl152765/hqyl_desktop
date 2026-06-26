from datetime import date
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
_INSERTED_SUPERBROWSER_PATH = False
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))
    _INSERTED_SUPERBROWSER_PATH = True

from implement.shopee.ads_expense_date_range import expense_range_t8_to_t1, previous_7_days_without_today

if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class ShopeeAdsExpenseDateRangeTests(unittest.TestCase):
    def test_expense_range_uses_t8_to_t1(self):
        self.assertEqual(
            expense_range_t8_to_t1(date(2026, 6, 25)),
            ("2026-06-17", "2026-06-24"),
        )

    def test_expense_range_handles_month_boundary(self):
        self.assertEqual(
            expense_range_t8_to_t1(date(2026, 7, 2)),
            ("2026-06-24", "2026-07-01"),
        )

    def test_legacy_helper_uses_expense_range(self):
        self.assertEqual(
            previous_7_days_without_today(date(2026, 6, 25)),
            ("2026-06-17", "2026-06-24"),
        )


if __name__ == "__main__":
    unittest.main()

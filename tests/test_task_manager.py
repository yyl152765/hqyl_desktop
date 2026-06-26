from __future__ import annotations

import time
import unittest

from backend.task_manager import TaskManager


class TaskManagerTests(unittest.TestCase):
    def test_latest_snapshot_can_be_scoped_to_tool(self) -> None:
        manager = TaskManager()
        manager.start("销量", lambda progress: {}, tool="group_sales")
        latest_ads = manager.latest_snapshot("shopee_ads")
        self.assertFalse(latest_ads["ok"])
        self.assertTrue(latest_ads["empty"])

        started = manager.start("广告充值", lambda progress: {}, tool="shopee_ads")
        for _ in range(100):
            latest_ads = manager.latest_snapshot("shopee_ads")
            if latest_ads.get("status") == "success":
                break
            time.sleep(0.005)

        self.assertEqual(latest_ads["id"], started["id"])
        self.assertEqual(latest_ads["tool"], "shopee_ads")


if __name__ == "__main__":
    unittest.main()

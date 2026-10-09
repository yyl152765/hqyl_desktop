from __future__ import annotations

import time
import unittest

from backend.task_manager import TaskManager


class TaskManagerTests(unittest.TestCase):
    def wait_finished(self, manager: TaskManager, task_id: str) -> dict:
        for _ in range(200):
            snapshot = manager.snapshot(task_id)
            if snapshot["status"] in {"success", "failed"}:
                return snapshot
            time.sleep(0.005)
        self.fail("Task did not finish")

    def test_incomplete_result_is_failed_and_keeps_export_and_checkpoint(self) -> None:
        manager = TaskManager()
        result = {
            "is_complete": False,
            "completion_message": "结果不完整：仍有 2 个 SKU 查询失败",
            "output_file": r"D:\reports\部分结果.xlsx",
            "checkpoint_file": r"D:\reports\进度.json",
            "confirmed_count": 8,
            "failed_skus": ["SKU-9", "SKU-10"],
        }
        started = manager.start("SKU 对标", lambda progress: result, tool="bigseller_sku_benchmark")
        snapshot = self.wait_finished(manager, started["id"])

        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["result"], result)
        self.assertEqual(snapshot["error"], result["completion_message"])
        self.assertIn(result["completion_message"], snapshot["logs"])
        self.assertNotIn("任务完成", snapshot["logs"])
        self.assertTrue(snapshot["finished_at"])

    def test_other_results_keep_success_status_unless_flag_is_explicit_false(self) -> None:
        for result in ({}, {"failed_count": 2}, {"success": False}, {"is_complete": True}, {"is_complete": None}, {"is_complete": 0}):
            with self.subTest(result=result):
                manager = TaskManager()
                started = manager.start("其他工具", lambda progress: result)
                snapshot = self.wait_finished(manager, started["id"])
                self.assertEqual(snapshot["status"], "success")
                self.assertEqual(snapshot["result"], result)
                self.assertEqual(snapshot["error"], "")

    def test_incomplete_result_without_message_has_actionable_failure(self) -> None:
        manager = TaskManager()
        started = manager.start("SKU 对标", lambda progress: {"is_complete": False})
        snapshot = self.wait_finished(manager, started["id"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("结果不完整", snapshot["error"])

    def test_exception_still_fails_without_a_result(self) -> None:
        def runner(progress):
            raise RuntimeError("无法登录")

        manager = TaskManager()
        started = manager.start("登录失败", runner)
        snapshot = self.wait_finished(manager, started["id"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertIsNone(snapshot["result"])
        self.assertIn("RuntimeError: 无法登录", snapshot["error"])

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

    def test_snapshot_preserves_task_context(self) -> None:
        manager = TaskManager()
        context = {"manifest_path": r"D:\reports\manifest.json", "period": "2026-05"}
        started = manager.start("越南报表", lambda progress: {}, tool="vietnam_final_reconciliation", context=context)
        snapshot = manager.snapshot(started["id"])

        self.assertEqual(snapshot["context"], context)
        context["period"] = "changed"
        self.assertEqual(snapshot["context"]["period"], "2026-05")


if __name__ == "__main__":
    unittest.main()

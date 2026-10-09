from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.services.sku_inventory_query import SkuInventoryResult


class SkuInventoryBridgeTests(unittest.TestCase):
    def test_coverage_mode_passes_filters_and_keeps_complete_counts_when_preview_truncates(self):
        bridge = AppBridge.__new__(AppBridge)
        bridge.config_store = Mock()
        bridge.tasks = Mock()
        bridge.tasks.start.return_value = {"ok": True, "id": "task"}
        bridge._payload_with_account = lambda payload, vendor: {
            **payload, "username": "bound-user", "password": "bound-password",
        }
        scope = ({"id": "W1", "name": "泰国仓", "country": "泰国"},)
        rows = tuple({"sku": f"SKU-{i}", "missing_warehouse_count": 1} for i in range(501))
        result = SkuInventoryResult(
            records=rows, developer_count=1, sku_count=501, warehouse_count=1,
            output_file=Path("coverage.xlsx"), query_mode="missing_warehouses",
            scope_warehouses=scope, missing_sku_count=501,
        )
        with patch("backend.app_bridge.run_sku_inventory_query", return_value=result) as service:
            response = bridge.start_sku_inventory_query({
                "developer_ids": ["101"], "query_mode": "missing_warehouses",
                "liveness_types": ["2"], "output_dir": "unused",
            })
            self.assertTrue(response["ok"])
            runner = bridge.tasks.start.call_args.args[1]
            response = runner(Mock())
        job = service.call_args.args[0]
        self.assertEqual(job.liveness_types, ("2",))
        self.assertEqual(job.query_mode, "missing_warehouses")
        self.assertEqual(job.start_date, "")
        self.assertEqual(response["query_mode"], "missing_warehouses")
        self.assertEqual(response["record_count"], 501)
        self.assertEqual(response["missing_sku_count"], 501)
        self.assertEqual(response["scope_warehouses"], list(scope))
        self.assertEqual(response["unclassified_warehouses"], [])
        self.assertTrue(response["preview_truncated"])
        self.assertEqual(len(response["rows"]), 500)


if __name__ == "__main__":
    unittest.main()

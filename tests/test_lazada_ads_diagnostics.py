"""Keep a failed DingTalk connection actionable without logging credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from backend.services import lazada_ads_data as service


class DingTalkDiagnosticsTests(unittest.TestCase):
    def test_resource_not_found_records_the_requested_workbook_before_browser_start(self):
        with tempfile.TemporaryDirectory() as directory:
            query = service.validate_lazada_ads_payload({
                "username": "configured-user", "password": "configured-password",
                "dingtalk_app_key": "fixture-app", "dingtalk_app_secret": "fixture-secret",
                "dingtalk_user_id": "fixture-user", "store_names": "shop A",
                "workbook_id": "fixture-workbook", "sheet_name": "26年10月",
                "target_date": "2026-01-01", "output_root": directory,
            })
            error = RuntimeError("invalidRequest.resource.notFound code: 404, uuid not exist request id: fixture-request")
            factory = Mock(side_effect=error)
            runtime = Mock()
            result = service.run_lazada_ads_data(query, sheet_factory=factory, runtime_factory=runtime)
            self.assertFalse(result["is_complete"])
            self.assertEqual(result["workbook_id"], query.workbook_id)
            message = result["stores"][0]["message"]
            self.assertIn("工作簿 ID=fixture-workbook", message)
            self.assertIn("Sheet=26年10月", message)
            self.assertIn("fixture-request", message)
            log = Path(result["log_file"]).read_text(encoding="utf-8")
            self.assertIn("连接钉钉工作簿：fixture-workbook", log)
            self.assertEqual(json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))["workbook_id"], query.workbook_id)
            runtime.assert_not_called()
            self.assertNotIn(query.dingtalk_app_secret, log)


if __name__ == "__main__":
    unittest.main()

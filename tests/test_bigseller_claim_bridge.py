from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services import bigseller_sku_benchmark as transport
from backend.services.bigseller_item_id_query import (
    BigSellerAuthenticationError,
    login_for_query,
)


class ClaimBridgeTests(unittest.TestCase):
    def test_captcha_server_error_is_actionable_without_exposing_remote_message(self):
        util = Mock()
        util.login_bigseller_by_request.side_effect = RuntimeError(
            "private details 500 Server Error: Internal Server Error for url: http://api.ttshitu.com/predict"
        )
        with self.assertRaisesRegex(BigSellerAuthenticationError, "验证码服务返回服务器错误") as raised:
            login_for_query(util, {}, Mock())
        self.assertNotIn("private", str(raised.exception))

    def bridge(self, bound=True):
        bridge = AppBridge()
        bridge.config_store = Mock()
        bridge.config_store.load.return_value = AppSettings(
            accounts=[BoundAccount(id="bs", vendor="bigseller", name="BS", username="bound", password="private")]
            if bound else [],
            active_account_ids={"bigseller": "bs"} if bound else {},
            captcha_username="captcha", captcha_password="private-captcha",
        )
        bridge.tasks = Mock()
        return bridge

    def test_bound_account_is_injected_without_credentials_in_task_context(self):
        bridge = self.bridge()
        job = SimpleNamespace(output_dir=Path("output"), source_file=Path("source.xlsx"),
                              sheet_name="新sku上架", site="MY", listing_scope="live", resume_checkpoint=None)
        with patch("backend.app_bridge.validate_bigseller_claim_payload", return_value=job) as validate, \
             patch("backend.app_bridge.run_bigseller_claim_query", return_value={"is_complete": False}) as run:
            bridge.start_bigseller_claim_query({"username": "override", "password": "override"})
            request = validate.call_args.args[0]
            self.assertEqual(request["username"], "bound")
            self.assertEqual(request["captcha_password"], "private-captcha")
            call = bridge.tasks.start.call_args
            self.assertEqual(call.kwargs["tool"], "bigseller_claim_query")
            self.assertEqual(call.kwargs["context"]["site"], "MY")
            self.assertNotIn("private", json.dumps(call.kwargs))
            self.assertEqual(call.args[1](Mock()), {"is_complete": False})
            run.assert_called_once()

    def test_unbound_account_cannot_start_and_unexpected_error_is_sanitized(self):
        bridge = self.bridge(False)
        self.assertTrue(bridge.start_bigseller_claim_query({})["requires_account"])
        bridge.tasks.start.assert_not_called()
        bridge = self.bridge()
        with patch("backend.app_bridge.validate_bigseller_claim_payload", side_effect=RuntimeError("private detail")):
            result = bridge.start_bigseller_claim_query({})
        self.assertFalse(result["ok"])
        self.assertNotIn("private", result["error"])
        bridge.tasks.start.assert_not_called()

    def test_inspection_finds_sku_sheet_and_missing_file_is_an_error(self):
        bridge = self.bridge(False)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.xlsx"
            book = Workbook()
            book.active.title = "说明"
            sheet = book.create_sheet("新品")
            sheet.append(["主SKU", "名称"])
            sheet.append(["00123", "商品"])
            book.save(source)
            book.close()
            result = bridge.inspect_bigseller_claim_file(str(source))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sheet_name"], "新品")
            self.assertIn("新品", result["sheets"])
            self.assertEqual(result["path"], str(source.resolve()))
            self.assertFalse(bridge.inspect_bigseller_claim_file(str(source.with_name("missing.xlsx")))["ok"])

    def test_shared_transport_honors_claim_query_and_keeps_old_default(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"code": 0, "data": {"page": {"rows": [], "totalSize": 0}}}
        query = {"searchType": "parentSku", "inquireType": 2, "orderBy": "create_time", "desc": False}
        with patch.object(transport, "_post_listing", return_value=response) as post:
            self.assertEqual(transport.request_benchmark_page(Mock(), Mock(), {}, "sku", "views", 1, Mock(), query=query), ([], 0))
            self.assertEqual(post.call_args.args[3], query)
            transport.request_benchmark_page(Mock(), Mock(), {}, "sku", "views", 1, Mock())
            self.assertEqual(post.call_args.args[3]["searchType"], "sku")
            self.assertEqual(post.call_args.args[3]["orderBy"], "views")


if __name__ == "__main__":
    unittest.main()

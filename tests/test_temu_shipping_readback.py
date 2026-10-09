"""Exercise the service with the real gateway/parser and simulated ERP responses."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from backend.services import temu_shipping_channel as service
from backend.services import temu_shipping_gateway as gateway
from tests.test_temu_shipping_gateway import COMPANY, channel_rows, settings_payload


class TemuShippingReadbackTests(unittest.TestCase):
    def run_sagawa(self, target, *, persist=True):
        original = {"length": "15", "width": "10", "height": "11", "weight": "275"}
        payload = settings_payload()
        soup = BeautifulSoup(payload["channelData"], "html.parser")
        for field, name in gateway.NUMBER_FIELDS.items():
            soup.select_one(f'input[name="{name}"]')["value"] = original[field]
        for name in gateway.SWITCH_FIELDS.values():
            soup.select_one(f'input[name="{name}"]')["checked"] = "checked"
        payload["channelData"] = str(soup)
        saves = []

        def post(path, *, form_data, **kwargs):
            if path == gateway.LIST_PATH:
                return {"code": 200, "data": {"list": [COMPANY], "totalCount": 1, "totalPage": 1}}
            if path == gateway.CHANNELS_PATH:
                return {"success": True, "message": channel_rows((101,)).replace("渠道 101", "standard-Sagawa")}
            if path == gateway.SETTINGS_PATH:
                return copy.deepcopy(payload)
            self.assertEqual(path, gateway.SAVE_PATH)
            saves.append(dict(form_data))
            if persist:
                for name in gateway.NUMBER_FIELDS.values():
                    soup.select_one(f'input[name="{name}"]')["value"] = saves[-1][name]
                payload["channelData"] = str(soup)
            return {"success": True}

        client = Mock()
        client.post_form_json.side_effect = post
        factory = lambda username, password: gateway.TemuShippingGateway(username, password, client=client)
        data = {"excel_row": 68, **target}
        workbook = {"input_file": "daily.xlsx", "sheet_name": "Sheet1", "row_count": 1,
                    "rows": [data], "fingerprint": service._digest([data])}
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            store = service.TemuShippingRunStore(Path(directory))
            with patch.object(service, "read_temu_shipping_workbook", return_value=workbook):
                preview = service.preview_temu_shipping(username="test", password="secret", input_file="daily.xlsx",
                                                        store=store, gateway_factory=factory)
            result = service.run_temu_shipping_batch(username="test", password="secret", run_id=preview["run_id"],
                                                     store=store, gateway_factory=factory, progress=logs.append)
        return result, saves, logs

    def test_reported_sagawa_values_equal_import_skips_write_with_explicit_evidence(self):
        result, saves, logs = self.run_sagawa({"length": "15", "width": "10", "height": "11", "weight": "275.00"})
        self.assertEqual(saves, [])
        self.assertEqual(result["rows"][0]["status"], "unchanged")
        self.assertEqual((result["changed_count"], result["unchanged_count"]), (0, 1))
        self.assertIn("standard-Sagawa（Excel 第 68 行）", logs[-2])
        self.assertIn("重量 275 g → 275 g", logs[-2])
        self.assertIn("本次未提交任何修改", result["message"])

    def test_each_dimension_and_weight_difference_causes_save_and_independent_readback(self):
        for field, target in (("length", "16"), ("width", "12"), ("height", "9"), ("weight", "280.25")):
            with self.subTest(field=field):
                values = {"length": "15", "width": "10", "height": "11", "weight": "275.00", field: target}
                result, saves, logs = self.run_sagawa(values)
                self.assertEqual(len(saves), 1)
                self.assertEqual(saves[0][gateway.NUMBER_FIELDS[field]], target)
                self.assertEqual(result["rows"][0]["status"], "success")
                self.assertEqual(result["rows"][0]["observed_values"][field], target)
                self.assertNotEqual(result["rows"][0]["original_values"][field], target)
                self.assertIn("保存后读回", logs[-2])

    def test_success_response_without_persisted_change_is_failed_with_actual_values(self):
        result, saves, logs = self.run_sagawa({"length": "16", "width": "12", "height": "9", "weight": "280.25"}, persist=False)
        self.assertEqual(len(saves), 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["rows"][0]["status"], "failed")
        self.assertEqual(result["rows"][0]["observed_values"]["weight"], "275")
        self.assertIn("重量实际 275 g，目标 280.25 g", result["rows"][0]["message"])
        self.assertIn("保存后读回：长 15 cm，宽 10 cm，高 11 cm，重量 275 g", logs[-2])

    def test_sagawa_reported_target_is_written_to_declaration_fields(self):
        result, saves, logs = self.run_sagawa({"length": "17", "width": "10", "height": "15", "weight": "425.00"})
        self.assertTrue(result["complete"])
        self.assertEqual({key: saves[0][key] for key in gateway.NUMBER_FIELDS.values()},
                         {"declareLength": "17", "declareWidth": "10", "declareHeight": "15", "declareWeight": "425"})
        self.assertIn("长 15 cm → 17 cm；宽 10 cm → 10 cm；高 11 cm → 15 cm；重量 275 g → 425 g", logs[-2])


if __name__ == "__main__":
    unittest.main()

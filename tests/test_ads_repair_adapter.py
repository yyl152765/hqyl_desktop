from unittest import TestCase
from unittest.mock import Mock

from scripts.apply_vn_id_ads_repair import ApplicationSheetAdapter


class RepairAdapterTests(TestCase):
    def adapter(self):
        adapter = ApplicationSheetAdapter.__new__(ApplicationSheetAdapter)
        adapter.doc_id, adapter.sheet_id = "doc", "sheet"
        adapter.union_id, adapter.token = "operator", "token"
        adapter.api, adapter.api_calls = Mock(), 0
        return adapter

    def test_batch_write_never_fills_a_gap_or_an_unlisted_column(self):
        adapter = self.adapter()
        adapter.write_cells("doc", "sheet", {
            "P2": {"value": "待核验"}, "P3": {"value": "待核验"}, "P5": {"value": "待核验"},
            "E2": {"formula": "=D2/7"}, "E3": {"formula": "=D3/7"},
        })
        calls = adapter.api.update_range.call_args_list
        self.assertEqual([c.args[4] for c in calls], ["E2:E3", "P2:P3", "P5:P5"])
        self.assertEqual(calls[0].args[5], [["=D2/7"], ["=D3/7"]])

    def test_bad_patch_is_rejected_before_any_mutation(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            adapter.write_cells("doc", "sheet", {"P2": {"value": "待核验"}, "E2": {"value": 1, "formula": "=1"}})
        adapter.api.update_range.assert_not_called()

    def test_other_document_cannot_be_written(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            adapter.write_cells("other", "sheet", {"P2": {"value": "待核验"}})
        adapter.api.update_range.assert_not_called()

    def test_numeric_repairs_follow_dingtalk_string_value_contract(self):
        adapter = self.adapter()
        adapter.write_cells("doc", "sheet", {"D56": {"value": 6471781}})
        self.assertEqual(adapter.api.update_range.call_args.args[5], [["6471781"]])

from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from backend.services import temu_shipping_channel as service
from backend.services.temu_shipping_gateway import parse_channel_rows as parse_actual_channel_rows
from backend.task_manager import TaskManager


USERNAME = "test-account"
PASSWORD = "do-not-store-this-password"


def channel(index):
    return {
        "channel_id": str(index), "channel_name": f"TEMU 渠道 {index}",
        "logistics_id": "100", "my_logistics_id": "200", "source": "erp",
        "enabled_state": "1",
    }


def workbook(count):
    # Deliberately repeat alternate dimensions: repetitions remain independent rows.
    rows = [{"excel_row": index + 2, "length": "17" if index % 2 == 0 else "16",
             "width": "12", "height": "10", "weight": "340.00" if index % 2 == 0 else "320.00"}
            for index in range(count)]
    return {"input_file": "daily.xlsx", "sheet_name": "Sheet1", "row_count": count,
            "rows": rows, "fingerprint": service._digest(rows), "calculation_date": "2026-09-10", "warnings": []}


@dataclass
class Snapshot:
    managed_values: dict
    other_fingerprint: str = "other-fields-unchanged"


def desired_values(row):
    return {field: row[field] for field in service.DIMENSIONS}


def settings_match(after, wanted, before=None):
    return after.managed_values == wanted and (before is None or after.other_fingerprint == before.other_fingerprint)


class FakeGateway:
    def __init__(self, channels):
        self.channels = copy.deepcopy(channels)
        self.settings = {item["channel_id"]: Snapshot({field: "1" for field in service.DIMENSIONS})
                         for item in channels}
        self.events = []
        self.fail_save = set()
        self.timeout_after_apply = set()
        self.fail_read = set()
        self.other_change_on_save = set()
        self.connection_error = None

    def factory(self, username, password):
        self.events.append(("connect", username))
        if self.connection_error:
            raise self.connection_error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def list_channels(self):
        self.events.append(("list",))
        return copy.deepcopy(self.channels)

    def read_settings(self, target):
        identity = target["channel_id"]
        self.events.append(("read", identity))
        if identity in self.fail_read:
            raise RuntimeError("读取失败")
        return copy.deepcopy(self.settings[identity])

    def save_settings(self, target, before, row):
        identity = target["channel_id"]
        self.events.append(("save", identity, copy.deepcopy(desired_values(row)), row["excel_row"]))
        if identity in self.fail_save:
            raise RuntimeError("保存失败")
        self.settings[identity] = Snapshot(desired_values(row), before.other_fingerprint)
        if identity in self.other_change_on_save:
            self.settings[identity].other_fingerprint = "other-fields-modified"
        if identity in self.timeout_after_apply:
            raise TimeoutError("请求超时")

    @property
    def saves(self):
        return [event for event in self.events if event[0] == "save"]


class TemuShippingChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)
        self.store = service.TemuShippingRunStore(self.directory)
        # Test service orchestration independently from HTTP parsing and transport.
        module = types.ModuleType("backend.services.temu_shipping_gateway")
        module.TemuShippingGateway = lambda *_args: self.fail("An explicit fake gateway is required")
        module.desired_values = desired_values
        module.settings_match = settings_match
        self.gateway_patch = patch.dict("sys.modules", {module.__name__: module})
        self.gateway_patch.start()
        self.addCleanup(self.gateway_patch.stop)
        self.progress = []

    def preview(self, gateway, count=None, *, username=USERNAME):
        with patch.object(service, "read_temu_shipping_workbook", return_value=workbook(len(gateway.channels) if count is None else count)):
            return service.preview_temu_shipping(username=username, password=PASSWORD, input_file="daily.xlsx",
                                                 store=self.store, progress=self.progress.append,
                                                 gateway_factory=gateway.factory)

    def run_batch(self, gateway, run_id, *, username=USERNAME, store=None):
        return service.run_temu_shipping_batch(username=username, password=PASSWORD, run_id=run_id,
                                               store=store or self.store, progress=self.progress.append,
                                               gateway_factory=gateway.factory)

    def load_record(self, run_id):
        return self.store.load(run_id, service.account_key(USERNAME))

    def test_disabled_rows_are_removed_before_continuous_excel_mapping(self):
        from tests.test_temu_shipping_gateway import channel_rows, COMPANY
        # Exercise the real row parser even while HTTP transport is replaced.
        targets = parse_actual_channel_rows(channel_rows((103, 102, 101), off_ids=(102,)), COMPANY)
        gateway = FakeGateway(targets)
        result = self.preview(gateway, count=2)
        self.assertTrue(result["can_apply"])
        self.assertEqual([(row["channel_id"], row["excel_row"]) for row in result["rows"]], [("103", 2), ("101", 3)])
        self.assertTrue(self.run_batch(gateway, result["run_id"])["complete"])
        self.assertEqual([event[1] for event in gateway.saves], ["103", "101"])

    def test_legacy_preview_is_not_restored_or_executed(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        record = self.load_record(preview["run_id"])
        record["schema_version"] = 1
        self.store.save(record)
        self.assertIsNone(self.store.latest(service.account_key(USERNAME)))
        with self.assertRaisesRegex(ValueError, "仅已开启渠道"):
            self.run_batch(gateway, preview["run_id"])
        self.assertFalse(gateway.saves)

    def test_channel_switched_off_after_preview_blocks_all_writes(self):
        gateway = FakeGateway([channel(1), channel(2)])
        preview = self.preview(gateway)
        gateway.channels = [channel(2)]
        result = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(result["can_apply"])
        self.assertEqual(result["status"], "stale")
        self.assertIn("开关状态已变化", result["message"])
        self.assertFalse(gateway.saves)

    def test_eighty_rows_for_ninety_nine_channels_block_all_writes(self):
        gateway = FakeGateway([channel(index) for index in range(99)])
        preview = self.preview(gateway, count=80)
        self.assertEqual(preview["channel_count"], 99)
        self.assertEqual(preview["ready_count"], 80)
        self.assertEqual(preview["status"], "blocked")
        self.assertFalse(preview["can_apply"])
        self.assertFalse(preview["is_complete"])
        self.assertIn("缺少 19 组", preview["message"])
        self.assertEqual(preview["rows"][79]["excel_row"], 81)
        self.assertIsNone(preview["rows"][80]["excel_row"])
        with self.assertRaisesRegex(ValueError, "缺少 19 组"):
            self.run_batch(gateway, preview["run_id"])
        self.assertEqual(gateway.saves, [])
        self.assertFalse(any(event[0] == "read" for event in gateway.events))

    def test_duplicate_dimensions_and_reordered_live_list_keep_original_mapping(self):
        gateway = FakeGateway([channel(3), channel(1), channel(2)])
        preview = self.preview(gateway)
        self.assertEqual([row["channel_id"] for row in preview["rows"]], ["3", "1", "2"])
        self.assertEqual([row["length"] for row in preview["rows"]], ["17", "16", "17"])
        gateway.channels.reverse()
        with patch.object(service, "read_temu_shipping_workbook", side_effect=AssertionError("Do not reread Excel during execution")):
            result = self.run_batch(gateway, preview["run_id"])
        self.assertTrue(result["complete"])
        self.assertEqual([(event[1], event[2]["length"], event[3]) for event in gateway.saves],
                         [("3", "17", 2), ("1", "16", 3), ("2", "17", 4)])

    def test_retry_only_failed_rows_reuses_exact_dimensions_after_restart(self):
        gateway = FakeGateway([channel(index) for index in range(3)])
        preview = self.preview(gateway)
        gateway.fail_save.add("1")
        first = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(first["complete"])
        self.assertFalse(first["is_complete"])
        self.assertTrue(first["can_apply"])
        self.assertIn("可继续未完成渠道", first["completion_message"])
        self.assertEqual((first["success_count"], first["failed_count"]), (2, 1))
        self.assertEqual([row["status"] for row in first["rows"]], ["success", "failed", "success"])
        gateway.fail_save.clear()
        gateway.events.clear()
        restarted_store = service.TemuShippingRunStore(self.directory)
        result = self.run_batch(gateway, preview["run_id"], store=restarted_store)
        self.assertTrue(result["complete"])
        self.assertEqual([(event[1], event[2]["length"], event[3]) for event in gateway.saves], [("1", "16", 3)])
        self.assertEqual(self.load_record(preview["run_id"])["rows"][1]["attempts"], 2)

    def test_timeout_after_server_commit_is_recovered_by_readback(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        gateway.timeout_after_apply.add("1")
        result = self.run_batch(gateway, preview["run_id"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"][0]["status"], "success")
        self.assertIn("重新读取", result["rows"][0]["message"])
        self.assertEqual(len(gateway.saves), 1)
        self.assertEqual([event[0] for event in gateway.events[-3:]], ["read", "save", "read"])

    def test_unchanged_values_are_recorded_and_not_reported_as_modifications(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        wanted = desired_values(workbook(1)["rows"][0])
        gateway.settings["1"] = Snapshot(wanted)
        result = self.run_batch(gateway, preview["run_id"])
        row = result["rows"][0]
        self.assertEqual((result["changed_count"], result["unchanged_count"]), (0, 1))
        self.assertEqual(row["original_values"], wanted)
        self.assertEqual(row["observed_values"], wanted)
        self.assertIn("未提交修改", row["message"])
        self.assertIn("本次未提交任何修改", result["message"])
        self.assertFalse(gateway.saves)
        self.assertIn("长 17 cm → 17 cm", self.progress[-2])
        self.assertIn("重量 340.00 g → 340.00 g", self.progress[-2])
        restored = service.public_result(self.load_record(preview["run_id"]))
        self.assertEqual(restored["rows"][0]["original_values"], wanted)

    def test_changes_include_original_target_and_actual_values_in_logs_and_result(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        result = self.run_batch(gateway, preview["run_id"])
        row = result["rows"][0]
        self.assertEqual(row["original_values"], dict.fromkeys(service.DIMENSIONS, "1"))
        self.assertEqual(row["observed_values"], desired_values(workbook(1)["rows"][0]))
        self.assertEqual(row["observation_stage"], "after_save")
        self.assertEqual((result["changed_count"], result["unchanged_count"]), (1, 0))
        self.assertIn("TEMU 渠道 1（Excel 第 2 行）", self.progress[-2])
        for text in ("长 1 cm → 17 cm", "宽 1 cm → 12 cm", "高 1 cm → 10 cm", "重量 1 g → 340.00 g", "保存后读回"):
            self.assertIn(text, self.progress[-2])

    def test_partial_write_keeps_first_original_values_across_retry(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        save = gateway.save_settings

        def partial_save(target, before, row):
            save(target, before, row)
            gateway.settings["1"].managed_values["weight"] = "1"

        gateway.save_settings = partial_save
        first = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(first["complete"])
        self.assertIn("重量实际 1 g，目标 340.00 g", first["rows"][0]["message"])
        self.assertEqual(first["rows"][0]["observed_values"]["weight"], "1")
        gateway.save_settings = save
        result = self.run_batch(gateway, preview["run_id"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"][0]["original_values"], dict.fromkeys(service.DIMENSIONS, "1"))
        self.assertEqual(result["rows"][0]["observed_values"]["weight"], "340.00")

    def test_failed_readback_cannot_display_target_or_before_read_as_actual_after_save(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)

        def save_then_read_fails(target, before, row):
            gateway.fail_read.add(target["channel_id"])

        gateway.save_settings = save_then_read_fails
        result = self.run_batch(gateway, preview["run_id"])
        row = result["rows"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["observed_values"], {})
        self.assertIsNone(row["observation_stage"])
        self.assertEqual(row["original_values"]["length"], "1")
        self.assertIn("实际值未读取，尚未核验", self.progress[-2])

    def test_saving_checkpoint_is_persisted_before_submit(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        original_save = gateway.save_settings

        def observe_checkpoint(target, before, row):
            checkpoint = self.load_record(preview["run_id"])["rows"][0]
            self.assertEqual(checkpoint["status"], "saving")
            self.assertEqual(checkpoint["other_fingerprint"], before.other_fingerprint)
            self.assertEqual(checkpoint["attempts"], 1)
            original_save(target, before, row)

        gateway.save_settings = observe_checkpoint
        self.assertTrue(self.run_batch(gateway, preview["run_id"])["complete"])

    def test_restart_saving_reads_first_and_skips_write_if_already_applied(self):
        for already_applied in (True, False):
            with self.subTest(already_applied=already_applied):
                gateway = FakeGateway([channel(1)])
                preview = self.preview(gateway)
                record = self.load_record(preview["run_id"])
                record["mode"] = "batch"
                record["status"] = "running"
                row = record["rows"][0]
                row.update(status="saving", attempts=1, other_fingerprint="other-fields-unchanged")
                self.store.save(record)
                if already_applied:
                    gateway.settings["1"] = Snapshot(desired_values(row))
                gateway.events.clear()
                result = self.run_batch(gateway, preview["run_id"], store=service.TemuShippingRunStore(self.directory))
                self.assertTrue(result["complete"])
                self.assertEqual(gateway.events[2], ("read", "1"))
                self.assertEqual(len(gateway.saves), 0 if already_applied else 1)

    def test_complete_record_does_not_connect_or_submit_again(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        self.assertTrue(self.run_batch(gateway, preview["run_id"])["complete"])
        gateway.events.clear()
        self.assertTrue(self.run_batch(gateway, preview["run_id"])["complete"])
        self.assertEqual(gateway.events, [])

    def test_checkpoint_failure_before_submit_pauses_and_preserves_mapping(self):
        gateway = FakeGateway([channel(1), channel(2), channel(3)])
        preview = self.preview(gateway)
        save = self.store.save

        def fail_second_pending(record):
            if record["rows"][1]["status"] == "saving":
                raise service.TemuShippingProgressError("本地进度保存失败")
            save(record)

        with patch.object(self.store, "save", side_effect=fail_second_pending):
            result = self.run_batch(gateway, preview["run_id"])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["is_complete"])
        self.assertTrue(result["can_apply"])
        self.assertIn("任务已暂停", result["message"])
        self.assertEqual([row["status"] for row in result["rows"]], ["success", "ready", "ready"])
        self.assertEqual([event[1] for event in gateway.saves], ["1"])
        saved = self.load_record(preview["run_id"])
        self.assertEqual([row["status"] for row in saved["rows"]], ["success", "ready", "ready"])
        gateway.events.clear()
        resumed = self.run_batch(gateway, preview["run_id"])
        self.assertTrue(resumed["complete"])
        self.assertEqual([(event[1], event[3]) for event in gateway.saves], [("2", 3), ("3", 4)])

    def test_checkpoint_failure_after_submit_does_not_repeat_committed_channel(self):
        for count in (1, 2):
            with self.subTest(count=count):
                gateway = FakeGateway([channel(index + 1) for index in range(count)])
                preview = self.preview(gateway)
                save = self.store.save

                def fail_after_submit(record):
                    if record["rows"][0]["status"] == "success":
                        raise service.TemuShippingProgressError("本地进度保存失败")
                    save(record)

                with patch.object(self.store, "save", side_effect=fail_after_submit):
                    result = self.run_batch(gateway, preview["run_id"])
                self.assertFalse(result["complete"])
                self.assertFalse(result["is_complete"])
                self.assertTrue(result["can_apply"])
                self.assertEqual([event[1] for event in gateway.saves], ["1"])
                self.assertEqual(self.load_record(preview["run_id"])["rows"][0]["status"], "saving")
                gateway.events.clear()
                resumed = self.run_batch(gateway, preview["run_id"])
                self.assertTrue(resumed["complete"])
                self.assertEqual(resumed["rows"][0]["status"], "success")
                self.assertEqual(resumed["rows"][0]["original_values"], dict.fromkeys(service.DIMENSIONS, "1"))
                self.assertEqual([event[1] for event in gateway.saves], ["2"] if count == 2 else [])

    def test_initial_checkpoint_failure_does_not_connect_or_change_saved_record(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        path = self.store._path(preview["run_id"])
        before = path.read_bytes()
        gateway.events.clear()
        with patch.object(self.store, "save", side_effect=service.TemuShippingProgressError("本地进度保存失败")):
            result = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(result["complete"])
        self.assertEqual(gateway.events, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.run_batch(gateway, preview["run_id"])["complete"])

    def test_final_checkpoint_failure_does_not_report_success_until_resumed(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        save = self.store.save

        def fail_final(record):
            if record["status"] == "complete":
                raise service.TemuShippingProgressError("本地进度保存失败")
            save(record)

        with patch.object(self.store, "save", side_effect=fail_final):
            result = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["success_count"], 1)
        gateway.events.clear()
        self.assertTrue(self.run_batch(gateway, preview["run_id"])["complete"])
        self.assertEqual(gateway.events, [])

    def test_checkpoint_failure_does_not_reenable_a_stale_channel_mapping(self):
        gateway = FakeGateway([channel(1), channel(2)])
        preview = self.preview(gateway)
        gateway.channels.pop()
        save = self.store.save

        def fail_stale(record):
            if record["status"] == "stale":
                raise service.TemuShippingProgressError("本地进度保存失败")
            save(record)

        with patch.object(self.store, "save", side_effect=fail_stale):
            result = self.run_batch(gateway, preview["run_id"])
        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["can_apply"])
        self.assertIn("重新查询预览", result["message"])
        self.assertEqual(gateway.saves, [])

    def test_retry_refuses_changed_other_settings_before_any_resubmission(self):
        for already_applied in (True, False):
            with self.subTest(already_applied=already_applied):
                gateway = FakeGateway([channel(1)])
                preview = self.preview(gateway)
                record = self.load_record(preview["run_id"])
                row = record["rows"][0]
                row.update(status="saving", attempts=1, other_fingerprint="original-other-fields")
                self.store.save(record)
                gateway.settings["1"].other_fingerprint = "changed-by-someone-else"
                if already_applied:
                    gateway.settings["1"].managed_values = desired_values(row)
                result = self.run_batch(gateway, preview["run_id"])
                self.assertFalse(result["complete"])
                self.assertIn("其他渠道设置发生变化", result["rows"][0]["message"])
                self.assertEqual(gateway.saves, [])
                self.assertEqual(self.load_record(preview["run_id"])["rows"][0]["other_fingerprint"],
                                 "original-other-fields")

    def test_nonthrowing_save_failure_is_caught_by_readback(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)

        def failed_save(target, _before, row):
            gateway.events.append(("save", target["channel_id"], desired_values(row), row["excel_row"]))
            return {"success": False, "message": "保存失败"}

        gateway.save_settings = failed_save
        result = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("保存后核验未通过", result["rows"][0]["message"])
        self.assertEqual(len(gateway.saves), 1)

    def test_added_or_removed_channels_block_writes_and_preserve_mapping(self):
        for add in (True, False):
            with self.subTest(add=add):
                gateway = FakeGateway([channel(1), channel(2)])
                preview = self.preview(gateway)
                if add:
                    gateway.channels.append(channel(3))
                else:
                    gateway.channels.pop()
                result = self.run_batch(gateway, preview["run_id"])
                self.assertEqual(result["status"], "stale")
                self.assertFalse(result["can_apply"])
                self.assertEqual(gateway.saves, [])
                self.assertEqual([row["channel_id"] for row in result["rows"]], ["1", "2"])
                with self.assertRaisesRegex(ValueError, "已变化"):
                    self.run_batch(gateway, preview["run_id"])

    def test_account_isolation_and_invalid_run_ids_prevent_network_access(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        gateway.events.clear()
        with self.assertRaisesRegex(ValueError, "其他马帮账号"):
            self.run_batch(gateway, preview["run_id"], username="other-account")
        for run_id in ("../outside", "A" * 32, "0" * 31, "", "../" + "0" * 32):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "任务记录无效"):
                self.run_batch(gateway, run_id)
        self.assertEqual(gateway.events, [])
        self.assertIsNone(self.store.latest(service.account_key("another-account")))
        self.assertEqual(self.store.latest(service.account_key(USERNAME))["run_id"], preview["run_id"])

    def test_tampered_mapping_is_rejected_before_network_access(self):
        for field, value in (("length", "29"), ("excel_row", 80), ("channel", channel(5))):
            with self.subTest(field=field):
                gateway = FakeGateway([channel(1)])
                preview = self.preview(gateway)
                record = self.load_record(preview["run_id"])
                record["rows"][0][field] = value
                self.store.save(record)
                gateway.events.clear()
                with self.assertRaisesRegex(ValueError, "对应记录已损坏"):
                    self.run_batch(gateway, preview["run_id"])
                self.assertEqual(gateway.events, [])

    def test_same_account_lock_is_exclusive_across_store_instances(self):
        key = service.account_key(USERNAME)
        other_store = service.TemuShippingRunStore(self.directory)
        outcomes = []

        def attempt_lock():
            try:
                with other_store.operation(key):
                    outcomes.append("unexpected acquired")
            except ValueError as exc:
                outcomes.append(str(exc))

        with self.store.operation(key):
            thread = threading.Thread(target=attempt_lock)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            with other_store.operation(service.account_key("other-account")):
                pass
        self.assertEqual(len(outcomes), 1)
        self.assertIn("已有 TEMU 任务正在运行", outcomes[0])
        with other_store.operation(key):
            pass

    def test_corrupt_record_shapes_are_reported_as_validation_errors(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        valid = self.load_record(preview["run_id"])
        bad_records = [[], None, "not an object"]
        for field in ("status", "mode", "excel_row_count", "input_file", "rows"):
            damaged = copy.deepcopy(valid)
            damaged.pop(field)
            bad_records.append(damaged)
        for value in (None, {}, [None], ["not a row"]):
            damaged = copy.deepcopy(valid)
            damaged["rows"] = value
            bad_records.append(damaged)
        for field in ("channel", "status", "attempts"):
            damaged = copy.deepcopy(valid)
            damaged["rows"][0].pop(field)
            bad_records.append(damaged)
        for record in bad_records:
            with self.subTest(record=record):
                self.store._path(preview["run_id"]).write_text(json.dumps(record), encoding="utf-8")
                gateway.events.clear()
                with self.assertRaisesRegex(ValueError, "任务.*(?:记录|映射)"):
                    self.run_batch(gateway, preview["run_id"])
                self.assertEqual(gateway.events, [])

    def test_latest_ignores_unrelated_nonobject_json_files(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        (self.directory / ("0" * 32 + ".json")).write_text("[]", encoding="utf-8")
        self.assertEqual(self.store.latest(service.account_key(USERNAME))["run_id"], preview["run_id"])

    def test_no_password_in_checkpoints_public_result_or_progress_even_on_failure(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        gateway.connection_error = RuntimeError("Cannot authenticate " + PASSWORD)
        result = self.run_batch(gateway, preview["run_id"])
        self.assertEqual(result["status"], "partial")
        self.assertIn("[已隐藏]", result["message"])
        for path in self.directory.glob("*.json"):
            self.assertNotIn(PASSWORD, path.read_text(encoding="utf-8"))
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn(PASSWORD, "\n".join(self.progress))
        self.assertNotIn("password", self.load_record(preview["run_id"]))

    def test_task_manager_surfaces_partial_result_and_resume_message(self):
        gateway = FakeGateway([channel(1), channel(2)])
        preview = self.preview(gateway)
        gateway.fail_save.add("2")
        manager = TaskManager()
        started = manager.start("TEMU 批量设置", lambda _progress: self.run_batch(gateway, preview["run_id"]),
                                tool="temu_shipping_batch")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = manager.snapshot(started["id"])
            if snapshot["status"] in {"success", "failed"}:
                break
            time.sleep(0.005)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["result"]["success_count"], 1)
        self.assertFalse(snapshot["result"]["is_complete"])
        self.assertEqual(snapshot["result"]["run_id"], preview["run_id"])
        self.assertEqual(snapshot["error"], snapshot["result"]["completion_message"])
        self.assertIn("可继续未完成渠道", snapshot["error"])

    def test_other_setting_change_after_save_never_counts_as_success(self):
        gateway = FakeGateway([channel(1)])
        preview = self.preview(gateway)
        gateway.other_change_on_save.add("1")
        result = self.run_batch(gateway, preview["run_id"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("其他设置不一致", result["rows"][0]["message"])

    def test_preview_empty_or_duplicate_channels_never_creates_a_run(self):
        for channels in ([], [channel(1), channel(1)]):
            with self.subTest(channels=channels):
                gateway = FakeGateway(channels)
                with self.assertRaises(ValueError):
                    self.preview(gateway, count=1)
                self.assertEqual(gateway.saves, [])
                self.assertEqual(list(self.directory.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()

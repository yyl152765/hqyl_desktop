# ruff: noqa: DTZ001, C408, RUF012
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook

from backend.services import erp_sku_earliest_store_service as service
from backend.services.erp_sku_earliest_store_checkpoint import (
    CheckpointError,
    read_checkpoint,
)
from backend.services.erp_sku_earliest_store_repository import (
    AuthorizedScope,
    CandidateRow,
    DatabaseSettings,
    ErpSkuRepository,
    QuerySpec,
    SkuQueryError,
    classify_database_error,
)
from backend.services.erp_sku_earliest_store_workbook import (
    export_workbook,
    inspect_workbook,
    load_input,
    parse_sku_text,
)

SETTINGS = DatabaseSettings(host="db.internal", database="erp", username="reader", password="top-secret")
COMPANY = AuthorizedScope(company_wide=True)
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=service.SHANGHAI)


def candidate(sku, status="MATCHED", **overrides):
    values = dict(
        input_sku=sku, result_status=status, candidate_shop_id="293688122", candidate_shop_name="店铺A", region="PH",
        item_id="54166445713", model_id="401400266476", matched_sku=sku,
        candidate_create_time=datetime(2026, 8, 22, 12, 32, 43), item_status="NORMAL", item_archived=False,
        model_archived=False, match_source="model_sku", matched_item_count=35, matched_shop_count=35,
        data_issue_count=0, tied_shop_count=1, base_clean_time=datetime(2026, 11, 27, 8, 27, 17),
        model_clean_time=datetime(2026, 11, 27, 8, 27, 17), tied_shop_ids=("293688122",), tied_shop_names=("店铺A",),
    )
    if status == "NOT_FOUND":
        values.update(candidate_shop_id="", candidate_shop_name="", item_id="", model_id="", matched_sku="",
                      candidate_create_time=None, matched_item_count=0, matched_shop_count=0, tied_shop_count=0,
                      tied_shop_ids=(), tied_shop_names=())
    values.update(overrides)
    return CandidateRow(**values)


class FakeRepository:
    def __init__(self, handler):
        self.handler, self.calls, self.closed = handler, [], False

    def query_batch(self, spec):
        self.calls.append(spec.skus)
        return self.handler(spec)

    def close(self):
        self.closed = True


def record_for(row: CandidateRow) -> dict:
    return {key: getattr(row, key) for key in row.__dataclass_fields__}


class FakeCursor:
    def __init__(self, connection):
        self.connection, self.description = connection, None

    def execute(self, sql, params):
        self.connection.executed.append((sql, params))
        if self.connection.error:
            raise self.connection.error
        rows = [record_for(candidate(sku)) for sku in params["skus"]]
        self.description = [(name,) for name in rows[0]] if rows else []
        self.rows = [tuple(row.values()) for row in rows]

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, error=None):
        self.executed, self.error, self.closed, self.autocommit = [], error, False, False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class PgError(Exception):
    def __init__(self, text, pgcode=None):
        super().__init__(text)
        self.pgcode = pgcode


class RepositoryTests(unittest.TestCase):
    def test_settings_fail_closed_and_hide_secret(self):
        with patch("backend.services.erp_sku_earliest_store_repository.builtin_database_settings", return_value=SETTINGS):
            builtin = DatabaseSettings.from_mapping({})
        self.assertEqual((builtin.host, builtin.port, builtin.database, builtin.username, builtin.sslmode),
                         ("db.internal", 5432, "erp", "reader", "require"))
        self.assertNotIn(builtin.password, repr(builtin) + str(builtin))
        with self.assertRaises(SkuQueryError) as raised:
            DatabaseSettings.from_mapping({"ERP_SKU_DB_HOST": ""})
        self.assertEqual(raised.exception.code, "config")
        with self.assertRaises(SkuQueryError) as raised:
            DatabaseSettings.from_mapping({"ERP_SKU_DB_HOST": "h"})
        self.assertEqual(raised.exception.code, "config")
        self.assertNotIn("top-secret", repr(SETTINGS) + str(SETTINGS))
        values = {f"ERP_SKU_DB_{k}": v for k, v in dict(HOST="h", DATABASE="d", USERNAME="u", PASSWORD="p").items()}
        self.assertEqual(DatabaseSettings.from_mapping(values).sslmode, "require")  # strict by default
        with self.assertRaises(SkuQueryError):
            DatabaseSettings.from_mapping({**values, "ERP_SKU_DB_SSLMODE": "prefer"})

    def test_empty_scope_fails_closed_and_env_parsing(self):
        with self.assertRaises(SkuQueryError):
            AuthorizedScope()
        self.assertTrue(AuthorizedScope.from_environment({}).company_wide)
        with self.assertRaises(SkuQueryError):
            AuthorizedScope.from_environment({"ERP_SKU_AUTHORIZED_SHOPS": ""})
        with self.assertRaises(SkuQueryError):
            AuthorizedScope.from_environment({"ERP_SKU_AUTHORIZED_SHOPS": "abc"})
        self.assertTrue(AuthorizedScope.from_environment({"ERP_SKU_AUTHORIZED_SHOPS": "*"}).company_wide)
        self.assertEqual(AuthorizedScope.from_environment({"ERP_SKU_AUTHORIZED_SHOPS": "1, 2;3"}).shop_ids, ("1", "2", "3"))

    def test_parameters_are_bound_and_scope_is_intersected(self):
        connection = FakeConnection()
        repo = ErpSkuRepository(SETTINGS, lambda _settings: connection)
        scope = AuthorizedScope(shop_ids=("1", "2"))
        rows = repo.query_batch(QuerySpec(("A'; DROP TABLE x;--", "B"), scope, shop_ids=("2", "9"), countries=("ph",)))
        self.assertEqual([row.input_sku for row in rows], ["A'; DROP TABLE x;--", "B"])
        sql, params = connection.executed[0]
        self.assertNotIn("DROP TABLE", sql)
        self.assertEqual(params["skus"], ["A'; DROP TABLE x;--", "B"])
        self.assertEqual((params["all_shops"], params["shop_ids"]), (False, ["2"]))
        self.assertEqual((params["filter_regions"], params["regions"]), (True, ["PH"]))
        self.assertTrue(connection.autocommit)
        repo.close()
        self.assertTrue(connection.closed)
        with self.assertRaises(SkuQueryError):  # no overlap with the authorised scope -> refuse
            ErpSkuRepository(SETTINGS, lambda _s: FakeConnection()).query_batch(QuerySpec(("A",), scope, shop_ids=("9",)))

    def test_company_scope_without_filter_queries_everything(self):
        connection = FakeConnection()
        ErpSkuRepository(SETTINGS, lambda _s: connection).query_batch(QuerySpec(("A",), COMPANY))
        self.assertEqual(connection.executed[0][1]["all_shops"], True)

    def test_errors_are_sanitised_and_connection_retried_once(self):
        calls = []

        def factory(_settings):
            calls.append(1)
            return FakeConnection(PgError("host=db.internal password=top-secret", "08006"))

        repo = ErpSkuRepository(SETTINGS, factory)
        with self.assertRaises(SkuQueryError) as raised:
            repo.query_batch(QuerySpec(("A",), COMPANY))
        self.assertEqual(len(calls), 2)
        self.assertEqual(raised.exception.code, "connection")
        self.assertNotIn("top-secret", str(raised.exception))
        self.assertEqual(classify_database_error(PgError("x", "57014")).code, "timeout")
        self.assertEqual(classify_database_error(PgError("x", "42501")).code, "auth")
        self.assertEqual(classify_database_error(PgError("x", "42P01")).code, "sql")
        auth = classify_database_error(PgError("FATAL: password authentication failed for user reader"), connecting=True)
        self.assertEqual(auth.code, "auth")
        self.assertNotIn("reader", str(auth))

    def test_non_retryable_error_is_not_retried_and_connection_closed(self):
        connections = []

        def factory(_settings):
            connections.append(FakeConnection(PgError("x", "57014")))
            return connections[-1]

        repo = ErpSkuRepository(SETTINGS, factory)
        with self.assertRaises(SkuQueryError) as raised:
            repo.query_batch(QuerySpec(("A",), COMPANY))
        self.assertEqual((raised.exception.code, len(connections)), ("timeout", 1))
        self.assertTrue(connections[0].closed)

    def test_result_must_match_input(self):
        class Short(FakeConnection):
            def cursor(self_inner):
                cursor = FakeCursor(self_inner)
                original = cursor.execute

                def execute(sql, params):
                    original(sql, {**params, "skus": params["skus"][:1]})
                cursor.execute = execute
                return cursor

        with self.assertRaises(SkuQueryError) as raised:
            ErpSkuRepository(SETTINGS, lambda _s: Short()).query_batch(QuerySpec(("A", "B"), COMPANY))
        self.assertEqual(raised.exception.code, "contract")


class RowBuildingTests(unittest.TestCase):
    def build(self, row):
        return service.build_row(row, "2026-11-27 12:00:00", NOW)

    def test_matched_not_found_and_no_model(self):
        matched = self.build(candidate("S"))
        self.assertEqual((matched["status"], matched["candidate_shop_name"]), ("matched", "店铺A"))
        self.assertEqual(matched["create_time"], "2026-08-22 12:32:43")
        self.assertEqual(self.build(candidate("S", "NOT_FOUND"))["status"], "not_found")
        no_model = self.build(candidate("S", match_source="item_sku_no_model", model_id="0"))
        self.assertEqual((no_model["status"], no_model["model_id"]), ("matched", ""))

    def test_ties_are_flagged_but_still_stable_choice(self):
        row = self.build(candidate("S", tied_shop_count=2, tied_shop_ids=("1", "2"), tied_shop_names=("甲", "乙")))
        self.assertEqual(row["status"], "matched")
        self.assertIn("并列", row["message"])
        self.assertEqual(row["tied_shops"], "1 甲；2 乙")

    def test_matched_with_partial_data_issues_keeps_winner_and_notes_skipped_rows(self):
        row = self.build(candidate("S", "MATCHED", data_issue_count=6))
        self.assertEqual(row["status"], "matched")
        self.assertEqual(row["candidate_shop_name"], "店铺A")
        self.assertIn("另有 6 条", row["message"])

    def test_incomplete_keeps_candidate_but_not_formal_result(self):
        row = self.build(candidate("S", "INCOMPLETE_DATA", data_issue_count=43))
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("店铺A", row["message"])
        self.assertIn("未作为", row["message"])

    def test_implausible_times_are_incomplete(self):
        future = self.build(candidate("S", candidate_create_time=datetime(2027, 6, 1)))
        old = self.build(candidate("S", candidate_create_time=datetime(1999, 1, 1)))
        missing = self.build(candidate("S", candidate_create_time=None))
        self.assertEqual({future["status"], old["status"], missing["status"]}, {"incomplete"})


def make_job(directory: Path, skus="A\nB", **extra):
    payload = {"sku_text": skus, "output_dir": str(directory), **extra}
    return service.validate_payload(payload, settings=SETTINGS, scope=COMPANY)


class ServiceRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_job(self, job, handler, **kwargs):
        repo = FakeRepository(handler)
        result = service.run_erp_sku_earliest_store(job, lambda _m: None, repository_factory=lambda: repo, **kwargs)
        return result, repo

    def test_successful_run_exports_files_and_closes_repository(self):
        job = make_job(self.dir)
        result, repo = self.run_job(job, lambda spec: [
            candidate(sku, "NOT_FOUND" if sku == "B" else "MATCHED") for sku in spec.skus])
        self.assertTrue(result["is_complete"])
        self.assertEqual((result["matched_count"], result["not_found_count"]), (1, 1))
        self.assertTrue(repo.closed)
        run_dir = Path(result["run_dir"])
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["is_complete"])
        self.assertNotIn("top-secret", (run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue((run_dir / "process.log").is_file())
        book = load_workbook(result["output_file"])
        self.assertIn("ERP创建时间对标明细", book.sheetnames)
        self.assertIn("ERP创建时间对标汇总", book.sheetnames)
        sheet = book["SKU查询"]
        self.assertEqual([cell.value for cell in sheet[1]][:2], ["SKU", "创建时间第一店铺"])
        self.assertEqual(sheet["B2"].value, "店铺A")
        self.assertEqual(sheet["D3"].value, "ERP留存范围未找到")

    def test_incomplete_data_marks_result_incomplete_and_leaves_formal_columns_empty(self):
        result, _ = self.run_job(make_job(self.dir, "A"), lambda spec: [
            candidate(sku, "INCOMPLETE_DATA", data_issue_count=3) for sku in spec.skus])
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["incomplete_count"], 1)
        self.assertIn("结果不完整", Path(result["output_file"]).name)
        sheet = load_workbook(result["output_file"])["SKU查询"]
        self.assertIsNone(sheet["B2"].value)
        self.assertEqual(sheet["D2"].value, "数据待核验")

    def test_timeout_bisects_batch_and_single_failure_is_not_not_found(self):
        def handler(spec):
            if len(spec.skus) > 1 or "BAD" in spec.skus:
                raise SkuQueryError("timeout", "ERP 数据库查询超时", "57014")
            return [candidate(sku) for sku in spec.skus]

        result, repo = self.run_job(make_job(self.dir, "A\nBAD\nC"), handler)
        self.assertEqual((result["matched_count"], result["failed_count"], result["not_found_count"]), (2, 1, 0))
        self.assertFalse(result["is_complete"])
        self.assertIn(("BAD",), repo.calls)

    def test_fatal_error_stops_remaining_batches_as_not_run(self):
        job = make_job(self.dir, "A\nB\nC")
        job = service.SkuEarliestJob(**{**job.__dict__, "batch_size": 1})

        def handler(spec):
            raise SkuQueryError("auth", "ERP 数据库认证或权限失败", "28P01")

        result, repo = self.run_job(job, handler)
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual((result["failed_count"], result["not_run_count"]), (1, 2))
        self.assertFalse(result["is_complete"])
        rows = {row["sku"]: row for row in result["rows"]}
        self.assertIn("未执行", rows["B"]["message"])

    def test_cancel_keeps_finished_batches(self):
        job = make_job(self.dir, "A\nB")
        job = service.SkuEarliestJob(**{**job.__dict__, "batch_size": 1})
        flags = iter([False, True])
        result, _ = self.run_job(job, lambda spec: [candidate(sku) for sku in spec.skus], cancel=lambda: next(flags))
        self.assertEqual((result["matched_count"], result["not_run_count"]), (1, 1))
        self.assertFalse(result["is_complete"])

    def test_resume_only_requeries_unconfirmed_and_rejects_changed_identity(self):
        job = make_job(self.dir, "A\nB")
        job = service.SkuEarliestJob(**{**job.__dict__, "batch_size": 1})

        def first(spec):
            if spec.skus == ("B",):
                raise SkuQueryError("timeout", "ERP 数据库查询超时", "57014")
            return [candidate(sku) for sku in spec.skus]

        result, _ = self.run_job(job, first)
        checkpoint = Path(result["checkpoint_file"])
        self.assertEqual(read_checkpoint(checkpoint, service.checkpoint_identity(job))["B"]["status"], "failed")
        resumed_job = make_job(self.dir, "A\nB", resume_checkpoint=str(checkpoint))
        second, repo = self.run_job(resumed_job, lambda spec: [candidate(sku) for sku in spec.skus])
        self.assertEqual(repo.calls, [("B",)])
        self.assertEqual((second["resumed_count"], second["matched_count"]), (1, 2))
        self.assertTrue(second["is_complete"])
        with self.assertRaises(CheckpointError):
            make_job(self.dir, "A\nB\nC", resume_checkpoint=str(checkpoint))
        with self.assertRaises(CheckpointError):  # scope change must not reuse old results
            make_job(self.dir, "A\nB", resume_checkpoint=str(checkpoint), countries="PH")

    def test_payload_validation(self):
        with self.assertRaises(ValueError):
            service.validate_payload({"output_dir": str(self.dir)}, settings=SETTINGS, scope=COMPANY)
        with self.assertRaises(ValueError):
            service.validate_payload({"sku_text": "A", "source_file": "x.xlsx", "output_dir": str(self.dir)},
                                     settings=SETTINGS, scope=COMPANY)
        with self.assertRaises(ValueError):
            make_job(self.dir, "A", countries="PHL")
        with self.assertRaises(ValueError):
            service.validate_payload({"sku_text": "A", "shop_ids": "9", "output_dir": str(self.dir)},
                                     settings=SETTINGS, scope=AuthorizedScope(shop_ids=("1",)))


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def source(self):
        book = Workbook()
        book.active.title = "其他"
        book.active.append(["备注", "SKU"])
        book.active.append(["x", "ignored"])
        sheet = book.create_sheet("动销总表")
        sheet.append(["说明", "SKU", "库存SKU编号", "备注"])
        sheet.append(["保留", "SHOULD-NOT-USE", "00123", "原备注"])
        sheet.append(["保留", "SHOULD-NOT-USE", "=cmd|' /C calc'!A0", ""])
        sheet.append(["保留", "SHOULD-NOT-USE", "00123", ""])
        sheet["C2"].number_format = "@"
        sheet["C3"].data_type = "s"  # a genuine text cell that merely starts with "="
        path = self.dir / "源文件.xlsx"
        book.save(path)
        return path

    def rows(self, skus):
        return {sku: {"sku": sku, **service.build_row(candidate(sku), "2026-11-27 12:00:00", NOW)} for sku in skus}

    def test_header_priority_sheet_choice_leading_zero_and_duplicates(self):
        path = self.source()
        info = inspect_workbook(path)
        self.assertEqual(info["sheet_name"], "动销总表")
        data = load_input(path, "动销总表")
        self.assertEqual(data.sku_column, 3)  # 库存SKU编号 outranks SKU
        self.assertEqual(data.skus, ("00123", "=cmd|' /C calc'!A0"))
        self.assertEqual(data.row_numbers["00123"], (2, 4))

    def test_export_preserves_source_blocks_formula_injection_and_is_idempotent(self):
        path = self.source()
        before = path.read_bytes()
        data = load_input(path, "动销总表")
        rows = self.rows(data.skus)
        first = export_workbook(data, rows, self.dir / "out")
        self.assertEqual(path.read_bytes(), before)
        book = load_workbook(first)
        sheet = book["动销总表"]
        self.assertEqual(sheet["D2"].value, "原备注")
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(headers[4:], ["创建时间第一店铺", "最早平台创建时间", "创建时间对标状态", "创建时间对标说明"])
        self.assertEqual((sheet["E2"].value, sheet["E4"].value), ("店铺A", "店铺A"))
        self.assertIsInstance(sheet["F2"].value, datetime)
        detail = book["ERP创建时间对标明细"]
        injected = [cell for row in detail.iter_rows(min_row=2) for cell in row if str(cell.value).startswith("=cmd")]
        self.assertTrue(injected and all(cell.data_type == "s" for cell in injected))
        self.assertEqual(detail["G2"].value, "293688122")
        self.assertEqual(detail["G2"].number_format, "@")
        again = export_workbook(load_input(first, "动销总表"), rows, self.dir / "out2")
        self.assertEqual(len(load_workbook(again)["动销总表"][1]), 8)  # no stacked columns
        self.assertEqual(sum("ERP创建时间对标明细" in name for name in load_workbook(again).sheetnames), 1)

    def test_user_sheet_with_same_name_is_not_overwritten(self):
        path = self.source()
        book = load_workbook(path)
        book.create_sheet("ERP创建时间对标明细")["A1"] = "用户内容"
        book.save(path)
        data = load_input(path, "动销总表")
        output = load_workbook(export_workbook(data, self.rows(data.skus), self.dir / "o"))
        self.assertEqual(output["ERP创建时间对标明细"]["A1"].value, "用户内容")
        self.assertIn("ERP创建时间对标明细_2", output.sheetnames)

    def test_text_input_keeps_case_and_suffix(self):
        data = parse_sku_text("  SYNTHETIC-Sku01 \nsynthetic-sku01,SYNTHETIC-Sku01")
        self.assertEqual(data.skus, ("SYNTHETIC-Sku01", "synthetic-sku01"))
        with self.assertRaises(ValueError):
            parse_sku_text(" \n ")


class BridgeTests(unittest.TestCase):
    ENV = {"ERP_SKU_DB_HOST": "h", "ERP_SKU_DB_DATABASE": "d", "ERP_SKU_DB_USERNAME": "u",
           "ERP_SKU_DB_PASSWORD": "private-secret", "ERP_SKU_AUTHORIZED_SHOPS": "*"}

    def bridge(self):
        from unittest.mock import Mock

        from backend.app_bridge import AppBridge

        bridge = AppBridge()
        bridge.config_store, bridge.tasks = Mock(), Mock()
        return bridge

    def test_task_context_carries_no_secrets_and_config_errors_are_actionable(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.ENV):
            bridge = self.bridge()
            bridge.start_erp_sku_earliest_store({"sku_text": "A", "output_dir": directory})
            call = bridge.tasks.start.call_args
            self.assertEqual(call.kwargs["tool"], "erp_sku_earliest_store")
            self.assertNotIn("private-secret", json.dumps(call.kwargs, default=str))
        bridge = self.bridge()  # build before clearing the environment (config paths need HOME/APPDATA)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            result = bridge.start_erp_sku_earliest_store({"sku_text": "A", "output_dir": directory})
            self.assertFalse(result["ok"])
            self.assertIn("ERP", result["error"])
            bridge.tasks.start.assert_not_called()
        bridge = self.bridge()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ERP_SKU_DB_HOST": "custom-only"}, clear=True):
            result = bridge.start_erp_sku_earliest_store({"sku_text": "A", "output_dir": directory})
            self.assertFalse(result["ok"])
            self.assertIn("ERP_SKU_DB_DATABASE", result["error"])
            bridge.tasks.start.assert_not_called()

if __name__ == "__main__":
    unittest.main()

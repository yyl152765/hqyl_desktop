from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import openpyxl
import pytest

from backend import app_bridge
from backend.config_store import AppSettings, BoundAccount
from backend.core import mabang_client
from backend.services import ph_cancel_refund_sync as sync
from launcher.main import resolve_entry_html


def row(order_id, shop_id="7", country="PH"):
    return {"platformOrderId": order_id, "shopId": shop_id, "countryCode": country,
            "platformId": "17", "showOrderStatusText": "取消订单", "exceptionInfo": "=1+1"}


class MockMabang:
    def __init__(self, monkeypatch, pages, receipts=()):
        self.pages = iter(pages)
        self.receipts = iter(receipts)
        self.submissions = []
        self.queries = []
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        monkeypatch.setattr(sync, "MabangClient", self.client)

    def client(self, base_url, **kwargs):
        assert kwargs["verify_ssl"] is True
        return mabang_client.MabangClient(base_url, client=self.http, **kwargs)

    def handle(self, request):
        if not request.url.query:
            return httpx.Response(200, json={"success": True})
        action = request.url.params.get("mod")
        if action == "order.list":
            return httpx.Response(200, text="""
                <script>var orderPageKey = 'fixture-page';</script>
                <label><input name="shopIdMultiple[]" value="7" data-platform-id="17"><span class="text">菲律宾测试店</span></label>
                <label><input name="shopIdMultiple[]" value="8" data-platform-id="17"><span class="text">泰国测试店</span></label>
                <label><input name="shopIdMultiple[]" value="9" data-platform-id="3"><span class="text">菲律宾其他平台</span></label>
            """)
        if action == "order.oTc":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            assert form["a"] == ["abnormalList"]
            assert form["fixCategory[]"] == ["10"]
            assert form["shopIdMultiple[]"] == ["7"]
            assert form["Order.shops[]"] == ["7"]
            assert form["platformId[]"] == ["17"]
            assert form["orderPageKey"] == ["fixture-page"]
            self.queries.append(form)
            page_rows, page_count = next(self.pages)
            return httpx.Response(200, json={"success": True, "orderDataList": page_rows, "pageCount": page_count})
        if action == "order.orderInfoSynchronize":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            assert form["type"] == ["1"]
            assert form["synchronization_type"] == ["2"]
            assert "isSyncStorage" not in form
            self.submissions.append(form["synchronizationOrderData"][0].splitlines())
            receipt = next(self.receipts)
            if isinstance(receipt, Exception):
                raise receipt
            return httpx.Response(200, json=receipt)
        return httpx.Response(200, json={"success": True})


def test_preview_pages_export_text_and_never_call_sync(monkeypatch, tmp_path):
    upstream = MockMabang(monkeypatch, [([row("00000000000000000007")], 2), ([row("TEST-2")], 2)])
    with upstream.http:
        queue, result = sync.preview_queue("fixture-user", "fixture-password", tmp_path, lambda _: None)
    assert list(queue) == ["00000000000000000007", "TEST-2"]
    assert [request["page"] for request in upstream.queries] == [["1"], ["2"]]
    assert upstream.submissions == []
    wb = openpyxl.load_workbook(result["output_file"], read_only=True)
    try:
        assert wb.sheetnames == ["汇总", "概览", "原始数据"]
        assert wb["概览"]["A2"].value == "00000000000000000007"
        assert wb["概览"]["E2"].data_type == "s"
    finally:
        wb.close()


def test_sync_rechecks_only_approved_orders_and_does_not_override_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "SYNC_BATCH", 1)
    current = [row("TEST-1"), row("TEST-2"), row("NEW-ORDER")]
    after = [row("TEST-2"), row("NEW-ORDER")]
    upstream = MockMabang(monkeypatch, [(current, 1), (after, 1)], [{"success": True}, {"success": False, "code": 111}])
    approved = {key: sync.CancelOrder(key, "7", "菲律宾测试店", row(key)) for key in ("TEST-1", "TEST-2", "TEST-3")}
    with upstream.http:
        result = sync.sync_queue("fixture-user", "fixture-password", tmp_path, approved, lambda _: None)
    assert upstream.submissions == [["TEST-1"], ["TEST-2"]]
    assert (result["accepted_count"], result["skipped_count"], result["review_count"], result["remaining_count"]) == (1, 1, 1, 1)
    assert result["is_complete"] is False
    assert result["rows"][0]["reason"] == "已离开异常列表"
    assert result["rows"][1]["status"] == "马帮要求人工确认"


def test_uncertain_receipt_saves_evidence_without_resubmission(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "SYNC_BATCH", 1)
    rows = [row("TEST-1"), row("TEST-2")]
    upstream = MockMabang(monkeypatch, [(rows, 1), (rows, 1)], [httpx.ReadTimeout("fixture-timeout")])
    approved = {item["platformOrderId"]: sync.CancelOrder(item["platformOrderId"], "7", "菲律宾测试店", item) for item in rows}
    with upstream.http:
        result = sync.sync_queue("fixture-user", "fixture-password", tmp_path, approved, lambda _: None)
    assert upstream.submissions == [["TEST-1"]]
    assert result["review_count"] == 2
    assert [item["status"] for item in result["rows"]] == ["回执未知", "未提交"]
    journal, = tmp_path.glob("*/同步记录.jsonl")
    assert [json.loads(line)["event"] for line in journal.read_text(encoding="utf-8").splitlines()] == ["准备提交", "批次回执"]


def test_rejects_order_outside_requested_scope_before_any_submission(monkeypatch, tmp_path):
    upstream = MockMabang(monkeypatch, [([row("OTHER-ORDER", shop_id="8", country="TH")], 1)])
    with upstream.http, pytest.raises(mabang_client.MabangApiError, match="无法核对范围"):
        sync.preview_queue("fixture-user", "fixture-password", tmp_path, lambda _: None)
    assert upstream.submissions == []


def _finished(bridge, task):
    for _ in range(200):
        record = bridge.get_task_status(task["id"])
        if record["status"] not in {"pending", "running"}:
            return record
        time.sleep(0.01)
    pytest.fail("Expected desktop task to finish")


def test_bridge_requires_same_account_confirmation_and_consumes_preview(monkeypatch, tmp_path):
    bridge = app_bridge.AppBridge()
    accounts = [BoundAccount(id=key, vendor="mabang", name=key, username=key, password="fixture-password") for key in ("test-a", "test-b")]
    settings = AppSettings(accounts=accounts, active_account_ids={"mabang": "test-a"})
    bridge.config_store = SimpleNamespace(load=lambda: settings)
    order = sync.CancelOrder("TEST-1", "7", "菲律宾测试店", row("TEST-1"))
    monkeypatch.setattr(sync, "preview_queue", lambda *_: ({order.order_id: order}, {"mode": "preview"}))
    calls = []

    def capture_sync(username, password, output, approved, progress):
        calls.append((username, output, list(approved)))
        return {"mode": "sync"}

    monkeypatch.setattr(sync, "sync_queue", capture_sync)
    task = bridge.start_ph_cancel_refund_preview({"account_id": "test-a", "output_dir": str(tmp_path)})
    record = _finished(bridge, task)
    assert record["status"] == "success"
    token = record["result"]["preview_token"]
    assert not bridge.start_ph_cancel_refund_sync({"preview_token": token, "confirm_sync": "true"})["ok"]
    assert not bridge.start_ph_cancel_refund_sync({"preview_token": token, "confirm_sync": True, "account_id": "test-b"})["ok"]
    result = bridge.start_ph_cancel_refund_sync({"preview_token": token, "confirm_sync": True, "order_ids": ["INJECTED"]})
    assert _finished(bridge, result)["status"] == "success"
    assert not bridge.start_ph_cancel_refund_sync({"preview_token": token, "confirm_sync": True})["ok"]
    assert calls == [("test-a", tmp_path, ["TEST-1"])]
    assert resolve_entry_html(["--page=ph-cancel-refund-sync"]).name == "ph-cancel-refund-sync.html"

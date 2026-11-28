"""Regression coverage for the eight independent acceptance findings."""
import csv
import io
import json
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
import pytest

from backend.core import mabang_sales_report as modern
from backend.core.mabang_client import MabangApiError, MabangClient
from backend.core.mabang_sales_report_classic import (
    iter_report_pages,
    validate_filter_echo,
)
from backend.services import developer_sales_income_summary as income
from backend.services import group_sales_report as group
from backend.services import warehouse_sales_export as warehouse
from tests.sales_report_fixtures import (
    ClassicClient,
    page_payload,
    sales_csv,
    sales_row,
)

START, END = date(2026, 11, 1), date(2026, 11, 30)
TARGET = income.PersonIncomeTarget("开发员", "developid[]", income.PersonOption("7", "合成人员"))


def make_client(handler, is_modern=False):
    client = MabangClient("https://erp.example.invalid", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client._logged_in = True
    client._modern_sales_report_available = is_modern
    return client


def modern_data(rows, total=None, **overrides):
    return {"total": len(rows) if total is None else total, "data": rows, "reportDate": "2026-11-28",
            "updateTime": "2026-11-28 01:00:00", "timezone": "UTC+8", "moneySuffix": "USD", **overrides}


def test_modern_client_income_contract_preserves_amount_and_zero_id():
    requests = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text='<iframe id="iframeContent" src="https://fe-private.mabangerp.com/views/product-sale-reports?key=synthetic-key&amp;tz=UTC%2B8"></iframe>')
        if request.url.path == modern.AUTH_PATH:
            return httpx.Response(200, json={"code": 200, "data": {"token": "synthetic-token"}})
        requests.append(json.loads(request.content))
        assert request.url.path == modern.COUNTRY_REPORT_PATH
        return httpx.Response(200, json={"code": 200, "data": modern_data([{"stockid": 1, "stockSku": "SYNTH", "saleMoney": "123.4567"}])})
    client = make_client(handler, True)
    try:
        target = income.PersonIncomeTarget("开发员", "developid[]", income.PersonOption("0", "未分配"))
        assert income.fetch_person_income_total(client, target, START, END) == Decimal("123.4567")
        assert requests[0]["developId"] == ["0"]
        assert len(requests) == 1
    finally:
        client.client.close()


@pytest.mark.parametrize("rows,overrides", [([], {}), ([{"stockSku": "SYNTH", "income": "1"}], {"moneySuffix": "RMB"}),
                                             ([{"stockSku": "SYNTH", "income": "1"}], {"updateTime": None})])
def test_modern_empty_or_invalid_currency_snapshot(rows, overrides):
    session = Mock()
    session.post.return_value = modern_data(rows, **overrides)
    if overrides:
        with pytest.raises(MabangApiError):
            modern._fetch_pages(session, modern.STOCK_SHOP_REPORT_PATH, {"currency": "USD"}, context="test")
    else:
        assert modern._fetch_pages(session, modern.STOCK_SHOP_REPORT_PATH, {"currency": "USD"}, context="test")[1] == 0


@pytest.mark.parametrize("second", [
    modern_data([{"stockid": 1, "stockSku": "A"}, {"stockid": 2, "stockSku": "B"}], total=4),
    modern_data([{"stockid": 3, "stockSku": "C"}], total=4),
    modern_data([{"stockid": 3, "stockSku": "C"}, {"stockid": 4, "stockSku": "D"}], total=5),
    modern_data([{"stockid": 3, "stockSku": "C"}, {"stockid": 4, "stockSku": "D"}], total=4, updateTime="changed"),
])
def test_modern_rejects_repeated_short_changed_pages(second):
    session = Mock()
    session.post.side_effect = [modern_data([{"stockid": 1, "stockSku": "A"}, {"stockid": 2, "stockSku": "B"}], total=4), second]
    with patch.object(modern, "PAGE_SIZE", 2), pytest.raises(MabangApiError):
        modern._fetch_pages(session, modern.STOCK_SHOP_REPORT_PATH, {"currency": "USD"}, context="test")


@pytest.mark.parametrize("payload", [{"success": True}, {"success": True, "tableContent": "<tr><td>商品</td></tr>", "pageHtml": "共 1 条"},
                                      {"success": True, "tableContent": '<tr><td data-field="income">5</td></tr>', "pageHtml": "共 2001 条"}])
def test_classic_person_does_not_treat_missing_structure_as_zero(payload):
    client = Mock()
    client._post_sales_report.return_value = payload
    with pytest.raises(MabangApiError):
        income.fetch_person_income_total(client, TARGET, START, END)


def test_classic_person_zero_and_negative_income_are_valid():
    assert income.fetch_person_income_total(ClassicClient([]), TARGET, START, END) == 0
    assert income.fetch_person_income_total(ClassicClient([sales_row(income="-0.1234")]), TARGET, START, END) == Decimal("-0.1234")


def test_classic_observed_empty_placeholder_is_zero_but_missing_data_is_not():
    placeholder = '<tr><td colspan="12"><div class="alert alert-nodata group-nodata"><h4><span>暂无数据</span></h4></div></td></tr>'
    valid = {"success": True, "pageHtml": "", "tableContent": placeholder}
    client = Mock()
    client._post_sales_report.return_value = valid
    assert income.fetch_person_income_total(client, TARGET, START, END) == 0
    for invalid in ({**valid, "tableContent": ""}, {**valid, "success": False},
                    {**valid, "tableContent": placeholder + '<tr><td data-field="income">10</td></tr>'},
                    {**valid, "pageHtml": "共 1 条"}):
        client._post_sales_report.return_value = invalid
        with pytest.raises(MabangApiError):
            income.fetch_person_income_total(client, TARGET, START, END)


def test_group_workbook_preserves_text_identifiers(tmp_path):
    from openpyxl import load_workbook

    source = tmp_path / "sales.csv"
    rows = [sales_row("000001", item_id="0000123", NCM="00042"),
            sales_row("000002", item_id="12345678901234567890", NCM="00043")]
    source.write_bytes(sales_csv(rows))
    workbook = group.Workbook()
    group.append_csv_sheet(workbook, source, "sales", set())
    target = tmp_path / "sales.xlsx"
    workbook.save(target)
    workbook.close()
    with target.open("rb") as stream:
        actual = load_workbook(stream)
        sheet = actual["sales"]
        for index, row in enumerate(rows, start=2):
            for column in ("SKU", "item_id", "NCM"):
                cell = sheet.cell(index, group.TARGET_SALES_HEADERS.index(column) + 1)
                assert cell.value == row[column]
                assert cell.data_type == "s"
        actual.close()


def test_classic_rejects_duplicate_pages_and_changed_snapshot():
    client = Mock()
    first = page_payload([sales_row("A"), sales_row("B")], total=4)
    for second in (first, page_payload([sales_row("C"), sales_row("D")], total=4, snapshot="changed")):
        client._post_sales_report.side_effect = [first, second]
        with pytest.raises(MabangApiError):
            list(iter_report_pages(client, url="unused", params={}, form_data={}, referer="", context="test", page_size=2))


def test_classic_echo_verifies_scope_currency_and_boundary():
    form = [("shopLabelIds[]", "1"), ("stockWarehouseId[]", "2"), ("moneyType", "RMB"), ("timeStart", "2026-11-01 00:00:00")]
    echo = {"shopLabelIds": ["1"], "stockWarehouseId": ["2"], "moneyType": "RMB", "timeStart": "2026-11-01 00:00:00 00:00:00"}
    validate_filter_echo({"postdata": echo}, form, "test")
    for key, value in (("stockWarehouseId", ["3"]), ("shopLabelIds", ["9"]), ("moneyType", "US")):
        with pytest.raises(MabangApiError):
            validate_filter_echo({"postdata": {**echo, key: value}}, form, "test")
    validate_filter_echo({"postdata": {"developIdIn": "0,7", "saleIdIn": "8"}},
                         [("developid[]", "0"), ("developid[]", "7"), ("saleid[]", "8")], "test")


def test_intersection_excludes_unselected_categories_sharing_a_sku(tmp_path):
    transactions = [("1", "2", 1), ("9", "2", 2), ("1", "3", 3)]
    class TransactionsClient(ClassicClient):
        def _post_sales_report(self, *, form_data, **kwargs):
            cats = {v for k, v in form_data if k == "shopLabelIds[]"}
            warehouses = {v for k, v in form_data if k == "stockWarehouseId[]"}
            quantity = sum(q for c, w, q in transactions if c in cats and w in warehouses)
            row = sales_row(quantity=str(quantity), income=str(quantity*10), 毛利=str(quantity*2))
            return page_payload([row])
    client = TransactionsClient([sales_row()])
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), ("2", "3"), "2026-11", "2026-11", tmp_path)
    content = warehouse.export_intersection_csv(client, job, START, END, 30)
    row = next(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert Decimal(row["销售数量"]) == 4
    assert Decimal(row["收入-订单金额"]) == 40
    assert Decimal(row["平均销售价格"].strip()) == 10
    assert Decimal(row["毛利率%"]) == 20
    assert Decimal(row["统一成本价"]) == 3
    assert Decimal(row["重量(单位:g)"]) == 100


def test_intersection_preserves_tail_fields_and_derives_average(tmp_path):
    row = sales_row(**{"退款金额": "1.25", "退款率%": "3", "毛利": "2", "毛利率%": "20"})
    client = ClassicClient([row], csv_content=sales_csv([row], classic_layout=True))
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), ("2",), "2026-11", "2026-11", tmp_path)
    result = next(csv.DictReader(io.StringIO(warehouse.export_intersection_csv(client, job, START, END, 30).decode("utf-8-sig"))))
    assert [result[c] for c in ("退款金额", "退款率%", "毛利", "毛利率%", "item_id")] == ["1.25", "3", "2", "20", "0000123"]
    assert Decimal(result["平均销售价格"].strip()) == 10


def test_classic_real_client_total_does_not_call_modern_auth():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"success": True, "pageHtml": "共 2000 条"})
    client = make_client(handler)
    try:
        assert warehouse.query_report_total(client, warehouse.build_report_form(("1",), (), START, END), "test") == 2000
    finally:
        client.client.close()


def test_non_boundary_truncation_is_rejected(tmp_path):
    rows = [sales_row("A"), sales_row("B"), sales_row("C")]
    client = ClassicClient(rows, csv_content=sales_csv(rows[:2]))
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), (), "2026-11", "2026-11", tmp_path)
    with patch.object(warehouse, "MAX_EXPORT_ATTEMPTS", 1), pytest.raises(MabangApiError, match="截断"):
        warehouse.export_month(client, tmp_path, job, 2026, 11)
    assert not list(tmp_path.glob("*.csv"))


def test_group_missing_columns_and_warehouse_bad_quantity_cannot_succeed(tmp_path):
    client = ClassicClient([], csv_content="SKU,销售数量\nA,1\n".encode())
    with pytest.raises(MabangApiError, match="列不完整"):
        group.export_group_csv(client, tmp_path, group.SalesGroup("合成", "1"), "period", START, END)
    row = sales_row(quantity="invalid")
    client = ClassicClient([row])
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), (), "2026-11", "2026-11", tmp_path)
    with patch.object(warehouse, "MAX_EXPORT_ATTEMPTS", 1), pytest.raises(MabangApiError):
        warehouse.export_month(client, tmp_path, job, 2026, 11)
    assert not list(tmp_path.glob("*.csv"))


def test_modern_field_alias_conflict_is_not_guessed():
    with pytest.raises(MabangApiError, match="歧义"):
        modern._field_value({"shippingCostNew": "5", "shippingCost": "8"}, "支出-运费")


def test_modern_401_refreshes_once_and_does_not_leak_secrets(caplog):
    responses = [httpx.Response(401), httpx.Response(200, json={"code": 200, "data": {"ok": True}})]
    client = make_client(lambda request: responses.pop(0), True)
    try:
        with patch.object(modern, "_authenticate", return_value={"Authorization": "Bearer synthetic-secret"}) as auth:
            session = modern.ModernSalesReportSession(client)
            assert session.post("/test", {}, context="test") == {"ok": True}
            assert auth.call_count == 2
        assert "synthetic-secret" not in caplog.text
    finally:
        client.client.close()


def test_modern_repeated_401_stops_after_one_refresh():
    client = make_client(lambda request: httpx.Response(401), True)
    try:
        with patch.object(modern, "_authenticate", return_value={}) as auth:
            session = modern.ModernSalesReportSession(client)
            with pytest.raises(MabangApiError):
                session.post("/test", {}, context="test")
            assert auth.call_count == 2
    finally:
        client.client.close()


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 200])
def test_modern_transient_and_non_json_responses_have_bounded_retry(status):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(status, text="secret token and non-json payload")
    client = make_client(handler, True)
    try:
        with patch.object(modern.time, "sleep"), pytest.raises(MabangApiError, match="重试耗尽") as error:
            modern._json_request(client, "/test", {}, {}, context="test")
        assert count == 3
        assert "secret" not in str(error.value)
    finally:
        client.client.close()


def test_classic_error_does_not_include_remote_body_or_query():
    from backend.core.mabang_client import _response_json_dict
    response = httpx.Response(200, text="<iframe>synthetic-secret</iframe>", request=httpx.Request("GET", "https://example.invalid/?key=synthetic-key"))
    with pytest.raises(MabangApiError) as error:
        _response_json_dict(response, context="test")
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-key" not in str(error.value)


def test_modern_permission_denial_is_not_retried():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403, text="synthetic-secret")
    client = make_client(handler, True)
    try:
        with pytest.raises(MabangApiError, match="403"):
            modern._json_request(client, "/test", {}, {}, context="test")
        assert len(requests) == 1
    finally:
        client.client.close()


def test_warehouse_missing_product_ids_are_enriched_without_replacing_metrics(tmp_path):
    from backend.core.mabang_client import MabangExportFile
    row = sales_row(quantity="3", income="30")
    stream = io.StringIO()
    headers = [c for c in group.TARGET_SALES_HEADERS if c not in {"item_id", "NCM"}]
    writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerow(row)
    class Client(ClassicClient):
        def download_product_sales_report_csv(self, **kwargs):
            if kwargs["report_type"] == "countryReports":
                assert kwargs["fields"] == ("salesSkuNewId",)
                assert not any(k == "stockWarehouseId[]" for k, _ in kwargs["form_data"])
                return MabangExportFile("meta.csv", "csv", "", b"SKU,item_id,NCM\nSYNTH-001,0000123,NCM\n")
            return super().download_product_sales_report_csv(**kwargs)
    client = Client([row], csv_content=stream.getvalue().encode("utf-8-sig"))
    job = warehouse.WarehouseSalesExportJob("u", "p", (), ("2",), "2026-11", "2026-11", tmp_path)
    result = warehouse.export_month(client, tmp_path, job, 2026, 11)
    output = next(csv.DictReader(io.StringIO(result.csv_path.read_text(encoding="utf-8-sig"))))
    assert output["item_id"] == "0000123"
    assert output["销售数量"] == "3"
    assert output["收入-订单金额"] == "30"


def test_warehouse_unknown_directory_id_is_rejected():
    with pytest.raises(MabangApiError, match="不可见"):
        warehouse._resolve_names(("unknown",), (warehouse.FilterOption("1", "合成仓"),))


def test_metadata_uses_bounded_native_sku_batches(tmp_path):
    from backend.core.mabang_client import MabangExportFile
    rows = [sales_row(f"SKU-{i}", item_id=f"000{i}") for i in range(5)]
    calls = []
    class Client:
        def download_product_sales_report_csv(self, **kwargs):
            form = dict(kwargs["form_data"])
            batch = form["selectValueMore"].splitlines()
            assert form["formType"] == "2" and form["selectTypeMore"] == "stockSkuIn"
            assert form["shopLabelIds[]"] == "1" and "stockWarehouseId[]" not in form
            assert len(batch) <= 2
            calls.append(batch)
            return MabangExportFile("meta.csv", "csv", "", sales_csv([r for r in rows if r["SKU"] in batch]))
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), ("2",), "2026-11", "2026-11", tmp_path)
    detail = [{"SKU": r["SKU"], "销售数量": "9"} for r in rows]
    with patch.object(warehouse, "METADATA_BATCH_SIZE", 2):
        warehouse._add_product_metadata(Client(), detail, job, START, END)
    assert len(calls) == 3 and sum(map(len, calls)) == 5
    assert [r["item_id"] for r in detail] == [r["item_id"] for r in rows]
    assert all(r["销售数量"] == "9" for r in detail)


@pytest.mark.parametrize("extra", [[], [sales_row("UNSELECTED")]])
def test_metadata_rejects_missing_and_out_of_scope_skus(tmp_path, extra):
    client = ClassicClient(extra)
    job = warehouse.WarehouseSalesExportJob("u", "p", (), ("2",), "2026-11", "2026-11", tmp_path)
    with pytest.raises(MabangApiError):
        warehouse._add_product_metadata(client, [{"SKU": "NEEDED"}], job, START, END)


def test_partial_month_failure_is_exposed_as_incomplete(tmp_path):
    from types import SimpleNamespace
    from backend.app_bridge import AppBridge
    bridge = AppBridge.__new__(AppBridge)
    bridge.tasks = Mock()
    job = warehouse.WarehouseSalesExportJob("u", "p", ("1",), (), "2026-10", "2026-11", tmp_path)
    result = SimpleNamespace(month_count=1, record_count=1, output_file=tmp_path/"x.xlsx", output_dir=tmp_path,
                             failed_months=("2026-10",), months=(warehouse.MonthExport("2026-11", 1, tmp_path/"x.csv"),))
    with patch.object(AppBridge, "_payload_with_account", return_value={"output_dir": str(tmp_path)}), patch("backend.app_bridge.validate_warehouse_export_payload", return_value=job), patch("backend.app_bridge.run_warehouse_sales_export", return_value=result):
        bridge.start_warehouse_sales_export({})
        runner = bridge.tasks.start.call_args.args[1]
        value = runner(lambda message: None)
    assert value["is_complete"] is False
    assert value["failed_months"] == ["2026-10"]
    assert value["output_file"].endswith("x.xlsx")

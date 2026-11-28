"""Synthetic examples of the native SPA DTO, never production customer data."""
import csv
import io
from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
import pytest

from backend.core import mabang_sales_report as sales
from backend.core import mabang_sales_metadata as metadata
from backend.core.mabang_client import MabangApiError, MabangClient, MabangLoginError
from backend.services import developer_sales_income_summary as income


def product(**overrides):
    # Explicit native names: this fixture deliberately does not derive its keys
    # from FIELD_CANDIDATES, so a wrong implementation mapping fails the test.
    row = dict(stockid=10, stockSku="000-SKU", nameCN="合成商品", nameEN=None, itemId="00001234567890123456789",
               ncm=None, salesSku="MAIN-001", salesSkuNewId=123, status=3, statusText=None,
               defaultCost="3", weight="100", stockQuantity=40, timeCreated="2026-01-01",
               sale="合成销售", develop="合成开发", orderNum=2, quantity=3, quantityAvg="99",
               averageSellingPrice="99", saleMoney="30", shippingTotal="3", otherIncome=0,
               subsidyAmount=0, incomeTotal="33", itemTotalCost="9", shippingCostNew="2",
               shippingCost="2", packageFee=None, platformFee="1", paypalFee=0, headShipping=0,
               fbaMoney=0, vatTaxation=0, voucherPrice=0, otherMoney="1", otherFee="999",
               otherExpend=0, evaluationFee=0, expendTotal="13", refundNum=1, itemRefundNum=1,
               refundMoney=None, refundMoneyRMB="-2", refundFeeRMB="-2", returnRate="0.125",
               gross="22", grossRMB="22", grossRate="0.73333333", deductibleShipping="3", stockWarehouseId=100)
    return dict(row, **overrides)


def page(rows, **overrides):
    return dict(data=rows, total=len(rows), moneySuffix="RMB", reportDate="2026-11-28 00:00:00",
                updateTime="2026-11-28 11:00:00", timezone="UTC+8", **overrides)


def test_real_field_semantics_precision_percent_and_explicit_null():
    row = next(csv.DictReader(io.StringIO(sales.rows_to_sales_csv([product()], expected_days=7, context="test").decode("utf-8-sig"))))
    assert len(row) == 41
    assert row["SKU"] == "000-SKU" and row["item_id"] == "00001234567890123456789"
    assert row["主SKU"] == "MAIN-001" and row["状态"] == "正常"
    assert row["商品英文名"] == "" and row["NCM"] == ""
    assert Decimal(row["支出-包材费"]) == 0
    assert Decimal(row["支出-其他费用"]) == 1  # otherMoney, not otherFee
    assert Decimal(row["退款金额"]) == -2  # native nullable alias fallback
    assert row["退款率%"] == "12.50" and row["毛利率%"] == "73.33"
    assert row["平均单天销量"].strip() == "0.43" and row["平均销售价格"].strip() == "10.0000"
    assert sales._field_value(product(shippingCostNew="2.0000", shippingCost=2), "支出-运费") == "2.0000"
    with pytest.raises(MabangApiError):
        sales.rows_to_sales_csv([product(grossRate="73.33%")], expected_days=7, context="test")


@pytest.mark.parametrize("field", ["saleMoney", "quantity", "incomeTotal", "expendTotal", "gross"])
def test_missing_or_null_principal_measure_never_becomes_zero(field):
    for missing in (False, True):
        row = product(**{field: None})
        if missing:
            del row[field]
        with pytest.raises(MabangApiError):
            sales.rows_to_sales_csv([row], expected_days=1, context="test")


def test_country_mode_batch_and_sales_directory_are_native():
    filters = sales.modern_filters_from_legacy_form({"timeStart": "2026-11-01 00:00:00", "timeEnd": "2026-11-30 23:59:59",
        "formType": "2", "selectTypeMore": "stockSkuIn", "selectValueMore": "A\nB", "developid[]": "0"}, report_type="countryReports")
    assert filters["type"] == "countryCode" and filters["searchValues"] == ["A", "B"]
    assert sales._report_path_for_filters(filters) == sales.COUNTRY_REPORT_PATH
    assert sales.parse_filter_options_payload({"sales": [{"id": 0, "name": "无销售员"}]}).salespeople[0].id == "0"


def test_same_product_with_changed_amount_on_second_page_is_rejected():
    session = Mock()
    session.post.side_effect = [dict(page([product()]), total=2), dict(page([product(saleMoney=35)]), total=2)]
    with patch.object(sales, "PAGE_SIZE", 1), pytest.raises(MabangApiError, match="重复商品身份"):
        sales._fetch_pages(session, sales.COUNTRY_REPORT_PATH, {"currency": "RMB"}, context="test")


@pytest.mark.parametrize("status", [403, 502])
def test_failed_probe_never_selects_classic_or_caches_false(status):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret"))) as http:
        client = MabangClient("https://erp.example.invalid", client=http)
        client._logged_in = True
        with pytest.raises(MabangApiError, match=str(status)) as exc:
            client.uses_modern_sales_report()
        assert "secret" not in str(exc.value)
        assert getattr(client, "_modern_sales_report_available", None) is None


def test_probe_accepts_observed_classic_controls_but_rejects_generic_200_page():
    for body, expected in [('<input name="shopLabelIds[]" value="1">', False), ("<h1>Access denied</h1>", None)]:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as http:
            client = MabangClient("https://erp.example.invalid", client=http)
            client._logged_in = True
            if expected is None:
                with pytest.raises(MabangApiError):client.uses_modern_sales_report()
            else:assert client.uses_modern_sales_report() is expected


@pytest.mark.parametrize("stage,reason", [(0, "entry_forbidden"), (1, "login_forbidden"), (2, "finalize_forbidden")])
def test_403_login_stage_is_diagnostic_and_never_retried(stage, reason):
    calls = []
    def handler(request):
        index = len(calls)
        calls.append(request)
        return httpx.Response(403, text="secret-cookie") if index == stage else httpx.Response(200, json={"success": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MabangClient("https://erp.example.invalid", client=http)
        with pytest.raises(MabangLoginError) as exc:
            client.login("synthetic-user", "synthetic-password")
        assert exc.value.reason == reason and "secret" not in str(exc.value)
        assert len(calls) == stage + 1 and not client._logged_in


def test_session_reused_but_each_expired_request_gets_only_one_refresh():
    client = Mock(spec=["_modern_report_session"])
    client._modern_report_session = None
    with patch.object(sales, "_authenticate", return_value={}) as auth, patch.object(sales, "_json_request") as request:
        session = sales._session(client)
        assert sales._session(client) is session
        for _ in range(2):
            request.side_effect = [sales.ModernSalesAuthenticationExpired(), {"ok": True}]
            assert session.post("/query", {}, context="test") == {"ok": True}
        assert auth.call_count == 3


def test_warehouse_uses_child_identity_and_merges_only_additive_fields():
    left, right = product(), product(stockWarehouseId=101, quantity=2, saleMoney="20", incomeTotal="22", gross="9", grossRMB="9")
    session = Mock()
    session.filter_options.return_value = sales.ModernFilterOptions((), (sales.ModernOption("100", "A"), sales.ModernOption("101", "B")), (), ())
    def post(path, body, **kwargs):
        if path == sales.WAREHOUSE_CHILD_PATH:
            source = left if body["stockIds"] == "100" else right
            return dict(page([source]), reportDate=None, updateTime=None, timezone=None)
        if path == sales.WAREHOUSE_REPORT_PATH:
            assert body["stockWarehouseId"] == []
            return page([left, right])
        if path == sales.COUNTRY_REPORT_PATH:
            return page([product(itemId="00001", stockQuantity=40)])
        raise AssertionError(path)
    session.post.side_effect = post
    rows, total, snapshot = sales._fetch_warehouse_rows(session, {"reportType": "warehouseReports", "currency": "RMB",
        "stockWarehouseId": [100, 101], "shopLabelIds": ["1", "2"]}, context="test")
    row = rows[0]
    assert total == 1 and row["quantity"] == 5 and row["saleMoney"] == 50
    assert row["stockQuantity"] == 40 and row["weight"] == "100" and row["defaultCost"] == "3"
    assert row["itemId"] == "00001" and row["grossRate"] == Decimal(31)/49
    assert snapshot[0] == "2026-11-28 00:00:00"


def test_warehouse_wrong_scope_and_missing_anchor_are_rejected():
    session = Mock()
    session.post.return_value = dict(page([product(stockWarehouseId=999)]), reportDate=None, updateTime=None, timezone=None)
    with pytest.raises(MabangApiError, match="仓库身份"):
        sales._fetch_pages(session, sales.WAREHOUSE_CHILD_PATH, {"currency": "RMB", "stockWarehouseId": [100]},
                           context="test", allow_undated_child=True)
    with pytest.raises(MabangApiError, match="快照"):
        sales._fetch_pages(session, sales.WAREHOUSE_CHILD_PATH, {"currency": "RMB"}, context="test")


def test_parent_financial_reconciliation_is_required():
    with pytest.raises(MabangApiError, match="不一致"):
        sales._verify_warehouse_amounts(product(saleMoney=40), [product()], "test")


@pytest.mark.parametrize("failure", ["", "missing", "wrong_id", "wrong_sku", "missing_ncm", "duplicate"])
def test_product_master_ncm_matches_both_sku_and_stock_id(failure):
    row = {"id": "10", "stockSku": "000-SKU", "stockNcm": "00123.45"}
    if failure == "wrong_id":row["id"] = "99"
    if failure == "wrong_sku":row["stockSku"] = "OTHER"
    if failure == "missing_ncm":del row["stockNcm"]
    rows = [] if failure == "missing" else [row, row] if failure == "duplicate" else [row]
    def handler(request):
        assert request.url.params["mod"] == "stock.getStockList"
        assert b"selecttype=stockSku" in request.content and b"status=&" in request.content
        return httpx.Response(200, json={"success": True, "stockData": rows})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MabangClient("https://erp.example.invalid", client=http)
        client._sales_stock_referer = metadata.STOCK_ORIGIN
        products = [product()]
        if failure:
            with pytest.raises(MabangApiError):metadata.enrich_ncm(client, products)
        else:
            metadata.enrich_ncm(client, products)
            assert products[0]["ncm"] == "00123.45" and products[0]["saleMoney"] == "30"


def test_duplicate_names_preserve_independent_ids_and_existing_suffix():
    options = [income.PersonOption("1", "同名"), income.PersonOption("2", "同名"), income.PersonOption("3", "同名>1")]
    labels = income._distinct_person_labels(options)
    assert [p.name for p in labels] == ["同名 [ID=1]", "同名 [ID=2]", "同名>1"]
    selected, missing = income.resolve_developer_options(["同名"], options)
    assert [p.id for p in selected] == ["1", "2", "3"] and not missing


def test_native_dash_person_placeholder_is_unassigned():
    content = "SKU,开发员,销售员,收入-订单金额\nS,-,-,12.1234\n".encode()
    row = income.parse_income_detail_rows(content, context="test")[0]
    assert row.developer == income.UNASSIGNED_DEVELOPER
    assert row.salesperson == income.UNASSIGNED_SALESPERSON and row.amount == Decimal("12.1234")


def test_blank_zero_person_label_survives_and_departed_aggregate_is_excluded():
    options = sales.parse_filter_options_payload({"developers": [{"id": 0, "name": ""}, {"id": -2, "name": "离职开发员"}],
        "sales": [{"id": 0, "name": ""}, {"id": -2, "name": "离职销售员"}]})
    client = Mock()
    client.uses_modern_sales_report.return_value = True
    with patch.object(income, "fetch_modern_filter_options", return_value=options):
        developers, salespeople = income.fetch_report_person_options(client)
    assert developers == [income.PersonOption("0", income.UNASSIGNED_DEVELOPER)]
    assert salespeople == [income.PersonOption("0", income.UNASSIGNED_SALESPERSON)]


def test_auth_and_probe_find_same_trusted_frame_after_unrelated_iframe():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text='<iframe id="iframeContent" src="https://example.invalid/other"></iframe>'
                '<iframe src="https://fe-private.mabangerp.com:invalid/views/product-sale-reports?key=synthetic"></iframe>'
                '<iframe src="https://fe-private.mabangerp.com/views/product-sale-reports?key=synthetic"></iframe>')
        return httpx.Response(200, json={"code": 200, "data": {"token": "synthetic"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MabangClient("https://erp.example.invalid", client=http)
        client._logged_in = True
        assert client.uses_modern_sales_report()
        assert sales._authenticate(client)["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize("empty_parent,empty_child", [(False, False), (False, True), (True, True)])
def test_warehouse_only_reconciles_independent_entire_date_range(empty_parent, empty_child):
    session = Mock()
    session.filter_options.return_value = sales.ModernFilterOptions((), (sales.ModernOption("100", "A"),), (), ())
    ranges = []
    parent = product(quantity=1, saleMoney=10, incomeTotal=11, expendTotal=5, gross=6, grossRMB=6)
    child = product(quantity=2, saleMoney=20, incomeTotal=22, expendTotal=10, gross=12, grossRMB=12)
    def post(path, body, **kwargs):
        if path == sales.WAREHOUSE_REPORT_PATH:
            assert not body.get("searchValues") and body["stockWarehouseId"] == []
            ranges.append((body["timeStart"], body["timeEnd"]))
            return page([] if empty_parent else [parent])
        if path == sales.WAREHOUSE_CHILD_PATH:
            return dict(page([] if empty_child else [child]), reportDate=None, updateTime=None, timezone=None)
        return page([product()])
    session.post.side_effect = post
    filters = {"reportType": "warehouseReports", "currency": "RMB", "stockWarehouseId": [100], "shopLabelIds": [],
               "timeStart": "2026-11-01 00:00:00", "timeEnd": "2026-11-08 23:59:59"}
    if empty_child and not empty_parent:
        with pytest.raises(MabangApiError, match="不一致"):
            sales._fetch_warehouse_rows(session, filters, context="test")
    else:
        rows, total, snapshot = sales._fetch_warehouse_rows(session, filters, context="test")
        assert total == (0 if empty_child else 1)
        assert snapshot[0] and bool(rows) != empty_child
    assert ranges[:2] == [("2026-11-01 00:00:00", "2026-11-07 23:59:59"), ("2026-11-08 00:00:00", "2026-11-08 23:59:59")]


def test_historical_zero_id_does_not_drop_contributions_or_borrow_master_ncm():
    session = Mock()
    session.post.return_value = page([product(), product(stockid=0, status=-1, quantity=0, saleMoney=-1)])
    rows, total, _ = sales._fetch_pages(session, sales.COUNTRY_REPORT_PATH, {"currency": "RMB"}, context="test")
    assert total == 2 and sum(Decimal(str(r["saleMoney"])) for r in rows) == 29
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"success": True,
            "stockData": [{"id": 10, "stockSku": "000-SKU", "stockNcm": "00123"}]}))) as http:
        client = MabangClient("https://erp.example.invalid", client=http)
        client._sales_stock_referer = metadata.STOCK_ORIGIN
        metadata.enrich_ncm(client, rows)
        assert rows[0]["ncm"] == "00123" and rows[1]["ncm"] is None


def test_income_projection_does_not_depend_on_product_master_or_41_column_metadata():
    rows = [{"stockid": 1, "stockSku": "S", "sale": None, "develop": "开发", "saleMoney": "1.2345"}]
    with patch.object(sales, "_session"), patch.object(sales, "_fetch_report_rows", return_value=(rows, 1, ("2026-11-28",))), patch.object(metadata, "enrich_ncm") as enrich:
        result = sales.modern_download_product_sales_report_csv(Mock(), fields=income.SUMMARY_EXPORT_FIELDS,
            form_data={"timeStart": "2026-11-01 00:00:00", "timeEnd": "2026-11-30 23:59:59", "moneyType": "US"}, report_type="countryReports")
    assert not enrich.called
    assert income.parse_income_rows(result.content, context="test") == [(income.UNASSIGNED_SALESPERSON, Decimal("1.2345"))]


def test_failed_worker_login_closes_its_client():
    client = Mock()
    client.login.side_effect = MabangLoginError("entry_forbidden")
    with patch.object(income, "MabangClient", return_value=client), pytest.raises(MabangLoginError):
        income.collect_all_people_income_totals(username="u", password="p", developer_options=[income.PersonOption("1", "合成")],
            salesperson_options=[], start_date=None, end_date=None, max_workers=1)
    client.close.assert_called_once()


def test_warehouse_auxiliary_field_missing_is_typed_but_explicit_null_is_zero():
    with pytest.raises(MabangApiError, match="refundFeeRMB"):
        sales._extra_measure({}, "refundFeeRMB", "test")
    assert sales._extra_measure({"refundFeeRMB": None}, "refundFeeRMB", "test") == 0


def test_snapshot_refresh_restarts_whole_read_once_without_mixing_pages():
    session = Mock()
    session.post.side_effect = [dict(page([product(saleMoney=99)]), total=2), dict(page([product(stockid=11)]), total=3),
        dict(page([product(saleMoney=12)]), total=2), dict(page([product(stockid=11, stockSku="B", saleMoney=13)]), total=2)]
    with patch.object(sales, "PAGE_SIZE", 1), patch.object(sales.time, "sleep"):
        rows, total, _ = sales._fetch_report_rows(session, {"reportType": "countryReports", "currency": "RMB"}, context="test")
    assert total == 2 and [r["saleMoney"] for r in rows] == [12, 13]
    assert [call.args[1]["page"] for call in session.post.call_args_list] == [1, 2, 1, 2]
    with patch.object(sales, "_fetch_pages", side_effect=sales.ModernSalesSnapshotChanged("changed")) as fetch, patch.object(sales.time, "sleep"):
        with pytest.raises(sales.ModernSalesSnapshotChanged):
            sales._fetch_report_rows(session, {"reportType": "countryReports"}, context="test")
        assert fetch.call_count == 2

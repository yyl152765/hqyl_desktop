from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl
from unittest.mock import patch

import httpx

from backend.config_store import ConfigStore
from backend.services.mabang_income_expense_report import (
    INCOME_EXPORT_FIELDS,
    INCOME_EXPORT_MOD,
    KNOWN_EMPTY_EXPORT_COLUMNS,
    ORDERED_COLUMNS,
    OVERSEAS_WAREHOUSES,
    REPORT_CATEGORIES,
    IncomeExpenseCsvExporter,
    IncomeExpenseExportError,
    build_income_detail_form,
    fetch_target_rows,
    get_category_options,
    get_warehouse_options,
    parse_malaysia_report_shop_scope,
    parse_malaysia_temu_shop_ids,
    parse_income_pagination,
    previous_month_date_range,
    process_income_rows,
    run_income_expense_report,
    validate_income_expense_payload,
)


class FakeIncomeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_form_json(self, _path: str, **kwargs):
        form = dict(kwargs["form_data"])
        self.calls.append(form)
        page = int(form["page"])
        if page == 1:
            return {
                "success": True,
                "pageHtml": '<span class="semibold">1/2</span>页 共 3 条',
                "exportData": [
                    {"platformOrder": "A-1", "expressTime": "2026-08-01 09:00:00"},
                    {"platformOrder": "A-2", "expressTime": "2026-08-02 09:00:00"},
                ],
            }
        return {
            "success": True,
            "pageHtml": '<span class="semibold">2/2</span>页 共 3 条',
            "exportData": [
                {"platformOrder": "A-3", "expressTime": "2026-08-03 09:00:00"},
            ],
        }


RAW_EXPORT_HEADER = [
    column for column in ORDERED_COLUMNS if column not in KNOWN_EMPTY_EXPORT_COLUMNS
]


def _official_export_row(order_number: str, marker: str = "") -> list[str]:
    values = {column: "" for column in RAW_EXPORT_HEADER}
    values.update(
        {
            "订单编号": order_number,
            "交易号": f"TX-{marker}" if marker else "",
            "店铺名称": f"SHOP-{marker}" if marker else "",
            "国家": "泰国" if marker else "",
            "销量": "1" if marker else "2",
            "商品种类": "1" if marker else "2",
        }
    )
    return [values[column] for column in RAW_EXPORT_HEADER]


def _official_query_payload(expected_count: int) -> dict[str, object]:
    return {
        "success": True,
        "exportData": [{}] if expected_count else [],
        "excelOutAllNewKey": "query-cache",
        "pageHtml": f"<span>共 {expected_count} 条</span>",
    }


def _official_real_no_data_query_payload() -> dict[str, object]:
    return {
        "success": True,
        "excelOutAllNewKey": "query-cache",
        "pageHtml": "",
        "tableContent": (
            '<tr><td><div class="alert alert-nodata text-center">'
            '<span class="fsize16">暂无数据</span></div></td></tr>'
        ),
        "tableFoot": "",
    }


def _official_chunk_payload(
    data: list[list[str]],
    *,
    cache_key: str,
    has_next: bool,
    max_page: int,
) -> dict[str, object]:
    return {
        "data": data,
        "cacheKey": cache_key,
        "hasNext": has_next,
        "max": max_page,
    }


def _make_official_exporter(
    handler,
    *,
    max_attempts: int = 1,
    sleep=lambda _seconds: None,
):
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    mabang = SimpleNamespace(
        base_url="https://900853.private.mabangerp.com",
        client=http_client,
    )
    exporter = IncomeExpenseCsvExporter(
        mabang,
        max_attempts=max_attempts,
        sleep=sleep,
        jitter=lambda: 0,
    )
    return exporter, http_client


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def _malaysia_report_page_html(
    *,
    overlap_temu_with_lazada: bool = False,
    other_count: int = 1,
    include_empty_shop_value: bool = False,
) -> str:
    lazada_ids = "LZ-1,TEMU-1" if overlap_temu_with_lazada else "LZ-1,LZ-2"
    other_inputs = "".join(
        f'<input name="shopIdMultiple[]" value="OTHER-{index}" '
        'data-shop-platform-id="2">'
        for index in range(1, other_count + 1)
    )
    empty_input = (
        '<input name="shopIdMultiple[]" value="" data-shop-platform-id="2">'
        if include_empty_shop_value
        else ""
    )
    return f"""
        <!doctype html>
        <html><body>
          <input name="shopLabelIds[]" value="1038956" data-shopIds="{lazada_ids}">
          <input name="shopLabelIds[]" value="1038957" data-shopIds="SP-1, SP-2">
          <input name="shopLabelIds[]" value="1042623" data-shopIds="TK-1,TK-2">
          <input name="shopIdMultiple[]" value="LZ-1" data-shop-platform-id="3">
          <input name="shopIdMultiple[]" value="LZ-2" data-shop-platform-id="3">
          <input name="shopIdMultiple[]" value="SP-1" data-shop-platform-id="4">
          <input name="shopIdMultiple[]" value="SP-2" data-shop-platform-id="4">
          <input name="shopIdMultiple[]" value="TK-1" data-shop-platform-id="5">
          <input name="shopIdMultiple[]" value="TK-2" data-shop-platform-id="5">
          {other_inputs}
          <input name="shopIdMultiple[]" value="TEMU-1" data-shop-platform-id="154">
          <input name="shopIdMultiple[]" value="TEMU-2" data-shop-platform-id="162" disabled>
          <input name="shopIdMultiple[]" value="TEMU-1" data-shop-platform-id="154">
          {empty_input}
        </body></html>
    """


class MabangIncomeExpenseReportTests(unittest.TestCase):
    def test_catalog_separates_legacy_categories_and_overseas_warehouses(self) -> None:
        options = get_category_options()
        ids = [option["id"] for option in options]
        names = {option["name"] for option in options}

        self.assertEqual(len(options), 21)
        self.assertEqual(len(set(ids)), 21)
        self.assertIn("lazada菲律宾", names)
        self.assertNotIn("1100204", ids)

        warehouses = get_warehouse_options()
        warehouse_keys = {option["key"] for option in warehouses}
        self.assertEqual(len(warehouses), 17)
        self.assertIn("1100204", warehouse_keys)
        self.assertIn("1100500", warehouse_keys)
        self.assertIn("1096191", warehouse_keys)
        self.assertIn("jiasente", warehouse_keys)
        jiasente = next(option for option in warehouses if option["key"] == "jiasente")
        self.assertEqual(jiasente["warehouse_ids"], ["1078593", "1099710"])

    def test_overseas_warehouse_catalog_contains_table_mappings(self) -> None:
        actual = {
            item.name: (item.filter_values, item.mabang_names)
            for item in OVERSEAS_WAREHOUSES
        }
        expected_ids = {
            "实速通": ("1091307",),
            "北俄": ("1089489",),
            "极光速达": ("1089481",),
            "艾姆勒": ("1090061",),
            "福仓": ("1097986",),
            "吉风": ("1098323",),
            "皓程": ("1078593",),
            "KEC": ("1093204",),
            "知意-胡志明": ("1081421",),
            "知意-河内": ("1091491",),
            "元仓": ("1096221",),
            "印尼雅仓海外仓": ("1100204",),
            "菲律宾雅仓海外仓": ("1100500",),
            "马来雅仓海外仓": ("1096191",),
            "AllSome": ("1097191",),
            "嘉森特": ("1078593", "1099710"),
            "腾海": ("1100602",),
        }
        self.assertEqual({name: values[0] for name, values in actual.items()}, expected_ids)

    def test_previous_month_date_range_uses_complete_calendar_month(self) -> None:
        self.assertEqual(
            previous_month_date_range(date(2026, 8, 18)),
            {"start_date": "2026-07-01", "end_date": "2026-07-31"},
        )

    def test_category_form_has_legacy_shape_and_only_shipping_time_filter(self) -> None:
        form_items = build_income_detail_form(
            REPORT_CATEGORIES[0],
            "2026-08-01",
            "2026-08-17",
            page=3,
            page_size=2000,
        )
        form = dict(form_items)

        self.assertEqual(len(form_items), 45)
        self.assertEqual(form["shopLabelIds[]"], "1038955")
        self.assertNotIn("stockWarehouseId[]", form)
        self.assertEqual(form["expresstimeTimeStart"], "2026-08-01 00:00:00")
        self.assertEqual(form["expresstimeTimeEnd"], "2026-08-17 23:59:59")
        self.assertEqual(form["paytimeTimeStart"], "")
        self.assertEqual(form["paytimeTimeEnd"], "")
        self.assertEqual(form["createTimeStart"], "")
        self.assertEqual(form["escrowReleaseTimeStart"], "")
        self.assertEqual(form["page"], "3")
        self.assertEqual(form["rowsPerPage"], "2000")

    def test_warehouse_form_uses_stock_warehouse_field_and_supports_group(self) -> None:
        yacang = next(item for item in OVERSEAS_WAREHOUSES if item.key == "1100204")
        form_items = build_income_detail_form(yacang, "2026-08-01", "2026-08-17")
        form = dict(form_items)
        self.assertEqual(len(form_items), 45)
        self.assertEqual(form["stockWarehouseId[]"], "1100204")
        self.assertNotIn("shopLabelIds[]", form)

        jiasente = next(item for item in OVERSEAS_WAREHOUSES if item.key == "jiasente")
        grouped = build_income_detail_form(jiasente, "2026-08-01", "2026-08-17")
        self.assertEqual(
            [value for key, value in grouped if key == "stockWarehouseId[]"],
            ["1078593", "1099710"],
        )
        self.assertFalse(any(key == "shopLabelIds[]" for key, _ in grouped))

    def test_malaysia_page_parser_keeps_temu_page_order_and_validates_groups(self) -> None:
        page_html = _malaysia_report_page_html()

        self.assertEqual(
            parse_malaysia_temu_shop_ids(page_html),
            ("TEMU-1", "TEMU-2"),
        )
        scope = parse_malaysia_report_shop_scope(page_html)

        self.assertEqual(
            scope.all_shop_ids,
            (
                "LZ-1",
                "LZ-2",
                "SP-1",
                "SP-2",
                "TK-1",
                "TK-2",
                "OTHER-1",
                "TEMU-1",
                "TEMU-2",
            ),
        )
        self.assertEqual(scope.temu_shop_ids, ("TEMU-1", "TEMU-2"))
        self.assertEqual(
            scope.label_shop_ids,
            (
                ("Lazada 马来", ("LZ-1", "LZ-2")),
                ("Shopee 马来", ("SP-1", "SP-2")),
                ("TikTok 马来", ("TK-1", "TK-2")),
            ),
        )
        self.assertEqual(scope.remaining_shop_ids, ("OTHER-1",))
        known_ids = set(scope.temu_shop_ids)
        for _, shop_ids in scope.label_shop_ids:
            self.assertTrue(known_ids.isdisjoint(shop_ids))
            known_ids.update(shop_ids)
        self.assertTrue(known_ids.isdisjoint(scope.remaining_shop_ids))
        self.assertEqual(known_ids | set(scope.remaining_shop_ids), set(scope.all_shop_ids))

    def test_malaysia_page_parser_fails_on_login_or_group_overlap(self) -> None:
        with self.subTest("login page"):
            with self.assertRaisesRegex(IncomeExpenseExportError, "登录页面"):
                parse_malaysia_report_shop_scope(
                    '<html><form action="/login"><input type="password"><button>登录</button></form></html>'
                )

        with self.subTest("overlapping groups"):
            with self.assertRaisesRegex(IncomeExpenseExportError, "店铺范围重叠"):
                parse_malaysia_report_shop_scope(
                    _malaysia_report_page_html(overlap_temu_with_lazada=True)
                )

        with self.subTest("empty shop value"):
            with self.assertRaisesRegex(IncomeExpenseExportError, "缺少 value"):
                parse_malaysia_report_shop_scope(
                    _malaysia_report_page_html(include_empty_shop_value=True)
                )

    def test_validate_payload_uses_known_categories_warehouses_and_local_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = validate_income_expense_payload(
                {
                    "username": "user",
                    "password": "password",
                    "category_ids": ["1038955", "1038955"],
                    "warehouse_keys": ["1100204", "jiasente", "1100204"],
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-17",
                    "output_dir": temp_dir,
                }
            )

        self.assertEqual([category.id for category in job.categories], ["1038955"])
        self.assertEqual([warehouse.key for warehouse in job.warehouses], ["1100204", "jiasente"])
        self.assertEqual(job.start_date, "2026-08-01")
        self.assertEqual(job.end_date, "2026-08-17")

    def test_validate_payload_rejects_unknown_category_and_invalid_range(self) -> None:
        base = {
            "username": "user",
            "password": "password",
            "category_ids": ["not-known"],
            "start_date": "2026-08-01",
            "end_date": "2026-08-17",
            "output_dir": str(Path.cwd()),
        }
        with self.assertRaisesRegex(ValueError, "分类不存在"):
            validate_income_expense_payload(base)

        base["category_ids"] = []
        base["warehouse_keys"] = ["not-known"]
        with self.assertRaisesRegex(ValueError, "海外仓不存在"):
            validate_income_expense_payload(base)

        base["category_ids"] = [REPORT_CATEGORIES[0].id]
        base["warehouse_keys"] = []
        base["start_date"] = "2026-08-18"
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            validate_income_expense_payload(base)

    def test_fetch_target_rows_checks_pagination_and_uses_warehouse_shipping_time(self) -> None:
        client = FakeIncomeClient()
        warehouse = next(item for item in OVERSEAS_WAREHOUSES if item.key == "1100204")
        rows, metadata = fetch_target_rows(
            client,
            warehouse,
            "2026-08-01",
            "2026-08-17",
            concurrency=1,
        )

        self.assertEqual([row["platformOrder"] for row in rows], ["A-1", "A-2", "A-3"])
        self.assertEqual(metadata["row_count"], 3)
        self.assertEqual(metadata["page_count"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["paytimeTimeStart"] == "" for call in client.calls))
        self.assertTrue(all(call["stockWarehouseId[]"] == "1100204" for call in client.calls))
        self.assertTrue(all("shopLabelIds[]" not in call for call in client.calls))
        self.assertTrue(
            all(call["expresstimeTimeStart"] == "2026-08-01 00:00:00" for call in client.calls)
        )

    def test_process_income_rows_keeps_legacy_columns_and_removes_total_rows(self) -> None:
        frame = process_income_rows(
            [
                {
                    "platformOrder": "ORDER-1",
                    "shopName": "测试店铺",
                    "expressTime": "2026-08-02 09:00:00",
                    "quantityTotal": "2",
                    "itemTotal": "15.50",
                },
                {"platformOrder": "合计", "quantityTotal": "2"},
                {"platformOrder": "", "quantityTotal": "1"},
            ]
        )

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["订单编号"], "ORDER-1")
        self.assertEqual(frame.iloc[0]["发货日期"], "2026-08-02 09:00:00")
        self.assertEqual(int(frame.iloc[0]["销量"]), 2)
        self.assertEqual(list(frame.columns)[0], "订单编号")
        self.assertEqual(list(frame.columns)[-1], "毛利率")

    def test_official_export_uses_cache_key_sequence_and_writes_51_columns(self) -> None:
        requests: list[httpx.Request] = []
        chunks = [
            _official_chunk_payload(
                [RAW_EXPORT_HEADER, _official_export_row("ORDER-1", "A")],
                cache_key="server-cache-2",
                has_next=True,
                max_page=1,
            ),
            _official_chunk_payload(
                [_official_export_row("ORDER-2", "B"), _official_export_row("合计")],
                cache_key="server-cache-2",
                has_next=False,
                max_page=1,
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json=_official_query_payload(2))
            return httpx.Response(200, json=chunks.pop(0))

        exporter, http_client = _make_official_exporter(handler)
        target = next(item for item in OVERSEAS_WAREHOUSES if item.key == "jiasente")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "income-expense.csv"
            try:
                row_count, chunk_count = exporter.export_to_csv(
                    target,
                    "2026-08-01",
                    "2026-08-31",
                    output_file,
                )
            finally:
                http_client.close()

            csv_rows = _read_csv_rows(output_file)

        self.assertEqual((row_count, chunk_count), (2, 2))
        self.assertEqual(csv_rows[0], list(ORDERED_COLUMNS))
        self.assertEqual(len(csv_rows[0]), 51)
        self.assertEqual([row[0] for row in csv_rows[1:]], ["ORDER-1", "ORDER-2"])
        self.assertTrue(all(len(row) == 51 for row in csv_rows))
        self.assertEqual(
            [request.url.params["mod"] for request in requests],
            ["reports.getIncomePayParticularsData", INCOME_EXPORT_MOD, INCOME_EXPORT_MOD],
        )
        query_form = parse_qs(requests[0].content.decode(), keep_blank_values=True)
        self.assertEqual(query_form["rowsPerPage"], ["100"])
        self.assertEqual(query_form["stockWarehouseId[]"], ["1078593", "1099710"])
        self.assertEqual(query_form["expresstimeTimeStart"], ["2026-08-01 00:00:00"])
        export_requests = requests[1:]
        self.assertEqual(
            [request.url.params["pageNo"] for request in export_requests],
            ["0", "1"],
        )
        self.assertEqual(
            [request.url.params["cacheKey"] for request in export_requests],
            ["1", "server-cache-2"],
        )
        self.assertEqual(export_requests[0].url.params.get_list("field[]"), list(INCOME_EXPORT_FIELDS))

    def test_malaysia_export_handles_one_real_no_data_group_and_merges_one_csv(self) -> None:
        requests: list[httpx.Request] = []
        query_forms: list[dict[str, list[str]]] = []
        exported_orders = iter(
            [
                ("ORDER-SHOPEE", "SP"),
                ("ORDER-TIKTOK", "TK"),
                ("ORDER-TEMU", "TEMU"),
                ("ORDER-OTHER", "OTHER"),
            ]
        )
        progress: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            mod = request.url.params["mod"]
            if request.method == "GET" and mod == "reports.incomePayParticulars":
                return httpx.Response(200, text=_malaysia_report_page_html())
            if request.method == "POST":
                query_forms.append(
                    parse_qs(request.content.decode(), keep_blank_values=True)
                )
                if len(query_forms) == 1:
                    return httpx.Response(200, json=_official_real_no_data_query_payload())
                return httpx.Response(200, json=_official_query_payload(1))
            order_number, marker = next(exported_orders)
            return httpx.Response(
                200,
                json=_official_chunk_payload(
                    [RAW_EXPORT_HEADER, _official_export_row(order_number, marker)],
                    cache_key=f"done-{marker}",
                    has_next=False,
                    max_page=0,
                ),
            )

        exporter, http_client = _make_official_exporter(handler)
        exporter.progress = progress.append
        target = next(item for item in OVERSEAS_WAREHOUSES if item.key == "1096191")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "malaysia-income-expense.csv"
            try:
                result = exporter.export_to_csv(
                    target,
                    "2026-08-01",
                    "2026-08-31",
                    output_file,
                )
            finally:
                http_client.close()
            csv_rows = _read_csv_rows(output_file)

        self.assertEqual(result, (4, 4))
        self.assertEqual(
            [(request.method, request.url.params["mod"]) for request in requests],
            [
                ("GET", "reports.incomePayParticulars"),
                ("POST", "reports.getIncomePayParticularsData"),
                ("POST", "reports.getIncomePayParticularsData"),
                ("GET", INCOME_EXPORT_MOD),
                ("POST", "reports.getIncomePayParticularsData"),
                ("GET", INCOME_EXPORT_MOD),
                ("POST", "reports.getIncomePayParticularsData"),
                ("GET", INCOME_EXPORT_MOD),
                ("POST", "reports.getIncomePayParticularsData"),
                ("GET", INCOME_EXPORT_MOD),
            ],
        )
        self.assertEqual(len(query_forms), 5)
        self.assertTrue(
            all(form["stockWarehouseId[]"] == ["1096191"] for form in query_forms)
        )
        self.assertTrue(
            all(
                form["expresstimeTimeStart"] == ["2026-08-01 00:00:00"]
                and form["expresstimeTimeEnd"] == ["2026-08-31 23:59:59"]
                for form in query_forms
            )
        )
        self.assertEqual(
            [form.get("shopLabelIds[]") for form in query_forms],
            [["1038956"], ["1038957"], ["1042623"], None, None],
        )
        self.assertEqual(
            query_forms[3]["shopIdMultiple[]"],
            ["TEMU-1", "TEMU-2"],
        )
        self.assertEqual(query_forms[4]["shopIdMultiple[]"], ["OTHER-1"])
        self.assertTrue(
            all("shopLabelIds[]" in form or "shopIdMultiple[]" in form for form in query_forms)
        )
        self.assertEqual(
            [row[0] for row in csv_rows[1:]],
            ["ORDER-SHOPEE", "ORDER-TIKTOK", "ORDER-TEMU", "ORDER-OTHER"],
        )
        self.assertEqual(csv_rows[0], list(ORDERED_COLUMNS))
        self.assertTrue(any("日期不拆分" in message for message in progress))
        self.assertTrue(any("4 个业务组店铺范围互不重叠" in message for message in progress))
        self.assertTrue(any("Lazada 马来: 官方查询共 0 条" in message for message in progress))
        self.assertTrue(any("其他店铺覆盖 1/1" in message for message in progress))

    def test_malaysia_cross_group_duplicate_preserves_existing_file(self) -> None:
        export_call_count = 0
        progress: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal export_call_count
            mod = request.url.params["mod"]
            if request.method == "GET" and mod == "reports.incomePayParticulars":
                return httpx.Response(200, text=_malaysia_report_page_html())
            if request.method == "POST":
                return httpx.Response(200, json=_official_query_payload(1))
            export_call_count += 1
            return httpx.Response(
                200,
                json=_official_chunk_payload(
                    [RAW_EXPORT_HEADER, _official_export_row("ORDER-DUPLICATE", "SAME")],
                    cache_key="done",
                    has_next=False,
                    max_page=0,
                ),
            )

        exporter, http_client = _make_official_exporter(handler)
        exporter.progress = progress.append
        target = next(item for item in OVERSEAS_WAREHOUSES if item.key == "1096191")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "malaysia-income-expense.csv"
            original = "existing-file-must-survive\n"
            output_file.write_text(original, encoding="utf-8")
            try:
                with self.assertRaisesRegex(IncomeExpenseExportError, "跨业务组重复明细"):
                    exporter.export_to_csv(
                        target,
                        "2026-08-01",
                        "2026-08-31",
                        output_file,
                    )
            finally:
                http_client.close()

            self.assertEqual(output_file.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(temp_dir).glob(".*.part")), [])

        self.assertEqual(export_call_count, 2)
        self.assertTrue(any("未覆盖目标文件" in message for message in progress))

    def test_malaysia_remaining_shop_batches_cover_850_boundary_without_overlap(self) -> None:
        target = next(item for item in OVERSEAS_WAREHOUSES if item.key == "1096191")

        for other_count, expected_batch_sizes in ((850, [850]), (851, [850, 1])):
            with self.subTest(other_count=other_count):
                query_forms: list[list[tuple[str, str]]] = []
                page_html = _malaysia_report_page_html(other_count=other_count)

                def handler(request: httpx.Request) -> httpx.Response:
                    mod = request.url.params["mod"]
                    if request.method == "GET" and mod == "reports.incomePayParticulars":
                        return httpx.Response(200, text=page_html)
                    if request.method == "POST":
                        query_forms.append(
                            parse_qsl(request.content.decode(), keep_blank_values=True)
                        )
                        return httpx.Response(
                            200,
                            json=_official_real_no_data_query_payload(),
                        )
                    raise AssertionError(f"unexpected request: {request.method} {mod}")

                exporter, http_client = _make_official_exporter(handler)
                with tempfile.TemporaryDirectory() as temp_dir:
                    output_file = Path(temp_dir) / "malaysia-income-expense.csv"
                    try:
                        result = exporter.export_to_csv(
                            target,
                            "2026-08-01",
                            "2026-08-31",
                            output_file,
                        )
                    finally:
                        http_client.close()
                    csv_rows = _read_csv_rows(output_file)

                coverage_forms = query_forms[4:]
                coverage_batches = [
                    [value for key, value in form if key == "shopIdMultiple[]"]
                    for form in coverage_forms
                ]
                self.assertEqual(
                    [len(batch) for batch in coverage_batches],
                    expected_batch_sizes,
                )
                flattened = [shop_id for batch in coverage_batches for shop_id in batch]
                self.assertEqual(
                    flattened,
                    [f"OTHER-{index}" for index in range(1, other_count + 1)],
                )
                self.assertEqual(len(flattened), len(set(flattened)))
                self.assertTrue(all(len(form) <= 895 for form in coverage_forms))
                self.assertTrue(
                    all(
                        ("stockWarehouseId[]", "1096191") in form
                        and ("expresstimeTimeStart", "2026-08-01 00:00:00") in form
                        and ("expresstimeTimeEnd", "2026-08-31 23:59:59") in form
                        for form in query_forms
                    )
                )
                self.assertEqual(result, (0, 0))
                self.assertEqual(csv_rows, [list(ORDERED_COLUMNS)])

    def test_official_export_retries_same_chunk_after_waf(self) -> None:
        export_requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_official_query_payload(1))
            export_requests.append(request)
            if len(export_requests) == 1:
                return httpx.Response(
                    200,
                    text="<!doctype html><script>submitWafFeedback()</script>",
                )
            return httpx.Response(
                200,
                json=_official_chunk_payload(
                    [RAW_EXPORT_HEADER, _official_export_row("ORDER-1", "A")],
                    cache_key="finished-cache",
                    has_next=False,
                    max_page=0,
                ),
            )

        exporter, http_client = _make_official_exporter(
            handler,
            max_attempts=2,
            sleep=sleeps.append,
        )
        target = REPORT_CATEGORIES[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                exporter.export_to_csv(
                    target,
                    "2026-08-01",
                    "2026-08-31",
                    Path(temp_dir) / "income-expense.csv",
                )
            finally:
                http_client.close()

        self.assertEqual(len(export_requests), 2)
        self.assertEqual([request.url.params["pageNo"] for request in export_requests], ["0", "0"])
        self.assertEqual([request.url.params["cacheKey"] for request in export_requests], ["1", "1"])
        self.assertEqual(sleeps, [60.0])

    def test_official_export_count_mismatch_preserves_existing_file(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_official_query_payload(2))
            return httpx.Response(
                200,
                json=_official_chunk_payload(
                    [RAW_EXPORT_HEADER, _official_export_row("ORDER-1", "A")],
                    cache_key="finished-cache",
                    has_next=False,
                    max_page=0,
                ),
            )

        exporter, http_client = _make_official_exporter(handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "income-expense.csv"
            original = "existing-file-must-survive\n"
            output_file.write_text(original, encoding="utf-8")
            try:
                with self.assertRaisesRegex(IncomeExpenseExportError, "导出明细数不一致"):
                    exporter.export_to_csv(
                        REPORT_CATEGORIES[0],
                        "2026-08-01",
                        "2026-08-31",
                        output_file,
                    )
            finally:
                http_client.close()

            self.assertEqual(output_file.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(temp_dir).glob(".*.part")), [])

    def test_official_export_real_no_data_shape_still_writes_header(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_official_real_no_data_query_payload())

        exporter, http_client = _make_official_exporter(handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "income-expense.csv"
            try:
                result = exporter.export_to_csv(
                    REPORT_CATEGORIES[0],
                    "2026-08-01",
                    "2026-08-31",
                    output_file,
                )
            finally:
                http_client.close()
            csv_rows = _read_csv_rows(output_file)

        self.assertEqual(result, (0, 0))
        self.assertEqual(csv_rows, [list(ORDERED_COLUMNS)])
        self.assertEqual([request.method for request in requests], ["POST"])

    def test_official_missing_export_data_without_exact_no_data_marker_fails(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "excelOutAllNewKey": "query-cache",
                    "pageHtml": "",
                    "tableContent": "<div>暂无数据</div>",
                    "tableFoot": "",
                },
            )

        exporter, http_client = _make_official_exporter(handler)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "income-expense.csv"
            try:
                with self.assertRaisesRegex(IncomeExpenseExportError, "缺少 exportData"):
                    exporter.export_to_csv(
                        REPORT_CATEGORIES[0],
                        "2026-08-01",
                        "2026-08-31",
                        output_file,
                    )
            finally:
                http_client.close()

            self.assertFalse(output_file.exists())
            self.assertEqual(list(Path(temp_dir).glob(".*.part")), [])

    def test_run_report_writes_each_selected_target_to_local_output(self) -> None:
        client_options: list[dict[str, object]] = []

        class FakeMabangClient:
            def __init__(self, *_args, **kwargs) -> None:
                self.logged_in = False
                client_options.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def login(self, username: str, password: str) -> None:
                self.logged_in = bool(username and password)

        class FakeOfficialExporter:
            def __init__(self, _client, **_kwargs) -> None:
                pass

            def export_to_csv(self, target, _start_date, _end_date, output_file):
                rows = [] if target.id == "1100204" else [
                    {
                        "platformOrder": "ORDER-1",
                        "expressTime": "2026-08-02 09:00:00",
                        "quantityTotal": "1",
                    }
                ]
                frame = process_income_rows(rows)
                frame.to_csv(output_file, index=False, encoding="utf-8-sig")
                return len(frame), 0 if not rows else 1

        with tempfile.TemporaryDirectory() as temp_dir:
            job = validate_income_expense_payload(
                {
                    "username": "user",
                    "password": "password",
                    "category_ids": ["1038955"],
                    "warehouse_keys": ["1100204"],
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-17",
                    "output_dir": temp_dir,
                }
            )
            with (
                patch(
                    "backend.services.mabang_income_expense_report.MabangClient",
                    FakeMabangClient,
                ),
                patch(
                    "backend.services.mabang_income_expense_report.IncomeExpenseCsvExporter",
                    FakeOfficialExporter,
                ),
            ):
                result = run_income_expense_report(job)

            self.assertEqual(result.target_count, 2)
            self.assertEqual(result.category_count, 1)
            self.assertEqual(result.warehouse_count, 1)
            self.assertEqual(result.csv_file_count, 2)
            self.assertEqual(result.record_count, 1)
            self.assertTrue(client_options[0]["verify_ssl"])
            self.assertTrue(all(path.is_file() for path in result.files))
            self.assertTrue(all(str(path).startswith(temp_dir) for path in result.files))
            empty_csv = next(path for path in result.files if "印尼雅仓海外仓" in path.name)
            self.assertEqual(len(empty_csv.read_text(encoding="utf-8-sig").splitlines()), 1)

    def test_pagination_parser_handles_commas(self) -> None:
        self.assertEqual(
            parse_income_pagination('<span class="semibold">1/12</span>页 共 12,345 条'),
            (12, 12345),
        )

    def test_config_store_round_trips_selected_categories_and_warehouses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            store.save(
                {
                    "income_expense_category_ids": ["1038955", "1038955"],
                    "income_expense_warehouse_keys": ["1100204", "jiasente", "1100204"],
                }
            )
            loaded = store.load()

        self.assertEqual(loaded.income_expense_category_ids, ["1038955"])
        self.assertEqual(loaded.income_expense_warehouse_keys, ["1100204", "jiasente"])

    def test_service_source_has_no_share_or_dingtalk_dependency(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "services"
            / "mabang_income_expense_report.py"
        ).read_text(encoding="utf-8")
        forbidden = ("1.1.1.28", "rpa流程", "dingtalk", "send_file_dingtalk")
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source.lower())


if __name__ == "__main__":
    unittest.main()

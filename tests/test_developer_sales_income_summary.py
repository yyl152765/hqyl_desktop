from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from backend.core.mabang_client import MabangApiError
from backend.services import developer_sales_income_summary as dev_module
from backend.services.developer_sales_income_summary import (
    DeveloperIncomeSummary,
    IncomeDetailRow,
    PersonIncomeTarget,
    PersonOption,
    UNASSIGNED_SALESPERSON,
    build_report_form_data,
    build_summary_workbook,
    parse_developer_names,
    parse_income_rows,
    parse_order_income_rows,
    parse_person_options,
    parse_table_foot_order_income,
    resolve_developer_options,
    summarize_all_people,
    validate_developer_sales_income_payload,
)


class DeveloperSalesIncomeSummaryTests(unittest.TestCase):
    def test_person_options_preserve_duplicate_suffix_and_independent_ids(self) -> None:
        html = """
        <label><input type="checkbox" name="developid[]" value="1117090">黄茵茵-开发&gt;1</label>
        <label><input type="checkbox" name="developid[]" value="1097736">A1018-黄茵茵-开发</label>
        <label><input type="checkbox" name="developid[]" value="0">无开发员</label>
        """

        options = parse_person_options(html, "developid[]")
        selected, missing = resolve_developer_options(["黄茵茵"], options)

        self.assertEqual(options[-1], PersonOption("0", "未分配开发员"))
        self.assertEqual([option.id for option in selected], ["1117090", "1097736"])
        self.assertEqual(
            [option.name for option in selected],
            ["黄茵茵-开发>1", "A1018-黄茵茵-开发"],
        )
        self.assertEqual(missing, ())

    def test_table_foot_uses_first_income_cell_as_order_income(self) -> None:
        html = (
            '<tr><th data-field="income">5,576.0500</th>'
            '<th data-field="income">92.1670</th>'
            '<th data-field="income">6,477.0550</th></tr>'
        )

        amount = parse_table_foot_order_income(html, context="黄茵茵-开发>1")

        self.assertEqual(amount, Decimal("5576.0500"))

    def test_table_foot_missing_income_field_fails_instead_of_zero(self) -> None:
        with self.assertRaisesRegex(MabangApiError, "缺少收入-订单金额合计字段"):
            parse_table_foot_order_income('<tr><th data-field="incomeTotal">0</th></tr>', context="全部人员")

    def test_order_income_rows_read_first_income_cell_per_row(self) -> None:
        html = (
            '<tr><td data-field="salesSkuNewId">A</td><td data-field="income">1,234.5000</td>'
            '<td data-field="income">9.0000</td></tr>'
            '<tr><td data-field="salesSkuNewId">B</td><td data-field="income">-2.2500</td></tr>'
        )

        self.assertEqual(
            parse_order_income_rows(html, context="测试"),
            [Decimal("1234.5000"), Decimal("-2.2500")],
        )

    def test_person_total_sums_every_page_instead_of_table_foot(self) -> None:
        class PagedClient:
            def __init__(self) -> None:
                self.pages: list[str] = []

            def _post_sales_report(self, *, form_data, **_kwargs):
                page = str(form_data.get("page"))
                self.pages.append(page)
                rows = 2 if page == "1" else 1
                body = "".join(
                    f'<tr><td><a data-stock-id="S{page}{i}">S{page}{i}</a></td>'
                    f'<td data-field="income">10.0000</td></tr>'
                    for i in range(rows)
                )
                return {"success": True, "pageHtml": "共 3 条", "updateDateStr": "stable", "tableContent": body, "tableFoot": '<th data-field="income">999999</th>'}

        client = PagedClient()
        target = PersonIncomeTarget("开发员", "developid[]", PersonOption("0", "未分配开发员"))
        with patch.object(dev_module, "INCOME_PAGE_SIZE", 2):
            total, rows = dev_module._fetch_person_income_rows_total(client, target, date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(client.pages, ["1", "2"])
        self.assertEqual(total, Decimal("30.0000"))
        self.assertEqual(rows, 3)

    def test_person_total_returns_zero_rows_as_zero(self) -> None:
        class EmptyClient:
            def _post_sales_report(self, **_kwargs):
                return {"success": True, "pageHtml": "共 0 条", "tableContent": "", "tableFoot": ""}

        target = PersonIncomeTarget("开发员", "developid[]", PersonOption("9", "无数据"))
        total, rows = dev_module._fetch_person_income_rows_total(EmptyClient(), target, date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual((total, rows), (Decimal("0"), 0))

    def test_all_people_mode_keeps_developer_and_salesperson_dimensions(self) -> None:
        with patch.object(dev_module, "fetch_person_income_total", side_effect=[Decimal("10"), Decimal("20"), Decimal("5"), Decimal("6")]), patch.object(
            dev_module, "MabangClient"
        ) as factory:
            factory.return_value.__enter__ = lambda self: self
            factory.return_value.__exit__ = lambda *args: False
            summaries, totals = dev_module.collect_all_people_income_totals(
                username="u",
                password="p",
                developer_options=[PersonOption("1", "开发甲"), PersonOption("2", "开发乙")],
                salesperson_options=[PersonOption("10", "销售丙"), PersonOption("11", "销售丁")],
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 2),
                max_workers=2,
            )

        self.assertEqual([(x.developer.name, x.amount) for x in summaries],
                         [("开发甲", Decimal("10")), ("开发乙", Decimal("20"))])
        self.assertEqual(totals, {"销售丙": Decimal("5"), "销售丁": Decimal("6")})

    def test_missing_developer_names_are_returned_without_dropping_matches(self) -> None:
        selected, missing = resolve_developer_options(
            ["黄茵茵", "不存在的人"],
            [PersonOption("1", "黄茵茵-开发>1"), PersonOption("2", "A1018-黄茵茵-开发")],
        )

        self.assertEqual(len(selected), 2)
        self.assertEqual(missing, ("不存在的人",))

    def test_parse_developer_names_supports_one_name_per_line_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_developer_names("黄茵茵\n\n 黄茵茵 \n刘琴铃"),
            ("黄茵茵", "刘琴铃"),
        )

    def test_validate_payload_uses_payment_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = validate_developer_sales_income_payload(
                {
                    "username": "user",
                    "password": "password",
                    "developer_names": ["黄茵茵"],
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-31",
                    "output_dir": temp_dir,
                }
            )

        self.assertEqual(job.developer_names, ("黄茵茵",))
        self.assertEqual(job.start_date, "2026-07-01")

    def test_validate_payload_allows_all_people_without_name_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = validate_developer_sales_income_payload(
                {
                    "username": "user",
                    "password": "password",
                    "developer_names": [],
                    "include_all": True,
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-31",
                    "output_dir": temp_dir,
                }
            )

        self.assertTrue(job.include_all)

    def test_report_form_uses_us_dollars_payment_time_and_developer_id(self) -> None:
        payload = build_report_form_data("1117090", date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(payload["timeKey"], "payTime")
        self.assertEqual(payload["moneyType"], "US")
        self.assertEqual(payload["developid[]"], "1117090")
        self.assertEqual(payload["timeStart"], "2026-07-01 00:00:00")
        self.assertEqual(payload["timeEnd"], "2026-07-31 23:59:59")

    def test_income_rows_skip_total_and_keep_unassigned_sales_income(self) -> None:
        content = (
            "SKU,开发员,销售员,收入-订单金额\n"
            "sku-1,李四-开发,张三,10.1234\n"
            "sku-2,李四-开发,--,2.5000\n"
            "合计,,,12.6234\n"
        ).encode("utf-8")

        rows = parse_income_rows(content, context="测试开发员")

        self.assertEqual(
            rows,
            [("张三", Decimal("10.1234")), (UNASSIGNED_SALESPERSON, Decimal("2.5000"))],
        )

    def test_all_people_summary_keeps_every_option_and_zero_amount_people(self) -> None:
        developer_options = [
            PersonOption("1", "黄茵茵-开发>1"),
            PersonOption("2", "A1018-黄茵茵-开发"),
            PersonOption("3", "零收入-开发"),
        ]
        salesperson_options = [PersonOption("10", "张三"), PersonOption("11", "零收入销售")]
        rows = [
            IncomeDetailRow("黄茵茵-开发>1", "张三", Decimal("10")),
            IncomeDetailRow("A1018-黄茵茵-开发", "张三", Decimal("20")),
        ]

        developers, salespeople = summarize_all_people(rows, developer_options, salesperson_options)

        self.assertEqual([item.developer.name for item in developers], [item.name for item in developer_options])
        self.assertEqual([item.amount for item in developers], [Decimal("10"), Decimal("20"), Decimal("0")])
        self.assertEqual(salespeople, {"张三": Decimal("30"), "零收入销售": Decimal("0")})

    def test_workbook_has_exactly_two_summary_sheets_and_separate_developers(self) -> None:
        summaries = [
            DeveloperIncomeSummary(PersonOption("1", "黄茵茵-开发>1"), Decimal("10.1234"), 2),
            DeveloperIncomeSummary(PersonOption("2", "A1018-黄茵茵-开发"), Decimal("2.5000"), 1),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "summary.xlsx"
            build_summary_workbook(
                output_path,
                summaries,
                {"张三": Decimal("12.6234")},
            )
            workbook = load_workbook(output_path, data_only=True)

        self.assertEqual(workbook.sheetnames, ["开发员", "销售员"])
        self.assertEqual(workbook["开发员"]["A2"].value, "黄茵茵-开发>1")
        self.assertEqual(workbook["开发员"]["A3"].value, "A1018-黄茵茵-开发")
        self.assertAlmostEqual(workbook["开发员"]["B4"].value, 12.6234)
        self.assertAlmostEqual(workbook["销售员"]["B3"].value, 12.6234)
        workbook.close()

    def test_workbook_rejects_developer_salesperson_total_mismatch(self) -> None:
        summaries = [DeveloperIncomeSummary(PersonOption("1", "张三-开发"), Decimal("1"), 1)]
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(MabangApiError, "合计不一致"):
                build_summary_workbook(
                    Path(temp_dir) / "summary.xlsx",
                    summaries,
                    {"李四": Decimal("2")},
                )

    def test_workbook_all_people_mode_keeps_independent_dimension_totals(self) -> None:
        summaries = [DeveloperIncomeSummary(PersonOption("1", "张三-开发"), Decimal("1"), 0)]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "summary.xlsx"
            build_summary_workbook(
                output_path,
                summaries,
                {"李四": Decimal("2")},
                require_equal_totals=False,
            )
            workbook = load_workbook(output_path, data_only=True)

        self.assertEqual(workbook["开发员"]["B3"].value, 1)
        self.assertEqual(workbook["销售员"]["B3"].value, 2)
        workbook.close()


if __name__ == "__main__":
    unittest.main()

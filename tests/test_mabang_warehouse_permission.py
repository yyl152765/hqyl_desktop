from __future__ import annotations

import base64
import unittest
from unittest import mock
from urllib.parse import parse_qsl

from backend.core.mabang_client import MabangClient
from backend.services import mabang_warehouse_permission as service
from backend.services.mabang_warehouse_permission import (
    ExpectedEmployeePermission,
    MabangEmployee,
    WarehousePermissionPreviewQuery,
    WarehousePermissionBatchJob,
    build_updated_form_data,
    exact_employee_matches,
    missing_permission_ids,
    parse_employee_names,
    parse_employee_rows,
    parse_permission_page,
    parse_permission_types,
    parse_warehouse_ids,
    preview_mabang_warehouse_permissions,
    run_mabang_warehouse_permission_batch,
)


def employee_row(employee_id: str, name: str, mobile: str, department: str = "运营部") -> str:
    cells = [
        "<td><input type='checkbox'></td>",
        "<td></td>",
        f"<td><a href='/index.php?mod=staff.staffdetail&id={employee_id}&channel_id=1'>{name}</a></td>",
        f"<td>{mobile}</td>",
        "<td>自动加仓库专用</td>",
        "<td>销售员</td>",
        f"<td>{department}</td>",
        "<td>管理员 正常</td>",
        "<td>2026-07-28</td>",
        (
            "<td><a href='/index.php?mod=staff.staffdetail&id="
            f"{employee_id}&channel_id=1&isDetail=2'>数据权限</a></td>"
        ),
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def permission_html(
    employee_id: str,
    *,
    selected: tuple[str, ...] = ("101",),
    view_mode: str = "1",
    product_selected: tuple[str, ...] | None = None,
    order_selected: tuple[str, ...] = ("999",),
    order_view_mode: str = "2",
    product_view_mode: str = "2",
    category_selected: tuple[str, ...] = (),
    parent_category_and_warehouse: bool = False,
) -> str:
    checked = lambda value: " checked" if value in selected else ""
    product_selected = selected if product_selected is None else product_selected
    product_checked = lambda value: " checked" if value in product_selected else ""
    order_checked = lambda value: " checked" if value in order_selected else ""
    category_checked = lambda value: " checked" if value in category_selected else ""
    return f"""
    <form name="formEmployee">
      <input type="hidden" name="mobile" value="13800000000">
      <input type="text" name="name" value="测试员工">
      <input type="radio" name="see_warehouse" value="1"{" checked" if view_mode == "1" else ""}>
      <input type="radio" name="see_warehouse" value="2"{" checked" if view_mode == "2" else ""}>
      <label><input type="checkbox" name="stockWarehouse[]" value="101"{product_checked("101")}>武汉仓</label>
      <label><input type="checkbox" name="stockWarehouse[]" value="102"{product_checked("102")}>泰国仓</label>
      <label><input type="checkbox" name="stockWarehouse[]" value="999"{product_checked("999")}>测试仓</label>
      <input type="radio" name="stockCategoryOrWarehouse" value="1"{" checked" if product_view_mode == "1" else ""}>按商品父目录查看
      <input type="radio" name="stockCategoryOrWarehouse" value="2"{" checked" if product_view_mode == "2" else ""}>按仓库查看
      <input type="radio" name="comboType" value="1"{" checked" if order_view_mode == "1" else ""}>按店铺查看
      <input type="radio" name="comboType" value="2"{" checked" if order_view_mode == "2" else ""}>按仓库查看
      <label><input type="checkbox" name="warehouseAssistant[]" value="101"{order_checked("101")}>武汉仓</label>
      <label><input type="checkbox" name="warehouseAssistant[]" value="102"{order_checked("102")}>泰国仓</label>
      <label><input type="checkbox" name="warehouseAssistant[]" value="999"{order_checked("999")}>测试仓</label>
      <label><input type="checkbox" name="allocationwarehouseAssistant[]" value="101"{checked("101")}>武汉仓</label>
      <label><input type="checkbox" name="allocationwarehouseAssistant[]" value="102"{checked("102")}>泰国仓</label>
      <label><input type="checkbox" name="allocationwarehouseAssistant[]" value="999"{checked("999")}>测试仓</label>
      <label><input type="checkbox" name="stockCategory[]" value="201"{category_checked("201")}>New服装、鞋类</label>
      <label><input type="checkbox" name="stockCategory[]" value="202"{category_checked("202")}>无商品目录</label>
      <label><input type="checkbox" name="stockCategory[]" value="1696862"{category_checked("1696862")}>EB 产品</label>
      <label><input type="checkbox" name="parent_category_and_warehouse" value="1"{" checked" if parent_category_and_warehouse else ""}>商品父目录和仓库联动</label>
      <input type="checkbox" name="stationId[]" value="1" checked>
      <input type="checkbox" name="stationId[]" value="2">
      <select name="departmentId"><option value="5" selected>运营部</option></select>
      <textarea name="notes">第一行\n第二行</textarea>
      <button type="button">保存修改</button>
    </form>
    <script>
      function doSaveEmployee() {{
        $.ajax({{url: "https://private-amz.mabangerp.com/index.php?mod=staff.doUpdateEmployee&id={employee_id}&DOMAIN=900853.private.mabangerp.com&channel_id=1"}});
      }}
    </script>
    """


def permission_page(
    employee_id: str,
    *,
    selected: tuple[str, ...],
    product_selected: tuple[str, ...] | None = None,
    order_selected: tuple[str, ...] = ("999",),
    order_view_mode: str = "2",
    view_mode: str = "1",
    documented: bool = False,
):
    return parse_permission_page(
        permission_html(
            employee_id,
            selected=selected,
            view_mode=view_mode,
            product_selected=selected if product_selected is None else product_selected,
            order_selected=order_selected,
            order_view_mode=order_view_mode,
            product_view_mode="1" if documented else "2",
            category_selected=("201", "202") if documented else (),
            parent_category_and_warehouse=documented,
        ),
        employee_id=employee_id,
        page_url=f"https://private-amz.mabangerp.com/index.php?mod=staff.staffdetail&id={employee_id}",
    )


class MabangWarehousePermissionTests(unittest.TestCase):
    def test_employee_search_url_keeps_mod_and_tenant_parameters(self) -> None:
        self.assertIn("?mod=staff.getEmployList&DOMAIN=", service.EMPLOYEE_SEARCH_URL)
        self.assertNotIn("getEmployList?DOMAIN", service.EMPLOYEE_SEARCH_URL)

    def test_mabang_login_finalizes_cross_domain_session(self) -> None:
        class FakeResponse:
            def __init__(self, url, payload=None):
                self.url = url
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeHttpClient:
            def __init__(self):
                self.get_urls = []

            def get(self, url, **kwargs):
                self.get_urls.append(url)
                return FakeResponse(url)

            def post(self, url, **kwargs):
                return FakeResponse(url, {"success": True})

        fake = FakeHttpClient()
        client = MabangClient("https://example.private.mabangerp.com", client=fake)
        client.login("user", "password")

        self.assertTrue(client._logged_in)
        self.assertTrue(any("mod=main.plogin" in url for url in fake.get_urls))

    def test_parse_employee_names_deduplicates_and_preserves_order(self) -> None:
        self.assertEqual(parse_employee_names(" 张三 \n\n李四\n张三"), ("张三", "李四"))

    def test_parse_warehouse_ids_rejects_non_numeric_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "仓库 ID"):
            parse_warehouse_ids(["101", "warehouse-x"])

    def test_permission_types_default_to_all_and_require_one_valid_option(self) -> None:
        self.assertEqual(parse_permission_types(None), ("product", "order", "warehouse"))
        self.assertEqual(parse_permission_types(["warehouse", "order"]), ("order", "warehouse"))
        with self.assertRaisesRegex(ValueError, "至少选择一项"):
            parse_permission_types([])
        with self.assertRaisesRegex(ValueError, "权限类型"):
            parse_permission_types(["customer"])

    def test_employee_rows_support_exact_and_duplicate_name_matching(self) -> None:
        html = employee_row("11", "张三", "13800000001") + employee_row("12", "张三", "13800000002")
        employees = parse_employee_rows(html)
        matches = exact_employee_matches(employees, " 张三 ")
        self.assertEqual([item.id for item in matches], ["11", "12"])
        self.assertEqual(matches[0].department, "运营部")

    def test_permission_page_parses_warehouse_ids_and_save_url(self) -> None:
        page = permission_page("11", selected=("101",))
        self.assertEqual([item.name for item in page.warehouses], ["武汉仓", "泰国仓", "测试仓"])
        self.assertEqual(page.selected_warehouse_ids, ("101",))
        self.assertEqual(page.warehouse_view_mode, "1")
        self.assertEqual(page.selected_product_warehouse_ids, ("101",))
        self.assertEqual(page.selected_order_warehouse_ids, ("999",))
        self.assertEqual(page.order_view_mode, "2")
        self.assertEqual(page.product_view_mode, "2")
        self.assertEqual(page.eb_product_category_ids, ("1696862",))
        self.assertIn("mod=staff.doUpdateEmployee&id=11", page.save_url)

    def test_build_updated_form_data_adds_warehouse_and_keeps_other_fields(self) -> None:
        page = permission_page("11", selected=("101",), order_view_mode="1")
        encoded = build_updated_form_data(page, ["102"])
        pairs = parse_qsl(base64.b64decode(encoded).decode("utf-8"), keep_blank_values=True)
        self.assertEqual([value for key, value in pairs if key == "stockWarehouse[]"], ["101", "102"])
        self.assertEqual(
            [value for key, value in pairs if key == "allocationwarehouseAssistant[]"],
            ["101", "102"],
        )
        self.assertEqual([value for key, value in pairs if key == "warehouseAssistant[]"], ["102", "999"])
        self.assertEqual([value for key, value in pairs if key == "comboType"], ["2"])
        self.assertEqual([value for key, value in pairs if key == "stockCategoryOrWarehouse"], ["1"])
        self.assertEqual([value for key, value in pairs if key == "stockCategory[]"], ["201", "202"])
        self.assertEqual([value for key, value in pairs if key == "parent_category_and_warehouse"], ["1"])
        self.assertNotIn(("stockCategory[]", "1696862"), pairs)
        self.assertEqual([value for key, value in pairs if key == "stationId[]"], ["1"])
        self.assertEqual([value for key, value in pairs if key == "see_warehouse"], ["1"])
        self.assertIn(("departmentId", "5"), pairs)
        self.assertIn(("notes", "第一行\r\n第二行"), pairs)

    def test_build_updated_form_data_only_changes_selected_permission_types(self) -> None:
        page = permission_page(
            "11",
            selected=("101",),
            product_selected=("101",),
            order_selected=("999",),
            order_view_mode="1",
        )
        pairs = parse_qsl(
            base64.b64decode(
                build_updated_form_data(page, ["102"], permission_types=["order"])
            ).decode("utf-8"),
            keep_blank_values=True,
        )

        self.assertEqual([value for key, value in pairs if key == "warehouseAssistant[]"], ["102", "999"])
        self.assertEqual([value for key, value in pairs if key == "comboType"], ["2"])
        self.assertEqual([value for key, value in pairs if key == "stockWarehouse[]"], ["101"])
        self.assertEqual([value for key, value in pairs if key == "allocationwarehouseAssistant[]"], ["101"])
        self.assertEqual([value for key, value in pairs if key == "stockCategoryOrWarehouse"], ["2"])

    def test_live_serialized_form_keeps_runtime_checked_fields(self) -> None:
        page = parse_permission_page(
            permission_html("11", selected=()),
            employee_id="11",
            page_url="https://private-amz.mabangerp.com/detail",
            serialized_form=(
                "mobile=13800000000&see_warehouse=1&comboType=2"
                "&stockWarehouse%5B%5D=101&allocationwarehouseAssistant%5B%5D=101"
            ),
        )
        pairs = parse_qsl(
            base64.b64decode(build_updated_form_data(page, ["102"])).decode("utf-8"),
            keep_blank_values=True,
        )

        self.assertIn(("comboType", "2"), pairs)
        self.assertEqual([value for key, value in pairs if key == "stockWarehouse[]"], ["101", "102"])
        self.assertEqual(
            [value for key, value in pairs if key == "allocationwarehouseAssistant[]"],
            ["101", "102"],
        )

    def test_build_updated_form_data_preserves_all_warehouse_mode(self) -> None:
        page = permission_page("11", selected=(), view_mode="2")
        pairs = parse_qsl(
            base64.b64decode(build_updated_form_data(page, ["102"])).decode("utf-8"),
            keep_blank_values=True,
        )

        self.assertEqual([value for key, value in pairs if key == "see_warehouse"], ["2"])
        self.assertEqual(
            [value for key, value in pairs if key == "allocationwarehouseAssistant[]"],
            ["102"],
        )

    def test_missing_permission_ids_checks_three_permission_groups_independently(self) -> None:
        only_product = permission_page("11", selected=(), product_selected=("102",), documented=True)
        only_warehouse = permission_page("12", selected=("102",), product_selected=(), documented=True)
        only_order = permission_page(
            "13",
            selected=(),
            product_selected=(),
            order_selected=("102",),
            documented=True,
        )

        self.assertEqual(missing_permission_ids(only_product, ("102",)), ((), ("102",), ("102",)))
        self.assertEqual(missing_permission_ids(only_warehouse, ("102",)), (("102",), ("102",), ()))
        self.assertEqual(missing_permission_ids(only_order, ("102",)), (("102",), (), ("102",)))

    def test_preview_never_marks_one_sided_permission_as_complete(self) -> None:
        employee = MabangEmployee("11", "张三", "13800000001", "运营部", "正常", "")

        class FakeGateway:
            page = None

            def __init__(self, *args, **kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def find_exact_employees(self, _name):
                return [employee]

            def get_permission_page(self, _employee_id):
                return self.page

        query = WarehousePermissionPreviewQuery("user", "password", ("张三",), ("102",))
        cases = (
            (
                permission_page(
                    "11",
                    selected=(),
                    product_selected=("102",),
                    order_selected=("102",),
                    documented=True,
                ),
                (),
                (),
                ("102",),
                "查看仓库缺 1 个",
            ),
            (
                permission_page(
                    "11",
                    selected=("102",),
                    product_selected=(),
                    order_selected=("102",),
                    documented=True,
                ),
                ("102",),
                (),
                (),
                "查看商品缺 1 个",
            ),
            (
                permission_page(
                    "11",
                    selected=("102",),
                    product_selected=("102",),
                    order_selected=(),
                    documented=True,
                ),
                (),
                ("102",),
                (),
                "查看订单缺 1 个",
            ),
            (
                permission_page(
                    "11",
                    selected=("102",),
                    product_selected=("102",),
                    order_selected=("102",),
                    order_view_mode="1",
                    documented=True,
                ),
                (),
                (),
                (),
                "查看订单待切换为按仓库查看",
            ),
        )
        for page, missing_product, missing_order, missing_warehouse, message in cases:
            FakeGateway.page = page
            with mock.patch.object(service, "MabangWarehousePermissionGateway", FakeGateway):
                result = preview_mabang_warehouse_permissions(query)

            row = result["rows"][0]
            self.assertEqual(row["status"], "ready")
            self.assertTrue(row["can_apply"])
            self.assertEqual(tuple(row["missing_product_warehouse_ids"]), missing_product)
            self.assertEqual(tuple(row["missing_order_warehouse_ids"]), missing_order)
            self.assertEqual(tuple(row["missing_view_warehouse_ids"]), missing_warehouse)
            self.assertIn(message, row["message"])

    def test_preview_only_checks_selected_permission_types(self) -> None:
        employee = MabangEmployee("11", "张三", "13800000001", "运营部", "正常", "")

        class FakeGateway:
            def __init__(self, *args, **kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def find_exact_employees(self, _name):
                return [employee]

            def get_permission_page(self, _employee_id):
                return permission_page(
                    "11",
                    selected=(),
                    product_selected=(),
                    order_selected=("102",),
                    documented=False,
                )

        query = WarehousePermissionPreviewQuery(
            "user",
            "password",
            ("张三",),
            ("102",),
            permission_types=("order",),
        )
        with mock.patch.object(service, "MabangWarehousePermissionGateway", FakeGateway):
            result = preview_mabang_warehouse_permissions(query)

        row = result["rows"][0]
        self.assertEqual(row["status"], "unchanged")
        self.assertFalse(row["can_apply"])
        self.assertEqual(row["missing_product_warehouse_ids"], [])
        self.assertEqual(row["missing_view_warehouse_ids"], [])
        self.assertEqual(result["permission_types"], ["order"])

    def test_batch_repairs_product_only_and_warehouse_only_permissions(self) -> None:
        employees = {
            "只有商品": MabangEmployee("11", "只有商品", "13800000001", "运营部", "正常", ""),
            "只有仓库": MabangEmployee("12", "只有仓库", "13800000002", "运营部", "正常", ""),
        }

        class FakeGateway:
            saved: set[str] = set()
            submitted: dict[str, list[tuple[str, str]]] = {}

            def __init__(self, *args, **kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def find_exact_employees(self, name):
                return [employees[name]]

            def get_permission_page(self, employee_id):
                if employee_id in self.saved:
                    return permission_page(
                        employee_id,
                        selected=("102",),
                        product_selected=("102",),
                        order_selected=("102", "999"),
                        documented=True,
                    )
                if employee_id == "11":
                    return permission_page(
                        employee_id,
                        selected=(),
                        product_selected=("102",),
                        documented=True,
                    )
                return permission_page(
                    employee_id,
                    selected=("102",),
                    product_selected=(),
                    documented=True,
                )

            def save_permission_page(self, page, form_data):
                pairs = parse_qsl(
                    base64.b64decode(form_data).decode("utf-8"),
                    keep_blank_values=True,
                )
                self.submitted[page.employee_id] = pairs
                self.saved.add(page.employee_id)
                return {"success": True}

        job = WarehousePermissionBatchJob(
            username="user",
            password="password",
            warehouse_ids=("102",),
            employees=(
                ExpectedEmployeePermission(
                    "只有商品",
                    "11",
                    (),
                    ("102",),
                    "1",
                    ("201", "202"),
                    True,
                    current_order_warehouse_ids=("999",),
                ),
                ExpectedEmployeePermission(
                    "只有仓库",
                    "12",
                    ("102",),
                    (),
                    "1",
                    ("201", "202"),
                    True,
                    current_order_warehouse_ids=("999",),
                ),
            ),
        )
        with mock.patch.object(service, "MabangWarehousePermissionGateway", FakeGateway):
            result = run_mabang_warehouse_permission_batch(job)

        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn("查看仓库补齐 1 个", result["rows"][0]["message"])
        self.assertIn("查看商品补齐 1 个", result["rows"][1]["message"])
        self.assertTrue(all(row["order_added_count"] == 1 for row in result["rows"]))
        self.assertTrue(all("查看订单补齐 1 个" in row["message"] for row in result["rows"]))
        for pairs in FakeGateway.submitted.values():
            self.assertIn(("stockWarehouse[]", "102"), pairs)
            self.assertIn(("warehouseAssistant[]", "102"), pairs)
            self.assertIn(("allocationwarehouseAssistant[]", "102"), pairs)

    def test_batch_continues_after_stale_employee_and_saves_next_employee(self) -> None:
        employees = {
            "张三": MabangEmployee("11", "张三", "13800000001", "运营部", "正常", ""),
            "李四": MabangEmployee("12", "李四", "13800000002", "运营部", "正常", ""),
        }

        class FakeGateway:
            saved: set[str] = set()

            def __init__(self, *args, **kwargs):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def find_exact_employees(self, name):
                return [employees[name]]

            def get_permission_page(self, employee_id):
                if employee_id == "11":
                    return permission_page("11", selected=("999",))
                selected = ("101", "102") if employee_id in self.saved else ("101",)
                return permission_page(
                    employee_id,
                    selected=selected,
                    order_selected=("102", "999") if employee_id in self.saved else ("999",),
                    documented=employee_id in self.saved,
                )

            def save_permission_page(self, page, form_data):
                self.saved.add(page.employee_id)
                return {"success": True}

        job = WarehousePermissionBatchJob(
            username="user",
            password="password",
            warehouse_ids=("102",),
            employees=(
                ExpectedEmployeePermission(
                    "张三",
                    "11",
                    ("101",),
                    ("101",),
                    "2",
                    (),
                    False,
                    current_order_warehouse_ids=("999",),
                ),
                ExpectedEmployeePermission(
                    "李四",
                    "12",
                    ("101",),
                    ("101",),
                    "2",
                    (),
                    False,
                    current_order_warehouse_ids=("999",),
                ),
            ),
        )
        with mock.patch.object(service, "MabangWarehousePermissionGateway", FakeGateway):
            result = run_mabang_warehouse_permission_batch(job)

        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["rows"][0]["status"], "failed")
        self.assertEqual(result["rows"][1]["status"], "success")


if __name__ == "__main__":
    unittest.main()

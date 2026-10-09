from __future__ import annotations

import base64
import unittest
from urllib.parse import parse_qsl, urlencode
from unittest.mock import patch

from backend.services import mabang_developer_permission as service
from backend.services.mabang_warehouse_permission import MabangEmployee


def permission_html(employee_id: str = "11", *, developer_id: str = "dynamic-developer") -> str:
    return f"""
    <form name="formEmployee">
      <input name="displayName" value="员工原名">
      <input name="csrf_token" value="token-old" type="hidden">
      <input name="renderTime" value="100" type="hidden">
      <input name="departmentId" value="dep-2" type="hidden">
      <select name="roleId"><option value="role-3" selected>运营专员</option></select>
      <label><input type="checkbox" name="stationId[]" value="sales" checked>销售员</label>
      <label><input type="checkbox" name="stationId[]" value="{developer_id}">开发员</label>
      <label><input type="checkbox" name="stationId[]" value="finance">财务专员</label>
      <label><input type="radio" name="stockCategoryOrWarehouse" value="2" checked>按仓库查看</label>
      <label><input type="radio" name="stockCategoryOrWarehouse" value="1">按商品父目录查看</label>
      <label><input type="radio" name="stockCategoryOrWarehouse" value="3">按人员岗位查看</label>
      <label><input type="radio" name="stockCategoryOrWarehouse" value="4">查看所有商品</label>
      <label><input type="checkbox" name="stockCategory[]" value="category-a" checked>服装</label>
      <label><input type="checkbox" name="stockCategory[]" value="category-b">鞋子</label>
      <label><input type="checkbox" name="parent_category_and_warehouse" value="1">目录和仓库</label>
      <input name="stockWarehouse[]" type="checkbox" value="101" checked>
      <input name="stockWarehouse[]" type="checkbox" value="102">
      <input name="allocationwarehouseAssistant[]" type="checkbox" value="201" checked>
      <input name="warehouseAssistant[]" type="checkbox" value="301" checked>
      <input name="see_warehouse" type="radio" value="1" checked>
      <input name="comboType" type="radio" value="2" checked>
      <input name="otherPermission[]" type="checkbox" value="keep" checked>
      <input name="unchangedEmpty" value="">
      <textarea name="notes">第一行\n第二行</textarea>
      <input name="ignoredDisabled" value="not-submitted" disabled>
      <input name="ignoredUnchecked" type="checkbox" value="not-submitted">
      <button type="button">保存修改</button>
    </form>
    <script>$.ajax({{url: "https://private-amz.mabangerp.com/index.php?mod=staff.doUpdateEmployee&id={employee_id}&DOMAIN=900853.private.mabangerp.com&channel_id=1"}});</script>
    """


def page(employee_id: str = "11", *, developer_id: str = "dynamic-developer", serialized_form: str = ""):
    return service.parse_developer_permission_page(
        permission_html(employee_id, developer_id=developer_id),
        employee_id=employee_id,
        page_url=f"https://private-amz.mabangerp.com/index.php?mod=staff.staffdetail&id={employee_id}",
        serialized_form=serialized_form,
    )


def changed_page(original, **replacements):
    pairs = [(key, value) for key, value in original.form_pairs if key not in replacements]
    for key, values in replacements.items():
        if isinstance(values, str):
            values = [values]
        pairs.extend((key, value) for value in values)
    return page(original.employee_id, developer_id=original.developer_station_id, serialized_form=urlencode(pairs))


def employee(employee_id: str, name: str, status: str = "正常") -> MabangEmployee:
    return MabangEmployee(employee_id, name, "13800000000", "运营部", status, "")


class FakeGateway:
    def __init__(self, employees: dict, pages: dict):
        self.employees = employees
        self.pages = pages
        self.saved: dict[str, tuple[tuple[str, str], ...]] = {}
        self.fail_search: set[str] = set()
        self.fail_save: set[str] = set()
        self.fail_readback: set[str] = set()
        self.keep_stale_readback: set[str] = set()
        self.corrupt_readback: set[str] = set()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def find_exact_employees(self, name):
        if name in self.fail_search:
            raise RuntimeError("模拟员工搜索失败")
        return self.employees.get(name, [])

    def get_permission_page(self, employee_id):
        if employee_id in self.saved and employee_id in self.fail_readback:
            raise RuntimeError("模拟回读失败")
        current = self.pages[employee_id]
        if isinstance(current, Exception):
            raise current
        return current

    def save_permission_page(self, original, form_data):
        pairs = tuple(parse_qsl(base64.b64decode(form_data).decode("utf-8"), keep_blank_values=True))
        self.saved[original.employee_id] = pairs
        if original.employee_id in self.fail_save:
            return {"success": False, "message": "模拟保存失败"}
        if original.employee_id not in self.keep_stale_readback:
            self.pages[original.employee_id] = page(original.employee_id, developer_id=original.developer_station_id, serialized_form=urlencode(pairs))
        if original.employee_id in self.corrupt_readback:
            self.pages[original.employee_id] = changed_page(self.pages[original.employee_id], **{"stockCategory[]": ["category-b"]})
        return {"success": True}


def query(names=("张三",)):
    return service.DeveloperPermissionPreviewQuery("test-user", "test-password", tuple(names))


def preview(gateway, names=("张三",)):
    with patch.object(service, "MabangDeveloperPermissionGateway", return_value=gateway):
        return service.preview_mabang_developer_permissions(query(names))


def batch_job(preview_result):
    return service.build_batch_job(username="test-user", password="test-password", preview_rows=preview_result["rows"])


def run_batch(gateway, job):
    with patch.object(service, "MabangDeveloperPermissionGateway", return_value=gateway):
        return service.run_mabang_developer_permission_batch(job)


class MabangDeveloperPermissionTests(unittest.TestCase):
    def test_truncated_search_cannot_claim_a_unique_employee(self):
        gateway = object.__new__(service.MabangDeveloperPermissionGateway)
        truncated = [employee(str(index), "张三" if index == 0 else f"张三{index}") for index in range(100)]
        with patch.object(gateway, "search_rows", return_value=truncated), self.assertRaisesRegex(service.MabangApiError, "无法确认唯一匹配"):
            gateway.find_exact_employees("张三")
        with patch.object(gateway, "search_rows", return_value=truncated[:2]):
            self.assertEqual([match.id for match in gateway.find_exact_employees(" 张三 ")], ["0"])

    def test_names_deduplicate_and_limit_one_hundred(self):
        parsed = service.validate_preview_payload({"username": "u", "password": "p", "employee_text": " 张三\n\n李四\n张三 "})
        self.assertEqual(parsed.employee_names, ("张三", "李四"))
        self.assertNotIn("password=", repr(parsed))
        for names in ([], [f"员工{index}" for index in range(101)]):
            with self.subTest(names_count=len(names)), self.assertRaises(ValueError):
                service.validate_preview_payload({"username": "u", "password": "p", "employee_names": names})

    def test_only_target_fields_change_and_all_other_successful_values_survive(self):
        original = page()
        updated = tuple(parse_qsl(base64.b64decode(service.build_updated_form_data(original)).decode("utf-8"), keep_blank_values=True))
        self.assertEqual([pair for pair in updated if pair[0] not in service.MANAGED_FIELDS], [pair for pair in original.form_pairs if pair[0] not in service.MANAGED_FIELDS])
        self.assertEqual([value for key, value in updated if key == service.STATION_FIELD], ["sales", "dynamic-developer"])
        self.assertEqual([value for key, value in updated if key == service.PRODUCT_VIEW_FIELD], ["1"])
        self.assertIn(("stockCategory[]", "category-a"), updated)
        self.assertNotIn(("stockCategory[]", "category-b"), updated)
        self.assertFalse(any(key == "parent_category_and_warehouse" for key, _value in updated))
        self.assertIn(("unchangedEmpty", ""), updated)
        self.assertFalse(any(key.startswith("ignored") for key, _value in updated))

    def test_dynamic_developer_value_and_runtime_serialization_are_used(self):
        original = page(developer_id="27")
        runtime = changed_page(original, **{service.STATION_FIELD: ["sales", "finance"], "stockCategory[]": ["category-b"], "parent_category_and_warehouse": ["1"]})
        updated = service.updated_form_pairs(runtime)
        self.assertEqual(runtime.current_station_names, ["销售员", "财务专员"])
        self.assertEqual([value for key, value in updated if key == service.STATION_FIELD], ["sales", "finance", "27"])
        self.assertIn(("stockCategory[]", "category-b"), updated)
        self.assertIn(("parent_category_and_warehouse", "1"), updated)

    def test_no_warehouse_or_eb_category_is_required(self):
        minimal = """<form name="formEmployee"><label><input type="checkbox" name="stationId[]" value="9">开发员</label><label><input type="radio" name="stockCategoryOrWarehouse" value="1" checked>按商品父目录查看</label></form><script>$.ajax({url: "https://private-amz.mabangerp.com/index.php?mod=staff.doUpdateEmployee&id=11"})</script>"""
        parsed = service.parse_developer_permission_page(minimal, employee_id="11", page_url="https://private-amz.mabangerp.com/index.php")
        self.assertEqual(parsed.developer_station_id, "9")
        self.assertIn((service.STATION_FIELD, "9"), service.updated_form_pairs(parsed))

    def test_missing_or_ambiguous_critical_controls_and_wrong_save_target_fail_closed(self):
        original = permission_html()
        invalid_pages = [
            original.replace('name="formEmployee"', 'name="wrong"'),
            original.replace("开发员", "采购员"),
            original.replace('value="dynamic-developer"', 'value="dynamic-developer" disabled'),
            original.replace('value="1">按商品父目录查看', 'value="1" disabled>按商品父目录查看'),
            original.replace('value="2" checked>按仓库查看', 'value="2">按仓库查看'),
            original.replace('value="1">按商品父目录查看', 'value="1" checked>按商品父目录查看'),
            original.replace("财务专员", "开发员"),
            original.replace("staff.doUpdateEmployee", "staff.unsupported"),
            original.replace("doUpdateEmployee&id=11", "doUpdateEmployee&id=12"),
            original.replace("https://private-amz.mabangerp.com", "https://example.com"),
        ]
        for index, invalid in enumerate(invalid_pages):
            with self.subTest(case=index), self.assertRaises(service.MabangApiError):
                service.parse_developer_permission_page(invalid, employee_id="11", page_url="https://private-amz.mabangerp.com/index.php")

    def test_preview_handles_unique_ambiguous_inactive_missing_and_error_independently(self):
        gateway = FakeGateway({
            "张三": [employee("11", "张三")],
            "李四": [employee("12", "李四"), employee("13", "李四")],
            "停用员工": [employee("14", "停用员工", "停用")],
            "坏页面": [employee("15", "坏页面")],
        }, {"11": page("11"), "15": RuntimeError("表单加载失败")})
        result = preview(gateway, ("张三", "李四", "停用员工", "不存在", "坏页面"))
        self.assertEqual([row["status"] for row in result["rows"]], ["ready", "ambiguous", "inactive", "not_found", "error"])
        self.assertEqual((result["ready_count"], result["attention_count"]), (1, 4))
        self.assertEqual(gateway.saved, {})
        self.assertEqual(len(result["rows"][0]["permission_fingerprint"]), 64)
        self.assertNotIn("form_pairs", result["rows"][0])

    def test_preview_and_batch_are_idempotent(self):
        gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": page()})
        initial = preview(gateway)
        job = batch_job(initial)
        result = run_batch(gateway, job)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(preview(gateway)["rows"][0]["status"], "unchanged")
        saved = dict(gateway.saved)
        self.assertEqual(run_batch(gateway, job)["skipped_count"], 1)
        self.assertEqual(saved, gateway.saved)
        saved_pairs = gateway.saved["11"]
        self.assertEqual([pair for pair in saved_pairs if pair == (service.STATION_FIELD, "dynamic-developer")], [(service.STATION_FIELD, "dynamic-developer")])

    def test_existing_developer_only_changes_product_mode(self):
        original = changed_page(page(), **{service.STATION_FIELD: ["sales", "dynamic-developer", "finance"]})
        gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": original})
        result = run_batch(gateway, batch_job(preview(gateway)))
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["rows"][0]["added_count"], 0)
        self.assertEqual(gateway.pages["11"].selected_station_ids, original.selected_station_ids)

    def test_batch_uses_latest_full_form_while_ignoring_render_tokens_in_snapshot(self):
        gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": page()})
        job = batch_job(preview(gateway))
        gateway.pages["11"] = changed_page(gateway.pages["11"], csrf_token="fresh-token", renderTime="200", displayName="最新员工名")
        result = run_batch(gateway, job)
        self.assertEqual(result["success_count"], 1)
        self.assertIn(("csrf_token", "fresh-token"), gateway.saved["11"])
        self.assertIn(("displayName", "最新员工名"), gateway.saved["11"])

    def test_permission_role_department_station_and_mode_drift_refuse_save(self):
        changes = [
            {service.STATION_FIELD: ["sales", "finance"]},
            {service.PRODUCT_VIEW_FIELD: "3"},
            {"stockCategory[]": ["category-b"]},
            {"parent_category_and_warehouse": ["1"]},
            {"stockWarehouse[]": ["102"]},
            {"allocationwarehouseAssistant[]": ["202"]},
            {"warehouseAssistant[]": ["302"]},
            {"roleId": "role-4"},
            {"departmentId": "dep-3"},
            {"otherPermission[]": []},
        ]
        for change in changes:
            with self.subTest(field=next(iter(change))):
                gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": page()})
                job = batch_job(preview(gateway))
                gateway.pages["11"] = changed_page(gateway.pages["11"], **change)
                result = run_batch(gateway, job)
                self.assertEqual(result["failed_count"], 1)
                self.assertIn("预览后发生变化", result["rows"][0]["message"])
                self.assertEqual(gateway.saved, {})

    def test_changed_employee_identity_or_status_refuses_save(self):
        for matches in ([], [employee("12", "张三")], [employee("11", "张三"), employee("12", "张三")], [employee("11", "张三", "停用")]):
            with self.subTest(matches=len(matches)):
                gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": page()})
                job = batch_job(preview(gateway))
                gateway.employees["张三"] = matches
                self.assertEqual(run_batch(gateway, job)["failed_count"], 1)
                self.assertEqual(gateway.saved, {})

    def test_save_and_readback_failures_do_not_report_success_or_block_next_employee(self):
        for failure in ("fail_save", "fail_readback", "keep_stale_readback", "corrupt_readback"):
            with self.subTest(failure=failure):
                gateway = FakeGateway({"张三": [employee("11", "张三")], "李四": [employee("12", "李四")]}, {"11": page("11"), "12": page("12")})
                job = batch_job(preview(gateway, ("张三", "李四")))
                getattr(gateway, failure).add("11")
                result = run_batch(gateway, job)
                self.assertEqual([row["status"] for row in result["rows"]], ["failed", "success"])
                self.assertTrue(result["rows"][0]["save_attempted"])
                self.assertIn("结果未确认", result["rows"][0]["message"])
                self.assertEqual((result["failed_count"], result["success_count"]), (1, 1))

    def test_stale_employee_does_not_block_next_employee(self):
        gateway = FakeGateway({"张三": [employee("11", "张三")], "李四": [employee("12", "李四")]}, {"11": page("11"), "12": page("12")})
        job = batch_job(preview(gateway, ("张三", "李四")))
        gateway.pages["11"] = changed_page(gateway.pages["11"], departmentId="moved")
        result = run_batch(gateway, job)
        self.assertEqual([row["status"] for row in result["rows"]], ["failed", "success"])
        self.assertEqual(set(gateway.saved), {"12"})

    def test_batch_requires_complete_preview_snapshot(self):
        gateway = FakeGateway({"张三": [employee("11", "张三")]}, {"11": page()})
        rows = preview(gateway)["rows"]
        for invalid_rows in ([], [dict(rows[0], permission_fingerprint="")], [rows[0], rows[0]], [dict(rows[0], can_apply=False)]):
            with self.subTest(rows=len(invalid_rows)), self.assertRaises(ValueError):
                service.build_batch_job(username="u", password="p", preview_rows=invalid_rows)


if __name__ == "__main__":
    unittest.main()

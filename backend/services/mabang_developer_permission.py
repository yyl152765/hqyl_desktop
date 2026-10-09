from __future__ import annotations

import base64
import hashlib
import html as html_module
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.core.mabang_client import MabangApiError
from backend.services.mabang_warehouse_permission import (
    MAX_EMPLOYEE_COUNT,
    MabangEmployee,
    MabangWarehouse,
    MabangWarehousePermissionGateway,
    ProgressCallback,
    exact_employee_matches,
    normalized_text,
    parse_employee_names,
    safe_text,
    serialize_successful_controls,
    validate_account_payload,
)


STATION_FIELD = "stationId[]"
PRODUCT_VIEW_FIELD = "stockCategoryOrWarehouse"
PRODUCT_PARENT_VIEW_MODE = "1"
MANAGED_FIELDS = frozenset({STATION_FIELD, PRODUCT_VIEW_FIELD})
PERMISSION_FIELD_PATTERN = re.compile(
    r"role|department|permission|assistant|warehouse|category|station|rights|^see_|^comboType$|^allow",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DeveloperPermissionPreviewQuery:
    username: str
    password: str = field(repr=False)
    employee_names: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedDeveloperPermission:
    input_name: str
    employee_id: str
    permission_fingerprint: str
    developer_station_id: str


@dataclass(frozen=True)
class DeveloperPermissionBatchJob:
    username: str
    password: str = field(repr=False)
    employees: tuple[ExpectedDeveloperPermission, ...]


@dataclass(frozen=True)
class EmployeeDeveloperPermissionPage:
    employee_id: str
    page_url: str
    save_url: str
    form_pairs: tuple[tuple[str, str], ...] = field(repr=False)
    stations: tuple[MabangWarehouse, ...]
    selected_station_ids: tuple[str, ...]
    developer_station_id: str
    product_view_mode: str
    protected_field_names: tuple[str, ...]

    @property
    def has_developer(self) -> bool:
        return self.developer_station_id in self.selected_station_ids

    @property
    def current_station_names(self) -> list[str]:
        selected = set(self.selected_station_ids)
        return [station.name for station in self.stations if station.id in selected]


def validate_preview_payload(payload: dict[str, Any]) -> DeveloperPermissionPreviewQuery:
    account = validate_account_payload(payload)
    names = parse_employee_names(payload.get("employee_names") or payload.get("employee_text"))
    if not names:
        raise ValueError("请至少输入一名员工")
    return DeveloperPermissionPreviewQuery(account.username, account.password, names)


def build_batch_job(
    *, username: str, password: str, preview_rows: Any
) -> DeveloperPermissionBatchJob:
    account = validate_account_payload({"username": username, "password": password})
    rows = preview_rows if isinstance(preview_rows, list) else []
    if len(rows) > MAX_EMPLOYEE_COUNT:
        raise ValueError(f"单次最多处理 {MAX_EMPLOYEE_COUNT} 名员工")
    employees: list[ExpectedDeveloperPermission] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "ready" or not row.get("can_apply"):
            continue
        input_name = normalized_text(row.get("input_name"))
        employee_id = safe_text(row.get("employee_id"))
        fingerprint = safe_text(row.get("permission_fingerprint"))
        developer_id = safe_text(row.get("developer_station_id"))
        if not input_name or not employee_id.isdigit() or not developer_id or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("员工预览快照不完整，请重新预览")
        if employee_id in seen_ids:
            raise ValueError("员工预览包含重复记录，请重新预览")
        seen_ids.add(employee_id)
        employees.append(ExpectedDeveloperPermission(input_name, employee_id, fingerprint, developer_id))
    if not employees:
        raise ValueError("当前预览没有可添加开发员的员工")
    return DeveloperPermissionBatchJob(account.username, account.password, tuple(employees))


def _control_label(form: Tag, control: Tag) -> str:
    label = control.find_parent("label")
    if label is None and control.get("id"):
        label = form.find("label", attrs={"for": control.get("id")})
    if label is None and isinstance(control.parent, Tag):
        label = control.parent
    return normalized_text(label.get_text(" ", strip=True) if label else "")


def parse_developer_permission_page(
    html_text: str, *, employee_id: str, page_url: str, serialized_form: str = ""
) -> EmployeeDeveloperPermissionPage:
    soup = BeautifulSoup(html_text or "", "html.parser")
    form = soup.find("form", attrs={"name": "formEmployee"})
    if not isinstance(form, Tag):
        raise MabangApiError(f"员工 {employee_id} 页面缺少 formEmployee 表单")

    stations: list[MabangWarehouse] = []
    developer_controls: list[Tag] = []
    seen_ids: set[str] = set()
    for control in form.find_all("input", attrs={"name": STATION_FIELD}):
        if safe_text(control.get("type")).lower() != "checkbox":
            raise MabangApiError("员工岗位控件格式异常，请人工检查")
        station_id = safe_text(control.get("value"))
        name = _control_label(form, control)
        if not station_id or station_id in seen_ids or not name:
            raise MabangApiError("员工岗位字段不完整或重复，请人工检查")
        seen_ids.add(station_id)
        stations.append(MabangWarehouse(station_id, name))
        if name == "开发员":
            developer_controls.append(control)
    if len(developer_controls) != 1 or developer_controls[0].has_attr("disabled"):
        raise MabangApiError("员工页面未找到唯一且可编辑的“开发员”岗位")

    mode_controls = form.find_all("input", attrs={"name": PRODUCT_VIEW_FIELD})
    modes = {safe_text(control.get("value")) for control in mode_controls if not control.has_attr("disabled")}
    parent_controls = [control for control in mode_controls if safe_text(control.get("value")) == PRODUCT_PARENT_VIEW_MODE]
    if (
        len(parent_controls) != 1
        or parent_controls[0].has_attr("disabled")
        or _control_label(form, parent_controls[0]) != "按商品父目录查看"
        or any(safe_text(control.get("type")).lower() != "radio" for control in mode_controls)
    ):
        raise MabangApiError("员工页面缺少可编辑的“按商品父目录查看”控件")

    pairs = parse_qsl(serialized_form, keep_blank_values=True) if serialized_form else serialize_successful_controls(form)
    selected_stations = tuple(value for key, value in pairs if key == STATION_FIELD)
    if not set(selected_stations).issubset(seen_ids):
        raise MabangApiError("员工实际岗位与页面选项不一致，请重新读取")
    current_modes = [value for key, value in pairs if key == PRODUCT_VIEW_FIELD]
    if len(current_modes) != 1 or current_modes[0] not in modes:
        raise MabangApiError("员工页面缺少唯一有效的商品查看方式")

    save_match = re.search(
        r"url\s*:\s*[\"']([^\"']*mod=staff\.doUpdateEmployee[^\"']*)[\"']",
        html_text or "", re.IGNORECASE,
    )
    if save_match is None:
        raise MabangApiError("员工页面缺少保存修改接口，无法继续")
    save_url = urljoin(page_url, html_module.unescape(save_match.group(1)))
    parsed_url = urlparse(save_url)
    query = parse_qs(parsed_url.query)
    if (
        parsed_url.scheme != "https"
        or not (parsed_url.hostname or "").endswith(".mabangerp.com")
        or query.get("mod") != ["staff.doUpdateEmployee"]
        or query.get("id") != [str(employee_id)]
    ):
        raise MabangApiError("员工保存修改接口与当前员工不匹配")

    # Hash only permission controls and role/department fields. Login tokens and
    # per-render timestamps are preserved in the save form but are not drift.
    protected_names = {
        safe_text(control.get("name"))
        for control in form.find_all(["input", "select", "textarea"])
        if control.get("name") and (
            control.name == "select"
            or safe_text(control.get("type")).lower() in {"radio", "checkbox"}
            or PERMISSION_FIELD_PATTERN.search(safe_text(control.get("name")))
        )
    }
    return EmployeeDeveloperPermissionPage(
        employee_id=str(employee_id), page_url=page_url, save_url=save_url,
        form_pairs=tuple(pairs), stations=tuple(stations), selected_station_ids=selected_stations,
        developer_station_id=safe_text(developer_controls[0].get("value")),
        product_view_mode=current_modes[0], protected_field_names=tuple(sorted(protected_names)),
    )


def permission_fingerprint(
    page: EmployeeDeveloperPermissionPage, *, form_pairs: tuple[tuple[str, str], ...] | None = None
) -> str:
    names = set(page.protected_field_names)
    pairs = page.form_pairs if form_pairs is None else form_pairs
    snapshot = [page.developer_station_id, sorted(names), sorted((key, value) for key, value in pairs if key in names)]
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def updated_form_pairs(page: EmployeeDeveloperPermissionPage) -> tuple[tuple[str, str], ...]:
    # Retain every other successful control, including repeated/blank values.
    pairs = [(key, value) for key, value in page.form_pairs if key != PRODUCT_VIEW_FIELD]
    if not page.has_developer:
        pairs.append((STATION_FIELD, page.developer_station_id))
    pairs.append((PRODUCT_VIEW_FIELD, PRODUCT_PARENT_VIEW_MODE))
    return tuple(pairs)


def build_updated_form_data(page: EmployeeDeveloperPermissionPage) -> str:
    return base64.b64encode(urlencode(updated_form_pairs(page)).encode("utf-8")).decode("ascii")


class MabangDeveloperPermissionGateway(MabangWarehousePermissionGateway):
    def find_exact_employees(self, employee_name: str) -> list[MabangEmployee]:
        rows = self.search_rows(employee_name, rows_per_page=100)
        if len(rows) >= 100:
            raise MabangApiError("员工搜索结果达到 100 条上限，无法确认唯一匹配，请核对完整姓名或人工处理")
        return exact_employee_matches(rows, employee_name)

    def get_permission_page(self, employee_id: str) -> EmployeeDeveloperPermissionPage:
        return parse_developer_permission_page(employee_id=employee_id, **self.get_employee_form(employee_id))


def _row(
    name: str, *, status: str, message: str, employee: MabangEmployee | None = None,
    page: EmployeeDeveloperPermissionPage | None = None, **extra: Any,
) -> dict[str, Any]:
    return {
        "input_name": name,
        "employee_id": employee.id if employee else "",
        "employee_name": employee.name if employee else "",
        "mobile": employee.mobile if employee else "",
        "department": employee.department if employee else "",
        "employee_status": employee.status if employee else "",
        "status": status, "message": message, "can_apply": status == "ready",
        "current_station_names": page.current_station_names if page else [],
        "current_station_ids": list(page.selected_station_ids) if page else [],
        "has_developer": page.has_developer if page else False,
        "product_view_mode": page.product_view_mode if page else "",
        "developer_station_id": page.developer_station_id if page else "",
        "permission_fingerprint": permission_fingerprint(page) if page else "",
        **extra,
    }


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


def preview_mabang_developer_permissions(
    query: DeveloperPermissionPreviewQuery, progress: ProgressCallback | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _emit(progress, "正在登录马帮并检查员工开发员岗位")
    with MabangDeveloperPermissionGateway(query.username, query.password) as gateway:
        for index, name in enumerate(query.employee_names, 1):
            _emit(progress, f"[{index}/{len(query.employee_names)}] 检查员工: {name}")
            employee = None
            try:
                matches = gateway.find_exact_employees(name)
                if not matches:
                    rows.append(_row(name, status="not_found", message="未找到姓名完全匹配的员工"))
                    continue
                if len(matches) != 1:
                    rows.append(_row(name, status="ambiguous", message=f"找到 {len(matches)} 名同名员工，请人工确认", candidates=[item.as_dict() for item in matches]))
                    continue
                employee = matches[0]
                if employee.status != "正常":
                    rows.append(_row(name, employee=employee, status="inactive", message="员工状态不是正常，无法自动保存"))
                    continue
                page = gateway.get_permission_page(employee.id)
                complete = page.has_developer and page.product_view_mode == PRODUCT_PARENT_VIEW_MODE
                changes = ([] if page.has_developer else ["添加开发员岗位"]) + ([] if page.product_view_mode == PRODUCT_PARENT_VIEW_MODE else ["查看商品切换为按商品父目录查看"])
                rows.append(_row(name, employee=employee, page=page, status="unchanged" if complete else "ready", message="已是开发员且按商品父目录查看，无需修改" if complete else "；".join(changes)))
            except Exception as exc:
                rows.append(_row(name, employee=employee, status="error", message=str(exc)))
    return {
        "mode": "preview", "employee_count": len(rows),
        "ready_count": sum(row["status"] == "ready" for row in rows),
        "skipped_count": sum(row["status"] == "unchanged" for row in rows),
        "attention_count": sum(row["status"] not in {"ready", "unchanged"} for row in rows),
        "rows": rows,
    }


def run_mabang_developer_permission_batch(
    job: DeveloperPermissionBatchJob, progress: ProgressCallback | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    _emit(progress, "正在登录马帮并准备批量添加开发员")
    with MabangDeveloperPermissionGateway(job.username, job.password) as gateway:
        for index, expected in enumerate(job.employees, 1):
            _emit(progress, f"[{index}/{len(job.employees)}] 处理员工: {expected.input_name}")
            employee = None
            page = None
            save_attempted = False
            try:
                matches = gateway.find_exact_employees(expected.input_name)
                if len(matches) != 1 or matches[0].id != expected.employee_id:
                    raise ValueError("员工匹配结果已变化，请重新预览")
                employee = matches[0]
                if employee.status != "正常":
                    raise ValueError("员工状态不是正常，请重新预览")
                page = gateway.get_permission_page(employee.id)
                if page.has_developer and page.product_view_mode == PRODUCT_PARENT_VIEW_MODE:
                    rows.append(_row(expected.input_name, employee=employee, page=page, status="skipped", message="已是开发员且按商品父目录查看，无需修改"))
                    continue
                if page.developer_station_id != expected.developer_station_id or permission_fingerprint(page) != expected.permission_fingerprint:
                    raise ValueError("员工岗位、权限、角色或部门在预览后发生变化，请重新预览")
                desired_fingerprint = permission_fingerprint(page, form_pairs=updated_form_pairs(page))
                save_attempted = True
                response = gateway.save_permission_page(page, build_updated_form_data(page))
                if response.get("success") not in (True, 1, "1"):
                    raise MabangApiError(safe_text(response.get("message")) or "马帮返回保存失败")
                verified = gateway.get_permission_page(employee.id)
                if not verified.has_developer or verified.product_view_mode != PRODUCT_PARENT_VIEW_MODE:
                    raise MabangApiError("保存后校验失败，开发员岗位或商品查看方式未生效")
                if permission_fingerprint(verified) != desired_fingerprint:
                    raise MabangApiError("保存后校验失败，其他岗位、权限、角色或部门与提交内容不一致")
                rows.append(_row(expected.input_name, employee=employee, page=verified, status="success", message="开发员岗位及按商品父目录查看已保存并校验", added_count=int(not page.has_developer), product_mode_changed=page.product_view_mode != PRODUCT_PARENT_VIEW_MODE))
                _emit(progress, f"[{index}/{len(job.employees)}] {expected.input_name}: 保存并校验成功")
            except Exception as exc:
                message = str(exc)
                if save_attempted:
                    message += "；保存请求已提交，结果未确认，请检查该员工后重新预览"
                rows.append(_row(expected.input_name, employee=employee, page=page, status="failed", message=message, employee_id=expected.employee_id, save_attempted=save_attempted))
                _emit(progress, f"[{index}/{len(job.employees)}] {expected.input_name}: 失败 - {message}")
    return {
        "mode": "batch", "employee_count": len(rows),
        "success_count": sum(row["status"] == "success" for row in rows),
        "skipped_count": sum(row["status"] == "skipped" for row in rows),
        "failed_count": sum(row["status"] == "failed" for row in rows),
        "rows": rows,
    }

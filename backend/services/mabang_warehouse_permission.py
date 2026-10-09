from __future__ import annotations

import base64
import html as html_module
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.core.mabang_client import MabangApiError


BASE_URL = "https://900853.private.mabangerp.com"
PRIVATE_BASE_URL = "https://private-amz.mabangerp.com"
TENANT_DOMAIN = "900853.private.mabangerp.com"
CHANNEL_ID = "1"
EMPLOYEE_LIST_PAGE_PATH = "/index.php?mod=employee.list"
EMPLOYEE_SEARCH_API_URL = f"{PRIVATE_BASE_URL}/index.php?mod=staff.getEmployList"
EMPLOYEE_SEARCH_URL = (
    f"{EMPLOYEE_SEARCH_API_URL}&DOMAIN={TENANT_DOMAIN}&channel_id={CHANNEL_ID}"
)
EMPLOYEE_DETAIL_PAGE_PATH = (
    "/index.php?mod=staff.staffdetail&id={employee_id}&channel_id="
    f"{CHANNEL_ID}&isDetail=2"
)
MAX_EMPLOYEE_COUNT = 100
EMPLOYEE_PAGE_TIMEOUT_MS = 60000
MAX_WAREHOUSE_COUNT = 500
EB_PRODUCT_CATEGORY_ID = "1696862"
PRODUCT_PARENT_VIEW_MODE = "1"
SPECIFIED_WAREHOUSE_VIEW_MODE = "1"
ORDER_WAREHOUSE_VIEW_MODE = "2"
ALL_PERMISSION_TYPES = ("product", "order", "warehouse")

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class MabangWarehouse:
    id: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class MabangEmployee:
    id: str
    name: str
    mobile: str
    department: str
    status: str
    permission_url: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "mobile": self.mobile,
            "department": self.department,
            "status": self.status,
            "permission_url": self.permission_url,
        }


@dataclass(frozen=True)
class MabangAccountQuery:
    username: str
    password: str


@dataclass(frozen=True)
class WarehousePermissionPreviewQuery:
    username: str
    password: str
    employee_names: tuple[str, ...]
    warehouse_ids: tuple[str, ...]
    permission_types: tuple[str, ...] = ALL_PERMISSION_TYPES


@dataclass(frozen=True)
class ExpectedEmployeePermission:
    input_name: str
    employee_id: str
    current_warehouse_ids: tuple[str, ...]
    current_product_warehouse_ids: tuple[str, ...]
    product_view_mode: str
    selected_product_category_ids: tuple[str, ...]
    parent_category_and_warehouse_enabled: bool
    warehouse_view_mode: str = SPECIFIED_WAREHOUSE_VIEW_MODE
    current_order_warehouse_ids: tuple[str, ...] = ()
    order_view_mode: str = ORDER_WAREHOUSE_VIEW_MODE


@dataclass(frozen=True)
class WarehousePermissionBatchJob:
    username: str
    password: str
    warehouse_ids: tuple[str, ...]
    employees: tuple[ExpectedEmployeePermission, ...]
    permission_types: tuple[str, ...] = ALL_PERMISSION_TYPES


@dataclass(frozen=True)
class EmployeePermissionPage:
    employee_id: str
    page_url: str
    save_url: str
    form_html: str
    serialized_form: str
    warehouses: tuple[MabangWarehouse, ...]
    selected_warehouse_ids: tuple[str, ...]
    warehouse_view_mode: str
    order_warehouses: tuple[MabangWarehouse, ...]
    selected_order_warehouse_ids: tuple[str, ...]
    order_view_mode: str
    product_warehouses: tuple[MabangWarehouse, ...]
    selected_product_warehouse_ids: tuple[str, ...]
    product_categories: tuple[MabangWarehouse, ...]
    selected_product_category_ids: tuple[str, ...]
    product_view_mode: str
    parent_category_and_warehouse_enabled: bool
    parent_category_and_warehouse_value: str
    eb_product_category_ids: tuple[str, ...]


def safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized_text(value: Any) -> str:
    return " ".join(safe_text(value).split())


def parse_employee_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_values: Iterable[Any] = value.splitlines()
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        name = normalized_text(item)
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
    if len(result) > MAX_EMPLOYEE_COUNT:
        raise ValueError(f"单次最多处理 {MAX_EMPLOYEE_COUNT} 名员工")
    return tuple(result)


def parse_warehouse_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_values: Iterable[Any] = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = []

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        warehouse_id = safe_text(item)
        if not warehouse_id or warehouse_id in seen:
            continue
        if not warehouse_id.isdigit():
            raise ValueError(f"仓库 ID 格式不正确: {warehouse_id}")
        seen.add(warehouse_id)
        result.append(warehouse_id)
    if len(result) > MAX_WAREHOUSE_COUNT:
        raise ValueError(f"单次最多选择 {MAX_WAREHOUSE_COUNT} 个仓库")
    return tuple(result)


def parse_permission_types(value: Any) -> tuple[str, ...]:
    if value is None:
        return ALL_PERMISSION_TYPES
    if isinstance(value, str):
        raw_values: Iterable[Any] = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = []

    selected = {safe_text(item).lower() for item in raw_values if safe_text(item)}
    unknown = selected - set(ALL_PERMISSION_TYPES)
    if unknown:
        raise ValueError(f"权限类型不正确: {', '.join(sorted(unknown))}")
    result = tuple(item for item in ALL_PERMISSION_TYPES if item in selected)
    if not result:
        raise ValueError("请至少选择一项开通权限")
    return result


def validate_account_payload(payload: dict[str, Any]) -> MabangAccountQuery:
    username = safe_text(payload.get("username"))
    password = "" if payload.get("password") is None else str(payload.get("password"))
    if not username:
        raise ValueError("请先绑定马帮账号")
    if not password:
        raise ValueError("马帮账号密码为空")
    return MabangAccountQuery(username=username, password=password)


def validate_preview_payload(payload: dict[str, Any]) -> WarehousePermissionPreviewQuery:
    account = validate_account_payload(payload)
    employee_names = parse_employee_names(payload.get("employee_names") or payload.get("employee_text"))
    warehouse_ids = parse_warehouse_ids(payload.get("warehouse_ids"))
    permission_types = parse_permission_types(payload.get("permission_types"))
    if not employee_names:
        raise ValueError("请至少输入一名员工")
    if not warehouse_ids:
        raise ValueError("请至少选择一个仓库")
    return WarehousePermissionPreviewQuery(
        username=account.username,
        password=account.password,
        employee_names=employee_names,
        warehouse_ids=warehouse_ids,
        permission_types=permission_types,
    )


def build_batch_job(
    *,
    username: str,
    password: str,
    warehouse_ids: Any,
    preview_rows: Any,
    permission_types: Any = None,
) -> WarehousePermissionBatchJob:
    account = validate_account_payload({"username": username, "password": password})
    normalized_warehouse_ids = parse_warehouse_ids(warehouse_ids)
    normalized_permission_types = parse_permission_types(permission_types)
    rows = preview_rows if isinstance(preview_rows, list) else []
    employees: list[ExpectedEmployeePermission] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "ready" or not row.get("can_apply"):
            continue
        input_name = normalized_text(row.get("input_name"))
        employee_id = safe_text(row.get("employee_id"))
        current_ids = parse_warehouse_ids(row.get("current_warehouse_ids"))
        if input_name and employee_id:
            employees.append(
                ExpectedEmployeePermission(
                    input_name=input_name,
                    employee_id=employee_id,
                    current_warehouse_ids=current_ids,
                    current_product_warehouse_ids=parse_warehouse_ids(row.get("current_product_warehouse_ids")),
                    product_view_mode=safe_text(row.get("product_view_mode")),
                    selected_product_category_ids=parse_warehouse_ids(row.get("selected_product_category_ids")),
                    parent_category_and_warehouse_enabled=bool(
                        row.get("parent_category_and_warehouse_enabled")
                    ),
                    warehouse_view_mode=safe_text(row.get("warehouse_view_mode"))
                    or SPECIFIED_WAREHOUSE_VIEW_MODE,
                    current_order_warehouse_ids=parse_warehouse_ids(
                        row.get("current_order_warehouse_ids")
                    ),
                    order_view_mode=safe_text(row.get("order_view_mode"))
                    or ORDER_WAREHOUSE_VIEW_MODE,
                )
            )
    if not normalized_warehouse_ids:
        raise ValueError("请至少选择一个仓库")
    if not employees:
        raise ValueError("当前预览没有可开通的员工")
    return WarehousePermissionBatchJob(
        username=account.username,
        password=account.password,
        warehouse_ids=normalized_warehouse_ids,
        employees=tuple(employees),
        permission_types=normalized_permission_types,
    )


def parse_employee_rows(html_fragment: str) -> list[MabangEmployee]:
    soup = BeautifulSoup(html_fragment or "", "html.parser")
    employees: list[MabangEmployee] = []
    seen_ids: set[str] = set()

    for permission_link in soup.find_all("a", href=True):
        href = safe_text(permission_link.get("href"))
        if "mod=staff.staffdetail" not in href:
            continue
        query = parse_qs(urlparse(html_module.unescape(href)).query)
        employee_id = safe_text((query.get("id") or [""])[0])
        is_detail = safe_text((query.get("isDetail") or [""])[0])
        link_text = normalized_text(permission_link.get_text(" ", strip=True))
        if not employee_id or (is_detail != "2" and link_text != "数据权限"):
            continue
        row = permission_link.find_parent("tr")
        if row is None or employee_id in seen_ids:
            continue

        cells = row.find_all("td", recursive=False)
        staff_links = []
        for candidate in row.find_all("a", href=True):
            candidate_href = safe_text(candidate.get("href"))
            if "mod=staff.staffdetail" not in candidate_href:
                continue
            candidate_query = parse_qs(urlparse(html_module.unescape(candidate_href)).query)
            if safe_text((candidate_query.get("id") or [""])[0]) == employee_id:
                staff_links.append(candidate)
        name = ""
        for candidate in staff_links:
            candidate_query = parse_qs(urlparse(html_module.unescape(safe_text(candidate.get("href")))).query)
            if safe_text((candidate_query.get("isDetail") or [""])[0]) != "2":
                name = normalized_text(candidate.get_text(" ", strip=True))
                if name and name not in {"查看", "数据权限", "停用", "启用"}:
                    break
        if not name and len(cells) > 2:
            name = normalized_text(cells[2].get_text(" ", strip=True))

        mobile = normalized_text(cells[3].get_text(" ", strip=True)) if len(cells) > 3 else ""
        if not mobile:
            phone_match = re.search(r"(?<!\d)1\d{10}(?!\d)", normalized_text(row.get_text(" ", strip=True)))
            mobile = phone_match.group(0) if phone_match else ""
        department = normalized_text(cells[6].get_text(" ", strip=True)) if len(cells) > 6 else ""
        row_text = normalized_text(row.get_text(" ", strip=True))
        status = "正常" if "正常" in row_text else "停用" if "停用" in row_text else ""

        seen_ids.add(employee_id)
        employees.append(
            MabangEmployee(
                id=employee_id,
                name=name,
                mobile=mobile,
                department=department,
                status=status,
                permission_url=urljoin(BASE_URL, html_module.unescape(href)),
            )
        )
    return employees


def exact_employee_matches(employees: Iterable[MabangEmployee], employee_name: str) -> list[MabangEmployee]:
    target = normalized_text(employee_name).casefold()
    return [employee for employee in employees if normalized_text(employee.name).casefold() == target]


def parse_permission_page(
    html_text: str,
    *,
    employee_id: str,
    page_url: str,
    serialized_form: str = "",
) -> EmployeePermissionPage:
    soup = BeautifulSoup(html_text or "", "html.parser")
    form = soup.find("form", attrs={"name": "formEmployee"})
    if not isinstance(form, Tag):
        raise MabangApiError(f"员工 {employee_id} 权限页缺少 formEmployee 表单")

    def parse_checkbox_options(name: str) -> tuple[list[MabangWarehouse], list[str]]:
        options: list[MabangWarehouse] = []
        selected: list[str] = []
        seen: set[str] = set()
        for input_element in form.find_all("input", attrs={"name": name}):
            option_id = safe_text(input_element.get("value"))
            if not option_id or option_id in seen:
                continue
            label = input_element.find_parent("label")
            if label is None:
                label = input_element.parent if isinstance(input_element.parent, Tag) else None
            option_name = normalized_text(label.get_text(" ", strip=True) if label else "") or option_id
            seen.add(option_id)
            options.append(MabangWarehouse(id=option_id, name=option_name))
            if input_element.has_attr("checked") and not input_element.has_attr("disabled"):
                selected.append(option_id)
        return options, selected

    warehouses, selected_ids = parse_checkbox_options("allocationwarehouseAssistant[]")
    if not warehouses:
        raise MabangApiError(f"员工 {employee_id} 权限页未找到‘查看仓库’字段")
    order_warehouses, selected_order_warehouse_ids = parse_checkbox_options("warehouseAssistant[]")
    if not order_warehouses:
        raise MabangApiError(f"员工 {employee_id} 权限页未找到‘查看订单 → 按仓库查看’字段")
    product_warehouses, selected_product_warehouse_ids = parse_checkbox_options("stockWarehouse[]")
    if not product_warehouses:
        raise MabangApiError(f"员工 {employee_id} 权限页未找到‘查看商品 → 按仓库查看’字段")
    if {item.id for item in product_warehouses} != {item.id for item in warehouses}:
        raise MabangApiError(f"员工 {employee_id} 的两组仓库权限字段不一致")

    product_categories, selected_product_category_ids = parse_checkbox_options("stockCategory[]")
    if not product_categories:
        raise MabangApiError(f"员工 {employee_id} 权限页未找到商品父目录字段")
    eb_product_category_ids = tuple(
        item.id
        for item in product_categories
        if re.sub(r"\s+", "", item.name).casefold() == "eb产品".casefold()
        or item.id == EB_PRODUCT_CATEGORY_ID
    )
    if not eb_product_category_ids:
        raise MabangApiError(f"员工 {employee_id} 权限页未找到 EB 产品父目录")

    parent_toggle = form.find("input", attrs={"name": "parent_category_and_warehouse"})
    parent_toggle_value = safe_text(parent_toggle.get("value")) if isinstance(parent_toggle, Tag) else ""
    if not parent_toggle_value:
        parent_toggle_value = "1"
    parent_toggle_enabled = bool(
        isinstance(parent_toggle, Tag)
        and parent_toggle.has_attr("checked")
        and not parent_toggle.has_attr("disabled")
    )

    html_view_mode = next(
        (
            safe_text(radio.get("value"))
            for radio in form.find_all("input", attrs={"name": "see_warehouse"})
            if radio.has_attr("checked")
        ),
        "",
    )
    html_product_view_mode = next(
        (
            safe_text(radio.get("value"))
            for radio in form.find_all("input", attrs={"name": "stockCategoryOrWarehouse"})
            if radio.has_attr("checked")
        ),
        "",
    )
    html_order_view_mode = next(
        (
            safe_text(control.get("value"))
            for control in form.find_all(attrs={"name": "comboType"})
            if (
                control.name != "input"
                or safe_text(control.get("type") or "text").lower() not in {"radio", "checkbox"}
                or control.has_attr("checked")
            )
        ),
        "",
    )
    live_pairs = parse_qsl(serialized_form, keep_blank_values=True) if serialized_form else []
    if live_pairs:
        selected_ids = [
            value for key, value in live_pairs if key == "allocationwarehouseAssistant[]"
        ]
        selected_product_warehouse_ids = [
            value for key, value in live_pairs if key == "stockWarehouse[]"
        ]
        selected_order_warehouse_ids = [
            value for key, value in live_pairs if key == "warehouseAssistant[]"
        ]
        selected_product_category_ids = [value for key, value in live_pairs if key == "stockCategory[]"]
        view_mode = next((value for key, value in live_pairs if key == "see_warehouse"), html_view_mode)
        order_view_mode = next(
            (value for key, value in live_pairs if key == "comboType"),
            html_order_view_mode,
        )
        product_view_mode = next(
            (value for key, value in live_pairs if key == "stockCategoryOrWarehouse"),
            html_product_view_mode,
        )
        parent_toggle_enabled = any(key == "parent_category_and_warehouse" for key, _ in live_pairs)
    else:
        view_mode = html_view_mode
        order_view_mode = html_order_view_mode
        product_view_mode = html_product_view_mode
    if view_mode not in {"1", "2"}:
        raise MabangApiError(f"员工 {employee_id} 权限页缺少仓库查看方式")
    if product_view_mode not in {"1", "2", "3", "4"}:
        raise MabangApiError(f"员工 {employee_id} 权限页缺少商品查看方式")
    if order_view_mode not in {"1", "2"}:
        raise MabangApiError(f"员工 {employee_id} 权限页缺少订单查看方式")

    save_match = re.search(
        r"url\s*:\s*[\"']([^\"']*mod=staff\.doUpdateEmployee[^\"']*)[\"']",
        html_text or "",
        flags=re.IGNORECASE,
    )
    if save_match:
        save_url = urljoin(page_url, html_module.unescape(save_match.group(1)))
    else:
        save_url = (
            f"{PRIVATE_BASE_URL}/index.php?mod=staff.doUpdateEmployee&id={employee_id}"
            f"&DOMAIN={TENANT_DOMAIN}&channel_id={CHANNEL_ID}"
        )

    return EmployeePermissionPage(
        employee_id=employee_id,
        page_url=page_url,
        save_url=save_url,
        form_html=str(form),
        serialized_form=serialized_form,
        warehouses=tuple(warehouses),
        selected_warehouse_ids=tuple(selected_ids),
        warehouse_view_mode=view_mode,
        order_warehouses=tuple(order_warehouses),
        selected_order_warehouse_ids=tuple(selected_order_warehouse_ids),
        order_view_mode=order_view_mode,
        product_warehouses=tuple(product_warehouses),
        selected_product_warehouse_ids=tuple(selected_product_warehouse_ids),
        product_categories=tuple(product_categories),
        selected_product_category_ids=tuple(selected_product_category_ids),
        product_view_mode=product_view_mode,
        parent_category_and_warehouse_enabled=parent_toggle_enabled,
        parent_category_and_warehouse_value=parent_toggle_value,
        eb_product_category_ids=eb_product_category_ids,
    )


def serialize_successful_controls(form: Tag) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for control in form.find_all(["input", "select", "textarea"]):
        name = safe_text(control.get("name"))
        if not name or control.has_attr("disabled"):
            continue
        tag_name = control.name.lower()
        if tag_name == "input":
            input_type = safe_text(control.get("type") or "text").lower()
            if input_type in {"button", "submit", "reset", "file", "image"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            value = control.get("value")
            if value is None and input_type in {"checkbox", "radio"}:
                value = "on"
            result.append((name, "" if value is None else str(value)))
            continue
        if tag_name == "textarea":
            value = control.get_text()
            value = re.sub(r"\r?\n", "\r\n", value)
            result.append((name, value))
            continue

        options = control.find_all("option")
        selected = [option for option in options if option.has_attr("selected") and not option.has_attr("disabled")]
        if not selected and not control.has_attr("multiple"):
            selected = [option for option in options if not option.has_attr("disabled")][:1]
        for option in selected:
            option_value = option.get("value")
            result.append((name, option.get_text() if option_value is None else str(option_value)))
    return result


def build_updated_form_data(
    page: EmployeePermissionPage,
    warehouse_ids: Iterable[str],
    permission_types: Any = None,
) -> str:
    targets = set(parse_warehouse_ids(tuple(warehouse_ids)))
    selected_types = set(parse_permission_types(permission_types))
    if "warehouse" in selected_types:
        missing_targets = targets - {warehouse.id for warehouse in page.warehouses}
        if missing_targets:
            raise ValueError(f"权限页不存在仓库 ID: {', '.join(sorted(missing_targets))}")
    if "product" in selected_types:
        missing_product_targets = targets - {warehouse.id for warehouse in page.product_warehouses}
        if missing_product_targets:
            raise ValueError(f"商品权限页不存在仓库 ID: {', '.join(sorted(missing_product_targets))}")
    if "order" in selected_types:
        missing_order_targets = targets - {warehouse.id for warehouse in page.order_warehouses}
        if missing_order_targets:
            raise ValueError(f"订单权限页不存在仓库 ID: {', '.join(sorted(missing_order_targets))}")
    if "product" in selected_types and not page.eb_product_category_ids:
        raise ValueError("权限页未找到 EB 产品父目录，无法按操作文档安全保存")

    if page.serialized_form:
        original_pairs = parse_qsl(page.serialized_form, keep_blank_values=True)
    else:
        soup = BeautifulSoup(page.form_html, "html.parser")
        form = soup.find("form", attrs={"name": "formEmployee"})
        if not isinstance(form, Tag):
            raise MabangApiError(f"员工 {page.employee_id} 权限表单无法重建")
        original_pairs = serialize_successful_controls(form)

    managed_keys: set[str] = set()
    if "product" in selected_types:
        managed_keys.update(
            {
                "stockWarehouse[]",
                "stockCategoryOrWarehouse",
                "stockCategory[]",
                "parent_category_and_warehouse",
            }
        )
    if "order" in selected_types:
        managed_keys.update({"comboType", "warehouseAssistant[]"})
    if "warehouse" in selected_types:
        managed_keys.update({"see_warehouse", "allocationwarehouseAssistant[]"})
    merged_pairs = [(key, value) for key, value in original_pairs if key not in managed_keys]

    def ordered_warehouse_ids(options: tuple[MabangWarehouse, ...], selected: tuple[str, ...]) -> list[str]:
        enabled = set(selected) | targets
        return [item.id for item in options if item.id in enabled]

    if "product" in selected_types:
        merged_pairs.append(("stockCategoryOrWarehouse", PRODUCT_PARENT_VIEW_MODE))
        merged_pairs.append(("parent_category_and_warehouse", page.parent_category_and_warehouse_value))
        excluded_categories = set(page.eb_product_category_ids)
        merged_pairs.extend(
            ("stockCategory[]", item.id)
            for item in page.product_categories
            if item.id not in excluded_categories
        )
        merged_pairs.extend(
            ("stockWarehouse[]", warehouse_id)
            for warehouse_id in ordered_warehouse_ids(
                page.product_warehouses,
                page.selected_product_warehouse_ids,
            )
        )
    if "order" in selected_types:
        merged_pairs.append(("comboType", ORDER_WAREHOUSE_VIEW_MODE))
        merged_pairs.extend(
            ("warehouseAssistant[]", warehouse_id)
            for warehouse_id in ordered_warehouse_ids(
                page.order_warehouses,
                page.selected_order_warehouse_ids,
            )
        )
    if "warehouse" in selected_types:
        merged_pairs.append(("see_warehouse", page.warehouse_view_mode))
        merged_pairs.extend(
            ("allocationwarehouseAssistant[]", warehouse_id)
            for warehouse_id in ordered_warehouse_ids(page.warehouses, page.selected_warehouse_ids)
        )

    serialized = urlencode(merged_pairs, doseq=True)
    if not serialized:
        raise MabangApiError(f"员工 {page.employee_id} 权限表单序列化结果为空")
    return base64.b64encode(serialized.encode("utf-8")).decode("ascii")


def required_product_category_ids(page: EmployeePermissionPage) -> tuple[str, ...]:
    excluded = set(page.eb_product_category_ids)
    return tuple(item.id for item in page.product_categories if item.id not in excluded)


def follows_documented_product_rules(page: EmployeePermissionPage) -> bool:
    required_categories = set(required_product_category_ids(page))
    selected_categories = set(page.selected_product_category_ids)
    return (
        page.product_view_mode == PRODUCT_PARENT_VIEW_MODE
        and page.parent_category_and_warehouse_enabled
        and selected_categories == required_categories
        and not selected_categories.intersection(page.eb_product_category_ids)
    )


def missing_permission_ids(
    page: EmployeePermissionPage,
    warehouse_ids: Iterable[str],
    permission_types: Any = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    targets = parse_warehouse_ids(tuple(warehouse_ids))
    selected_types = set(parse_permission_types(permission_types))
    product_selected = set(page.selected_product_warehouse_ids)
    order_selected = set(page.selected_order_warehouse_ids)
    warehouse_selected = set(page.selected_warehouse_ids)
    missing_product = tuple(item for item in targets if item not in product_selected) if "product" in selected_types else ()
    missing_order = tuple(item for item in targets if item not in order_selected) if "order" in selected_types else ()
    missing_warehouse = tuple(item for item in targets if item not in warehouse_selected) if "warehouse" in selected_types else ()
    return missing_product, missing_order, missing_warehouse


class MabangWarehousePermissionGateway:
    """Run Mabang employee operations inside an isolated headless Chrome session."""

    def __init__(self, username: str, password: str) -> None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright 运行环境，无法启动马帮权限工具") from exc

        self._timeout_error = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self.frame = None
        try:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--lang=zh-CN",
                ],
            }
            chrome_path = _find_chrome_executable()
            if chrome_path:
                launch_options["executable_path"] = str(chrome_path)
            else:
                launch_options["channel"] = "chrome"
            self.browser = self._playwright.chromium.launch(**launch_options)
            self.context = self.browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
            )
            self.page = self.context.new_page()
            self.page.set_default_timeout(30000)
            self._login(username, password)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "MabangWarehousePermissionGateway":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _login(self, username: str, password: str) -> None:
        assert self.page is not None
        self.page.goto(f"{BASE_URL}/index.htm", wait_until="domcontentloaded", timeout=30000)
        form = self.page.locator("#user-login")
        form.locator('input[name="username"]').fill(username)
        form.locator('input[name="password"]').fill(password)
        form.locator("#login-but").click()
        try:
            self.page.wait_for_url(lambda url: "index.htm" not in url, timeout=30000)
        except self._timeout_error as exc:
            error_text = normalized_text(
                self.page.locator("#error-container").text_content()
                if self.page.locator("#error-container").count()
                else ""
            )
            page_text = normalized_text(self.page.locator("body").inner_text())
            if "验证码" in page_text:
                raise MabangApiError("马帮登录要求验证码，请稍后重试或先在网页完成登录") from exc
            raise MabangApiError(error_text or "马帮登录超时，请检查账号密码") from exc

    def _open_employee_list(self) -> None:
        assert self.page is not None
        self.page.goto(
            f"{BASE_URL}{EMPLOYEE_LIST_PAGE_PATH}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        iframe = self.page.locator("#iframeContent")
        iframe.wait_for(state="attached", timeout=30000)
        self.frame = iframe.element_handle().content_frame()
        if self.frame is None:
            raise MabangApiError("马帮员工列表 iframe 加载失败")
        self.frame.wait_for_load_state("domcontentloaded", timeout=30000)
        self.frame.locator("#searchEmployText").wait_for(state="attached", timeout=30000)
        self.frame.wait_for_function("typeof window.jQuery === 'function'", timeout=30000)

    def search_rows(
        self,
        search_text: str,
        *,
        search_type: str = "nameLike",
        rows_per_page: int = 100,
    ) -> list[MabangEmployee]:
        self._open_employee_list()
        assert self.frame is not None
        payload = {
            "search-content": search_type,
            "searchtext": search_text,
            "roleIdS": "",
            "employee_data_role_id": "",
            "departmentId": "",
            "stationIdLike": "",
            "statusshow": "1",
            "page": "1",
            "rowsPerPage": str(max(10, min(rows_per_page, 100))),
            "status": "1",
        }
        search_request = {
            "url": EMPLOYEE_SEARCH_URL,
            "data": payload,
        }
        result = self.frame.evaluate(
            """
            async (request) => await new Promise((resolve) => {
              $.ajax({
                url: request.url,
                type: 'post',
                timeout: 30000,
                dataType: 'json',
                data: request.data
              }).done((data) => resolve({ok: true, data: data}))
                .fail((xhr) => resolve({
                  ok: false,
                  status: xhr.status,
                  message: (xhr.responseText || '').slice(0, 300)
                }));
            })
            """,
            search_request,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise MabangApiError(
                f"马帮员工搜索失败: {safe_text((result or {}).get('message') if isinstance(result, dict) else result)}"
            )
        response = result.get("data") or {}
        if not isinstance(response, dict):
            raise MabangApiError("马帮员工搜索返回格式异常")
        html_fragment = safe_text(response.get("html"))
        if not response.get("success") and not html_fragment:
            return []
        return parse_employee_rows(html_fragment)

    def find_exact_employees(self, employee_name: str) -> list[MabangEmployee]:
        return exact_employee_matches(self.search_rows(employee_name), employee_name)

    def get_employee_form(self, employee_id: str) -> dict[str, str]:
        """Read the complete runtime form without assuming a particular permission layout."""
        assert self.page is not None
        detail_path = EMPLOYEE_DETAIL_PAGE_PATH.format(employee_id=employee_id)
        self.page.goto(f"{BASE_URL}{detail_path}", wait_until="domcontentloaded", timeout=EMPLOYEE_PAGE_TIMEOUT_MS)
        iframe = self.page.locator("#iframeContent")
        iframe.wait_for(state="attached", timeout=30000)
        self.frame = iframe.element_handle().content_frame()
        if self.frame is None:
            raise MabangApiError(f"员工 {employee_id} 权限 iframe 加载失败")
        # Employee pages embed a large staff/permission payload before DOM ready.
        self.frame.wait_for_load_state("domcontentloaded", timeout=EMPLOYEE_PAGE_TIMEOUT_MS)
        self.frame.locator('form[name="formEmployee"]').wait_for(state="attached", timeout=30000)
        self.frame.wait_for_function("typeof window.jQuery === 'function'", timeout=30000)
        snapshot = self.frame.evaluate(
            r"""
            (employeeId) => {
              const form = document.querySelector('form[name="formEmployee"]');
              if (!form) throw new Error('员工页面缺少 formEmployee 表单');
              const clone = form.cloneNode(true);
              clone.querySelectorAll('script').forEach((script) => script.remove());
              const saveUrls = new Set();
              const sources = typeof window.doSaveEmployee === 'function'
                ? [window.doSaveEmployee.toString()]
                : [...document.scripts].map((script) => script.textContent || '');
              for (const source of sources) {
                const pattern = /url\s*:\s*["']([^"']*mod=staff\.doUpdateEmployee[^"']*)["']/ig;
                for (const match of source.matchAll(pattern)) {
                  const target = new URL(match[1].replace(/&amp;/g, '&'), location.href);
                  if (target.searchParams.get('id') === employeeId) saveUrls.add(target.href);
                }
              }
              if (saveUrls.size !== 1) throw new Error('员工页面缺少唯一的保存修改接口');
              const saveUrl = [...saveUrls][0];
              return {
                html_text: clone.outerHTML + '<script>$.ajax({url: ' + JSON.stringify(saveUrl) + '});</script>',
                serialized_form: $(form).serialize()
              };
            }
            """,
            str(employee_id),
        )
        return {
            "html_text": snapshot["html_text"],
            "page_url": self.frame.url,
            "serialized_form": snapshot["serialized_form"],
        }

    def get_permission_page(self, employee_id: str) -> EmployeePermissionPage:
        return parse_permission_page(
            employee_id=employee_id,
            **self.get_employee_form(employee_id),
        )

    def save_permission_page(self, page: EmployeePermissionPage, form_data: str) -> dict[str, Any]:
        if self.frame is None or self.frame.is_detached() or self.frame.url != page.page_url:
            self.get_permission_page(page.employee_id)
        assert self.frame is not None
        result = self.frame.evaluate(
            """
            async ({saveUrl, formData}) => await new Promise((resolve) => {
              $.ajax({
                type: 'POST',
                url: saveUrl,
                timeout: 30000,
                data: {formData: formData},
                dataType: 'json'
              }).done((data) => resolve({ok: true, data: data}))
                .fail((xhr) => resolve({
                  ok: false,
                  status: xhr.status,
                  message: (xhr.responseText || '').slice(0, 300)
                }));
            })
            """,
            {"saveUrl": page.save_url, "formData": form_data},
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise MabangApiError(
                f"马帮员工权限保存请求失败: {safe_text((result or {}).get('message') if isinstance(result, dict) else result)}"
            )
        payload = result.get("data") or {}
        if not isinstance(payload, dict):
            raise MabangApiError("马帮员工权限保存返回格式异常")
        return payload


def load_mabang_warehouse_options(
    query: MabangAccountQuery,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    _emit(progress, "正在登录马帮并读取员工权限页")
    with MabangWarehousePermissionGateway(query.username, query.password) as gateway:
        employees: list[MabangEmployee] = []
        if query.username.isdigit():
            employees = gateway.search_rows(query.username, search_type="mobile", rows_per_page=20)
        if not employees:
            employees = gateway.search_rows("", rows_per_page=20)
        if not employees:
            raise MabangApiError("员工列表为空，无法读取仓库列表")

        last_error: Exception | None = None
        for employee in employees[:5]:
            try:
                page = gateway.get_permission_page(employee.id)
                warehouses = [warehouse.as_dict() for warehouse in page.warehouses]
                _emit(progress, f"已读取 {len(warehouses)} 个仓库")
                return {"warehouses": warehouses, "warehouse_count": len(warehouses)}
            except Exception as exc:
                last_error = exc
        raise MabangApiError(f"无法从员工权限页读取仓库列表: {last_error}")


def preview_mabang_warehouse_permissions(
    query: WarehousePermissionPreviewQuery,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    selected_types = set(query.permission_types)
    _emit(progress, "正在登录马帮")
    with MabangWarehousePermissionGateway(query.username, query.password) as gateway:
        total = len(query.employee_names)
        for index, employee_name in enumerate(query.employee_names, start=1):
            _emit(progress, f"[{index}/{total}] 检查员工: {employee_name}")
            try:
                matches = gateway.find_exact_employees(employee_name)
                if not matches:
                    rows.append(_preview_row(employee_name, status="not_found", message="未找到姓名完全匹配的员工"))
                    continue
                if len(matches) > 1:
                    rows.append(
                        _preview_row(
                            employee_name,
                            status="ambiguous",
                            message=f"找到 {len(matches)} 名同名员工，请人工确认",
                            candidates=[employee.as_dict() for employee in matches],
                        )
                    )
                    continue

                employee = matches[0]
                page = gateway.get_permission_page(employee.id)
                warehouse_names = {
                    warehouse.id: warehouse.name
                    for warehouse in (*page.warehouses, *page.product_warehouses, *page.order_warehouses)
                }
                unknown_ids = [
                    warehouse_id
                    for warehouse_id in query.warehouse_ids
                    if warehouse_id not in {item.id for item in page.warehouses}
                ]
                if "warehouse" in selected_types and unknown_ids:
                    rows.append(
                        _preview_row(
                            employee_name,
                            status="error",
                            employee=employee,
                            message=f"权限页缺少所选仓库 ID: {', '.join(unknown_ids)}",
                        )
                    )
                    continue
                unknown_order_ids = [
                    warehouse_id
                    for warehouse_id in query.warehouse_ids
                    if warehouse_id not in {item.id for item in page.order_warehouses}
                ]
                if "order" in selected_types and unknown_order_ids:
                    rows.append(
                        _preview_row(
                            employee_name,
                            status="error",
                            employee=employee,
                            message=f"查看订单权限页缺少所选仓库 ID: {', '.join(unknown_order_ids)}",
                        )
                    )
                    continue
                unknown_product_ids = [
                    warehouse_id
                    for warehouse_id in query.warehouse_ids
                    if warehouse_id not in {item.id for item in page.product_warehouses}
                ]
                if "product" in selected_types and unknown_product_ids:
                    rows.append(
                        _preview_row(
                            employee_name,
                            status="error",
                            employee=employee,
                            message=f"查看商品权限页缺少所选仓库 ID: {', '.join(unknown_product_ids)}",
                        )
                    )
                    continue

                current_ids = tuple(page.selected_warehouse_ids)
                missing_product_ids, missing_order_ids, missing_view_warehouse_ids = missing_permission_ids(
                    page,
                    query.warehouse_ids,
                    query.permission_types,
                )
                missing_ids = tuple(
                    warehouse_id
                    for warehouse_id in query.warehouse_ids
                    if warehouse_id
                    in set(missing_product_ids) | set(missing_order_ids) | set(missing_view_warehouse_ids)
                )
                product_rules_complete = (
                    "product" not in selected_types or follows_documented_product_rules(page)
                )
                order_mode_complete = (
                    "order" not in selected_types or page.order_view_mode == ORDER_WAREHOUSE_VIEW_MODE
                )
                if not missing_ids and product_rules_complete and order_mode_complete:
                    status = "unchanged"
                    message = "所选权限均已包含目标仓库"
                    can_apply = False
                else:
                    status = "ready"
                    pending_parts: list[str] = []
                    if missing_product_ids:
                        pending_parts.append(f"查看商品缺 {len(missing_product_ids)} 个")
                    if missing_order_ids:
                        pending_parts.append(f"查看订单缺 {len(missing_order_ids)} 个")
                    if not order_mode_complete:
                        pending_parts.append("查看订单待切换为按仓库查看")
                    if missing_view_warehouse_ids:
                        pending_parts.append(f"查看仓库缺 {len(missing_view_warehouse_ids)} 个")
                    if "product" in selected_types and not product_rules_complete:
                        pending_parts.append("商品父目录待同步")
                    message = "；".join(pending_parts)
                    can_apply = True

                rows.append(
                    _preview_row(
                        employee_name,
                        status=status,
                        employee=employee,
                        message=message,
                        can_apply=can_apply,
                        current_warehouse_ids=list(current_ids),
                        current_warehouse_names=[warehouse_names.get(item, item) for item in current_ids],
                        current_product_warehouse_ids=list(page.selected_product_warehouse_ids),
                        current_order_warehouse_ids=list(page.selected_order_warehouse_ids),
                        missing_warehouse_ids=list(missing_ids),
                        missing_warehouse_names=[warehouse_names.get(item, item) for item in missing_ids],
                        missing_product_warehouse_ids=list(missing_product_ids),
                        missing_product_warehouse_names=[
                            warehouse_names.get(item, item) for item in missing_product_ids
                        ],
                        missing_order_warehouse_ids=list(missing_order_ids),
                        missing_order_warehouse_names=[
                            warehouse_names.get(item, item) for item in missing_order_ids
                        ],
                        missing_view_warehouse_ids=list(missing_view_warehouse_ids),
                        missing_view_warehouse_names=[
                            warehouse_names.get(item, item) for item in missing_view_warehouse_ids
                        ],
                        target_warehouse_count=len(query.warehouse_ids),
                        warehouse_view_mode=page.warehouse_view_mode,
                        order_view_mode=page.order_view_mode,
                        product_view_mode=page.product_view_mode,
                        selected_product_category_ids=list(page.selected_product_category_ids),
                        parent_category_and_warehouse_enabled=page.parent_category_and_warehouse_enabled,
                        permission_types=list(query.permission_types),
                    )
                )
            except Exception as exc:
                rows.append(_preview_row(employee_name, status="error", message=str(exc)))

    ready_count = sum(1 for row in rows if row["status"] == "ready")
    return {
        "mode": "preview",
        "employee_count": len(rows),
        "ready_count": ready_count,
        "skipped_count": sum(1 for row in rows if row["status"] == "unchanged"),
        "attention_count": sum(1 for row in rows if row["status"] in {"not_found", "ambiguous", "error"}),
        "permission_types": list(query.permission_types),
        "rows": rows,
    }


def run_mabang_warehouse_permission_batch(
    job: WarehousePermissionBatchJob,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    target_set = set(job.warehouse_ids)
    selected_types = set(job.permission_types)
    _emit(progress, "正在登录马帮并准备保存权限")
    with MabangWarehousePermissionGateway(job.username, job.password) as gateway:
        total = len(job.employees)
        for index, expected in enumerate(job.employees, start=1):
            _emit(progress, f"[{index}/{total}] 处理员工: {expected.input_name}")
            try:
                matches = gateway.find_exact_employees(expected.input_name)
                if len(matches) != 1 or matches[0].id != expected.employee_id:
                    raise ValueError("员工匹配结果已变化，请重新预览")
                employee = matches[0]
                page = gateway.get_permission_page(employee.id)
                current_set = set(page.selected_warehouse_ids)
                current_product_set = set(page.selected_product_warehouse_ids)
                current_order_set = set(page.selected_order_warehouse_ids)
                missing_product_ids, missing_order_ids, missing_view_warehouse_ids = missing_permission_ids(
                    page,
                    job.warehouse_ids,
                    job.permission_types,
                )
                if (
                    not missing_product_ids
                    and not missing_order_ids
                    and not missing_view_warehouse_ids
                    and ("order" not in selected_types or page.order_view_mode == ORDER_WAREHOUSE_VIEW_MODE)
                    and ("product" not in selected_types or follows_documented_product_rules(page))
                ):
                    rows.append(
                        _batch_row(
                            employee,
                            status="skipped",
                            message="所选权限均已包含目标仓库",
                        )
                    )
                    continue
                if "warehouse" in selected_types and current_set != set(expected.current_warehouse_ids):
                    raise ValueError("员工仓库权限在预览后发生变化，请重新预览")
                if "product" in selected_types and current_product_set != set(expected.current_product_warehouse_ids):
                    raise ValueError("员工商品仓库权限在预览后发生变化，请重新预览")
                if "order" in selected_types and current_order_set != set(expected.current_order_warehouse_ids):
                    raise ValueError("员工订单仓库权限在预览后发生变化，请重新预览")
                if "warehouse" in selected_types and page.warehouse_view_mode != expected.warehouse_view_mode:
                    raise ValueError("员工仓库查看方式在预览后发生变化，请重新预览")
                if "order" in selected_types and page.order_view_mode != expected.order_view_mode:
                    raise ValueError("员工订单查看方式在预览后发生变化，请重新预览")
                if "product" in selected_types and page.product_view_mode != expected.product_view_mode:
                    raise ValueError("员工商品查看方式在预览后发生变化，请重新预览")
                if "product" in selected_types and set(page.selected_product_category_ids) != set(expected.selected_product_category_ids):
                    raise ValueError("员工商品父目录权限在预览后发生变化，请重新预览")
                if (
                    "product" in selected_types
                    and page.parent_category_and_warehouse_enabled
                    != expected.parent_category_and_warehouse_enabled
                ):
                    raise ValueError("员工商品父目录联动权限在预览后发生变化，请重新预览")

                warehouse_ids_on_page = {warehouse.id for warehouse in page.warehouses}
                unknown_ids = target_set - warehouse_ids_on_page
                if "warehouse" in selected_types and unknown_ids:
                    raise ValueError(f"权限页缺少仓库 ID: {', '.join(sorted(unknown_ids))}")
                product_warehouse_ids_on_page = {warehouse.id for warehouse in page.product_warehouses}
                unknown_product_ids = target_set - product_warehouse_ids_on_page
                if "product" in selected_types and unknown_product_ids:
                    raise ValueError(
                        f"商品权限页缺少仓库 ID: {', '.join(sorted(unknown_product_ids))}"
                    )
                order_warehouse_ids_on_page = {warehouse.id for warehouse in page.order_warehouses}
                unknown_order_ids = target_set - order_warehouse_ids_on_page
                if "order" in selected_types and unknown_order_ids:
                    raise ValueError(
                        f"订单权限页缺少仓库 ID: {', '.join(sorted(unknown_order_ids))}"
                    )
                missing_count = len(
                    set(missing_product_ids) | set(missing_order_ids) | set(missing_view_warehouse_ids)
                )
                form_data = build_updated_form_data(page, job.warehouse_ids, job.permission_types)
                response = gateway.save_permission_page(page, form_data)
                if not response.get("success"):
                    raise MabangApiError(safe_text(response.get("message")) or "马帮返回保存失败")

                verified = gateway.get_permission_page(employee.id)
                if "warehouse" in selected_types and (
                    verified.warehouse_view_mode != page.warehouse_view_mode
                    or not target_set.issubset(set(verified.selected_warehouse_ids))
                ):
                    raise MabangApiError("保存后校验失败，‘查看仓库’未勾选目标仓库")
                if "product" in selected_types and not target_set.issubset(set(verified.selected_product_warehouse_ids)):
                    raise MabangApiError("保存后校验失败，‘查看商品 → 按仓库查看’未勾选目标仓库")
                if "order" in selected_types and (
                    verified.order_view_mode != ORDER_WAREHOUSE_VIEW_MODE
                    or not target_set.issubset(set(verified.selected_order_warehouse_ids))
                ):
                    raise MabangApiError("保存后校验失败，‘查看订单 → 按仓库查看’未勾选目标仓库")
                if "product" in selected_types and not follows_documented_product_rules(verified):
                    raise MabangApiError("保存接口返回成功，但商品父目录权限未按操作文档生效")
                completed_parts: list[str] = []
                if missing_product_ids:
                    completed_parts.append(f"查看商品补齐 {len(missing_product_ids)} 个")
                if missing_order_ids:
                    completed_parts.append(f"查看订单补齐 {len(missing_order_ids)} 个")
                if "order" in selected_types and page.order_view_mode != ORDER_WAREHOUSE_VIEW_MODE:
                    completed_parts.append("查看订单已切换为按仓库查看")
                if missing_view_warehouse_ids:
                    completed_parts.append(f"查看仓库补齐 {len(missing_view_warehouse_ids)} 个")
                if "product" in selected_types and not follows_documented_product_rules(page):
                    completed_parts.append("商品父目录权限已同步")
                if not completed_parts:
                    completed_parts.append("所选权限已同步")
                message = "；".join(completed_parts)
                rows.append(
                    _batch_row(
                        employee,
                        status="success",
                        message=message,
                        added_count=missing_count,
                        product_added_count=len(missing_product_ids) if "product" in selected_types else 0,
                        order_added_count=len(missing_order_ids) if "order" in selected_types else 0,
                        order_mode_changed=(
                            "order" in selected_types and page.order_view_mode != ORDER_WAREHOUSE_VIEW_MODE
                        ),
                        warehouse_added_count=len(missing_view_warehouse_ids) if "warehouse" in selected_types else 0,
                    )
                )
                _emit(progress, f"[{index}/{total}] {employee.name}: {message}")
            except Exception as exc:
                message = str(exc)
                rows.append(
                    {
                        "input_name": expected.input_name,
                        "employee_id": expected.employee_id,
                        "employee_name": expected.input_name,
                        "mobile": "",
                        "department": "",
                        "status": "failed",
                        "added_count": 0,
                        "message": message,
                    }
                )
                _emit(progress, f"[{index}/{total}] {expected.input_name}: 失败 - {message}")

    return {
        "mode": "batch",
        "employee_count": len(rows),
        "success_count": sum(1 for row in rows if row["status"] == "success"),
        "skipped_count": sum(1 for row in rows if row["status"] == "skipped"),
        "failed_count": sum(1 for row in rows if row["status"] == "failed"),
        "permission_types": list(job.permission_types),
        "rows": rows,
    }


def _find_chrome_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    return next((candidate for candidate in candidates if str(candidate) and candidate.is_file()), None)


def _preview_row(
    input_name: str,
    *,
    status: str,
    message: str,
    employee: MabangEmployee | None = None,
    can_apply: bool = False,
    candidates: list[dict[str, Any]] | None = None,
    current_warehouse_ids: list[str] | None = None,
    current_warehouse_names: list[str] | None = None,
    current_product_warehouse_ids: list[str] | None = None,
    current_order_warehouse_ids: list[str] | None = None,
    missing_warehouse_ids: list[str] | None = None,
    missing_warehouse_names: list[str] | None = None,
    missing_product_warehouse_ids: list[str] | None = None,
    missing_product_warehouse_names: list[str] | None = None,
    missing_order_warehouse_ids: list[str] | None = None,
    missing_order_warehouse_names: list[str] | None = None,
    missing_view_warehouse_ids: list[str] | None = None,
    missing_view_warehouse_names: list[str] | None = None,
    target_warehouse_count: int = 0,
    warehouse_view_mode: str = "",
    order_view_mode: str = "",
    product_view_mode: str = "",
    selected_product_category_ids: list[str] | None = None,
    parent_category_and_warehouse_enabled: bool = False,
    permission_types: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "input_name": input_name,
        "employee_id": employee.id if employee else "",
        "employee_name": employee.name if employee else "",
        "mobile": employee.mobile if employee else "",
        "department": employee.department if employee else "",
        "employee_status": employee.status if employee else "",
        "status": status,
        "message": message,
        "can_apply": can_apply,
        "candidates": candidates or [],
        "current_warehouse_ids": current_warehouse_ids or [],
        "current_warehouse_names": current_warehouse_names or [],
        "current_product_warehouse_ids": current_product_warehouse_ids or [],
        "current_order_warehouse_ids": current_order_warehouse_ids or [],
        "missing_warehouse_ids": missing_warehouse_ids or [],
        "missing_warehouse_names": missing_warehouse_names or [],
        "missing_product_warehouse_ids": missing_product_warehouse_ids or [],
        "missing_product_warehouse_names": missing_product_warehouse_names or [],
        "missing_order_warehouse_ids": missing_order_warehouse_ids or [],
        "missing_order_warehouse_names": missing_order_warehouse_names or [],
        "missing_view_warehouse_ids": missing_view_warehouse_ids or [],
        "missing_view_warehouse_names": missing_view_warehouse_names or [],
        "product_permission_complete": not bool(missing_product_warehouse_ids),
        "order_permission_complete": (
            not bool(missing_order_warehouse_ids)
            and order_view_mode == ORDER_WAREHOUSE_VIEW_MODE
        ),
        "warehouse_permission_complete": not bool(missing_view_warehouse_ids),
        "target_warehouse_count": int(target_warehouse_count or 0),
        "warehouse_view_mode": warehouse_view_mode,
        "order_view_mode": order_view_mode,
        "product_view_mode": product_view_mode,
        "selected_product_category_ids": selected_product_category_ids or [],
        "parent_category_and_warehouse_enabled": parent_category_and_warehouse_enabled,
        "permission_types": permission_types or [],
        "current_warehouse_count": len(current_warehouse_ids or []),
        "add_count": len(missing_warehouse_ids or []),
    }


def _batch_row(
    employee: MabangEmployee,
    *,
    status: str,
    message: str,
    added_count: int = 0,
    product_added_count: int = 0,
    order_added_count: int = 0,
    order_mode_changed: bool = False,
    warehouse_added_count: int = 0,
) -> dict[str, Any]:
    return {
        "input_name": employee.name,
        "employee_id": employee.id,
        "employee_name": employee.name,
        "mobile": employee.mobile,
        "department": employee.department,
        "status": status,
        "added_count": added_count,
        "product_added_count": product_added_count,
        "order_added_count": order_added_count,
        "order_mode_changed": order_mode_changed,
        "warehouse_added_count": warehouse_added_count,
        "message": message,
    }


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)

# -*- coding: utf-8 -*-
"""
DingTalk Sheet Utilities
Author: 玉林 杨
功能: 钉钉文档/表格常用操作封装，包括创建/删除 sheet，行列操作，读取/更新范围
"""

import os
import re
import sys
if sys.stdout:
    sys.stdout.reconfigure(encoding='utf-8')
import requests
import time
import string
from pathlib import Path
from typing import Any, List, Mapping
import yaml
from alibabacloud_dingtalk.doc_1_0.client import Client
from alibabacloud_dingtalk.doc_1_0 import models as doc_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from openpyxl.utils import get_column_letter
import math
from datetime import date, datetime, timedelta

HTTP_TIMEOUT = (5, 30)
HQYL_MARKERS = (
    "HQYL_ATTEMPT_ID",
    "HQYL_JOB_DIR",
    "HQYL_OUTPUT_DIR",
    "HQYL_INPUT_FILE",
    "HQYL_PARAMETERS_JSON",
    "HQYL_ASSETS_JSON",
)


def _is_hqyl_runtime(environment: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environment is None else environment
    return any(str(values.get(name) or "").strip() for name in HQYL_MARKERS)


def _load_legacy_dingtalk_config(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load the old local configuration only for direct/PyCharm execution."""
    values = os.environ if environment is None else environment
    if _is_hqyl_runtime(values):
        return {}
    config_file = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    if not config_file.is_file():
        return {}
    with config_file.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not isinstance(config, Mapping):
        return {}
    return dict(
        (((config.get("dingtalk") or {}).get("app") or {}).get("rpa") or {})
    )


def _resolve_dingtalk_config(
    supplied: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve config with environment assets taking highest precedence."""
    values = os.environ if environment is None else environment
    resolved = _load_legacy_dingtalk_config(values)
    for key, value in (supplied or {}).items():
        if value is not None and (not isinstance(value, str) or value.strip()):
            resolved[key] = value
    for environment_name, config_name in (
        ("DINGTALK_APP_KEY", "app_key"),
        ("DINGTALK_APP_SECRET", "app_secret"),
        ("DINGTALK_USER_ID", "user_id"),
        ("DINGTALK_OPERATOR_ID", "operator_id"),
    ):
        value = str(values.get(environment_name) or "").strip()
        if value:
            resolved[config_name] = value
    return resolved


_DEFAULT_DINGTALK_CONFIG = _resolve_dingtalk_config()
# Compatibility constants for legacy direct callers. They contain only injected
# environment values or non-HQYL local fallback values, never source literals.
APP_KEY = str(_DEFAULT_DINGTALK_CONFIG.get("app_key") or "")
APP_SECRET = str(_DEFAULT_DINGTALK_CONFIG.get("app_secret") or "")
USER_ID = str(_DEFAULT_DINGTALK_CONFIG.get("user_id") or "")


def _runtime_options() -> util_models.RuntimeOptions:
    return util_models.RuntimeOptions(
        autoretry=False,
        ignore_ssl=False,
        connect_timeout=5_000,
        read_timeout=30_000,
    )


def _request_json(method: str, url: str, **kwargs) -> dict[str, Any]:
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        # requests exceptions may include a prepared URL containing credentials.
        raise RuntimeError("DingTalk HTTP request failed") from None
    except ValueError:
        raise RuntimeError("DingTalk returned an invalid JSON response") from None
    if not isinstance(payload, dict):
        raise RuntimeError("DingTalk returned an unexpected response type")
    return payload


def _raise_api_error(action: str, response: Mapping[str, Any]) -> None:
    code = response.get("errcode")
    message = str(response.get("errmsg") or "unknown error")
    raise RuntimeError(f"{action} failed (code={code}): {message}")

# ========== 全局客户端 ==========
def create_client() -> Client:
    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    return Client(config)

client = create_client()

# ========== 重试装饰器 ==========
_DOCUMENT_ERROR_HINTS = {
    "invalidRequest.inputArgs.workbookIdIllegal": "工作簿ID不合法",
    "invalidRequest.inputArgs.operatorIdIllegal": "操作人ID不合法，请核对操作者unionId资产",
    "invalidRequest.inputArgs.invalid": "请求参数不合法",
    "forbidden.operationIllegal": "服务端拒绝此操作，请核对请求位置、工作表容量和文档限制",
    "forbidden.accessDenied": "操作者无权限，请核对文档授权及应用表格权限",
    "forbidden.acrossOrg": "文档与应用不属于同一组织",
    "forbidden.document.sizeOverLimit": "在线表格容量超限",
    "invalidRequest.resource.notFound": "工作簿或工作表不存在",
    "invalidAuthentication": "访问令牌无效，请核对应用资产",
    "invalidAuthentication.accessToken.invalid": "访问令牌无效，请核对应用资产",
    "invalidAuthentication.accessToken.expire": "访问令牌已过期",
    "Forbidden.AccessDenied.AccessTokenPermissionDenied": "应用缺少接口权限",
    "Forbidden.AccessDenied.AccessTokenExpired": "访问令牌已过期",
    "Forbidden.AccessDenied.AccessTokenInvalid": "访问令牌无效，请核对应用资产",
    "Throttling": "接口调用限流",
    "Throttling.Api": "接口调用限流",
    "internalError": "钉钉服务内部错误",
    "serviceBusy": "钉钉服务繁忙",
}


def safe_exception_summary(error: Exception) -> str:
    """Unwrap Tea/Darabonba failures without exposing request or response text.

    SDK messages/data may echo credentials, operator IDs or authenticated URLs.
    Only known API codes, HTTP status and UUID/hex request IDs are loggable.
    """
    pending, seen, errors = [error], set(), []
    while pending and len(errors) < 10:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        pending.extend(getattr(current, key, None) for key in
                       ("inner_exception", "__cause__", "__context__"))
    names = list(dict.fromkeys(type(item).__name__ for item in errors))
    details = [" -> ".join(names)]
    for current in errors:
        data = getattr(current, "data", None)
        data = data if isinstance(data, Mapping) else {}
        code = getattr(current, "code", None) or data.get("code") or data.get("Code")
        if isinstance(code, str) and code in _DOCUMENT_ERROR_HINTS:
            details.extend((f"code={code}", _DOCUMENT_ERROR_HINTS[code]))
        elif code is not None:
            details.append("code=未收录（已隐藏）")
        status = (getattr(current, "statusCode", None) or getattr(current, "status_code", None)
                  or data.get("statusCode"))
        if isinstance(status, (int, str)) and re.fullmatch(r"[45]\d{2}", str(status)):
            details.append(f"HTTP={status}")
        request_id = (data.get("requestid") or data.get("requestId") or data.get("RequestId")
                      or getattr(current, "request_id", None))
        if isinstance(request_id, str) and re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", request_id
        ):
            details.append(f"request_id={request_id}")
    if any(isinstance(item, requests.exceptions.SSLError) for item in errors):
        details.append("TLS证书或握手失败")
    elif any(isinstance(item, requests.exceptions.ProxyError) for item in errors):
        details.append("代理连接失败")
    elif any(isinstance(item, (requests.exceptions.Timeout, TimeoutError)) for item in errors):
        details.append("请求超时")
    elif any(isinstance(item, requests.exceptions.ConnectionError) for item in errors):
        details.append("网络连接失败")
    return "; ".join(dict.fromkeys(details))


def retry(func=None, *, retries=3, delay=1):
    def decorator(f):
        def wrapper(*args, **kwargs):
            for i in range(1, retries+1):
                try:
                    return f(*args, **kwargs)
                except Exception as exc:
                    detail = safe_exception_summary(exc)
                    if i < retries:
                        print(f"调用 {f.__name__} 失败（第 {i}/{retries} 次），将重试: {detail}",
                              file=sys.stderr, flush=True)
                        time.sleep(delay)
                    else:
                        print(f"调用 {f.__name__} 最终失败（共 {i} 次）: {detail}",
                              file=sys.stderr, flush=True)
                        raise
        return wrapper
    return decorator if func is None else decorator(func)

# ========== Access Token ==========
@retry
def get_access_token(app_key: str, app_secret: str) -> str:
    app_key = str(app_key or "").strip()
    app_secret = str(app_secret or "").strip()
    if not app_key or not app_secret:
        raise ValueError("DINGTALK_APP_KEY/DINGTALK_APP_SECRET are required")
    resp = _request_json(
        "GET",
        "https://oapi.dingtalk.com/gettoken",
        params={"appkey": app_key, "appsecret": app_secret},
    )
    if resp.get("errcode") != 0:
        _raise_api_error("get DingTalk access token", resp)
    return resp["access_token"]

# ========== 用户 UnionID ==========
@retry
def get_union_id(access_token: str, user_id: str, language: str = "zh_CN") -> str:
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    payload = {"userid": user_id, "language": language}
    response = _request_json(
        "POST", url, params={"access_token": access_token}, json=payload
    )
    if response.get("errcode") == 0 and "result" in response:
        union_id = response["result"].get("unionid")
        print(f"userId: {user_id} -> unionId: {union_id}")
        return union_id
    else:
        _raise_api_error("query DingTalk user", response)
# ========== 员工/上级查询 ==========
@retry
def get_user_detail(access_token: str, user_id: str, language: str = "zh_CN") -> dict:
    """
    获取员工详情
    :param access_token: access_token
    :param user_id: 员工 user_id
    :param language: 语言
    :return: 用户详情 result
    """
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    payload = {
        "userid": user_id,
        "language": language
    }
    response = _request_json(
        "POST", url, params={"access_token": access_token}, json=payload
    )
    if response.get("errcode") != 0:
        _raise_api_error("get DingTalk user detail", response)
    return response.get("result", {})


@retry
def get_department_user_list(access_token: str, dept_id: int, cursor: int = 0, size: int = 100,
                             language: str = "zh_CN", contain_access_limit: bool = False) -> dict:
    """
    分页获取部门用户列表
    :param access_token: access_token
    :param dept_id: 部门ID
    :param cursor: 游标
    :param size: 每页数量
    :param language: 语言
    :param contain_access_limit: 是否包含权限受限数据
    :return: result
    """
    url = "https://oapi.dingtalk.com/topapi/v2/user/list"
    payload = {
        "dept_id": dept_id,
        "cursor": cursor,
        "size": size,
        "language": language,
        "contain_access_limit": contain_access_limit
    }
    response = _request_json(
        "POST", url, params={"access_token": access_token}, json=payload
    )
    if response.get("errcode") != 0:
        _raise_api_error("list DingTalk department users", response)
    return response.get("result", {})


def find_userids_by_name_in_dept(access_token: str, dept_id: int, employee_name: str) -> list:
    """
    在指定部门中按姓名精确匹配员工，返回 user_id 列表
    :param access_token: access_token
    :param dept_id: 部门ID
    :param employee_name: 员工姓名
    :return: user_id 列表
    """
    matched_userids = []
    cursor = 0
    size = 100

    while True:
        result = get_department_user_list(
            access_token=access_token,
            dept_id=dept_id,
            cursor=cursor,
            size=size
        )

        user_list = result.get("list", [])
        for user in user_list:
            if user.get("name") == employee_name and user.get("userid"):
                matched_userids.append(user.get("userid"))

        has_more = result.get("has_more", False)
        next_cursor = result.get("next_cursor")

        if not has_more or next_cursor is None:
            break

        cursor = next_cursor

    return matched_userids


def get_manager_info_by_userid(access_token: str, user_id: str) -> dict:
    """
    通过员工 user_id 查询直属上级信息
    :param access_token: access_token
    :param user_id: 员工 user_id
    :return: 员工及直属上级信息
    """
    user_info = get_user_detail(access_token, user_id)
    manager_userid = user_info.get("manager_userid")

    result = {
        "employee_userid": user_id,
        "employee_name": user_info.get("name"),
        "manager_userid": manager_userid,
        "manager_name": None
    }

    if manager_userid:
        try:
            manager_info = get_user_detail(access_token, manager_userid)
            result["manager_name"] = manager_info.get("name")
        except Exception as e:
            result["manager_error"] = str(e)

    return result

def get_user_detail_by_name(access_token: str, dept_id: int, employee_name: str) -> list:
    """
    根据姓名查用户详细信息（包含 user_id、部门、手机号等）
    """
    userids = find_userids_by_name_in_dept(access_token, dept_id, employee_name)

    results = []
    for uid in userids:
        try:
            info = get_user_detail(access_token, uid)
            results.append({
                "userid": uid,
                "name": info.get("name"),
                "mobile": info.get("mobile"),
                "dept_id_list": info.get("dept_id_list")
            })
        except Exception as e:
            results.append({
                "userid": uid,
                "error": str(e)
            })

    return results

def get_employee_manager_by_name(access_token: str, dept_id: int, employee_name: str) -> dict:
    """
    通过 员工姓名 + 部门ID 查询直属上级
    :param access_token: access_token
    :param dept_id: 部门ID
    :param employee_name: 员工姓名
    :return: 查询结果
    """
    userids = find_userids_by_name_in_dept(access_token, dept_id, employee_name)

    if not userids:
        return {
            "success": False,
            "message": f"部门ID={dept_id} 下未找到姓名为【{employee_name}】的员工",
            "items": []
        }

    items = []
    for uid in userids:
        try:
            items.append(get_manager_info_by_userid(access_token, uid))
        except Exception as e:
            items.append({
                "employee_userid": uid,
                "error": str(e)
            })

    return {
        "success": True,
        "dept_id": dept_id,
        "employee_name": employee_name,
        "count": len(items),
        "items": items
    }


def get_employee_manager_by_userid(access_token: str, user_id: str) -> dict:
    """
    直接通过 user_id 查询直属上级
    :param access_token: access_token
    :param user_id: 员工 user_id
    :return: 查询结果
    """
    try:
        info = get_manager_info_by_userid(access_token, user_id)
        return {
            "success": True,
            "item": info
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }

def get_userid_by_name_global(access_token: str, employee_name: str):
    """
    全公司查 user_id（慢一点，但最全）
    """
    departments = get_all_departments(access_token)

    all_results = []

    for dept in departments:
        dept_id = dept.get("dept_id")
        try:
            userids = find_userids_by_name_in_dept(access_token, dept_id, employee_name)
            for uid in userids:
                all_results.append({
                    "userid": uid,
                    "dept_id": dept_id,
                    "dept_name": dept.get("name")
                })
        except Exception as e:
            print(f"部门 {dept_id} 查询失败: {e}")

    return all_results

@retry
def get_all_departments(access_token: str):
    resp = _request_json(
        "POST",
        "https://oapi.dingtalk.com/topapi/v2/department/listsub",
        params={"access_token": access_token},
        json={"dept_id": 1},
    )
    if resp.get("errcode") != 0:
        _raise_api_error("list DingTalk departments", resp)
    return resp.get("result", [])
# ========== Sheet 操作 ==========
@retry
def get_sheet_content(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict:
    headers = doc_models.GetSheetHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetSheetRequest(operator_id=operator_id)
    response = client.get_sheet_with_options(doc_id, sheet_name, request, headers, _runtime_options())
    js = response.to_map()['body']
    return {
        'sheetid': js['id'],
        'sheet_name': js['name'],
        'row_count': js['rowCount'],
        'column_count': js['columnCount'],
        'last_non_empty_row': js['lastNonEmptyRow']+1,
        'last_non_empty_column': js['lastNonEmptyColumn']+1
    }

@retry
def get_sheet_all_sheet(access_token: str, operator_id: str, doc_id: str) -> dict:
    headers = doc_models.GetAllSheetsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetAllSheetsRequest(operator_id=operator_id)
    response = client.get_all_sheets_with_options(doc_id, request, headers, _runtime_options())
    js = response.to_map()['body']['value']
    return js

@retry(retries=1)
def create_sheet(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict:
    # Creation is not idempotent: DingTalk may rename duplicate sheet names.
    # A lost response must not trigger another POST and create an extra sheet.
    headers = doc_models.CreateSheetHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.CreateSheetRequest(name=sheet_name, operator_id=operator_id)
    response = client.create_sheet_with_options(doc_id, request, headers, _runtime_options())
    return response.to_map()['body']

@retry
def delete_sheet(access_token: str, operator_id: str, doc_id: str, sheet_id: str) -> dict:
    headers = doc_models.DeleteSheetHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.DeleteSheetRequest(operator_id=operator_id)
    response = client.delete_sheet_with_options(doc_id, sheet_id, request, headers, _runtime_options())
    return response.to_map().get('body', {})

def get_sheet_meta_by_name(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict | None:
    sheet_name = str(sheet_name).strip()
    for sheet in get_sheet_all_sheet(access_token, operator_id, doc_id):
        if str(sheet.get('name', '')).strip() == sheet_name:
            return sheet
    return None

def ensure_sheet_exists(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict:
    sheet_meta = get_sheet_meta_by_name(access_token, operator_id, doc_id, sheet_name)
    if sheet_meta is not None:
        return sheet_meta

    create_sheet(access_token, operator_id, doc_id, sheet_name)
    time.sleep(0.5)
    sheet_meta = get_sheet_meta_by_name(access_token, operator_id, doc_id, sheet_name)
    if sheet_meta is None:
        raise Exception(f'创建 sheet 失败: {sheet_name}')
    return sheet_meta
@retry
def delete_rows(doc_id: str, sheet_id: str, operator_id: str, access_token: str, row_index: int, row_count: int):
    headers = doc_models.DeleteRowsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.DeleteRowsRequest(operator_id=operator_id, row=row_index, row_count=row_count)
    client.delete_rows_with_options(doc_id, sheet_id, request, headers, _runtime_options())
    print(f"成功删除 Sheet {sheet_id} 第 {row_index} 行起的 {row_count} 行")

@retry
def delete_columns(doc_id: str, sheet_id: str, operator_id: str, access_token: str, column_index: int, column_count: int):
    headers = doc_models.DeleteColumnsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.DeleteColumnsRequest(operator_id=operator_id, column=column_index, column_count=column_count)
    client.delete_columns_with_options(doc_id, sheet_id, request, headers, _runtime_options())
    print(f"成功删除 Sheet {sheet_id} 第 {column_index} 列起的 {column_count} 列")

@retry
def insert_rows(doc_id: str, sheet_id: str, operator_id: str, access_token: str, row_index: int, row_count: int):
    headers = doc_models.InsertRowsBeforeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.InsertRowsBeforeRequest(operator_id=operator_id, row=row_index, row_count=row_count)
    client.insert_rows_before_with_options(doc_id, sheet_id, request, headers, _runtime_options())
    print(f"成功在 Sheet {sheet_id} 第 {row_index} 行前插入 {row_count} 行")

@retry
def insert_columns(doc_id: str, sheet_id: str, operator_id: str, access_token: str, column_index: int, column_count: int):
    headers = doc_models.InsertColumnsBeforeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.InsertColumnsBeforeRequest(operator_id=operator_id, column=column_index, column_count=column_count)
    client.insert_columns_before_with_options(doc_id, sheet_id, request, headers, _runtime_options())
    print(f"成功在 Sheet {sheet_id} 第 {column_index} 列前插入 {column_count} 列")

# ========== Range 操作 ==========
def to_2d_array(x) -> List[list]:
    if isinstance(x, str):
        return [[x]]
    elif isinstance(x, (list, tuple)):
        if all(not isinstance(i, (list, tuple)) for i in x):
            return [list(x)]
        else:
            return [list(i) if not isinstance(i, list) else i for i in x]
    else:
        return [[x]]

def generate_excel_columns():
    columns = []

    # 单字母列 A-Z
    for c in string.ascii_uppercase:
        columns.append(c)

    # 双字母列 AA-ZZ
    for first in string.ascii_uppercase:
        for second in string.ascii_uppercase:
            columns.append(first + second)

    return columns

@retry
def update_range(doc_id: str, sheet_id: str, operator_id: str, access_token: str,
                 cell_range: str, values: list, background_colors: list = None,
                 hyperlinks: list = None, number_format: str = "@"):
    headers = doc_models.UpdateRangeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.UpdateRangeRequest(
        operator_id=operator_id,
        values=values,
        background_colors=background_colors,
        hyperlinks=hyperlinks,
        number_format=number_format
    )
    client.update_range_with_options(doc_id, sheet_id, cell_range, request, headers, _runtime_options())
    print(f"成功更新 Sheet {sheet_id} 范围 {cell_range}")

@retry
def read_range(doc_id: str, sheet_id: str, operator_id: str, access_token: str,
               cell_range: str, select: str = 'values') -> list:
    headers = doc_models.GetRangeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetRangeRequest(operator_id=operator_id, select=select)
    result = client.get_range_with_options(doc_id, sheet_id, cell_range, request, headers, _runtime_options())
    return getattr(result.body, select, [])

def safe_delete_rows(access_token, operator_id, doc_id, sheet_name, row_index, row_count):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    delete_rows(doc_id, sheet_id, operator_id, access_token, row_index, row_count)

def safe_delete_columns(access_token, operator_id, doc_id, sheet_name, column_index, column_count):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    delete_columns(doc_id, sheet_id, operator_id, access_token, column_index, column_count)

def safe_insert_rows(access_token, operator_id, doc_id, sheet_name, row_index, row_count):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    insert_rows(doc_id, sheet_id, operator_id, access_token, row_index, row_count)

def safe_insert_columns(access_token, operator_id, doc_id, sheet_name, column_index, column_count):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    insert_columns(doc_id, sheet_id, operator_id, access_token, column_index, column_count)

def safe_update_range(access_token, operator_id, doc_id, sheet_name, cell_range, values, background_colors=None, hyperlinks=None,number_format='@'):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    update_range(doc_id, sheet_id, operator_id, access_token, cell_range, to_2d_array(values),
                 background_colors, hyperlinks,number_format=number_format)

def safe_read_range(access_token, operator_id, doc_id, sheet_name, cell_range, select='values'):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    return read_range(doc_id, sheet_id, operator_id, access_token, cell_range, select)

def get_sheet_row_count(access_token, operator_id, doc_id, sheet_name):
    last_non_empty_row = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['last_non_empty_row']
    return last_non_empty_row

def get_sheet_colum_count(access_token, operator_id, doc_id, sheet_name):
    last_non_empty_column = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['last_non_empty_column']
    return last_non_empty_column,generate_excel_columns()[last_non_empty_column-1]

def excel_date_to_datetime(excel_val):
    if isinstance(excel_val, int):
        return (datetime(1900, 1, 1) + timedelta(days=excel_val - 2)).strftime('%Y-%m-%d')
    return excel_val

def get_access_token_from_config(dingtalk_config: dict | None = None) -> str:
    dingtalk_config = dingtalk_config or {}
    app_key = dingtalk_config.get('app_key') or APP_KEY
    app_secret = dingtalk_config.get('app_secret') or APP_SECRET
    return get_access_token(app_key, app_secret)

def resolve_operator_id(access_token: str, dingtalk_config: dict | None = None) -> str:
    dingtalk_config = dingtalk_config or {}
    operator_id = (
        dingtalk_config.get('operator_id')
        or dingtalk_config.get('union_id')
        or dingtalk_config.get('unionId')
    )
    if operator_id:
        return str(operator_id).strip()
    user_id = str(dingtalk_config.get('user_id') or USER_ID).strip()
    return get_union_id(access_token, user_id)

def _sheet_id(sheet: dict) -> str:
    return str(sheet.get('id') or sheet.get('sheetId') or sheet.get('sheet_id') or '').strip()

def _normalize_workbook_cell(value: Any) -> Any:
    if value is None:
        return ''
    if isinstance(value, float) and math.isnan(value):
        return ''
    if isinstance(value, datetime):
        if value.hour == value.minute == value.second == value.microsecond == 0:
            return value.strftime('%Y-%m-%d')
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    return value

def dataframe_to_sheet_values(df) -> list[list[Any]]:
    import pandas as pd

    df = df.astype(object).where(pd.notna(df), '')
    values = [[str(column) for column in df.columns.tolist()]]
    for row in df.itertuples(index=False, name=None):
        values.append([_normalize_workbook_cell(item) for item in row])
    return values

def ensure_sheet_capacity(access_token: str, operator_id: str, doc_id: str, sheet_name: str, row_count: int, column_count: int) -> None:
    sheet_content = get_sheet_content(access_token, operator_id, doc_id, sheet_name)
    physical_rows = int(sheet_content.get('row_count') or 1)
    physical_columns = int(sheet_content.get('column_count') or 1)
    # DingTalk insert anchors must point at an existing zero-based row/column index.
    if row_count > physical_rows:
        insert_rows(doc_id, sheet_content['sheetid'], operator_id, access_token, max(0, physical_rows - 1), row_count - physical_rows)
    if column_count > physical_columns:
        insert_columns(doc_id, sheet_content['sheetid'], operator_id, access_token, max(0, physical_columns - 1), column_count - physical_columns)

def write_sheet_values_paginated(
    access_token: str,
    operator_id: str,
    doc_id: str,
    sheet_name: str,
    values: list[list[Any]],
    page_size: int = 500,
    number_format: str = 'General',
) -> None:
    if not values:
        values = [['']]
    width = max(1, max(len(row) for row in values))
    normalized_values = []
    for row in values:
        current = [_normalize_workbook_cell(item) for item in row]
        if len(current) < width:
            current.extend([''] * (width - len(current)))
        normalized_values.append(current)

    ensure_sheet_capacity(access_token, operator_id, doc_id, sheet_name, len(normalized_values), width)
    end_col = get_column_letter(width)
    page_size = max(1, int(page_size or 500))
    for start in range(0, len(normalized_values), page_size):
        chunk = normalized_values[start:start + page_size]
        start_row = start + 1
        end_row = start_row + len(chunk) - 1
        safe_update_range(
            access_token,
            operator_id,
            doc_id,
            sheet_name,
            f'A{start_row}:{end_col}{end_row}',
            chunk,
            number_format=number_format,
        )

def replace_workbook_sheets_from_excel(
    access_token: str,
    operator_id: str,
    doc_id: str,
    excel_file: str,
    page_size: int = 500,
    number_format: str = 'General',
) -> None:
    import pandas as pd

    excel_path = Path(excel_file)
    with pd.ExcelFile(excel_path) as workbook:
        sheet_names = [str(name).strip() for name in workbook.sheet_names if str(name).strip()]
        if not sheet_names:
            raise ValueError(f'Excel文件没有可同步的sheet: {excel_path}')

        temp_sheet_name = f'_sync_tmp_{datetime.now().strftime("%Y%m%d%H%M%S")}'
        while get_sheet_meta_by_name(access_token, operator_id, doc_id, temp_sheet_name) is not None:
            temp_sheet_name = f'_sync_tmp_{datetime.now().strftime("%Y%m%d%H%M%S%f")}'[:31]
        create_sheet(access_token, operator_id, doc_id, temp_sheet_name)
        time.sleep(0.5)

        try:
            for sheet in list(get_sheet_all_sheet(access_token, operator_id, doc_id)):
                sheet_name = str(sheet.get('name', '')).strip()
                sheet_id = _sheet_id(sheet)
                if sheet_name == temp_sheet_name or not sheet_id:
                    continue
                delete_sheet(access_token, operator_id, doc_id, sheet_id)
                time.sleep(0.2)

            for sheet_name in sheet_names:
                create_sheet(access_token, operator_id, doc_id, sheet_name)
                time.sleep(0.5)
                df = pd.read_excel(workbook, sheet_name=sheet_name, dtype=object)
                write_sheet_values_paginated(
                    access_token,
                    operator_id,
                    doc_id,
                    sheet_name,
                    dataframe_to_sheet_values(df),
                    page_size=page_size,
                    number_format=number_format,
                )
        finally:
            sheets = get_sheet_all_sheet(access_token, operator_id, doc_id)
            if len(sheets) > 1:
                temp_meta = get_sheet_meta_by_name(access_token, operator_id, doc_id, temp_sheet_name)
                temp_id = _sheet_id(temp_meta or {})
                if temp_id:
                    delete_sheet(access_token, operator_id, doc_id, temp_id)

def write_dingtalk_doc_paginated(token, union_id, doc_id, sheet_name, df, page_size=1000, start_row=2):
    """
    分页将 DataFrame 写入钉钉在线表格，每页最多 page_size 行。

    :param token: dingtalk_doc_token
    :param union_id: 用户 union_id
    :param doc_id: 钉钉文档 doc_id
    :param sheet_name: sheet 页名称
    :param df: pandas DataFrame
    :param page_size: 每页写入行数
    :param start_row: 起始行（默认从 A2 开始）
    """
    # 填充空值并转字符串
    df = df.fillna("").astype(str)
    total_rows = len(df)
    n_cols = df.shape[1]
    end_col = get_column_letter(n_cols)
    # 分页次数
    total_pages = math.ceil(total_rows / page_size)
    for page in range(total_pages):
        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total_rows)
        page_data = df.iloc[start_idx:end_idx].values.tolist()

        # 插入行（分页情况下每次插入行数为当前页的行数）
        safe_insert_rows(token, union_id, doc_id, sheet_name, start_row, len(page_data))
        # 写入范围
        write_range = f"A{start_row}:{end_col}{start_row + len(page_data) - 1}"
        safe_update_range(token, union_id, doc_id, sheet_name, write_range, page_data)
        # 下一页的起始行
        start_row += len(page_data)

def write_dingtalk_doc_paginated2(token, union_id, doc_id, sheet_name, df, page_size=1000, start_row=2,tp ='General'):
    """
    分页将 DataFrame 写入钉钉在线表格，每页最多 page_size 行。

    :param token: dingtalk_doc_token
    :param union_id: 用户 union_id
    :param doc_id: 钉钉文档 doc_id
    :param sheet_name: sheet 页名称
    :param df: pandas DataFrame
    :param page_size: 每页写入行数
    :param start_row: 起始行（默认从 A2 开始）
    """
    # 填充空值并转字符串
    df = df.fillna("").astype(str)
    total_rows = len(df)
    n_cols = df.shape[1]
    end_col = get_column_letter(n_cols)
    # 分页次数
    total_pages = math.ceil(total_rows / page_size)
    for page in range(total_pages):
        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total_rows)
        page_data = df.iloc[start_idx:end_idx].values.tolist()

        # # 插入行（分页情况下每次插入行数为当前页的行数）
        # safe_insert_rows(token, union_id, doc_id, sheet_name, start_row, len(page_data))
        # 写入范围
        write_range = f"A{start_row}:{end_col}{start_row + len(page_data) - 1}"
        safe_update_range(token, union_id, doc_id, sheet_name, write_range, page_data,number_format=tp)
        # 下一页的起始行
        start_row += len(page_data)

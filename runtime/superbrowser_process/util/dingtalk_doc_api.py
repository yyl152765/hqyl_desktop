# -*- coding: utf-8 -*-
"""
DingTalk Sheet Utilities
Author: 玉林 杨
功能: 钉钉文档/表格常用操作封装，包括创建/删除 sheet，行列操作，读取/更新范围
"""

import sys
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')  # 全局设置 UTF-8 输出

import requests
import time
import string
from typing import  List
from alibabacloud_dingtalk.doc_1_0.client import Client
from alibabacloud_dingtalk.doc_1_0 import models as doc_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from openpyxl.utils import get_column_letter
import math

# ========== 配置 ==========
APP_KEY = ""
APP_SECRET = ""
USER_ID = ""

# ========== 全局客户端 ==========
def create_client() -> Client:
    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'

    return Client(config)

client = create_client()

DINGTALK_HTTP_TIMEOUT = (5, 20)
DINGTALK_HTTP_HEADERS = {
    "Connection": "close",
    "User-Agent": "superbrowser-process/1.0",
}


def _dingtalk_request_json(method: str, url: str, **kwargs) -> dict:
    """Call DingTalk oapi directly first, then retry with environment proxy."""
    timeout = kwargs.pop("timeout", DINGTALK_HTTP_TIMEOUT)
    headers = {**DINGTALK_HTTP_HEADERS, **kwargs.pop("headers", {})}
    last_error_type = None

    for use_env_proxy in (False, True):
        session = requests.Session()
        session.trust_env = use_env_proxy
        try:
            response = session.request(
                method,
                url,
                timeout=timeout,
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_error_type = type(e).__name__
        finally:
            session.close()

    raise RuntimeError(f"DingTalk oapi request failed: {last_error_type}")

# ========== 重试装饰器 ==========
def retry(func=None, *, retries=3, delay=1):
    def decorator(f):
        def wrapper(*args, **kwargs):
            for i in range(1, retries+1):
                try:
                    return f(*args, **kwargs)
                except Exception as e:
                    print(f"调用 {f.__name__} 失败，重试 {i}/{retries}: {repr(e)}")
                    if i < retries:
                        time.sleep(delay)
                    else:
                        print(f"调用 {f.__name__} 最终失败: {repr(e)}")
                        raise
        return wrapper
    return decorator if func is None else decorator(func)

# ========== Access Token ==========
@retry
def get_access_token(app_key: str, app_secret: str) -> str:
    url = "https://oapi.dingtalk.com/gettoken"
    resp = _dingtalk_request_json(
        "GET",
        url,
        params={"appkey": app_key, "appsecret": app_secret},
    )
    if resp.get("errcode") != 0:
        raise Exception(f"获取 access_token 失败: {resp}")
    return resp["access_token"]

# ========== 用户 UnionID ==========
@retry
def get_union_id(access_token: str, user_id: str, language: str = "zh_CN") -> str:
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    payload = {"userid": user_id, "language": language}
    response = _dingtalk_request_json(
        "POST",
        url,
        params={"access_token": access_token},
        json=payload,
    )
    if response.get("errcode") == 0 and "result" in response:
        union_id = response["result"].get("unionid")
        return union_id
    else:
        raise Exception(f"查询失败: {response.get('errmsg')}")

# ========== Sheet 操作 ==========
@retry
def get_sheet_content(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict:
    headers = doc_models.GetSheetHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetSheetRequest(operator_id=operator_id)
    response = client.get_sheet_with_options(doc_id, sheet_name, request, headers, util_models.RuntimeOptions())
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
def get_all_sheets(access_token: str, operator_id: str, doc_id: str) -> list[dict]:
    headers = doc_models.GetAllSheetsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetAllSheetsRequest(operator_id=operator_id)
    response = client.get_all_sheets_with_options(doc_id, request, headers, util_models.RuntimeOptions())
    sheets = []
    for item in getattr(response.body, "value", None) or []:
        sheets.append({
            "sheetid": str(getattr(item, "id", "") or ""),
            "sheet_name": str(getattr(item, "name", "") or ""),
        })
    return sheets

@retry
def create_sheet(access_token: str, operator_id: str, doc_id: str, sheet_name: str) -> dict:
    headers = doc_models.CreateSheetHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.CreateSheetRequest(name=sheet_name, operator_id=operator_id)
    response = client.create_sheet_with_options(doc_id, request, headers, util_models.RuntimeOptions())
    return {
        "sheetid": str(getattr(response.body, "id", "") or ""),
        "sheet_name": str(getattr(response.body, "name", "") or sheet_name),
    }

@retry
def delete_rows(doc_id: str, sheet_id: str, operator_id: str, access_token: str, row_index: int, row_count: int):
    headers = doc_models.DeleteRowsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.DeleteRowsRequest(operator_id=operator_id, row=row_index, row_count=row_count)
    client.delete_rows_with_options(doc_id, sheet_id, request, headers, util_models.RuntimeOptions())
    print(f"成功删除 Sheet {sheet_id} 第 {row_index} 行起的 {row_count} 行")

@retry
def delete_columns(doc_id: str, sheet_id: str, operator_id: str, access_token: str, column_index: int, column_count: int):
    headers = doc_models.DeleteColumnsHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.DeleteColumnsRequest(operator_id=operator_id, column=column_index, column_count=column_count)
    client.delete_columns_with_options(doc_id, sheet_id, request, headers, util_models.RuntimeOptions())
    print(f"成功删除 Sheet {sheet_id} 第 {column_index} 列起的 {column_count} 列")

@retry
def insert_rows(doc_id: str, sheet_id: str, operator_id: str, access_token: str, row_index: int, row_count: int):
    headers = doc_models.InsertRowsBeforeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.InsertRowsBeforeRequest(operator_id=operator_id, row=row_index, row_count=row_count)
    client.insert_rows_before_with_options(doc_id, sheet_id, request, headers, util_models.RuntimeOptions())
    print(f"成功在 Sheet {sheet_id} 第 {row_index} 行前插入 {row_count} 行")

@retry
def insert_columns(doc_id: str, sheet_id: str, operator_id: str, access_token: str, column_index: int, column_count: int):
    headers = doc_models.InsertColumnsBeforeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.InsertColumnsBeforeRequest(operator_id=operator_id, column=column_index, column_count=column_count)
    client.insert_columns_before_with_options(doc_id, sheet_id, request, headers, util_models.RuntimeOptions())
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
    client.update_range_with_options(doc_id, sheet_id, cell_range, request, headers, util_models.RuntimeOptions())
    print(f"成功更新 Sheet {sheet_id} 范围 {cell_range}")

@retry
def read_range(doc_id: str, sheet_id: str, operator_id: str, access_token: str,
               cell_range: str, select: str = 'values') -> list:
    headers = doc_models.GetRangeHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = doc_models.GetRangeRequest(operator_id=operator_id, select=select)
    result = client.get_range_with_options(doc_id, sheet_id, cell_range, request, headers, util_models.RuntimeOptions())
    return getattr(result.body, select, [])

# ========== 封装安全调用 ==========

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

def safe_update_range(access_token, operator_id, doc_id, sheet_name, cell_range, values, background_colors=None, hyperlinks=None,number_format: str = "@"):
    sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
    update_range(doc_id, sheet_id, operator_id, access_token, cell_range, to_2d_array(values),
                 background_colors, hyperlinks,number_format)

def safe_update_range_by_sheet_id(access_token, operator_id, doc_id, sheet_id, cell_range, values, background_colors=None, hyperlinks=None, number_format: str = "@"):
    update_range(doc_id, sheet_id, operator_id, access_token, cell_range, to_2d_array(values),
                 background_colors, hyperlinks, number_format)

def safe_read_range(access_token, operator_id, doc_id, sheet_name, cell_range, select='values'):
    last_error = None
    for attempt in range(1, 6):
        try:
            sheet_id = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['sheetid']
            return read_range(doc_id, sheet_id, operator_id, access_token, cell_range, select)
        except Exception as e:
            last_error = e
            if attempt >= 5:
                break
            wait_seconds = min(5 * attempt, 20)
            print(f"safe_read_range 读取失败，等待 {wait_seconds}s 后重试 {attempt}/5: {repr(e)}")
            time.sleep(wait_seconds)
    raise last_error

def get_sheet_row_count(access_token, operator_id, doc_id, sheet_name):
    last_non_empty_row = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['last_non_empty_row']
    return last_non_empty_row

def get_sheet_colum_count(access_token, operator_id, doc_id, sheet_name):
    last_non_empty_column = get_sheet_content(access_token, operator_id, doc_id,sheet_name)['last_non_empty_column']
    return last_non_empty_column,generate_excel_columns()[last_non_empty_column-1]



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

if __name__ == "__main__":
    DOC_ID = "o14dA3GK8g55RABYU9xK9PbqV9ekBD76"
    SHEET_NAME = "lazada打电话"
    token = get_access_token(APP_KEY, APP_SECRET)
    union_id = get_union_id(token, USER_ID)
    print("Union ID:", union_id)
    # 示例操作：
    # safe_insert_rows(token, union_id, DOC_ID, SHEET_NAME, 1, 2)
    # safe_delete_rows(token, union_id, DOC_ID, SHEET_NAME, 1, 2)
    # safe_insert_columns(token, union_id, DOC_ID, SHEET_NAME, 1, 1)
    # safe_delete_columns(token, union_id, DOC_ID, SHEET_NAME, 1, 1)
    # safe_update_range(token, union_id, DOC_ID, SHEET_NAME, "A1:A1", [["测试"]])
    data = safe_read_range(token, union_id, DOC_ID, SHEET_NAME, "A1:F30")
    # print(data)
    # last_non_empty_row = get_sheet_row_count(token, union_id, DOC_ID, SHEET_NAME)
    # print(last_non_empty_row)
    # last_non_empty_column,last_non_empty_column_name = get_sheet_colum_count(token, union_id, DOC_ID, SHEET_NAME)
    # print(last_non_empty_column,last_non_empty_column_name)

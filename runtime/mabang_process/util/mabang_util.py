from time import sleep
from selenium.common.exceptions import TimeoutException
import yaml
import pandas as pd
from . import yingdao_util as yd
from . import dirver_web_util as dv
from . import mabang_html_parse as mb
from .dirver_web_util import load_onclick
from .log import setup_logger
import time
from datetime import datetime
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
import os
import re
import requests
from bs4 import BeautifulSoup
import json
import html as html_lib

SALES_REPORT_EXPORT_TYPE_BY_PLATFORM_OR_SHOP = {
    "country": "countryReports",
    "warehouse": "warehouseReports",
    "stockShop": "stockShopReports",
}


def _safe_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _copy_form_payload(form_data):
    if hasattr(form_data, "items"):
        return dict(form_data)
    return [(key, value) for key, value in list(form_data or [])]


def _get_form_payload_first(form_data, key):
    if hasattr(form_data, "items"):
        return form_data.get(key)
    for item_key, item_value in list(form_data or []):
        if item_key == key:
            return item_value
    return ""


def _set_form_payload_value(form_data, key, value):
    if hasattr(form_data, "items"):
        form_data[key] = value
        return form_data
    return [(item_key, item_value) for item_key, item_value in list(form_data or []) if item_key != key] + [(key, value)]


def _positive_int(value, default):
    try:
        parsed = int(str(value).replace(",", "").strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _sales_report_type_from_request(params, url, form_data, default_report_type):
    mod_value = _safe_text((params or {}).get("mod"))
    if not mod_value:
        match = re.search(r"[?&]mod=([^&]+)", _safe_text(url))
        mod_value = match.group(1) if match else ""
    if mod_value.startswith("reports."):
        report_type = mod_value.split(".", 1)[1]
        if report_type in {"countryReports", "warehouseReports", "stockShopReports"}:
            return report_type

    platform_or_shop = _safe_text(_get_form_payload_first(form_data, "platformOrShop"))
    return SALES_REPORT_EXPORT_TYPE_BY_PLATFORM_OR_SHOP.get(platform_or_shop, default_report_type)


def _sales_report_platform_or_shop(form_data, report_type, default_platform_or_shop):
    platform_or_shop = _safe_text(_get_form_payload_first(form_data, "platformOrShop"))
    if platform_or_shop:
        return platform_or_shop
    for key, value in SALES_REPORT_EXPORT_TYPE_BY_PLATFORM_OR_SHOP.items():
        if value == report_type:
            return key
    return default_platform_or_shop


def _parse_total_items(page_html):
    match = re.search(r'共\s*<span[^>]*>([\d,]+)</span>\s*条', page_html or "")
    if not match:
        text = BeautifulSoup(page_html or "", "html.parser").get_text(" ", strip=True)
        match = re.search(r"共\s*([\d,]+)\s*条", text)
    return _positive_int(match.group(1), 0) if match else 0


def _parse_total_pages(page_html):
    match = re.search(r'<span class="semibold">[\d,]+/([\d,]+)</span>页', page_html or "")
    if not match:
        text = BeautifulSoup(page_html or "", "html.parser").get_text(" ", strip=True)
        match = re.search(r"[\d,]+\s*/\s*([\d,]+)\s*页", text)
    return _positive_int(match.group(1), 0) if match else 0


def _strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "")


def _parse_sales_report_rows_fast(table_html):
    """
    Fast parser for Mabang sales report rows.

    The report table can be several MB per page. BeautifulSoup's full DOM parser
    is too slow for large Thailand warehouse pages, while the fields we need have
    stable HTML markers.
    """
    records = []
    row_pattern = re.compile(r'<tr\s+class="view-select">(.*?)</tr>', re.S)
    sku_pattern = re.compile(r'<a[^>]*class="[^"]*viewicon[^"]*"[^>]*>(.*?)</a>', re.S)
    title_pattern = re.compile(r'<p[^>]*class="[^"]*ellipsis[^"]*"[^>]*title="([^"]*)"', re.S)
    field_patterns = {
        "defaultCost": re.compile(r'data-field="defaultCost"[^>]*>(.*?)</td>', re.S),
        "weight": re.compile(r'data-field="weight"[^>]*>(.*?)</td>', re.S),
        "timeCreated": re.compile(r'data-field="timeCreated"[^>]*>(.*?)</td>', re.S),
        "quantity": re.compile(r'data-field="quantity"[^>]*>(.*?)</td>', re.S),
    }

    for row_html in row_pattern.findall(table_html or ""):
        sku_match = sku_pattern.search(row_html)
        if not sku_match:
            continue
        record = {
            "stock_code": html_lib.unescape(_strip_tags(sku_match.group(1))).strip(),
            "product_title": "",
            "defaultCost": "",
            "weight": "",
            "timeCreated": "",
            "quantity": "0",
        }
        title_match = title_pattern.search(row_html)
        if title_match:
            record["product_title"] = html_lib.unescape(title_match.group(1)).strip()
        for field, pattern in field_patterns.items():
            match = pattern.search(row_html)
            if match:
                record[field] = html_lib.unescape(_strip_tags(match.group(1))).strip()
        records.append(record)
    return records


def _request_reports_total(header, url, params, form_data):
    total_items = 0
    total_pages = 0
    request_form = _copy_form_payload(form_data)
    original_rows_per_page = _positive_int(_get_form_payload_first(form_data, "rowsPerPage"), 2000)
    request_rows_per_page = min(original_rows_per_page, 20)
    request_form = _set_form_payload_value(request_form, "page", "1")
    request_form = _set_form_payload_value(request_form, "rowsPerPage", str(request_rows_per_page))
    response = requests.post(url, params=params, headers=header, data=request_form)
    try:
        resp = response.json()
        if resp['success']:
            page_html = resp['pageHtml']
            total_items = _parse_total_items(page_html)
            if total_items:
                total_pages = (total_items + original_rows_per_page - 1) // original_rows_per_page
            else:
                total_pages = _parse_total_pages(page_html)
            print(f"共有 {total_items} 条记录，一共 {total_pages} 页数据。")

    except Exception as e:
        print(f"请求失败或解析异常：{e}")
    return total_items, total_pages


def get_visible_sync_dialog_data_and_close(driver):
    # 找到所有弹窗 wrapper
    wrappers = driver.find_elements(By.CSS_SELECTOR, "div.el-dialog__wrapper")

    # 筛选可见弹窗
    visible_wrappers = [w for w in wrappers if w.value_of_css_property("display") != "none"]
    if not visible_wrappers:
        return []  # 没有可见弹窗

    dialog = visible_wrappers[0].find_element(By.CSS_SELECTOR, "div.el-dialog.sync-result-dialog")

    # 抓取表格数据
    table_rows = dialog.find_elements(By.CSS_SELECTOR, "div.el-table__body tbody tr")
    data = []
    for row in table_rows:
        cells = row.find_elements(By.CSS_SELECTOR, "td div.cell")
        data.append([cell.text for cell in cells])

    # 点击取消按钮关闭弹窗
    cancel_button = dialog.find_element(By.XPATH, './/button[span[text()="取消"]]')
    cancel_button.click()

    return data
def ansy_goods(driver,log):
    url = 'https://900853.private.mabangerp.com/index.php?mod=stock.stockSyncNew'
    driver.get(url)
    # 切换到iframe里面
    WebDriverWait(driver, 20).until(
        EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
    )
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, 'div.el-loading-mask')
    # 选择页数
    dv.click_element_no_head(driver,'//input[@placeholder="请选择" and contains(@class,"el-input__inner")]',By.XPATH)
    # dv.click_element_no_head(driver,"//li[contains(@class,'el-select-dropdown__item') and contains(text(),'100条/页')]",By.XPATH)
    # 找到对应 li
    option_li = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (By.XPATH, "//li[contains(@class,'el-select-dropdown__item') and contains(.,'100条/页')]")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", option_li)
    # 等待可点击再点击
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//li[contains(@class,'el-select-dropdown__item') and contains(.,'100条/页')]"))
    ).click()
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, 'div.el-loading-mask',timeout=60)
    click_next = True
    index = 1
    result_data = []
    while click_next:
        log.info(f'正在处理第{index}页')
        checkbox = driver.find_elements(By.CSS_SELECTOR,'span.el-checkbox__input')[0]
        if "is-checked" not in checkbox.get_attribute("class"):
            # 滚动到元素
            driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
            time.sleep(0.3)  # 等待滚动完成
            # 用 JS 点击
            driver.execute_script("arguments[0].click();", checkbox)

        dv.click_element_no_head(driver,'//button[span[text()="手动同步"]]',By.XPATH)
        log.info(f'第{index}页同步完成')
        time.sleep(5)
        resp_data = get_visible_sync_dialog_data_and_close(driver)
        for data in resp_data:
            result_data =  result_data + data
            log.info(f'处理结果{data}')
        click_next = dv.get_button_disabled(driver, "button.btn-next")
        if click_next:
            dv.click_element_no_head(driver, "button.btn-next")
            dv.wait_for_invisible(driver, 'div.el-loading-mask',timeout=60)
            index = index + 1
    return result_data
def request_reports_getIncomePayData(header,url,params,form_data):
    """
    接口查询库存SKU
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 库存信息
    """
    response = requests.post(url, headers=header, data=form_data,params=params, timeout=60)
    stock_list = []
    try:
        data = response.json()
        tablehtml = data['tableContent']
        stock_list = mb.parse_reports_getIncomePayData(tablehtml)
        print(f"第 1 页解析成功，数据详情：{data['updateDateStr']}")
    except Exception as e:
        print("JSON解析失败:", e)
    return stock_list


def request_reports_getIncomePayData_table(header, url, params, form_data, total_pages, output_file, rowsPerPage=2000):
    """
    Export income/pay report pages to Excel.

    total_pages is treated as an upper bound. The report usually has fewer pages
    than callers know upfront, so stop when the current page is empty or partial.
    """
    all_data = []
    max_pages = _positive_int(total_pages, 1)
    rows_per_page = _positive_int(rowsPerPage, 2000)

    for page in range(1, max_pages + 1):
        request_form = _copy_form_payload(form_data)
        request_form = _set_form_payload_value(request_form, "page", str(page))
        request_form = _set_form_payload_value(request_form, "rowsPerPage", str(rows_per_page))

        response = requests.post(
            url,
            headers=header,
            data=request_form,
            params=params,
            timeout=60,
        )
        response.raise_for_status()

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"getIncomePayData page {page} returned non-JSON response") from exc

        if not data.get("success"):
            message = data.get("message") or data.get("msg") or "unknown error"
            raise RuntimeError(f"getIncomePayData page {page} failed: {message}")

        records = mb.parse_reports_getIncomePayData(data.get("tableContent", ""))
        if not records:
            print(f"getIncomePayData page {page} returned no records, stop paging")
            break

        all_data.extend(records)
        print(f"getIncomePayData page {page} exported {len(records)} records")

        if len(records) < rows_per_page:
            break

    if not all_data:
        raise RuntimeError("getIncomePayData returned no records")

    pd.DataFrame(all_data).to_excel(output_file, index=False)
    return output_file



def request_stock_getStockList(header,url,params,form_data):
    """
    接口查询库存SKU
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 库存信息
    """
    response = requests.post(url, headers=header, data=form_data,params=params)
    stock_list = []
    try:
        # json_data = response.json()
        text = response.content.decode('utf-8-sig')
        json_data = json.loads(text)
        stock_list = json_data['stockData']
    except Exception as e:
        print("JSON解析失败:", e)
    return stock_list



def request_stock_showStockShippingDetail(header,url,params,form_data):
    """
    接口查询库存SKU
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 库存信息
    """
    response = requests.post(url, headers=header, data=form_data,params=params)
    stock_list = []
    try:
        json_data = response.json()
        allocationhtml = json_data['allocationhtml']
        stock_list = mb.parse_shipping_row(allocationhtml)
        return stock_list
    except Exception as e:
        print("JSON解析失败:", e)
    return stock_list


def request_purchases_getPurchaseList(url,headers,form_data,params,page =1):
    response = requests.post(url, headers=headers, data=form_data,params=params)
    order_data = []
    total_pages = 1
    try:
        json_data = response.json()
        purchaseDetailData = json_data['purchaseDetailData']
        requestDetailNumData = json_data['requestDetailNumData']
        purchaseData = json_data['purchaseData']
        for order_id,order_value in requestDetailNumData.items():
            order = {"purchase_no": order_id, "items": [],"warehouse": ""}
            for sku_id,num in order_value.items():
                is_break = False
                for plan,plan_value in purchaseDetailData.items():
                    for plans in plan_value:
                        if plans['stockId'] ==sku_id and plans['purchaseId'] in order_id:
                            p_js = {"sku": plans['stockSku'], "quantity": plans['purchaseNum']}
                            order['items'].append(p_js)
                            is_break =True
                            if is_break:
                                break
                    if is_break:
                        break
            for purchase in  purchaseData:
                if purchase['groupId'] == order['purchase_no']:
                    order['warehouse'] =purchase['targetWarehouseName']
            order_data.append(order)
        if page == 1:
            pageHtml = json_data['pageHtml']
            soup = BeautifulSoup(pageHtml, 'html.parser')
            static_text = soup.find('div', class_='static-text')
            if static_text:
                text_content = static_text.get_text(strip=True)
                # 匹配 "1/203页" 这样的格式
                match = re.search(r'(\d+)/(\d+)页', text_content)
                if match:
                    total_pages = int(match.group(2))

    except Exception as e:
        print("JSON解析失败:", e)
    return order_data,total_pages



def request_purchasenew_myapplylistsearc(header,url,params,form_data):
    """
    采购计划查询
    :param header:
    :param url:
    :param params:
    :param form_data:
    :return:
    """
    response = requests.post(url, headers=header, data=form_data,params=params)
    df = pd.DataFrame
    try:
        json_data = response.json()
        html_str = json_data['html']
        df = mb.parse_purchase_table(html_str)
    except Exception as e:
        print("JSON解析失败:", e)
    return df

def request_stock_getOrderItemRecordfh(header,url,params,form_data):
    """
    接口查询出库信息
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: table
    """
    response = requests.post(url, headers=header, data=form_data,params=params)
    data = []
    if response.status_code == 200:
        try:
            # result = response.json()
            text = response.content.decode('utf-8-sig')
            json_data = json.loads(text)
            data = mb.parse_table_stock_getOrderItemRecordfh(json_data['message'])
        except Exception as e:
            print("JSON解析失败:", e)
    else:
        print(f"请求失败，状态码：{response.status_code}")
    return data


def request_stock_getStorageInOutLog(header,url,params,form_data):
    """
    接口查询出库信息
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: table
    """
    response = requests.post(url, headers=header, data=form_data,params=params)
    data = []
    if response.status_code == 200:
        try:
            # result = response.json()
            text = response.content.decode('utf-8-sig')
            json_data = json.loads(text)
            data = mb.parse_table_stock_getStorageInOutLog(json_data['getStorageInOutLogMessage'])

        except Exception as e:
            print("JSON解析失败:", e)
    else:
        print(f"请求失败，状态码：{response.status_code}")
    return data


def request_reports_table_by_product_export(header, url, params, form_data, output_file, report_type, platform_or_shop):
    from util.mabang_client import MabangClient

    base_url = url.split("/index.php", 1)[0]
    cookie = header.get("Cookie") or header.get("cookie") or ""
    resolved_report_type = _sales_report_type_from_request(params, url, form_data, report_type)
    resolved_platform_or_shop = _sales_report_platform_or_shop(form_data, resolved_report_type, platform_or_shop)
    with MabangClient(base_url, timeout_seconds=300, verify_ssl=False) as client:
        if cookie:
            client.client.cookies.update({
                item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
                for item in cookie.split(";")
                if "=" in item
            })
        return str(client.export_sales_report_by_product_all(
            url=url,
            params=params,
            form_data=form_data,
            output_path=output_file,
            report_type=resolved_report_type,
            platform_or_shop=resolved_platform_or_shop,
            fallback_to_pagination=False,
        ))

def request_reports_stockShopReports_total(header,url,params,form_data):
    """
    接口查询销量报表总页数和总记录条数（按店铺）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 总页数，总记录
    """
    return _request_reports_total(header, url, params, form_data)

def request_reports_stockShopReports_table(header,url,params,form_data,total_pages,output_file):
    """
    接口查询销量报表（按店铺）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 表格
    """
    try:
        return request_reports_table_by_product_export(
            header, url, params, form_data, output_file,
            report_type="stockShopReports",
            platform_or_shop=_get_form_payload_first(form_data, "platformOrShop") or "stockShop",
        )
    except Exception as exc:
        print(f"按商品导出全部失败，退回分页抓取：{exc}")

    all_data = []
    seen_sku = set()

    # 单线程循环抓取每一页
    for page in range(1, total_pages + 1):
        form_data['page'] = str(page)
        resp_page =None
        response = requests.post(url, params=params, headers=header, data=form_data)
        try:
            resp_page = response.json()
            if resp_page['success']:
                print(f"第 {page} 页请求解析成功")
        except Exception as e:
            print(f"第 {page} 页请求失败或解析异常：{e}")
        if resp_page and 'tableContent' in resp_page:
            records = _parse_sales_report_rows_fast(resp_page['tableContent'])
            if not records:
                records = mb.parse_table_reports_countryReports(resp_page['tableContent'])
            for item in records:
                sku = item.get('stock_code')
                if sku and sku not in seen_sku:
                    seen_sku.add(sku)
                    all_data.append(item)
    res = [[v.get('stock_code'), v.get('product_title', ''), v.get('defaultCost', ''), v.get('weight', ''),
            v.get('timeCreated', ''), v.get('quantity', 0)] for v in all_data]
    columns = ["SKU", "商品中文名", "统一成本价", "重量(单位:g)", "创建时间", "销售数量"]
    df = pd.DataFrame(res, columns=columns)
    # 保存 Excel
    df.to_excel(output_file, index=False)
    return output_file


def request_reports_warehouseReports_total(header,url,params,form_data):
    """
    接口查询销量报表总页数和总记录条数（按照仓库）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 总页数，总记录
    """
    return _request_reports_total(header, url, params, form_data)



def request_reports_warehouseReports_table(header,url,params,form_data,total_pages,output_file):
    """
    接口查询销量报表（按仓库）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 表格
    """
    if os.environ.get("MABANG_SALES_REPORT_EXPORT_MODE", "").strip().lower() != "pagination":
        try:
            return request_reports_table_by_product_export(
                header, url, params, form_data, output_file,
                report_type="warehouseReports",
                platform_or_shop=_get_form_payload_first(form_data, "platformOrShop") or "warehouse",
            )
        except Exception as exc:
            print(f"按商品导出全部失败，退回分页抓取：{exc}")
    else:
        print("已启用分页模式，跳过按商品导出全部接口")

    all_data = []
    seen_sku = set()
    page_timeout = _positive_int(os.environ.get("MABANG_SALES_REPORT_PAGE_TIMEOUT"), 180)
    page_retries = _positive_int(os.environ.get("MABANG_SALES_REPORT_PAGE_RETRIES"), 3)

    # 单线程循环抓取每一页
    for page in range(1, total_pages + 1):
        request_form = _copy_form_payload(form_data)
        request_form = _set_form_payload_value(request_form, "page", str(page))
        resp_page =None
        response = None
        for attempt in range(1, page_retries + 1):
            try:
                response = requests.post(
                    url,
                    params=params,
                    headers=header,
                    data=request_form,
                    timeout=page_timeout,
                )
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt >= page_retries:
                    raise
                print(f"第 {page} 页请求异常：{exc}，第 {attempt + 1} 次重试")
                sleep(min(10 * attempt, 30))
        try:
            resp_page = response.json() if response is not None else None
            if resp_page['success']:
                print(f"第 {page} 页请求解析成功")
        except Exception as e:
            print(f"第 {page} 页请求失败或解析异常：{e}")
        if resp_page and 'tableContent' in resp_page:
            records = mb.parse_table_reports_countryReports(resp_page['tableContent'])
            for item in records:
                sku = item.get('stock_code')
                if sku and sku not in seen_sku:
                    seen_sku.add(sku)
                    all_data.append(item)
    res = [[v.get('stock_code'), v.get('product_title', ''), v.get('defaultCost', ''), v.get('weight', ''),
            v.get('timeCreated', ''), v.get('quantity', 0)] for v in all_data]
    columns = ["SKU", "商品中文名", "统一成本价", "重量(单位:g)", "创建时间", "销售数量"]
    df = pd.DataFrame(res, columns=columns)
    # 保存 Excel
    df.to_excel(output_file, index=False)
    return output_file




def request_reports_countryReports_total(header,url,params,form_data):
    """
    接口查询销量报表总页数和总记录条数（按照国家地区）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :return: 总页数，总记录
    """
    return _request_reports_total(header, url, params, form_data)




def request_reports_countryReports_table(header,url,params,form_data,total_pages,output_file,*,export_mode=None):
    """
    接口查询销量报表（按照国家地区）
    :param header: 请求头
    :param url: 请求地址
    :param params: 请求参数
    :param form_data: 请求体
    :param export_mode: 显式导出模式；省略时沿用环境配置，pagination 表示只使用分页
    :return: 表格
    """
    resolved_export_mode = export_mode if export_mode is not None else os.environ.get("MABANG_SALES_REPORT_EXPORT_MODE", "")
    if _safe_text(resolved_export_mode).lower() != "pagination":
        try:
            return request_reports_table_by_product_export(
                header, url, params, form_data, output_file,
                report_type="countryReports",
                platform_or_shop=_get_form_payload_first(form_data, "platformOrShop") or "country",
            )
        except Exception as exc:
            print(f"按商品导出全部失败（{type(exc).__name__}），退回分页抓取", flush=True)
    else:
        print("已启用分页模式，跳过按商品导出全部接口", flush=True)

    all_data = []
    seen_sku = set()
    page_timeout = _positive_int(os.environ.get("MABANG_SALES_REPORT_PAGE_TIMEOUT"), 180)
    page_retries = _positive_int(os.environ.get("MABANG_SALES_REPORT_PAGE_RETRIES"), 3)

    def report_attribute(tag, name):
        match = re.search(
            rf'\s{re.escape(name)}\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27|([^\s>]+))',
            tag, re.I,
        )
        if match:
            return html_lib.unescape(next(value for value in match.groups() if value is not None))
        return None

    # 分页接口是只读查询，可有限重试；全部分页校验通过后才写报表。
    for page in range(1, total_pages + 1):
        request_form = _copy_form_payload(form_data)
        request_form = _set_form_payload_value(request_form, "page", str(page))
        for attempt in range(1, page_retries + 1):
            print(f"国家销量第 {page}/{total_pages} 页请求中（第 {attempt}/{page_retries} 次）", flush=True)
            try:
                response = requests.post(
                    url, params=params, headers=header, data=request_form,
                    timeout=(15, page_timeout),
                )
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                # 不输出原始异常，避免认证 URL 或响应中的敏感信息进入日志。
                error = f"国家销量第 {page}/{total_pages} 页请求失败（{type(exc).__name__}）"
                if attempt >= page_retries:
                    raise RuntimeError(f"{error}，已尝试 {page_retries} 次，未保存报表") from None
                print(f"{error}，即将重试", flush=True)
                sleep(min(10 * attempt, 30))
        try:
            resp_page = response.json()
        except ValueError:
            raise RuntimeError(f"国家销量第 {page}/{total_pages} 页返回无效 JSON，未保存报表") from None
        if not isinstance(resp_page, dict) or resp_page.get("success") not in (True, 1):
            raise RuntimeError(f"国家销量第 {page}/{total_pages} 页响应未成功，未保存报表")
        table_html = resp_page.get("tableContent")
        if not isinstance(table_html, str) or not table_html.strip():
            raise RuntimeError(f"国家销量第 {page}/{total_pages} 页缺少表格内容，未保存报表")
        try:
            records = _parse_sales_report_rows_fast(table_html)
        except Exception as exc:
            raise RuntimeError(
                f"国家销量第 {page}/{total_pages} 页表格解析失败（{type(exc).__name__}），未保存报表"
            ) from None
        if not records or any(not item.get("stock_code") for item in records):
            raise RuntimeError(f"国家销量第 {page}/{total_pages} 页为空或缺少 SKU，未保存报表")
        # 快速解析只接受已有稳定格式，先核对每个业务行和已声明字段，不能静默漏行/漏列。
        expected_rows = sum(
            "view-select" in (report_attribute(tag, "class") or "").split()
            for tag in re.findall(r'<tr\b[^>]*>', table_html, re.I)
        )
        source_rows = re.findall(r'<tr\s+class="view-select">(.*?)</tr>', table_html, re.S)
        if len(records) != expected_rows or len(source_rows) != expected_rows:
            raise RuntimeError(f"国家销量第 {page}/{total_pages} 页行格式不支持快速解析，未保存报表")
        for row_html, record in zip(source_rows, records):
            field_counts = {}
            for tag in re.findall(r'<td\b[^>]*>', row_html, re.I):
                field = report_attribute(tag, "data-field")
                if field in ("defaultCost", "weight", "timeCreated", "quantity"):
                    field_counts[field] = field_counts.get(field, 0) + 1
            if field_counts.get("quantity") != 1:
                raise RuntimeError(f"国家销量第 {page}/{total_pages} 页缺少唯一销量字段，未保存报表")
            for field, count in field_counts.items():
                if count != 1 or not re.search(rf'data-field="{field}"[^>]*>(.*?)</td>', row_html, re.S):
                    raise RuntimeError(f"国家销量第 {page}/{total_pages} 页字段格式不支持快速解析，未保存报表")
            # 与旧解析器一致：商品名仅取第 2 个单元格的首个 p.ellipsis，
            # 后续段落/其他列的 title 可能是 SKU、分类或备注，不能据此拒绝整页。
            cells = re.findall(r'<td\b[^>]*>(.*?)</td\s*>', row_html, re.I | re.S)
            record["product_title"] = ""
            if len(cells) > 1:
                for tag in re.findall(r'<p\b[^>]*>', cells[1], re.I):
                    if "ellipsis" in (report_attribute(tag, "class") or "").split():
                        record["product_title"] = report_attribute(tag, "title") or ""
                        break
        for item in records:
            sku = item.get('stock_code')
            if sku not in seen_sku:
                seen_sku.add(sku)
                all_data.append(item)
        print(f"国家销量第 {page}/{total_pages} 页完成：{len(records)} 行，累计 {len(all_data)} 个 SKU", flush=True)
    res = [[v.get('stock_code'), v.get('product_title', ''), v.get('defaultCost', ''), v.get('weight', ''),
            v.get('timeCreated', ''), v.get('quantity', 0)] for v in all_data]
    columns = ["SKU", "商品中文名", "统一成本价", "重量(单位:g)", "创建时间", "销售数量"]
    df = pd.DataFrame(res, columns=columns)
    # 保存 Excel
    df.to_excel(output_file, index=False)
    return output_file



# 点击对应的仓库
def click_warehouse(driver, warehouse_id, timeout=10):
    """根据库存导出界面点击对应仓库
    @:param driver
    @:param warehouse_id 仓库对应的id
    @:param timeout
    """
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR, f"li[onclick*='{warehouse_id}'], a[onclick*='{warehouse_id}']"
            ))
        )
        driver.execute_script("arguments[0].click();", el)
        print(f"点击仓库 ID {warehouse_id} 成功")
        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
        return True
    except Exception as e:
        print(f"未找到仓库 {warehouse_id}: {e}")
        xpath = f"//a[@data-id='{warehouse_id}']"

        el = driver.find_element(By.XPATH, xpath)
        driver.execute_script("arguments[0].click();", el)
        print(f"点击仓库 ID {warehouse_id} 成功")

        return False
def click_warehouse_new(driver, warehouse_id, timeout=10):
    """
    新版仓库选择：点击 checkbox
    """
    try:
        wait = WebDriverWait(driver, timeout)

        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
        dropdown_btn = wait.until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[.//span[contains(text(),'仓库')]]"
            ))
        )
        driver.execute_script("arguments[0].click();", dropdown_btn)
        checkbox = wait.until(
            EC.presence_of_element_located((
                By.XPATH,
                f"//input[@name='warehouseIdArr[]' and @value='{warehouse_id}']"
            ))
        )
        if not checkbox.is_selected():
            driver.execute_script("arguments[0].click();", checkbox)
        print(f"[RPA] 仓库勾选成功 warehouse_id={warehouse_id}")
        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
        return True

    except TimeoutException:
        print(f"[RPA] 未找到仓库 checkbox，warehouse_id={warehouse_id}")
        return False

    except Exception as e:
        print(f"[RPA] 点击仓库异常 warehouse_id={warehouse_id}，错误：{e}")
        return False


def select_warehouses_by_country(driver, countries):
    """
    根据国家关键字选中仓库复选框
    :param driver: Selenium webdriver 实例
    :param countries: 国家关键字列表，例如 ["泰国", "菲律宾"]
    """
    # 找到所有复选框对应的 label
    labels = driver.find_elements(By.CSS_SELECTOR, "div.col-sm-10 label.checkbox-inline")

    for label in labels:
        span_text = label.find_element(By.TAG_NAME, "span").text
        # if any(country in span_text for country in countries):
        if countries in span_text and '仓' in span_text:
            checkbox = label.find_element(By.TAG_NAME, "input")
            if not checkbox.is_selected():
                driver.execute_script("arguments[0].click();", checkbox)


# 点击对应的仓库
def click_second_warehouse(driver, warehouse_id, timeout=10):
    try:
        # 等待所有匹配元素出现
        elements = WebDriverWait(driver, timeout).until(
            EC.presence_of_all_elements_located((
                By.CSS_SELECTOR,
                f"li[onclick*='{warehouse_id}'], a[onclick*='{warehouse_id}']"
            ))
        )

        # 至少要有两个
        if len(elements) < 2:
            print(f"仓库 {warehouse_id} 少于两个可选项，无法点击第二个")
            return False

        el = elements[1]

        driver.execute_script("arguments[0].scrollIntoView(true);", el)
        driver.execute_script("arguments[0].click();", el)

        print(f"点击仓库 {warehouse_id} 的 **第二个** 成功")

        return True

    except Exception as e:
        print(f"点击第二个仓库失败: {e}")
        return False



def download_warehouse_transfer_shipment_new(driver,warehouse_id,outp_file):
    """
     仓库调拨发货
    :param driver:
    :param warehouse_id: 导出的仓库id
    :param check_value_list:  导出的表头 ["uq101","uq102"...]
    :param op_path: 导出文件存储路径
    :param op_name: 导出文件文件名
    :return:
    """
    driver.get("https://900853.private.mabangerp.com/index.php?mod=allocationwarehouse.shipments&platform=other&version=1")

    # 切换到iframe里面
    WebDriverWait(driver, 20).until(
        EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
    )
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
    # 点击全部
    dv.click_element_no_head(driver,"//ul[@id='data-search-click']//a[contains(@class,'search-click') and normalize-space(text())='待签收']",By.XPATH)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-mask')
    # 更多条件
    dv.click_element_no_head(driver, 'div.more-conditions > span#btn-screen')
    # 点击仓库名称
    click_second_warehouse(driver,warehouse_id)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-mask')

    # 加载100页
    load_onclick(driver,"getPaginationData(1,100)")
    dv.wait_for_invisible(driver, '.loading-mask')
    time.sleep(2)
    """ 获取数量"""
    elem = driver.find_element(
        By.XPATH,
        "//*[@id='pagehtml']//span[@class='semibold'][1]"
    )

    total_text = driver.execute_script(
        "return arguments[0].textContent;", elem
    ).strip()
    total_count = 0
    if total_text.isdigit():
        total_count = int(total_text)
    val_list = []
    for m in range(1,total_count + 1):
        # 获取相似元素
        elements = driver.find_elements(By.CSS_SELECTOR, "textarea.fhRemark")
        print(f"获取第{m}个元素")
        val_content = elements[m-1].get_attribute("val")
        val_list.append(val_content)
    temp = []
    for val in val_list:
        driver.get(f"https://900853.private.mabangerp.com/index.php?mod=warehouseallocation.editallocation&id={val}&type=1&tablebase=&platform=other&version=1")
        # 切换到iframe里面
        WebDriverWait(driver, 20).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
        )
        remark = driver.find_element(
            By.XPATH,
            "//label[normalize-space(text())='备注']/following::p[@class='loadtext'][1]"
        ).text
        batch_code = driver.find_element(
            By.NAME, "allocationCode"
        ).get_attribute("value")
        print("备注：", remark)
        print("批次号：", batch_code)
        sku_elements = driver.find_elements(
            By.CSS_SELECTOR,
            "#skulist a.needaddsku"
        )
        result = [[e.text.strip(),remark,batch_code] for e in sku_elements]
        temp = temp + result
    merged_df = pd.DataFrame(temp, columns=["库存SKU编号", "在途情况","批次号"])

    merged_df.to_excel(outp_file, index=False)
    return merged_df



def download_warehouse_transfer_shipment(driver,warehouse_id,check_value_list,op_path,op_name,download_directory):
    """
     仓库调拨发货
    :param driver:
    :param warehouse_id: 导出的仓库id
    :param check_value_list:  导出的表头 ["uq101","uq102"...]
    :param op_path: 导出文件存储路径
    :param op_name: 导出文件文件名
    :return:
    """
    driver.get("https://900853.private.mabangerp.com/index.php?mod=allocationwarehouse.shipments&platform=other&version=1")

    # 切换到iframe里面
    WebDriverWait(driver, 20).until(
        EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
    )
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
    # 点击全部
    dv.click_element_no_head(driver,"//ul[@id='data-search-click']//a[contains(@class,'search-click') and normalize-space(text())='待签收']",By.XPATH)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-mask')
    # 更多条件
    dv.click_element_no_head(driver, 'div.more-conditions > span#btn-screen')
    # 点击仓库名称
    click_second_warehouse(driver,warehouse_id)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-mask')

    # 加载100页
    load_onclick(driver,"getPaginationData(1,100)")
    dv.wait_for_invisible(driver, '.loading-mask')
    time.sleep(2)
    """ 获取数量"""
    elem = driver.find_element(
        By.XPATH,
        "//*[@id='pagehtml']//span[@class='semibold'][1]"
    )

    total_text = driver.execute_script(
        "return arguments[0].textContent;", elem
    ).strip()
    total_count = 0
    if total_text.isdigit():
        total_count = int(total_text)
    dfs = []
    for m in range(1,total_count + 1):
        if m > 1:
            # 切换到iframe里面
            WebDriverWait(driver, 20).until(
                EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
            )
        # 选中新的窗口
        dv.check_allow_by_index(driver, "allot[]", m-1,isselect=False, by=By.NAME)
        # 获取相似元素
        elements = driver.find_elements(By.CSS_SELECTOR, "textarea.fhRemark")
        print(f"获取第{m}个元素")
        mark_content = elements[m-1].text
        # 根据下标选中
        dv.check_allow_by_index(driver,"allot[]",m,isselect=True,by=By.NAME)
        # ---------- 点击“添加/导出”按钮 ----------
        dv.click_element_no_head(driver, '//button[span[contains(text(),"添加/导出")]]',By.XPATH)
        # 记录窗口
        original_window = driver.current_window_handle
        sleep(1)
        # dv.click_element_no_head(driver, '//a[normalize-space(text())="导出调拨批次"]', By.XPATH)
        exp_ele = driver.find_element(By.XPATH, '//a[normalize-space(text())="导出调拨批次"]')
        driver.execute_script("arguments[0].click();", exp_ele)
        # 等待新窗口打开
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > 1)
        # 切换到新窗口
        new_window = [w for w in driver.window_handles if w != original_window][0]
        driver.switch_to.window(new_window)
        print("已切换至新窗口")
        # 等待操作成功出现
        dv.wait_for_invisible(driver, 'span.gritter-title')
        yd.clean_directory(download_directory)
        sleep(3)

        # 循环选择input
        for value in check_value_list:
            dv.check_input_no_head(driver,f'input[name="fieldlabel"][value="{value}"]')
        dv.submit_btn_no_head(driver, '//button[@onclick="doExport()"]', By.XPATH)
        file_list = yd.get_download_file_list_version2(path=download_directory)

        # df = pd.read_excel(file_list[0],dtype=str)
        df = pd.read_excel(file_list[0], dtype=str, engine="openpyxl")
        df['在途情况'] = mark_content
        dfs.append(df)
        op_name2 = op_name.replace('.xlsx',f'_{m}.xlsx')
        df.to_excel(rf"{op_path}\{op_name2}",index=False)

        driver.close()
        driver.switch_to.window(original_window)
    merged_df = pd.concat(dfs, ignore_index=True)
    op_file = rf"{op_path}\{op_name}"
    merged_df.to_excel(op_file, index=False)
    return op_file,merged_df



def download_warehouse_sku_more(driver,click_content,check_value_list,op_path,op_name,download_directory):
    """
     库存导出下载数据
    :param driver:
    :param click_content: 导出的类型  例如 马来不备；印尼不备；泰国不备...
    :param check_value_list:  导出的表头 ["uq101","uq102"...]
    :param op_path: 导出文件存储路径
    :param op_name: 导出文件文件名
    :return:
    """
    op_file = rf"{op_path}\{op_name}"
    if os.path.exists(op_file):
        print(f"{op_file}文件已经创建。")
        return op_file,driver

    driver.get("https://900853.private.mabangerp.com/index.php?mod=stock.list&searchStatus=3")

    # 切换到iframe里面
    WebDriverWait(driver, 20).until(
        EC.frame_to_be_available_and_switch_to_it((By.ID, 'iframeContent'))
    )
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
    # 点击全部
    dv.click_element_no_head(driver,'//ul[@id="sku-status"]/li[a[normalize-space(text())="全部"]]',By.XPATH)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-center')
    # 更多条件
    dv.click_element_no_head(driver, 'div.more-conditions > span#btn-screen')
    # 点击定义的条件 "//li[.//a[text()="菲律宾不备"]]"
    time.sleep(2)
    dv.click_element_no_head(driver,f'//ul[@id="logistics-ul-label"]/li[contains(@class,"stock-search-form-click")][a[normalize-space()="{click_content}"]]',By.XPATH)
    # dv.click_element_no_head(driver, f'//ul[@id="logistics-ul-label"]/li[contains(@class,"stock-search-form-click") and .//a[normalize-space()="{click_content}"]]',By.XPATH)
    time.sleep(1)
    # dv.click_element_no_head(driver, f'//ul[@id="logistics-ul-label"]/li[a[normalize-space(text())="{click_content}"]]',By.XPATH)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-center')
    # ---------- 点击“添加/导出”按钮 ----------
    dv.click_element_no_head(driver, '//button[span[contains(text(),"添加/导出")]]',By.XPATH)
    # 记录窗口
    original_window = driver.current_window_handle
    dv.click_element_no_head(driver, '//a[normalize-space(text())="导出搜索的全部记录"]', By.XPATH)
    # 等待新窗口打开
    WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > 1)
    # 切换到新窗口
    new_window = [w for w in driver.window_handles if w != original_window][0]
    driver.switch_to.window(new_window)
    print("已切换至新窗口")
    # 等待操作成功出现
    dv.wait_for_invisible(driver, 'span.gritter-title')
    yd.clean_directory(download_directory)
    pages = 1
    is_block,p_element = dv.wait_element_block(driver,"p.mb0.text-warning.mt10",By.CSS_SELECTOR)
    if is_block:
        match = re.search(r'分为(\d+)页', p_element.text)
        if match:
            pages = int(match.group(1))
    print(f"共 {pages} 页")
    file_list = []
    if pages > 1:
        for i in range(1, pages + 1):
            if i > 1:
                dv.write_content_element(driver, i, 'input.form-control[placeholder="页码"]')
                # 点击跳转
                dv.submit_btn_no_head(driver, '//button[@onclick="submitGotoPage()"]', By.XPATH)
                dv.wait_for_invisible(driver, 'span.gritter-title')
            # 循环选择input
            for value in check_value_list:
                dv.check_input_no_head(driver,f'input[name="fieldlabel"][value="{value}"]')
            dv.submit_btn_no_head(driver, '//button[@onclick="doExport()"]', By.XPATH)
            file_list = yd.get_dowmlaod_file_list(path=download_directory,target_count=i)
            print(file_list)
    else:
        for value in check_value_list:
            dv.check_input_no_head(driver, f'input[name="fieldlabel"][value="{value}"]')
        dv.submit_btn_no_head(driver, '//button[@onclick="doExport()"]', By.XPATH)
        file_list = yd.get_dowmlaod_file_list(path=download_directory)
    if len(file_list) > 1:
        op_file = rf"{op_path}\{op_name}"
        yd.hebing_excel(file_list, op_file)
    else:
        op_file = yd.copy_file(file_list[0], op_path, op_name)
    driver.close()
    driver.switch_to.window(original_window)

    return op_file,driver

def is_export_modal_visible(driver, timeout=3):
    """ 判断库存导出界面分页导出是否弹出"""
    is_alert = False
    page_count = 1
    modal =None
    try:
        # 等待 modal 出现并且可见
        modal = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR, "form.modal-content.advance-search.choicestaff"
            ))
        )
        # 获取总页数
        page_count_elem = modal.find_element(By.ID, "page-count")
        page_count = int(page_count_elem.text.strip())
        is_alert = True
    except:
        page_count = 1
        print("当前仓库无分页导出!")
    return is_alert,page_count,modal


def click_bootbox_confirm(driver, timeout=3):
    """点击 Bootbox 弹窗的确定按钮（无头可用）"""
    try:
        # 等待弹窗出现
        modal_title = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "div.bootbox.modal div.modal-header h4.modal-title"))
        )

        if modal_title.text.strip() == "操作确认":
            # 找到确定按钮
            confirm_btn = driver.find_element(
                By.CSS_SELECTOR, "div.bootbox.modal div.modal-footer button.btn.btn-primary"
            )
            # 使用 jQuery trigger 点击
            driver.execute_script("$(arguments[0]).trigger('click');", confirm_btn)
            time.sleep(0.5)
    except:
        print("无再次确认按钮")
def export_warehousedetail_by_sku(driver,sku_str,down_count,export_title_list,download_path,select_other=False):
    dv.switch_iframe(driver,'iframeContent')

    dv.double_click_input(driver,"search-content-text1",By.ID)
    sleep(1)
    dv.write_textarea_nohead(driver,"stockData",sku_str,By.NAME)

    dv.click_element_no_head(driver,"#platformTracknumberSearchIn .modal-footer .btn.btn-primary")
    sleep(1)
    # 等待 loading 遮罩隐藏
    dv.wait_for_invisible(driver, '.loading-center')
    # ---------- 点击“添加/导出”按钮 ----------
    dv.click_element_no_head(driver,'//button[span[contains(text(),"添加/导出")]]',By.XPATH)
    # 记录窗口
    original_window = driver.current_window_handle
    # 点击导出全部
    dv.click_element_no_head(driver,'//a[normalize-space(text())="导出搜索的全部记录"]',By.XPATH)
    # 等待新窗口打开
    WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > 1)
    # 切换到新窗口
    new_window = [w for w in driver.window_handles if w != original_window][0]
    driver.switch_to.window(new_window)
    print("已切换至新窗口")
    #等待操作成功出现
    dv.wait_for_invisible(driver, 'span.gritter-title')
    pages = 1
    dv.not_select_input_no_head(driver, 'hbddgyxx',By.ID)
    for value in export_title_list:
        dv.check_input_no_head(driver, f'input[name="fieldlabel"][value="{value}"]')
    if select_other:
        dv.check_input_no_head(driver, "//input[@type='checkbox' and @value='330978']",By.XPATH)
    driver.execute_script("doExport()")

    file_list = yd.get_dowmlaod_file_list(path =download_path, target_count=down_count)
    driver.close()
    driver.switch_to.window(original_window)
    driver.refresh()
    return driver,file_list

def inventory_export_by_realtime(driver,output_directory,warehouse_map,download_path, select_warehouse_url="https://900853.private.mabangerp.com/index.php?mod=warehouse.inventorydetail"):
    """马帮库存实时导出
    @:param driver
    @:param output_directory 导出路径
    @:param download_path 库存导出地址
    @:warehouse_map 仓库id映射字典
    @:select_warehouse_url 实时库存导出地址
    @:return inventory_file_list 输出文件地址
    """
    inventory_file_list = []
    for warehouse_name, warehouse_id in warehouse_map.items():
        if os.path.exists(rf"{output_directory}\{warehouse_name}.xlsx"):
            inventory_file_list.append(rf"{output_directory}\{warehouse_name}.xlsx")
            continue

        driver.get(select_warehouse_url)
        print(f'======刷新库存导出UI网页：{select_warehouse_url}')
        print(f'Loadding.....')
        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
        # 等待 iframe 出现
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "iframeContent"))
        )
        # 切换到 iframe
        driver.switch_to.frame(iframe)
        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
        sleep(5)
        print(f"======下载 {warehouse_name} 实时库存=======")
        yd.clean_directory(download_path)
        success = click_warehouse(driver, warehouse_id)
        # success = click_warehouse_new(driver, warehouse_id)
        if not success:
            print(f"跳过 {warehouse_name}，未找到元素")
        time.sleep(2)
        search_btn  = driver.find_element(By.XPATH, "//button[contains(@onclick,'searchwarehousestock')]")
        driver.execute_script("arguments[0].click();", search_btn )
        dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')

        driver.execute_script("checkExport();")# 点击导出
        time.sleep(0.5)
        isalert,page_count,modal = is_export_modal_visible(driver, timeout=3)
        """ 解决马帮导出BUG start=========="""
        while page_count>10:
            print(f"马帮BUG触发,出现 {str(page_count)}页数据,正在修复..")
            click_warehouse(driver, warehouse_id)
            # success = click_warehouse_new(driver, warehouse_id)
            time.sleep(2)
            driver.execute_script("checkExport();")  # 点击导出
            isalert, page_count, modal = is_export_modal_visible(driver, timeout=3)
            if page_count > 5:
                yd.clean_directory(download_path)
        """ 解决马帮导出BUG end=========="""
        output_filename = f"{warehouse_name}.xlsx"
        output_file =rf"{output_directory}\{output_filename}"
        if isalert:
            print(f"执行分页导出,需{str(page_count)}页数据")
            file_list = []
            for page in range(1, page_count + 1):
                # 使用 Selectize 内部 API 选择页数（无头模式最稳）
                driver.execute_script(f"""
                    var selectize = document.querySelector('#page-select').selectize;
                    selectize.setValue('{page}');
                """)
                time.sleep(0.3)  # 等待页面刷新
                # 点击“确定”按钮触发导出
                confirm_btn = modal.find_element(By.CSS_SELECTOR, "div.modal-footer button.btn.btn-primary")
                driver.execute_script("$(arguments[0]).trigger('click');", confirm_btn)
                time.sleep(0.5)
                # 二次确认
                click_bootbox_confirm(driver)

                file_list = yd.get_dowmlaod_file_list(download_path,page)
            yd.hebing_excel(file_list,output_file)
            try:
                driver.execute_script("""
                    var modal = document.querySelector('form.modal-content.advance-search.choicestaff');
                    if(modal && $(modal).modal) {
                        $(modal).modal('hide');
                    }
                """)
            except:
                pass
        else:
            print(f"正在导出{warehouse_name}库存数据")
            file_list = yd.get_dowmlaod_file_list(download_path, page_count)
            output_file = yd.copy_file(file_list[0], output_directory, output_filename)
        driver.switch_to.default_content()
        inventory_file_list.append(output_file)
    return inventory_file_list


def inventory_export_by_realtime_new(driver,output_directory,warehouse_map,download_path, select_warehouse_url="https://900853.private.mabangerp.com/index.php?mod=warehouse.inventorydetail"):
    """马帮库存实时导出
    @:param driver
    @:param output_directory 导出路径
    @:param download_path 库存导出地址
    @:warehouse_map 仓库id映射字典
    @:select_warehouse_url 实时库存导出地址
    @:return inventory_file_list 输出文件地址
    """
    inventory_file_list = []
    for warehouse_name, warehouse_id in warehouse_map.items():
        if os.path.exists(rf"{output_directory}\{warehouse_name}.xlsx"):
            inventory_file_list.append(rf"{output_directory}\{warehouse_name}.xlsx")
            continue
        output_file = export_warehouse_new(driver,select_warehouse_url,warehouse_name,download_path,warehouse_id,output_directory)
        inventory_file_list.append(output_file)
    return inventory_file_list

def export_warehouse_new(driver,select_warehouse_url,warehouse_name,download_path,warehouse_id,output_directory):
    driver.get(select_warehouse_url)
    print(f'======刷新库存导出UI网页：{select_warehouse_url}')
    print(f'Loadding.....')
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
    # 等待 iframe 出现
    iframe = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "iframeContent"))
    )
    # 切换到 iframe
    driver.switch_to.frame(iframe)
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')
    sleep(5)
    print(f"======下载 {warehouse_name} 实时库存=======")
    yd.clean_directory(download_path)

    success = click_warehouse_new(driver, warehouse_id)
    if not success:
        print(f"跳过 {warehouse_name}，未找到元素")
    time.sleep(2)
    search_btn = driver.find_element(By.XPATH, "//button[contains(@onclick,'searchwarehousestock')]")
    driver.execute_script("arguments[0].click();", search_btn)
    dv.wait_for_invisible(driver, 'div.loading_large.loading-mask')

    driver.execute_script("checkExport();")  # 点击导出
    time.sleep(0.5)
    isalert, page_count, modal = is_export_modal_visible(driver, timeout=3)
    """ 解决马帮导出BUG start=========="""
    while page_count > 10:
        print(f"马帮BUG触发,出现 {str(page_count)}页数据,正在修复..")
        driver.switch_to.default_content()
        output_file =  export_warehouse_new(driver,select_warehouse_url,warehouse_name,download_path,warehouse_id,output_directory)
        return output_file
        # # click_warehouse(driver, warehouse_id)
        # success = click_warehouse_new(driver, warehouse_id)
        # time.sleep(2)
        # driver.execute_script("checkExport();")  # 点击导出
        # isalert, page_count, modal = is_export_modal_visible(driver, timeout=3)
        # if page_count > 5:
        #     yd.clean_directory(download_path)
    """ 解决马帮导出BUG end=========="""
    output_filename = f"{warehouse_name}.xlsx"
    output_file = rf"{output_directory}\{output_filename}"
    if isalert:
        print(f"执行分页导出,需{str(page_count)}页数据")
        file_list = []
        for page in range(1, page_count + 1):
            # 使用 Selectize 内部 API 选择页数（无头模式最稳）
            driver.execute_script(f"""
                        var selectize = document.querySelector('#page-select').selectize;
                        selectize.setValue('{page}');
                    """)
            time.sleep(0.3)  # 等待页面刷新
            # 点击“确定”按钮触发导出
            confirm_btn = modal.find_element(By.CSS_SELECTOR, "div.modal-footer button.btn.btn-primary")
            driver.execute_script("$(arguments[0]).trigger('click');", confirm_btn)
            time.sleep(0.5)
            # 二次确认
            click_bootbox_confirm(driver)

            file_list = yd.get_dowmlaod_file_list(download_path, page)
        yd.hebing_excel(file_list, output_file)
        try:
            driver.execute_script("""
                        var modal = document.querySelector('form.modal-content.advance-search.choicestaff');
                        if(modal && $(modal).modal) {
                            $(modal).modal('hide');
                        }
                    """)
        except:
            pass
    else:
        print(f"正在导出{warehouse_name}库存数据")
        file_list = yd.get_dowmlaod_file_list(download_path, page_count)
        output_file = yd.copy_file(file_list[0], output_directory, output_filename)
    driver.switch_to.default_content()
    return output_file

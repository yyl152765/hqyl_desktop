from bs4 import BeautifulSoup
import re
from html import unescape
import pandas as pd





def parse_vxe_table_html_to_df(html)->pd.DataFrame:
    """
    解析 VXE-Table HTML，返回 pandas DataFrame
    :param html: 表格所在的 HTML 字符串
    :return: pd.DataFrame
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="vxe-table--body")
    rows = table.find_all("tr", class_="vxe-body--row")  # 只取数据行

    data_list = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 9:  # 安全检查
            continue

        # 解析每列内容
        row_data = {
            "选择": cells[0].get_text(strip=True),
            "活动名称": cells[1].find("a", class_="shope_content").get_text(strip=True) if cells[1].find("a",
                                                                                                         class_="shope_content") else "",
            "店铺名": cells[1].find("span", class_="tag").get_text(strip=True) if cells[1].find("span",
                                                                                              class_="tag") else "",
            "编号": cells[2].find("span").get_text(strip=True) if cells[2].find("span") else "",
            "数量": cells[3].get_text(strip=True),
            "时间段": " ".join([div.get_text(strip=True) for div in cells[4].find_all("div")]) if cells[4].find_all(
                "div") else cells[4].get_text(strip=True),
            "状态": cells[5].get_text(strip=True),
            "销量/库存": cells[6].get_text(strip=True),
            "结束时间": cells[7].get_text(strip=True)+":00",
            "操作": cells[8].get_text(strip=True)
        }
        data_list.append(row_data)
    return pd.DataFrame(data_list)


def parse_getStock_List(stock_list):
    list = []
    for stock_data in stock_list:
        sku = stock_data['stockSku']
        sku_id = stock_data['id']
        warehouse_detail = stock_data['stockWarehouseData']
        for warehouse_data in warehouse_detail:
            #在途量
            allotShippingQuantity =  warehouse_data['allotShippingQuantity']
            warehouse_name = warehouse_data['name']
            list.append([sku,sku_id,warehouse_name,allotShippingQuantity])

    return list


def parse_shipping_row(html_row):
    """解析货柜信息HTML行数据，安全版本"""
    try:
        soup = BeautifulSoup(html_row, 'html.parser')
        tds = [td.get_text(strip=True) for td in soup.find_all('td')]

        if len(tds) < 9:
            return []

        return [{
            '调拨批次': tds[0],
            '起始仓': tds[1],
            '目标仓': tds[2],
            '发货备注': tds[8]
        }]
    except Exception:
        return []


def parse_sync_result(html_text):
    results = []

    # 按 <br> 分割
    lines = html_text.replace("\n", "").split("<br>")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        count_match = re.search(r"同步成功(\d+)条", line)
        count = int(count_match.group(1)) if count_match else None

        status = "success" if "同步完成" in line or "同步成功" in line else "failed"
        prefix = line.split(",")[0]  # 例如：文布逊-印尼1-母婴
        m_full = re.match(r"(.+?)-?(印尼\d+(?:\(.+?\))?)(?:-[^,]*)?", prefix)

        if m_full:
            shop_name = m_full.group(2)
            person = m_full.group(1)
        else:
            # 情况B：只有“印尼xxx”
            shop_name = prefix
            person = ""
        results.append([shop_name, person, status, count])

    return results

def parse_discount_event_table_old(html: str) -> pd.DataFrame:
    """
    解析折扣活动 老页面
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tbody tr.content")

    result = []

    for tr in rows:
        tds = tr.find_all("td")

        # 活动ID（第二列 span）
        activity_id = tds[1].select_one("span.m-right5")
        activity_id = activity_id.text.strip() if activity_id else ""

        # 活动名称（第三列）
        name = tds[2].get_text(strip=True)

        # 店铺账号（第四列）
        shop = tds[3].get_text(strip=True)

        # 产品数（第五列）
        product_count = tds[4].get_text(strip=True)

        # 时间（第六列）
        time_html = tds[5]
        start_text = time_html.text.split("结束：")[0].replace("开始：", "").strip()
        end_span = time_html.select_one("span.endTimeText")
        end_text = end_span.text.strip() if end_span else ""

        # 状态（第七列）
        status = tds[6].get_text(strip=True)

        result.append({
            "activity_id": activity_id,
            "activity_name": name,
            "shop_name": shop,
            "activity_type": product_count,
            "status": status,
            "start_time": start_text + ':00',
            "end_time": end_text + ':00'
        })
    print(result)
    return pd.DataFrame(result)


def parse_discount_event_table(html: str) -> pd.DataFrame:
    """
    解析 Shopee 折扣活动表格 HTML，返回 DataFrame。
    适配 <br> 分隔的开始/结束时间，并自动补 :00 秒。
    """
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.discount_table_row")
    data_list = []

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 8:
            continue

        # ---- 基础字段 ----
        activity_id_tag = tds[1].find("span")
        activity_id = activity_id_tag.get_text(strip=True) if activity_id_tag else ""

        activity_name = tds[2].get_text(strip=True)
        shop_name = tds[3].get_text(strip=True)
        product_count = tds[4].get_text(strip=True)

        # ---- 时间解析 ----
        time_text = tds[5].get_text(" ", strip=True)
        start_time_match = re.search(r"开始：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", time_text)
        end_time_match = re.search(r"结束：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", time_text)

        start_time = start_time_match.group(1) + ":00" if start_time_match else ""
        end_time = end_time_match.group(1) + ":00" if end_time_match else ""

        # ---- 活动状态 ----
        status_span = tds[6].find("span")
        status = status_span.get_text(strip=True) if status_span else ""

        # ---- data-id / shopid / discountid ----
        data_td = tds[7]
        data_id = data_td.get("data-id", "")
        shop_id = data_td.get("data-shopid", "")
        discount_id = data_td.get("data-discountid", "")

        # ---- 隐藏 input ----
        extend_end_input = row.find("input", class_="extendEndTime")
        extend_end_time = extend_end_input.get("value", "") if extend_end_input else ""

        duration_input = row.find("input", class_="activityTimeDurationDays")
        duration_days = duration_input.get("value", "") if duration_input else ""

        # ---- 加入数据 ----
        data_list.append({
            "activity_id": activity_id,
            "activity_name": activity_name,
            "shop_name": shop_name,
            "product_count": product_count,
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "data_id": data_id,
            "shop_id": shop_id,
            "discount_id": discount_id,
            "extend_end_time": extend_end_time,
            "duration_days": duration_days,
        })

    return pd.DataFrame(data_list)


def parse_purchase_table(html_text) ->pd.DataFrame:
    """
    解析 HTML 表格，返回数量、订单号、SKU 的中文列 DataFrame
    :param html_text: HTML 内容
    :return: pandas DataFrame
    """
    soup = BeautifulSoup(html_text, 'html.parser')
    data = []

    for row in soup.find_all('tr', class_='content'):
        tds = row.find_all('td')

        # 数量 <span class="text">
        quantity = ''
        if len(tds) > 4:
            span_tag = tds[4].find('span', class_='text')
            if span_tag:
                quantity = span_tag.get_text(strip=True)

        # 单号 <td>里面的 p 标签
        order_no = ''
        if len(tds) > 2:
            p_tag = tds[2].find('p')
            if p_tag:
                order_no = p_tag.get_text(strip=True)

        # SKU <a class="stocksku">
        sku = ''
        if len(tds) > 1:
            sku_tag = tds[1].find('a', class_='stocksku')
            if sku_tag:
                sku = sku_tag.get_text(strip=True)

        data.append({
            '库存SKU': sku,
            '采购批次号': order_no,
            '备货数量': quantity
        })

    # 转 DataFrame
    df = pd.DataFrame(data)
    return df

def parse_discount_bundle_table_old(html_text) -> pd.DataFrame:
    soup = BeautifulSoup(html_text, 'html.parser')
    rows = soup.select("tbody tr.content")
    results = []

    for tr in rows:
        tds = tr.find_all("td")

        # 1) 主键 ID
        data_id = tr.get("data-id") or tds[6].get("data-id", "")

        # 2) 活动名称
        name = tds[1].select_one(".productHead")
        name = name.get_text(strip=True) if name else ""

        # 3) 活动 ID（链接）
        act_id_tag = tds[1].select_one("a.limingcentUrlpic")
        act_id = act_id_tag.get_text(strip=True) if act_id_tag else ""
        url = act_id_tag.get("href") if act_id_tag else ""

        # 4) 活动类型
        act_type = tds[2].get_text(strip=True)

        # 5) 产品数
        product_count = tds[3].get_text(strip=True)

        # 6) 时间解析
        time_html = tds[4]

        # 开始时间： "开始：xxxx"
        start_text = time_html.decode_contents()
        start_time = ""
        end_time = ""

        # 提取开始时间（前面有 "开始："）
        if "开始：" in start_text:
            start_time = start_text.split("开始：")[1].split("<")[0].strip()

        # 结束时间：优先取 span.endTimeText
        end_span = time_html.select_one(".endTimeText")
        if end_span:
            end_time = end_span.get_text(strip=True)
        else:
            # 如果 span 没有，则从文本中找 “结束：xxxx”
            if "结束：" in start_text:
                end_time = start_text.split("结束：")[1].split("<")[0].strip()

        # 7) 状态
        status = tds[5].get_text(strip=True)

        # 8) shopId
        shop_id = tds[6].get("data-shopid", "")
        discount_id = tds[6].get("data-discountid", "")

        # 9) 运营人（p标签里面）
        operator_tag = tds[1].select_one("p.fColor2")
        operator = operator_tag.get_text(strip=True).replace("「", "").replace("」", "") if operator_tag else ""

        results.append({
            "activity_name": name,
            "activity_id": act_id,
            "url": url,
            "type": act_type,
            "product_count": product_count,
            "start_time": start_time + ":00",
            "end_time": end_time + ":00",
            "status": status,
            "data_id": data_id,
            "shop_id": shop_id,
            "discount_id": discount_id,
            "shop_name": operator,
        })
    return pd.DataFrame(results)
def parse_discount_bundle_table(html_text) -> pd.DataFrame:
    """
    解析 Shopee 绑赠/折扣活动 列表（vxe-table 新结构）
    返回英文字段 DataFrame
    """
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.select("tr.bundle_table_row")

    data = []

    for row in rows:
        cols = row.select("td.vxe-body--column")
        if len(cols) < 7:
            continue  # 防御

        # ---------------------------
        # 1. 读取基础字段（列固定）
        # ---------------------------
        # col_34: 活动名称 + discount_id + shop_name
        col_34 = cols[1]
        title = col_34.select_one("span")
        title = title.text.strip() if title else ""

        discount_a = col_34.select_one("a.limingcentUrlpic")
        discount_id = discount_a.text.strip() if discount_a else ""
        discount_url = discount_a.get("href", "") if discount_a else ""

        shop_name_p = col_34.select_one("p")
        shop_name = shop_name_p.text.strip() if shop_name_p else ""

        # col_35: 活动类型（如 %）
        col_35 = cols[2]
        act_type_span = col_35.select_one("span.vxe-cell--label")
        activity_type = act_type_span.text.strip() if act_type_span else ""

        # col_36: 折扣比例（或产品数）
        col_36 = cols[3]
        pc_span = col_36.select_one("span.vxe-cell--label")
        product_count = pc_span.text.strip() if pc_span else ""

        # ---------------------------
        # 2. 时间区间解析（重点）
        # ---------------------------
        col_37 = cols[4]
        time_text = col_37.get_text(" ", strip=True)

        # 例如："开始：2025-11-18 19:00   结束：2026-04-29 20:00"
        start_time = ""
        end_time = ""
        if "开始：" in time_text and "结束：" in time_text:
            try:
                start_time = time_text.split("开始：")[1].split("结束：")[0].strip()
                end_time = time_text.split("结束：")[1].strip()
            except:
                pass
        start_time += ":00" if start_time and len(start_time) == 16 else ""
        end_time += ":00" if end_time and len(end_time) == 16 else ""

        # ---------------------------
        # 3. 状态 col_38
        # ---------------------------
        col_38 = cols[5]
        status_span = col_38.select_one("span")
        status = status_span.text.strip() if status_span else ""

        # ---------------------------
        # 4. 编辑链接 & shop_id
        # ---------------------------
        col_39 = cols[6]
        edit_a = col_39.select_one("a[href*='detail']")
        edit_url = edit_a.get("href", "") if edit_a else ""

        # 从 edit_url 获取 shopId 参数
        shop_id = ""
        if edit_url and "shopId=" in edit_url:
            try:
                shop_id = edit_url.split("shopId=")[1]
            except:
                pass

        # ---------------------------
        # 5. activity_id (rowid)
        # ---------------------------
        activity_id = row.get("rowid", "")

        # ---------------------------
        # 6. 汇总一行
        # ---------------------------
        data.append({
            "activity_id": activity_id,
            "activity_name": title,
            "discount_id": discount_id,
            "discount_url": discount_url,
            "shop_name": shop_name,
            "activity_type": activity_type,
            "product_count": product_count,
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "shop_id": shop_id,
            "edit_url": edit_url
        })

    return pd.DataFrame(data)

def parse_discount_coupon_table_old(html_text) -> pd.DataFrame:
    """
        解析优惠券表格 HTML，返回列表字典，每条字典包含详细优惠券信息。
        """
    soup = BeautifulSoup(html_text, "html.parser")
    result = []

    rows = soup.select("tbody tr.content")
    for row in rows:
        cells = row.find_all("td")

        # 优惠券 ID 与店铺 ID
        coupon_id = row.get("data-couponid") or row.get("data-id")
        shop_id = row.select_one(".shopeeCouponsListAction").get("data-shopid")

        # 优惠券名称、优惠券码、店铺/owner 信息
        coupon_name = cells[1].select_one(".productHead").get_text(strip=True)
        coupon_code_tag = cells[1].select_one(".limingcentUrlpic.gray-c")
        coupon_code = coupon_code_tag.get_text(strip=True) if coupon_code_tag else ""
        owner_tag = cells[1].select_one("p.fColor2")
        owner = owner_tag.get_text(strip=True) if owner_tag else ""

        # 活动名称/类型
        coupon_type = cells[2].get_text(strip=True)

        # 商品适用范围
        product_scope = cells[3].get_text(strip=True)

        # 目标买家
        target_buyer = cells[4].get_text(strip=True)

        # 折扣金额
        discount_amount_tag = cells[5].select_one("span:nth-of-type(2)")
        discount_value = float(discount_amount_tag.get_text(strip=True)) if discount_amount_tag else 0

        # 优惠券总数和已使用数量
        coupon_total = int(cells[6].get_text(strip=True))
        used_count = int(cells[7].get_text(strip=True))

        # 状态及时间
        status_time_text = cells[8].get_text(separator=" ", strip=True)
        status = status_time_text.split()[0] if status_time_text else ""

        # 开始时间和结束时间
        times = cells[8].select("span")
        start_time = times[0].get_text(strip=True) if len(times) > 0 else ""
        end_time = times[1].get_text(strip=True) if len(times) > 1 else ""

        # 编辑 URL
        edit_tag = cells[9].select_one("a[target='_blank']")
        edit_url = edit_tag.get("href") if edit_tag else ""

        result.append({
            "coupon_id": coupon_id,
            "activity_name": coupon_name,
            "coupon_code": coupon_code,
            "shop_name": owner,
            # "activity_name": coupon_type,
            "product_scope": product_scope,
            "target_buyer": target_buyer,
            "discount_value": discount_value,
            "coupon_total": coupon_total,
            "used_count": used_count,
            "status": status,
            "start_time": start_time + ":00",
            "end_time": end_time + ":00",
            "shop_id": shop_id,
            "edit_url": edit_url
        })
    return pd.DataFrame(result)

def parse_discount_coupon_table(html_text) -> pd.DataFrame:
    """
    解析 Shopee 优惠券表格 HTML，返回 DataFrame。
    自动从状态列中拆分 status / start_time / end_time
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tr.vxe-body--row")
    data = []

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 10:
            continue

        # ---------- 1. 优惠券名 / code / 店铺 ----------
        col_7 = tds[1]
        coupon_name_tag = col_7.select_one("span.no-word-break")
        coupon_name = coupon_name_tag.get_text(strip=True) if coupon_name_tag else ""

        coupon_code_tag = col_7.select_one(".mt5.flex.gray-c span")
        coupon_code = coupon_code_tag.get_text(strip=True) if coupon_code_tag else ""

        owner_tag = col_7.select_one(".mt5.f-color2 span")
        owner = owner_tag.get_text(strip=True) if owner_tag else ""

        # ---------- 2. 类型 / 范围 / 买家 ----------
        coupon_type = tds[2].get_text(strip=True)
        product_scope = tds[3].get_text(strip=True)
        target_buyer = tds[4].get_text(strip=True)

        # ---------- 3. 折扣金额 ----------
        discount_value = tds[5].get_text(" ", strip=True)

        # ---------- 4. 总数 / 已使用 ----------
        coupon_total = tds[6].get_text(strip=True)
        used_count = tds[7].get_text(strip=True)

        # ---------- 5. 状态 + 时间 ----------
        col_14 = tds[8]
        text = col_14.get_text(" ", strip=True)  # 将 div 和 span 都拼成空格
        # 例子: "进行中 2025-09-23 13:47 - 2025-12-25 14:47"
        status_match = re.match(r"(.*?)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", text)
        if status_match:
            status = status_match.group(1)
            start_time = status_match.group(2) + ":00"  # 自动补秒
            end_time = status_match.group(3) + ":00"
        else:
            status = text
            start_time = ""
            end_time = ""

        # ---------- 6. 编辑链接 ----------
        col_15 = tds[9]
        edit_a = col_15.find("a", href=True)
        edit_url = edit_a["href"] if edit_a else ""

        # ---------- 7. coupon_id / shop_id ----------
        coupon_id = row.get("rowid", "")
        shop_id = coupon_id  # 暂时用 rowid

        data.append({
            "coupon_id": coupon_id,
            "coupon_name": coupon_name,
            "coupon_code": coupon_code,
            "shop_name": owner,
            "activity_name": coupon_type,
            "product_scope": product_scope,
            "target_buyer": target_buyer,
            "discount_value": discount_value,
            "coupon_total": coupon_total,
            "used_count": used_count,
            "status": status,
            "start_time": start_time,
            "end_time": end_time,
            "shop_id": shop_id,
            "edit_url": edit_url
        })

    return pd.DataFrame(data)
def parse_discount_coupon_table2(html_text) -> pd.DataFrame:
    """
    解析 Shopee 优惠券表格 HTML，返回 DataFrame
    状态 / 开始时间 / 结束时间 使用 DOM 结构解析（不再依赖正则）
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tr.vxe-body--row")
    data = []

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 10:
            continue

        # ---------- 1. 优惠券名 / code / 店铺 ----------
        col_7 = tds[1]

        coupon_name_tag = col_7.select_one("span.no-word-break")
        coupon_name = coupon_name_tag.get_text(strip=True) if coupon_name_tag else ""

        coupon_code_tag = col_7.select_one(".mt5.flex.gray-c span")
        coupon_code = coupon_code_tag.get_text(strip=True) if coupon_code_tag else ""

        owner_tag = col_7.select_one(".mt5.f-color2 span")
        owner = owner_tag.get_text(strip=True) if owner_tag else ""

        # ---------- 2. 类型 / 范围 / 买家 ----------
        coupon_type = tds[2].get_text(strip=True)
        product_scope = tds[3].get_text(strip=True)
        target_buyer = tds[4].get_text(strip=True)

        # ---------- 3. 折扣金额 ----------
        discount_value = " ".join(tds[5].stripped_strings)

        # ---------- 4. 总数 / 已使用 ----------
        coupon_total = tds[6].get_text(strip=True)
        used_count = tds[7].get_text(strip=True)

        # ---------- 5. 状态 + 时间（关键修改点） ----------
        col_14 = tds[8]

        # 状态
        status_div = col_14.find("div")
        status = status_div.get_text(strip=True) if status_div else ""

        # 时间
        time_spans = col_14.find_all("span")
        if len(time_spans) >= 2:
            start_time = time_spans[0].get_text(strip=True) + ":00"
            end_time = time_spans[1].get_text(strip=True) + ":00"
        else:
            start_time = ""
            end_time = ""

        # ---------- 6. 编辑链接 ----------
        col_15 = tds[9]
        edit_a = col_15.find("a", href=True)
        edit_url = edit_a["href"] if edit_a else ""

        # ---------- 7. coupon_id / shop_id ----------
        # 优先从 edit 链接中取 id
        coupon_id = ""
        if edit_a and "id=" in edit_a["href"]:
            coupon_id = edit_a["href"].split("id=")[-1]
        else:
            coupon_id = row.get("rowid", "")

        shop_id = coupon_id  # 目前逻辑保持不变

        data.append({
            "coupon_id": coupon_id,
            "coupon_name": coupon_name,
            "coupon_code": coupon_code,
            "shop_name": owner,
            "activity_name": coupon_type,
            "product_scope": product_scope,
            "target_buyer": target_buyer,
            "discount_value": discount_value,
            "coupon_total": coupon_total,
            "used_count": used_count,
            "status": status,
            "start_time": start_time,
            "end_time": end_time,
            "shop_id": shop_id,
            "edit_url": edit_url
        })

    return pd.DataFrame(data)

def parse_discount_add_table_old(html_text)->pd.DataFrame:
    """
      解析活动表格 HTML，返回列表字典，每条字典包含活动信息。
      """
    soup = BeautifulSoup(html_text, "html.parser")
    result = []

    rows = soup.select("tbody tr.content")
    for row in rows:
        cells = row.find_all("td")

        # 活动 ID
        activity_id_tag = cells[1].select_one("p.gray-b span")
        activity_id = activity_id_tag.get_text(strip=True) if activity_id_tag else ""

        # 活动名称
        activity_name_tag = cells[1].select_one("p.limingcentUrlpic")
        activity_name = activity_name_tag.get_text(strip=True) if activity_name_tag else ""

        # 店铺账号
        shop_name = cells[2].get_text(strip=True)

        # 活动类型
        activity_type = cells[3].get_text(strip=True)

        # 主产品数量
        main_product_count = int(cells[4].get_text(strip=True))

        # 加购品/赠品数量
        gift_product_count = int(cells[5].get_text(strip=True))

        # 开始时间和结束时间
        start_time_tag = cells[6].select_one("p:nth-of-type(1)")
        start_time = start_time_tag.get_text(strip=True).replace("开始：", "") if start_time_tag else ""
        end_time_tag = cells[6].select_one("p:nth-of-type(2) span.endTimeText")
        end_time = end_time_tag.get_text(strip=True) if end_time_tag else ""

        # 状态
        status_tag = cells[7].select_one("span.start-status-color")
        status = status_tag.get_text(strip=True) if status_tag else ""

        # 编辑 URL
        edit_tag = cells[8].select_one("a[target='_blank']")
        edit_url = edit_tag.get("href") if edit_tag else ""

        result.append({
            "activity_id": activity_id,
            "activity_name": activity_name,
            "shop_name": shop_name,
            "activity_type": activity_type,
            "main_product_count": main_product_count,
            "gift_product_count": gift_product_count,
            "start_time": start_time + ":00",
            "end_time": end_time + ":00",
            "status": status,
            "edit_url": edit_url
        })
    return pd.DataFrame(result)

def parse_discount_add_table(html_text)->pd.DataFrame:
    """
    解析 vxe-table 活动列表，返回 DataFrame
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tr.shopee-product-table-row")

    data = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 9:
            continue

        # activity_id
        checkbox = cols[0].select_one("input[type='checkbox']")
        activity_id = checkbox.get("value", "") if checkbox else row.get("rowid", "")

        # activity_name & sub_id
        name_div = cols[1].select_one("div.mt5.no-word-break")
        activity_name = name_div.text.strip() if name_div else ""
        sub_id_div = cols[1].select_one("div.ml5.gray-c span")
        sub_id = sub_id_div.text.strip().replace("ID：", "") if sub_id_div else ""

        # shop_name
        shop_name = cols[2].text.strip()

        # activity_type
        activity_type = cols[3].text.strip()

        # main_product & gift_product
        main_product = cols[4].text.strip()
        gift_product = cols[5].text.strip()

        # start_time & end_time
        time_spans = cols[6].select("span")
        start_time = time_spans[0].text.strip() + ":00" if len(time_spans) >= 1 else ""
        end_time = time_spans[1].text.strip() + ":00" if len(time_spans) >= 2 else ""

        # status
        status_span = cols[7].select_one("span")
        status = status_span.text.strip() if status_span else ""

        # edit_link
        edit_a = cols[8].select_one("a")
        edit_link = edit_a.get("href", "") if edit_a else ""

        data.append({
            "activity_id": activity_id,
            "activity_name": activity_name,
            "sub_id": sub_id,
            "shop_name": shop_name,
            "activity_type": activity_type,
            "main_product": main_product,
            "gift_product": gift_product,
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "edit_link": edit_link
        })

    return pd.DataFrame(data)

def parse_sync_activity(html_text):
    results = []

    # 按 <br> 分割
    lines = html_text.replace("\n", "").split("<br>")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        count_match = re.search(r"共同步成功(\d+)个活动", line)
        count = int(count_match.group(1)) if count_match else None

        status = "success" if "同步完成" in line else "failed"
        prefix = line.split(",")[0]  # 例如：文布逊-印尼1-母婴
        m_full = re.match(r"(.+?)-?(印尼\d+(?:\(.+?\))?)(?:-[^,]*)?", prefix)

        if m_full:
            shop_name = m_full.group(2)
            person = m_full.group(1)
        else:
            # 情况B：只有“印尼xxx”
            shop_name = prefix
            person = ""
        results.append([shop_name, person, status, count])

    return results



import re
from html import unescape

def parse_sync_failed(html_text):
    """
    解析同步失败详情，返回二维数组：
    [
        [product_id, sku, reason],
        ...
    ]
    若某项缺失，用 "" 占位，确保不报错不崩溃。
    """
    results = []
    text = unescape(html_text.replace("\n", ""))

    blocks = text.split("<br>")
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # product_id
        m_id = re.search(r"id=(\d+)", block)
        product_id = m_id.group(1) if m_id else ""

        # sku
        m_sku = re.search(r">([\w\-]+)</a>", block)
        sku = m_sku.group(1) if m_sku else ""

        # reason
        m_reason = re.search(r"</a>，\s*失败原因：\"?.*?:?(.*)", block)
        reason = m_reason.group(1).strip() if m_reason else ""
        reason = reason.rstrip(";；")

        if not (product_id or sku or reason):
            continue

        results.append([product_id, sku, reason])

    return results


def parse_reports_getIncomePayData(html: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")

    data_list = []
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 2:
            continue

        shop_td = tds[1]
        full_shop_name = shop_td.text.strip()
        parts = full_shop_name.split("-")

        data = {}
        data["姓名"] = parts[0] if len(parts) > 0 else ""
        data["店铺"] = full_shop_name
        data["店铺名称"] = parts[1] if len(parts) > 1 else ""
        data["类型"] = parts[2] if len(parts) > 2 else ""

        # totalNum → 订单数
        total_num_td = row.find("td", {"data-field": "totalNum"})
        incomeTotal_td = row.find("td", {"data-field": "incomeTotal"})

        data["订单数"] = total_num_td.text.strip() if total_num_td else ""
        data["收入合计"] = incomeTotal_td.text.strip() if incomeTotal_td else ""

        # incomeTotal → 合计
        income_total_td = row.find("td", {"data-field": "incomeTotal"})
        data["合计"] = income_total_td.text.strip() if income_total_td else ""
        data_list.append(data)
    return data_list


def parse_warehouse_table(html: str):
    """
    解析马帮 ERP 仓库表格 HTML，返回仓库信息列表
    :param html: 仓库列表 HTML 字符串
    :return: list[dict]
    """
    soup = BeautifulSoup(html, "html.parser")
    warehouses = []

    for tr in soup.find_all("tr"):
        # 仓库ID
        checkbox = tr.find("input", class_="checkWarehouse")
        if not checkbox or not checkbox.get("data-id"):
            continue  # 空行跳过

        data = {}
        data["id"] = checkbox["data-id"]

        # 仓库名称
        name_tag = tr.find("a", class_="semibold")
        data["name"] = name_tag.text.strip() if name_tag else None

        # 仓库类型（表格第3列 text-center）
        tds_center = tr.find_all("td", class_="text-center")
        data["type"] = tds_center[0].text.strip() if len(tds_center) > 0 else None

        # 库存
        stock_tag = tr.find("span", class_="totalStock")
        stock_text = stock_tag.text.replace(",", "").strip() if stock_tag else "0"
        data["totalStock"] = int(stock_text) if stock_text.isdigit() else 0

        # 总货值
        value_tag = tr.select_one("td .totalInventoryValue")
        value_text = value_tag.text.replace(",", "").strip() if value_tag else "0"
        try:
            data["totalInventoryValue"] = float(value_text)
        except ValueError:
            data["totalInventoryValue"] = 0.0

        # 在途货值
        transit_tag = tr.select_one("td .totalInTransitValue")
        transit_text = transit_tag.text.replace(",", "").strip() if transit_tag else "0"
        try:
            data["totalInTransitValue"] = float(transit_text)
        except ValueError:
            data["totalInTransitValue"] = 0.0

        # 状态
        status_tag = tr.find("td", class_="text-success")
        data["status"] = status_tag.text.strip() if status_tag else None

        warehouses.append(data)

    return warehouses


def parse_table_stock_getStorageInOutLog(html):
    """
    解析库存历史信息表格
    :param html:
    :return:
    """
    soup = BeautifulSoup(html, "html.parser")
    data = []
    for tr in soup.find_all("tr"):
        row = []
        for td in tr.find_all("td"):
            # 提取单元格内所有文本，空格连接，去除多余空白
            text = ' '.join(td.stripped_strings)
            row.append(text)
        data.append(row)
    return data

def parse_table_stock_getOrderItemRecordfh(message_html: str):
    soup = BeautifulSoup(message_html, "lxml")
    rows = []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 13:
            continue

        row = {
            "shop_owner": tds[0].text.strip(),
            "account": tds[1].text.strip(),
            "order_id": tds[2].text.strip(),
            "country": tds[3].text.strip(),
            "Item": tds[4].text.strip(),
            "buy_qty": int(tds[5].text.strip()),
            "order_amount": float(tds[6].text.strip().replace(',','')),
            "shipping_fee": float(tds[7].text.strip().replace(',','')),
            "order_time": tds[8].text.strip(),
            "ship_time": tds[9].text.strip(),
            "weight": float(tds[10].text.strip().replace(',','')),
            "receiver": tds[11].text.strip(),
            "status": tds[12].text.strip(),
        }
        rows.append(row)

    return rows


def parse_table_reports_countryReports(html):
    """
    解析销量报表（按照国家地区）
    :param html:
    :return:
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", class_="view-select")
    records = []

    for row in rows:
        record = {}
        tds = row.find_all("td")
        for td in tds:
            key = td.get("data-field")
            if key:
                record[key] = td.get_text(strip=True)
        if len(tds) > 1:
            product_td = tds[1]
            img_tag = product_td.find("img")
            record["img_src"] = img_tag["src"] if img_tag else None
            stock_tag = product_td.find("a", class_="viewicon")
            if stock_tag:
                record["stock_id"] = stock_tag.get("data-stock-id")
                record["stock_code"] = stock_tag.get_text(strip=True)
            title_tag = product_td.find("p", class_="ellipsis")
            if title_tag:
                record["product_title"] = title_tag.get("title")
        records.append(record)
    return records

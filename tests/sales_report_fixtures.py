"""Synthetic fixtures for the observed classic table/CSV schema (no live data)."""
import csv
from html import escape
from io import StringIO

from backend.core.mabang_client import MabangExportFile
from backend.services.group_sales_report import TARGET_SALES_HEADERS


def sales_row(sku="SYNTH-001", quantity="1", income="10", **changes):
    row = dict.fromkeys(TARGET_SALES_HEADERS, "0")
    row.update({"SKU": sku, "商品中文名": "合成商品", "商品英文名": "Synthetic", "item_id": "0000123",
                "NCM": "NCM", "主SKU": sku, "状态": "正常", "创建时间": "2026-01-01", "销售员": "销售甲",
                "开发员": "开发甲", "销售数量": str(quantity), "关联订单数": str(quantity),
                "收入-订单金额": str(income), "收入-合计": str(income), "统一成本价": "3",
                "重量(单位:g)": "100", "毛利": "2", "毛利率%": "20", "平均销售价格": ""})
    row.update(changes)
    return row


def sales_csv(rows, *, classic_layout=False):
    header = list(TARGET_SALES_HEADERS)
    if classic_layout:
        header.remove("平均销售价格")
        header.insert(header.index("退款金额"), "退款数量")
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(c, "") for c in header if c != "退款数量"])
    return stream.getvalue().encode("utf-8-sig")


def table_row(row):
    def td(field, value):
        return f'<td data-field="{field}">{escape(str(value))}</td>'
    sku = escape(row["SKU"])
    text = f'<tr><td>1</td><td><a data-stock-id="{sku}">{sku}</a><p title="{escape(row["商品中文名"])}"></p><p title="{escape(row["商品英文名"])}"></p></td>'
    for field, column in [("salesSkuNewId", "主SKU"), ("status", "状态"), ("defaultCost", "统一成本价"),
                          ("weight", "重量(单位:g)"), ("stockQuantity", "库存"), ("timeCreated", "创建时间"),
                          ("sale", "销售员"), ("develop", "开发员"), ("orderNum", "关联订单数"), ("quantity", "销售数量")]:
        text += td(field, row[column])
    for col in ("收入-订单金额", "收入-运费", "收入-其他收入", "收入-补贴金额", "收入-合计"):
        text += td("income", row[col])
    for col in ("支出-商品成本", "支出-运费", "支出-包材费", "支出-平台费", "支出-转帐费", "支出-头程费", "支出-FBA费用", "支出-VAT税费", "支出-优惠券", "支出-其他费用", "支出-其他支出", "支出-测评费", "支出-合计"):
        text += td("vatTaxation" if col == "支出-VAT税费" else "expenditure", row[col])
    for field, col in [("refundNum", "退款订单数"), ("itemRefundNum", "退款商品数"), ("refundMoney", "退款金额"),
                       ("returnRate", "退款率%"), ("gross", "毛利"), ("grossRate", "毛利率%")]:
        text += td(field, row[col])
    return text + "</tr>"


def page_payload(rows, *, total=None, snapshot="2026-11-27", echo=None):
    payload = {"success": True, "tableContent": "".join(table_row(r) for r in rows),
               "pageHtml": f"共 {len(rows) if total is None else total} 条", "updateDateStr": snapshot}
    if echo is not None:
        payload["postdata"] = echo
    return payload


class ClassicClient:
    def __init__(self, rows, *, csv_content=None):
        self.rows = rows
        self.content = sales_csv(rows) if csv_content is None else csv_content
        self.export_calls = 0
        self.total_queries = 0
        self.calls = []

    def uses_modern_sales_report(self):
        return False

    def download_product_sales_report_csv(self, **kwargs):
        self.export_calls += 1
        return MabangExportFile("synthetic.csv", "csv", "", self.content)

    def _post_sales_report(self, *, form_data, **kwargs):
        self.total_queries += 1
        self.calls.append(form_data)
        form = dict(form_data)
        page, size = int(form.get("page", 1)), int(form.get("rowsPerPage", 2000))
        return page_payload(self.rows[(page-1)*size:page*size], total=len(self.rows))

    def post_form_json(self, *args, **kwargs):
        return self._post_sales_report(**kwargs)

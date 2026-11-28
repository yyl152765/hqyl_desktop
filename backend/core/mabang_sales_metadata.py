"""Product-master NCM enrichment for the MAS sales DTO, which returns null NCM.

Uses the stock page's native, read-only 500-SKU batch contract. It does not
read legacy sales reports or replace any sales, cost, refund or profit value.
"""
from __future__ import annotations

from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError

STOCK_PAGE = "/index.php?mod=stock.list&searchStatus=3"
STOCK_ORIGIN = "https://private-amz.mabangerp.com"
STOCK_QUERY = STOCK_ORIGIN + "/index.php?mod=stock.getStockList"
BATCH_SIZE = 500


def _open_stock_page(client) -> str:
    try:
        outer = client.client.get(client._url(STOCK_PAGE))
        if outer.status_code != 200:
            raise MabangApiError(f"商品资料入口被拒绝: HTTP={outer.status_code}")
        soup = BeautifulSoup(outer.text, "html.parser")
        frame = soup.find("iframe", id="iframeContent") or soup.find("iframe", src=True)
        target = urljoin(str(outer.url), frame.get("src", "") if frame else "")
        parsed = urlsplit(target)
        if (parsed.scheme != "https" or parsed.hostname != "private-amz.mabangerp.com"
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.path != "/index.php"):
            raise MabangApiError("商品资料入口不符合已验证的地址契约")
        response = client.client.get(target, headers={"Referer": str(outer.url)})
        if response.status_code != 200 or 'name="stockData"' not in response.text:
            raise MabangApiError("商品资料批量查询入口不可用")
        # Never retain the SSO query in diagnostics or public metadata.
        return STOCK_ORIGIN + STOCK_PAGE
    except httpx.HTTPError:
        raise MabangApiError("商品资料入口网络异常") from None
    except ValueError:
        raise MabangApiError("商品资料入口地址无效") from None


def enrich_ncm(client, rows: list[dict]) -> None:
    needed = {str(row["stockSku"]): row["stockid"] for row in rows if row.get("ncm") is None and row.get("stockid", 0) > 0}
    if any(row.get("stockid", 0) > 0 and row["stockSku"] in needed
           and row["stockid"] != needed[row["stockSku"]] for row in rows):
        raise MabangApiError("商品资料同 SKU 对应多个商品 ID，不能猜选")
    if not needed:
        return
    if any(not sku or "\n" in sku or "\r" in sku for sku in needed):
        raise MabangApiError("商品资料 SKU 无法用于批量查询")
    referer = getattr(client, "_sales_stock_referer", None)
    if referer is None:
        referer = _open_stock_page(client)
        client._sales_stock_referer = referer
    metadata = {}
    skus = list(needed)
    for offset in range(0, len(skus), BATCH_SIZE):
        batch = skus[offset:offset + BATCH_SIZE]
        form = {"searchKey": "Stock_stockSku", "operate": "=", "searchValue": "", "status": "",
                "isBatchSearch": "1", "selecttype": "stockSku", "stockData": "\n".join(batch),
                "page": "1", "rowsPerPage": "500", "warehouseMold": "all", "stockOrderby": ""}
        try:
            response = client.client.post(STOCK_QUERY, content=urlencode(form), headers={
                "Referer": referer, "Origin": STOCK_ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            })
        except httpx.HTTPError:
            raise MabangApiError("商品资料批量查询网络异常") from None
        if response.status_code != 200:
            raise MabangApiError(f"商品资料批量查询被拒绝: HTTP={response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise MabangApiError("商品资料批量查询返回非 JSON") from None
        if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("stockData"), list):
            raise MabangApiError("商品资料批量查询结构无效或无权限")
        for product in payload["stockData"]:
            sku = str(product.get("stockSku") or "")
            if sku not in batch or str(product.get("id")) != str(needed[sku]):
                raise MabangApiError("商品资料 SKU/商品 ID 不匹配，不能猜选")
            if sku in metadata or "stockNcm" not in product:
                raise MabangApiError("商品资料重复或缺少 NCM 字段")
            metadata[sku] = product["stockNcm"]
        if any(sku not in metadata for sku in batch):
            raise MabangApiError("商品资料批量结果不完整，不能生成报表")
    for row in rows:
        if row["stockSku"] in metadata and row.get("stockid") == needed[row["stockSku"]] and row.get("ncm") is None:
            row["ncm"] = metadata[row["stockSku"]]

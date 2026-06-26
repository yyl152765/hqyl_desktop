"""mabang_order_client 的离线单元测试。"""

from __future__ import annotations

import unittest
from typing import Any

import httpx

from backend.core.mabang_client import MabangApiError, MabangClient
from backend.core.mabang_order_client import (
    OrderRecord,
    build_output_record,
    fetch_order_items,
    fetch_orders_with_retry,
    is_sample_order,
    list_order_shops,
)


def _html_response(url: str, html: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=html.encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", url),
    )


def _json_response(url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


class FakeOrderHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        html = """
        <html>
          <script>var orderPageKey = 'page-key-123';</script>
          <ul><li class="active" data-id="333">全部订单</li></ul>
          <label>
            <input name="shopIdMultiple[]" value="101" data-platform-id="TK" data-platform-name="TikTok">
            <span class="text">TK泰国本土001-店铺A</span>
          </label>
          <label>
            <input name="shopIdMultiple[]" value="102">
            <span class="text">TK泰国本土002-店铺B</span>
          </label>
        </html>
        """
        return _html_response(url, html)

    def post(self, url: str, data: Any = None, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        del kwargs
        self.posts.append({"url": url, "data": data, "headers": headers or {}})
        if "order.oTc" in url:
            return _json_response(
                url,
                {
                    "success": True,
                    "rowsPerPage": 500,
                    "orderDataList": [
                        {
                            "id": "1",
                            "platformOrderId": "PO-1",
                            "shopId": "101",
                            "shopIdText": "TK泰国本土001-店铺A",
                            "paidTime": "2026-06-24 10:20:30",
                            "createDate": "2026-06-24 09:00:00",
                            "order_label": "样品订单, 已审核",
                            "tableBase": "2",
                            "orderByItemForAry": "id asc,stockId asc",
                            "orderWeight": "1200.0000",
                        },
                        {
                            "id": "2",
                            "platformOrderId": "PO-2",
                            "shopId": "101",
                            "shopIdText": "TK泰国本土001-店铺A",
                            "paidTime": "2026-06-24 11:20:30",
                            "createDate": "2026-06-24 10:00:00",
                            "order_label": "普通订单",
                            "tableBase": "2",
                            "orderByItemForAry": "id asc,stockId asc",
                        },
                    ],
                },
            )
        if "order.showOrderItems" in url:
            return _json_response(
                url,
                {
                    "success": True,
                    "order_list_html_header": {
                        "1": """
                        <tr>
                          <td><a class="SkuNumber">SKU-1</a></td>
                          <td><span data-field="productName" title="测试商品A">ignored</span></td>
                          <td data-field="sellPrice"><p>12.3000</p></td>
                        </tr>
                        """,
                    },
                },
            )
        raise AssertionError(f"unexpected url: {url}")


class MabangOrderClientTests(unittest.TestCase):
    def _client(self, fake_http: FakeOrderHttpClient | None = None) -> MabangClient:
        client = MabangClient("https://example.private.mabangerp.com", client=fake_http or FakeOrderHttpClient())
        client._logged_in = True
        return client

    def test_list_order_shops_uses_desktop_client_attribute(self):
        client = self._client()

        shops = list_order_shops(client)

        self.assertEqual([(s.shop_id, s.shop_name) for s in shops], [
            ("101", "TK泰国本土001-店铺A"),
            ("102", "TK泰国本土002-店铺B"),
        ])
        self.assertEqual(client._order_page_key, "page-key-123")
        self.assertEqual(client._order_tab_id, "333")

    def test_fetch_orders_uses_real_mabang_payload_and_parses_rows(self):
        fake = FakeOrderHttpClient()
        client = self._client(fake)

        orders = fetch_orders_with_retry(
            client,
            ["101"],
            "2026-06-24 00:00:00",
            "2026-06-24 23:59:59",
        )

        self.assertEqual(len(orders), 2)
        self.assertTrue(is_sample_order(orders[0]))
        query_post = next(post for post in fake.posts if "order.oTc" in post["url"])
        self.assertEqual(query_post["data"]["Order.shops[]"], ["101"])
        self.assertEqual(query_post["data"]["shopIdMultiple[]"], ["101"])
        self.assertEqual(query_post["data"]["queryTime"], "paidTime")
        self.assertEqual(query_post["data"]["startTime1"], "2026-06-24 00:00:00")
        self.assertEqual(query_post["data"]["endTime1"], "2026-06-24 23:59:59")
        self.assertEqual(query_post["data"]["orderPageKey"], "page-key-123")
        self.assertEqual(query_post["data"]["tabId"], "333")

    def test_fetch_order_items_batches_by_order_item_iq_and_maps_by_order_id(self):
        fake = FakeOrderHttpClient()
        client = self._client(fake)
        client._order_page_key = "page-key-123"
        client._order_tab_id = "333"
        order = OrderRecord(
            order_id="1",
            platform_order_id="PO-1",
            shop_name="TK泰国本土001-店铺A",
            pay_time="2026-06-24 10:20:30",
            create_time="2026-06-24 09:00:00",
            table_base="2",
            order_by_item_for_ary="id asc,stockId asc",
            order_weight="1200",
        )

        items = fetch_order_items(client, [order])

        self.assertEqual(len(items), 1)
        output = build_output_record(items[0])
        self.assertEqual(output["订单编号"], "PO-1")
        self.assertEqual(output["SKU"], "SKU-1")
        self.assertEqual(output["商品总成本"], "12.3")
        self.assertEqual(output["重量"], "1200")
        detail_post = next(post for post in fake.posts if "order.showOrderItems" in post["url"])
        self.assertEqual(detail_post["data"]["orderItemIq"], "1")
        self.assertEqual(detail_post["data"]["tableBase"], "2")
        self.assertEqual(detail_post["data"]["orderItemBy"], "id asc,stockId asc")
        self.assertEqual(detail_post["data"]["tabId"], "333")

    def test_fetch_orders_raises_after_degrade_paths_exhausted(self):
        class FailingHttpClient(FakeOrderHttpClient):
            def post(self, url: str, data: Any = None, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
                if "order.oTc" in url:
                    return _json_response(url, {"success": False, "message": "boom"})
                return super().post(url, data=data, headers=headers, **kwargs)

        client = self._client(FailingHttpClient())

        with self.assertRaises(MabangApiError):
            fetch_orders_with_retry(
                client,
                ["101"],
                "2026-06-24 00:00:00",
                "2026-06-24 00:30:00",
                page_size=100,
                max_retries=0,
            )


if __name__ == "__main__":
    unittest.main()

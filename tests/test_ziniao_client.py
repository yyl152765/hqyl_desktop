from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core.ziniao_client import ZiniaoClient, ZiniaoCredentials


class ZiniaoClientTests(unittest.TestCase):
    def make_client(self) -> ZiniaoClient:
        return ZiniaoClient(
            ZiniaoCredentials("company", "username", "password"),
            Path("ziniao.exe"),
            port=16851,
        )

    def test_authenticated_actions_include_full_credentials(self) -> None:
        response = Mock()
        response.json.return_value = {"statusCode": 0, "browserList": []}
        with patch("backend.core.ziniao_client.requests.post", return_value=response) as post:
            result = self.make_client().request("getBrowserList")

        self.assertEqual(result["statusCode"], 0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["company"], "company")
        self.assertEqual(payload["username"], "username")
        self.assertEqual(payload["password"], "password")

    def test_unauthenticated_action_does_not_include_credentials(self) -> None:
        response = Mock()
        response.json.return_value = {"statusCode": 0, "browsers": []}
        with patch("backend.core.ziniao_client.requests.post", return_value=response) as post:
            self.make_client().request("getRunningInfo")

        payload = post.call_args.kwargs["json"]
        self.assertNotIn("password", payload)
        self.assertNotIn("username", payload)

    def test_open_browser_creates_download_directory_and_returns_session(self) -> None:
        client = self.make_client()
        client.request = Mock(
            return_value={
                "statusCode": 0,
                "debuggingPort": 9222,
                "launcherPage": "https://banhang.shopee.vn/",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir) / "downloads"
            session = client.open_browser(
                {"browserOauth": "opaque", "browserName": "越南店铺"},
                download_dir,
            )

            self.assertTrue(download_dir.is_dir())
            self.assertEqual(session.debugging_port, 9222)
            self.assertEqual(session.browser_name, "越南店铺")
            self.assertEqual(client.request.call_args.kwargs["forceDownloadPath"], str(download_dir.resolve()))


if __name__ == "__main__":
    unittest.main()

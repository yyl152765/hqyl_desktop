from __future__ import annotations

import hashlib
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

import httpx

from backend.update_service import (
    _version_tuple,
    calculate_sha256,
    check_update_manifest,
    download_update_package,
    result_payload,
)
from backend.update_sources import DEFAULT_UPDATE_MANIFEST_URL, DOMAIN_UPDATE_MANIFEST_URL


class UpdateServiceTests(unittest.TestCase):
    def test_version_tuple_compares_numeric_parts(self) -> None:
        self.assertGreater(_version_tuple("0.2.10"), _version_tuple("0.2.9"))
        self.assertGreater(_version_tuple("1.0.0"), _version_tuple("0.9.9"))

    def test_manifest_includes_package_hash(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "version": "0.2.2",
            "package_url": "https://example.com/setup.exe",
            "sha256": "ab" * 32,
        }
        with patch("backend.update_service.httpx.get", return_value=response):
            result = check_update_manifest("https://example.com/latest.json", "0.2.1")
        self.assertTrue(result.update_available)
        self.assertEqual(result.sha256, ("ab" * 32).upper())

    def test_remote_mandatory_flag_cannot_require_an_update(self) -> None:
        for mandatory in (True, False, "true", 1, None):
            with self.subTest(mandatory=mandatory):
                response = Mock()
                response.json.return_value = {
                    "version": "0.2.2",
                    "mandatory": mandatory,
                    "package_url": "https://example.com/setup.exe",
                    "sha256": "ab" * 32,
                }
                with patch("backend.update_service.httpx.get", return_value=response):
                    result = check_update_manifest("https://example.com/latest.json", "0.2.1")
                payload = result_payload(result)
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["update_available"])
                self.assertFalse(payload["mandatory"])
                self.assertEqual(payload["package_url"], "https://example.com/setup.exe")

    def test_update_server_failure_returns_an_optional_error(self) -> None:
        with patch("backend.update_service.httpx.get", side_effect=httpx.ConnectError("offline")):
            result = check_update_manifest("https://example.com/latest.json", "0.2.1")
        payload = result_payload(result)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["mandatory"])
        self.assertEqual(payload["current_version"], "0.2.1")
        self.assertEqual(payload["error"], "offline")

    def test_existing_official_manifest_downloads_from_the_server_that_answered(self) -> None:
        version = "1.0.1"
        installer = f"HQYLAutomationSetup_{version}.exe"
        current_package = DEFAULT_UPDATE_MANIFEST_URL.replace("latest.json", installer)
        old_package = DOMAIN_UPDATE_MANIFEST_URL.replace("latest.json", installer)
        for primary, failed, expected_source, advertised, expected_url in (
            (DEFAULT_UPDATE_MANIFEST_URL, None, DEFAULT_UPDATE_MANIFEST_URL, old_package, current_package),
            (DOMAIN_UPDATE_MANIFEST_URL, 502, DEFAULT_UPDATE_MANIFEST_URL, old_package, current_package),
            (DEFAULT_UPDATE_MANIFEST_URL, "offline", DOMAIN_UPDATE_MANIFEST_URL, current_package, old_package),
        ):
            with self.subTest(primary=primary, failure=failed):
                seen = []

                def get(url, **kwargs):
                    seen.append(url)
                    request = httpx.Request("GET", url)
                    if url == primary and failed == "offline":
                        raise httpx.ConnectError("offline", request=request)
                    if len(seen) == 1 and isinstance(failed, int):
                        return httpx.Response(failed, request=request)
                    return httpx.Response(200, request=request, json={
                        "version": version, "package_url": advertised, "sha256": "ab" * 32,
                    })

                with patch("backend.update_service.httpx.get", side_effect=get):
                    result = check_update_manifest(primary, "1.0.0")
                self.assertFalse(result.error)
                expected_requests = [primary]
                if failed == "offline":
                    expected_requests.append(primary)
                if failed:
                    expected_requests.append(expected_source)
                self.assertEqual(seen, expected_requests)
                self.assertEqual(result.package_url, expected_url)
                self.assertEqual(result.sha256, "AB" * 32)

    def test_custom_sources_and_package_urls_stay_as_published(self) -> None:
        custom = "https://mirror.example/latest.json"
        official_package = DOMAIN_UPDATE_MANIFEST_URL.replace("latest.json", "HQYLAutomationSetup_1.0.1.exe")
        for source, package in (
            (custom, official_package),
            (DEFAULT_UPDATE_MANIFEST_URL, "https://download.example/releases/setup.exe?signature=example"),
            (DEFAULT_UPDATE_MANIFEST_URL, official_package + "?channel=custom"),
            (DEFAULT_UPDATE_MANIFEST_URL, official_package.replace("1.0.1", "1.0.0")),
        ):
            with self.subTest(source=source, package=package):
                response = httpx.Response(200, request=httpx.Request("GET", source),
                                          json={"version": "1.0.1", "package_url": package})
                with patch("backend.update_service.httpx.get", return_value=response) as get:
                    result = check_update_manifest(source, "1.0.0")
                self.assertFalse(result.error)
                get.assert_called_once()
                self.assertEqual(result.package_url, package)

    def test_bad_manifest_and_custom_gateway_errors_do_not_switch_sources(self) -> None:
        for source, status, payload in (
            (DEFAULT_UPDATE_MANIFEST_URL, 200, {}),
            (DEFAULT_UPDATE_MANIFEST_URL, 404, {}),
            ("https://mirror.example/latest.json", 502, {}),
        ):
            with self.subTest(source=source, status=status):
                response = httpx.Response(status, request=httpx.Request("GET", source), json=payload)
                with patch("backend.update_service.httpx.get", return_value=response) as get:
                    result = check_update_manifest(source, "1.0.0")
                self.assertTrue(result.error)
                self.assertFalse(result.update_available)
                get.assert_called_once()

    def test_both_official_sources_unavailable_return_optional_error(self) -> None:
        with patch("backend.update_service.httpx.get", side_effect=httpx.ConnectError("offline")) as get:
            result = check_update_manifest(DEFAULT_UPDATE_MANIFEST_URL, "1.0.1")
        self.assertEqual([call.kwargs["trust_env"] for call in get.call_args_list], [False, True, True])
        self.assertIn("两个官方更新地址", result.error)
        self.assertFalse(result.mandatory)

    def test_origin_download_can_restart_through_system_proxy_after_interrupted_transfer(self) -> None:
        url = DEFAULT_UPDATE_MANIFEST_URL.replace("latest.json", "HQYLAutomationSetup_1.0.1.exe")
        content = b"complete verified installer"
        request = httpx.Request("GET", url)
        routes = []

        def interrupted():
            yield b"x" * (256 * 1024)
            raise httpx.ReadError("connection closed", request=request)

        @contextmanager
        def stream(method, target, *, trust_env, **kwargs):
            self.assertEqual((method, target), ("GET", url))
            routes.append(trust_env)
            response = httpx.Response(200, request=request, content=content if trust_env else interrupted())
            try:
                yield response
            finally:
                response.close()

        with TemporaryDirectory() as temp_dir, patch("backend.update_service.httpx.stream", side_effect=stream):
            path = download_update_package(url, "1.0.1", hashlib.sha256(content).hexdigest(), Path(temp_dir), lambda _: None)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(list(Path(temp_dir).iterdir()), [path])
        self.assertEqual(routes, [False, True])

    def test_origin_checksum_failure_does_not_retry_or_leave_an_installer(self) -> None:
        url = DEFAULT_UPDATE_MANIFEST_URL.replace("latest.json", "HQYLAutomationSetup_1.0.1.exe")
        context = MagicMock()
        context.__enter__.return_value = httpx.Response(200, request=httpx.Request("GET", url), content=b"wrong")
        with TemporaryDirectory() as temp_dir, patch("backend.update_service.httpx.stream", return_value=context) as stream:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                download_update_package(url, "1.0.1", "AB" * 32, Path(temp_dir), lambda _: None)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])
            stream.assert_called_once()

    def test_calculate_sha256(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "package.exe"
            path.write_bytes(b"hqyl-update")
            self.assertEqual(
                calculate_sha256(path),
                "576EC08486F4BCFD449D8C30EC1043D651468E3C75F453BEC4F85E2AF1F27F2A",
            )

    def test_download_update_package_verifies_hash(self) -> None:
        content = b"valid-installer"
        response = httpx.Response(
            200,
            content=content,
            headers={"content-length": str(len(content))},
            request=httpx.Request("GET", "https://example.com/setup.exe"),
        )
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with TemporaryDirectory() as temp_dir, patch("backend.update_service.httpx.stream", return_value=context) as stream:
            expected = "11C22243629AC48E6B70ADF316DBABE4C01E313BD957C4DC5013ABECF5D6A8B4"
            path = download_update_package(
                "https://example.com/setup.exe",
                "0.2.2",
                expected,
                Path(temp_dir),
                lambda _message: None,
            )
            self.assertTrue(path.exists())
            self.assertEqual(calculate_sha256(path), expected)
            self.assertTrue(stream.call_args.kwargs["trust_env"])


if __name__ == "__main__":
    unittest.main()

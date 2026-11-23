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
from backend.update_sources import DEFAULT_UPDATE_MANIFEST_URL, DOMAIN_UPDATE_MANIFEST_URL, LEGACY_MANIFEST_URLS, installer_object_url


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

    def test_legacy_sources_use_shared_feed_and_preserve_signed_download(self):
        signed = installer_object_url("1.0.3") + "?signature=fixture"
        device = DEFAULT_UPDATE_MANIFEST_URL.replace("https://", "https://device:fixture@")
        for source in (*LEGACY_MANIFEST_URLS, device, DEFAULT_UPDATE_MANIFEST_URL):
            with self.subTest(source=source):
                response = httpx.Response(200, request=httpx.Request("GET", DEFAULT_UPDATE_MANIFEST_URL),
                    json={"version": "1.0.3", "package_url": signed, "sha256": "ab" * 32})
                with patch("backend.update_service.httpx.get", return_value=response) as get:
                    result = check_update_manifest(source, "1.0.2")
                self.assertEqual(get.call_args.args, (DEFAULT_UPDATE_MANIFEST_URL,))
                self.assertFalse(get.call_args.kwargs["trust_env"])
                self.assertEqual(result.package_url, signed)

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
        self.assertEqual([call.kwargs["trust_env"] for call in get.call_args_list], [False, True])
        self.assertEqual(result.error, "offline")
        self.assertFalse(result.mandatory)

    def test_origin_download_can_restart_through_system_proxy_after_interrupted_transfer(self) -> None:
        url = installer_object_url("1.0.1") + "?signature=fixture"
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
        url = installer_object_url("1.0.1") + "?signature=fixture"
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

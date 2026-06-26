from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

import httpx

from backend.update_service import (
    _version_tuple,
    calculate_sha256,
    check_update_manifest,
    download_update_package,
)


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
        with TemporaryDirectory() as temp_dir, patch("backend.update_service.httpx.stream", return_value=context):
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


if __name__ == "__main__":
    unittest.main()

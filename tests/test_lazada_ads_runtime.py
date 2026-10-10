"""Native Lazada startup must not require the legacy sibling source project."""
from __future__ import annotations

import importlib
import logging
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.core.ziniao_client import ZiniaoClient, ZiniaoClientError
from backend.services import lazada_withdrawal_statistics as withdrawal
from backend.services.lazada_ads_data import LazadaAdsBrowserRuntime


class LazadaAdsNativeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.installed_client = self.root / "installed" / "ziniao.exe"
        self.installed_client.parent.mkdir()
        self.installed_client.touch()

        self.stack.enter_context(patch.dict(os.environ, {
            "ZINIAO_CLIENT_PATH": str(self.installed_client),
            "SUPERBROWSER_PROCESS_ROOT": str(self.root / "missing-source"),
            "ProgramFiles": str(self.root / "program-files"),
            "ProgramFiles(x86)": str(self.root / "program-files-x86"),
            "LOCALAPPDATA": str(self.root / "local-app-data"),
            "APPDATA": str(self.root / "app-data"),
            "PROGRAMDATA": str(self.root / "program-data"),
        }))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root / "home"))
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir

        def fixture_file_only(path: Path) -> bool:
            # Discovery may propose system defaults, but tests never inspect them.
            try:
                path.relative_to(self.root)
            except ValueError:
                return False
            return original_is_file(path)

        self.stack.enter_context(patch.object(Path, "is_file", autospec=True, side_effect=fixture_file_only))

        def fixture_directory_only(path: Path) -> bool:
            try:
                path.relative_to(self.root)
            except ValueError:
                return False
            return original_is_dir(path)

        self.stack.enter_context(patch.object(Path, "is_dir", autospec=True, side_effect=fixture_directory_only))
        # Keep the pre-fix regression runnable before the standalone resolver is
        # introduced. Once present, prohibit consulting the real Windows registry.
        try:
            paths = importlib.import_module("backend.core.ziniao_paths")
        except ModuleNotFoundError as exc:
            if exc.name != "backend.core.ziniao_paths":
                raise
        else:
            self.stack.enter_context(patch.object(paths, "_registry_client_paths", return_value=[]))
        self.legacy = self.stack.enter_context(patch.object(
            withdrawal, "_reference_modules",
            side_effect=AssertionError("Lazada native startup must not load superbrowser_process"),
        ))
        self.playwright = self.stack.enter_context(patch("playwright.sync_api.sync_playwright"))

    def runtime(self, client_path: str = "") -> LazadaAdsBrowserRuntime:
        job = SimpleNamespace(
            client_path=client_path, company="fixture-company", username="fixture-user",
            password="fixture-password", socket_port=16851,
        )
        return LazadaAdsBrowserRuntime(job, Mock(spec=logging.Logger))

    def assert_starts_native_client(self, runtime: LazadaAdsBrowserRuntime, expected: Path) -> None:
        with patch.object(ZiniaoClient, "ensure_started", autospec=True) as ensure_started:
            runtime.start()
        self.assertIs(type(runtime.client), ZiniaoClient)
        self.assertEqual(runtime.client.client_path, expected)
        self.assertEqual(runtime.client.credentials.company, "fixture-company")
        self.assertEqual(runtime.client.credentials.username, "fixture-user")
        self.assertEqual(runtime.client.credentials.password, "fixture-password")
        ensure_started.assert_called_once_with(runtime.client)
        self.playwright.assert_called_once_with()
        self.playwright.return_value.start.assert_called_once_with()
        self.legacy.assert_not_called()

    def test_empty_path_discovers_installed_client_without_sibling_source(self) -> None:
        self.assert_starts_native_client(self.runtime(), self.installed_client)

    def test_explicit_client_path_takes_precedence_over_discovery(self) -> None:
        selected = self.root / "selected-ziniao.exe"
        selected.touch()
        self.assert_starts_native_client(self.runtime(str(selected)), selected)

    def test_frozen_runtime_discovers_installed_client_without_legacy_modules(self) -> None:
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(self.root / "bundle"), create=True):
            self.assert_starts_native_client(self.runtime(), self.installed_client)

    def test_missing_installation_and_unavailable_service_request_client_selection(self) -> None:
        runtime = self.runtime()
        with patch.dict(os.environ, {"ZINIAO_CLIENT_PATH": str(self.root / "missing-ziniao.exe")}), \
                patch.object(Path, "is_file", return_value=False), \
                patch.object(ZiniaoClient, "_service_responds", autospec=True, return_value=False):
            with self.assertRaisesRegex(ZiniaoClientError, "选择.*紫鸟.*客户端|紫鸟.*客户端.*选择") as captured:
                runtime.start()
        self.assertNotIn("superbrowser_process", str(captured.exception))
        self.assertNotIn("fixture-password", str(captured.exception))
        self.legacy.assert_not_called()
        self.playwright.assert_not_called()

    def test_running_service_is_reused_when_no_installation_path_is_found(self) -> None:
        runtime = self.runtime()
        with patch.dict(os.environ, {"ZINIAO_CLIENT_PATH": ""}), \
                patch.object(Path, "is_file", return_value=False), \
                patch.object(ZiniaoClient, "_service_responds", autospec=True, return_value=True) as responds, \
                patch.object(ZiniaoClient, "request", autospec=True, return_value={"statusCode": 0}) as request, \
                patch("backend.core.ziniao_client.subprocess.Popen") as spawn:
            runtime.start()
        self.assertIs(type(runtime.client), ZiniaoClient)
        self.assertEqual(runtime.client.client_path, Path(""))
        self.assertFalse(runtime.client.started_by_client)
        responds.assert_called_once_with(runtime.client)
        request.assert_called_once_with(runtime.client, "updateCore", timeout=120)
        spawn.assert_not_called()
        self.playwright.assert_called_once_with()
        self.playwright.return_value.start.assert_called_once_with()
        self.legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

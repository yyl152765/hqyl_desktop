"""Exercise saved settings, real drive logic and SDK models without remote I/O."""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount, ConfigStore, DingTalkSettings, DingTalkUserBinding
from backend.services import lazada_withdrawal_statistics as service


ROOT = Path(__file__).resolve().parents[1]


class WithdrawalDriveIntegrationTests(unittest.TestCase):
    def test_saved_settings_upload_workbook_through_real_drive_and_sdk_requests(self) -> None:
        """Mock only auth, SDK remote methods and PUT; keep file/path logic real."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {
                "LOCALAPPDATA": str(root / "local"),
                "HQYL_LEGACY_DINGTALK_CONFIG": "",
                "LAZADA_WITHDRAWAL_DINGTALK_APP_KEY": "",
                "LAZADA_WITHDRAWAL_DINGTALK_APP_SECRET": "",
                "LAZADA_WITHDRAWAL_DINGTALK_USER_ID": "",
            }):
                store = ConfigStore(root / "settings.json")
                store.save(AppSettings(
                    output_dir=str(root),
                    accounts=[BoundAccount("fixture-account", "ziniao", "测试公司", "fixture-login", "fixture-password")],
                    active_account_ids={"ziniao": "fixture-account"},
                    dingtalk=DingTalkSettings(
                        app_key="fixture-app-key", app_secret="fixture-app-secret",
                        users=[DingTalkUserBinding("操作人甲", "fixture-user")],
                    ),
                ))
                bridge = AppBridge.__new__(AppBridge)
                bridge.config_store = ConfigStore(root / "settings.json")
                bridge.tasks = Mock()
                bridge.tasks.start.return_value = {"ok": True, "id": "fixture-task"}
                workbook_path = root / "测试店铺_提现统计.xlsx"
                workbook = Workbook()
                workbook.active.append(["交易时间", "类型", "金额", "备注"])
                workbook.active.append(["2026-09-15", "提现", -12.5, "fixture"])
                workbook.save(workbook_path)
                workbook.close()
                workbook_bytes = workbook_path.read_bytes()

                legacy = ModuleType("util.dingtalk_doc_api")
                legacy.APP_KEY = legacy.APP_SECRET = legacy.USER_ID = ""
                legacy.get_access_token = Mock(return_value="fixture-access-token")
                legacy.get_union_id = Mock(return_value="fixture-union")
                package = ModuleType("util")
                package.__path__ = []
                source_path = ROOT.parent / "superbrowser_process/util/dingtalk_drive_util.py"
                spec = importlib.util.spec_from_file_location("util.dingtalk_drive_util", source_path)
                drive = importlib.util.module_from_spec(spec)
                with (
                    patch.dict("sys.modules", {"util": package, "util.dingtalk_doc_api": legacy, "util.dingtalk_drive_util": drive}),
                    patch.object(service, "_ensure_superbrowser_path"),
                    patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected HTTP request")) as network,
                    patch("socket.socket.connect", side_effect=AssertionError("unexpected socket connection")),
                ):
                    spec.loader.exec_module(drive)
                    for country in ("MY", "PH", "TH"):
                        with self.subTest(country=country):
                            children = {}
                            folder_calls = []
                            uploaded_bytes = []
                            commits = []
                            country_root = service.COUNTRIES[country]["drive_root_id"]

                            def check_request(space_id, request, headers):
                                self.assertEqual(space_id, service.DRIVE_SPACE_ID)
                                self.assertEqual(request.union_id, "fixture-union")
                                self.assertEqual(headers.x_acs_dingtalk_access_token, "fixture-access-token")

                            def list_children(space_id, request, headers, options):
                                self.assertIsInstance(request, drive.storage_models.ListDentriesRequest)
                                check_request(space_id, request, headers)
                                return SimpleNamespace(body=SimpleNamespace(dentries=children.get(request.parent_id, []), next_token=None))

                            def add_folder(space_id, parent_id, request, headers, options):
                                self.assertIsInstance(request, drive.storage_models.AddFolderRequest)
                                check_request(space_id, request, headers)
                                folder_calls.append((parent_id, request.name))
                                dentry = SimpleNamespace(id=f"fixture-folder-{len(folder_calls)}", name=request.name, type="FOLDER", parent_id=parent_id, space_id=space_id)
                                children.setdefault(parent_id, []).append(dentry)
                                return SimpleNamespace(body=SimpleNamespace(dentry=dentry))

                            def get_upload_info(space_id, request, headers, options):
                                self.assertIsInstance(request, drive.storage_models.GetFileUploadInfoRequest)
                                check_request(space_id, request, headers)
                                precheck = request.option.pre_check_param
                                self.assertEqual(precheck.parent_id, "fixture-folder-2")
                                self.assertEqual(precheck.name, workbook_path.name)
                                self.assertEqual(precheck.size, len(workbook_bytes))
                                self.assertEqual(precheck.md_5, hashlib.md5(workbook_bytes).hexdigest())
                                return SimpleNamespace(body=SimpleNamespace(
                                    upload_key="fixture-upload-key", header_signature_info=SimpleNamespace(
                                        resource_urls=["https://upload.invalid/fixture"], headers={"fixture-header": "fixture-value"},
                                    ),
                                ))

                            def put_resource(url, *, headers, data, timeout):
                                self.assertEqual(url, "https://upload.invalid/fixture")
                                self.assertEqual(headers, {"fixture-header": "fixture-value"})
                                self.assertEqual(timeout, 90)
                                uploaded_bytes.append(data.read())
                                return SimpleNamespace(status_code=200)

                            def commit(space_id, request, headers, options):
                                self.assertIsInstance(request, drive.storage_models.CommitFileRequest)
                                check_request(space_id, request, headers)
                                self.assertEqual(request.parent_id, "fixture-folder-2")
                                self.assertEqual(request.name, workbook_path.name)
                                self.assertEqual(request.option.size, len(workbook_bytes))
                                self.assertEqual(request.upload_key, "fixture-upload-key")
                                commits.append(request)
                                dentry = SimpleNamespace(id="fixture-file", name=request.name, type="FILE", parent_id=request.parent_id, space_id=space_id)
                                children[request.parent_id] = [dentry]
                                return SimpleNamespace(body=SimpleNamespace(dentry=dentry))

                            with (
                                patch.object(drive.StorageClient, "list_dentries_with_options", side_effect=list_children),
                                patch.object(drive.StorageClient, "add_folder_with_options", side_effect=add_folder),
                                patch.object(drive.StorageClient, "get_file_upload_info_with_options", side_effect=get_upload_info),
                                patch.object(drive.StorageClient, "commit_file_with_options", side_effect=commit),
                                patch.object(drive.requests, "put", side_effect=put_resource),
                            ):
                                response = bridge.start_lazada_withdrawal_statistics({
                                    "country": country, "start_date": "2026-09-01", "end_date": "2026-09-30",
                                    "store_names": "测试店铺", "dingtalk_app_key": "forged", "dingtalk_user_id": "forged",
                                })
                                self.assertTrue(response["ok"], response)
                                with patch.object(app_bridge, "run_lazada_withdrawal_statistics") as run:
                                    bridge.tasks.start.call_args.args[1](Mock())
                                job = run.call_args.args[0]
                                logger = Mock(spec=logging.Logger)
                                path = service._upload_file(job, "测试店铺", str(workbook_path), logger)
                                repeated_path = service._upload_file(job, "测试店铺", str(workbook_path), logger)

                            expected_country = service.COUNTRIES[country]["name"].replace("马来西亚", "马来")
                            self.assertEqual(path, f"提现统计/lazada{expected_country}/9月/测试店铺/{workbook_path.name}")
                            self.assertEqual(repeated_path, path)
                            self.assertEqual(folder_calls, [(country_root, "9月"), ("fixture-folder-1", "测试店铺")])
                            self.assertEqual(uploaded_bytes, [workbook_bytes, workbook_bytes])
                            self.assertIsNone(commits[0].overwrite_dentry_id)
                            self.assertEqual(commits[1].overwrite_dentry_id, "fixture-file")
                            self.assertEqual(commits[1].option.conflict_strategy, "OVERWRITE")
                    legacy.get_access_token.assert_called_with("fixture-app-key", "fixture-app-secret")
                    legacy.get_union_id.assert_called_with("fixture-access-token", "fixture-user")
                    network.assert_not_called()


if __name__ == "__main__":
    unittest.main()

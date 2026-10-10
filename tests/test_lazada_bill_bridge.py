"""Lazada 后台收支数据页与桌面桥接层的离线契约测试。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from backend import app_bridge, config_store
from backend.app_bridge import AppBridge
from backend.config_store import (
    ConfigStore,
    DingTalkSettings,
    DingTalkUserBinding,
)
from backend.services import lazada_bill_detail as service


class LazadaBillBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = config_store.AppSettings(
            output_dir=str(self.root),
            accounts=[
                config_store.BoundAccount(
                    "ziniao-a", "ziniao", "公司 A", "operator-a", "private-a", {"company": "公司 A"}
                )
            ],
            active_account_ids={"ziniao": "ziniao-a"},
            dingtalk=DingTalkSettings(
                app_key="fixture-app-key",
                app_secret="fixture-app-secret",
                users=[DingTalkUserBinding("操作人甲", "fixture-user-a")],
            ),
        )
        with patch.object(config_store, "_base_local_data_dir", return_value=self.root / "local"):
            self.store = ConfigStore(self.root / "settings.json")
        self.settings = self.store.save(settings)
        with patch.object(config_store, "_base_local_data_dir", return_value=self.root / "local"):
            with patch.object(app_bridge, "ConfigStore", return_value=self.store):
                self.bridge = AppBridge()
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-bill-task"}
        self.network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("offline test must not use network"),
        ).start()
        # 不使用本机 user_map.yaml，避免测试依赖运维人员映射。
        patch.object(config_store, "_load_default_dingtalk_user_bindings", return_value=[]).start()
        self.addCleanup(patch.stopall)

    def payload(self, **extra):
        return {
            "country": "TH",
            "sheet_name": "26年9月",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "store_names": "店铺 A\n店铺 B",
            **extra,
        }

    def assert_no_credentials(self, result) -> None:
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in (
            self.settings.accounts[0].password,
            "fixture-app-key",
            "fixture-app-secret",
            "fixture-user-a",
            "password",
            "dingtalk_app_key",
            "dingtalk_app_secret",
            "dingtalk_user_id",
        ):
            self.assertNotIn(secret, serialized)

    @staticmethod
    def previous_month() -> tuple[str, str]:
        end = date.today().replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()

    def test_info_returns_country_defaults_without_credentials(self) -> None:
        today = date.today()
        with patch.object(
            app_bridge,
            "resolve_lazada_monthly_report_runtime_paths",
            return_value={"client_path": "fixture-client", "webdriver_path": "fixture-driver"},
        ):
            result = self.bridge.get_lazada_bill_detail_info()
        start, end = self.previous_month()
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["code"] for item in result["countries"]], ["TH", "PH", "MY", "ID", "VN"]
        )
        self.assertEqual(result["default_sheet_name"], f"{today.year % 100:02d}年{today.month}月")
        self.assertEqual(result["date_range"], {"start_date": start, "end_date": end})
        self.assertEqual(result["workbook_id"], service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(result["dws_node"], "")
        self.assertEqual(result["output_dir"], str(self.root))
        self.assertEqual(result["screenshot_root"], service.default_screenshot_root())
        self.assertEqual(result["image_column"], "F")
        self.assertEqual(result["operator_names"], ["操作人甲"])
        self.assertEqual(result["default_dingtalk_operator_name"], "操作人甲")
        self.assertTrue(result["dingtalk_configured"])
        self.assertEqual(result["client_path"], "fixture-client")
        self.assertEqual(result["webdriver_path"], "fixture-driver")
        self.assert_no_credentials(result)
        self.bridge.tasks.start.assert_not_called()
        self.network.assert_not_called()

    def test_info_loads_without_accounts_dingtalk_or_installed_runtime(self) -> None:
        self.store.save(
            config_store.AppSettings(
                output_dir=str(self.root),
                accounts=[],
                dingtalk=DingTalkSettings(),
            )
        )
        with patch.object(
            app_bridge,
            "resolve_lazada_monthly_report_runtime_paths",
            side_effect=RuntimeError("未安装客户端"),
        ):
            result = self.bridge.get_lazada_bill_detail_info()
        self.assertTrue(result["ok"])
        self.assertFalse(result["dingtalk_configured"])
        self.assertEqual(result["operator_names"], [])
        self.assertEqual(result["client_path"], "")
        self.assert_no_credentials(result)

    def test_info_reports_dws_image_support(self) -> None:
        """运行前就要能知道 F 列图片会不会写入：缺 dws 时给出安装提示。"""
        with patch.object(shutil, "which", return_value=None):
            missing = self.bridge.get_lazada_bill_detail_info()
        self.assertTrue(missing["ok"], missing)
        self.assertFalse(missing["dws_available"])
        self.assertEqual(missing["dws_path"], "")
        self.assertIn("未安装 dws", missing["dws_hint"])
        self.assertIn("PATH", missing["dws_hint"])
        with patch.object(shutil, "which", return_value=r"C:\tools\dws.exe"):
            ready = self.bridge.get_lazada_bill_detail_info()
        self.assertTrue(ready["dws_available"])
        self.assertEqual(ready["dws_path"], r"C:\tools\dws.exe")
        self.assertEqual(ready["dws_hint"], "")
        self.assertEqual(ready["image_column"], "F")

    def test_save_preferences_stores_one_shared_document_and_normalizes_links(self) -> None:
        linked = "https://alidocs.dingtalk.com/i/nodes/AbCdEf123456?dentryKey=AbCdEf123456"
        result = self.bridge.save_lazada_bill_preferences(
            {
                "country": "th",
                "workbook_id": linked,
                "dws_node": "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
                "client_path": '"C:\\Ziniao\\ziniao.exe"',
                "screenshot_root": str(self.root / "shots"),
            }
        )
        self.assertTrue(result["ok"], result)
        saved = self.store.load()
        self.assertEqual(saved.lazada_bill_workbook_id, "AbCdEf123456")
        self.assertEqual(saved.lazada_bill_dws_node, "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l")
        self.assertEqual(saved.lazada_bill_client_path, "C:\\Ziniao\\ziniao.exe")
        self.assertEqual(saved.lazada_bill_screenshot_dir, str(self.root / "shots"))

    def test_save_preferences_is_country_independent_and_can_be_cleared(self) -> None:
        self.bridge.save_lazada_bill_preferences({"country": "TH", "workbook_id": "shared-workbook-1"})
        self.bridge.save_lazada_bill_preferences({"country": "VN", "workbook_id": "shared-workbook-2"})
        self.assertEqual(self.store.load().lazada_bill_workbook_id, "shared-workbook-2")
        self.bridge.save_lazada_bill_preferences({"workbook_id": ""})
        self.assertEqual(self.store.load().lazada_bill_workbook_id, "")

    def test_save_preferences_rejects_invalid_values(self) -> None:
        result = self.bridge.save_lazada_bill_preferences(
            {"country": "TH", "workbook_id": "https://alidocs.dingtalk.com/other"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("工作簿", result["error"])
        self.assertEqual(self.store.load().lazada_bill_workbook_id, "")

        node = self.bridge.save_lazada_bill_preferences({"country": "TH", "dws_node": "short-id"})
        self.assertFalse(node["ok"])
        self.assertIn("钉钉图片节点格式无效", node["error"])
        self.assertEqual(self.store.load().lazada_bill_dws_node, "")

        self.assertFalse(self.bridge.save_lazada_bill_preferences("not-a-dict")["ok"])

    def test_start_uses_bound_credentials_dingtalk_and_country_defaults(self) -> None:
        payload = self.payload(
            account_id="ziniao-a",
            username="forged",
            password="forged",
            company="forged",
            dingtalk_operator_name="操作人甲",
            dingtalk_app_key="forged",
            dingtalk_app_secret="forged",
            dingtalk_user_id="forged",
        )
        with patch.object(
            service,
            "validate_lazada_bill_detail_payload",
            wraps=service.validate_lazada_bill_detail_payload,
        ) as validate:
            result = self.bridge.start_lazada_bill_detail(payload)
        self.assertTrue(result["ok"], result)
        request = validate.call_args.args[0]
        self.assertEqual(
            (request["username"], request["password"], request["company"]),
            ("operator-a", "private-a", "公司 A"),
        )
        self.assertEqual(
            (request["dingtalk_app_key"], request["dingtalk_app_secret"], request["dingtalk_user_id"]),
            ("fixture-app-key", "fixture-app-secret", "fixture-user-a"),
        )
        self.assertEqual(request["workbook_id"], service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(request["dws_node"], "")
        self.assertEqual(payload["password"], "forged", "Bridge must not mutate the caller payload")
        call = self.bridge.tasks.start.call_args
        self.assertEqual(call.args[0], "泰国 Lazada 后台收支数据")
        self.assertEqual(call.kwargs["tool"], "lazada_bill_detail")
        self.assertEqual(
            call.kwargs["context"],
            {
                "account_id": "ziniao-a",
                "country": "TH",
                "country_name": "泰国",
                "sheet_name": "26年9月",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
                "store_count": 2,
                "output_root": str(self.root),
                "screenshot_root": service.default_screenshot_root(),
                "screenshot_dir": str(
                    service.screenshot_folder(
                        service.default_screenshot_root(), "泰国", "2026-08-01", "2026-08-31"
                    )
                ),
            },
        )
        self.assert_no_credentials(call.kwargs["context"])
        progress = Mock()
        expected = {"success_count": 2, "failed_count": 0, "output_dir": str(self.root)}
        with patch.object(service, "run_lazada_bill_detail", return_value=expected) as run:
            actual = call.args[1](progress)
        self.assertIs(actual, expected)
        self.assertIs(run.call_args.args[1], progress)
        job = run.call_args.args[0]
        self.assertEqual(job.country, "TH")
        self.assertEqual(job.sheet_name, "26年9月")
        self.assertEqual((job.start_date, job.end_date), ("2026-08-01", "2026-08-31"))
        self.assertEqual(job.store_names, ("店铺 A", "店铺 B"))
        self.assertEqual(job.workbook_id, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(job.dws_node, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(job.screenshot_root, service.default_screenshot_root())
        self.network.assert_not_called()

    def test_start_prefers_saved_shared_document(self) -> None:
        self.store.save(
            {
                "lazada_bill_workbook_id": "saved-shared-workbook",
                "lazada_bill_dws_node": "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
                "lazada_bill_screenshot_dir": str(self.root / "saved-shots"),
                "lazada_bill_client_path": "C:\\saved\\ziniao.exe",
            }
        )
        result = self.bridge.start_lazada_bill_detail(
            self.payload(country="VN", dingtalk_operator_name="操作人甲")
        )
        self.assertTrue(result["ok"], result)
        call = self.bridge.tasks.start.call_args
        self.assertEqual(call.args[0], "越南 Lazada 后台收支数据")
        self.assertEqual(call.kwargs["context"]["country"], "VN")
        self.assertEqual(call.kwargs["context"]["screenshot_root"], str(self.root / "saved-shots"))
        self.assertEqual(
            call.kwargs["context"]["screenshot_dir"],
            str(service.screenshot_folder(str(self.root / "saved-shots"), "越南", "2026-08-01", "2026-08-31")),
        )
        with patch.object(
            service, "run_lazada_bill_detail", return_value={"success": True}
        ) as run:
            call.args[1](Mock())
        job = run.call_args.args[0]
        self.assertEqual(job.workbook_id, "saved-shared-workbook")
        self.assertEqual(job.dws_node, "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l")
        self.assertEqual(job.client_path, "C:\\saved\\ziniao.exe")
        self.assertEqual(job.screenshot_root, str(self.root / "saved-shots"))

    def test_start_honours_page_overrides_and_vietnam_defaults(self) -> None:
        override = self.bridge.start_lazada_bill_detail(
            self.payload(
                country="TH",
                workbook_id="page-workbook",
                dws_node="https://alidocs.dingtalk.com/i/nodes/page-node",
                dingtalk_operator_name="操作人甲",
            )
        )
        self.assertTrue(override["ok"], override)
        call = self.bridge.tasks.start.call_args
        with patch.object(service, "run_lazada_bill_detail", return_value={"success": True}) as run:
            call.args[1](Mock())
        job = run.call_args.args[0]
        self.assertEqual(job.workbook_id, "page-workbook")
        self.assertEqual(job.dws_node, "https://alidocs.dingtalk.com/i/nodes/page-node")

        self.bridge.tasks.start.reset_mock()
        vietnam = self.bridge.start_lazada_bill_detail(
            self.payload(country="越南", dingtalk_operator_name="操作人甲")
        )
        self.assertTrue(vietnam["ok"], vietnam)
        call = self.bridge.tasks.start.call_args
        with patch.object(service, "run_lazada_bill_detail", return_value={"success": True}) as run:
            call.args[1](Mock())
        job = run.call_args.args[0]
        self.assertEqual(job.country, "VN")
        self.assertEqual(job.workbook_id, service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(job.dws_node, service.DEFAULT_WORKBOOK_ID)

    def test_start_requires_ziniao_account_and_dingtalk_operator(self) -> None:
        self.store.save(
            config_store.AppSettings(
                output_dir=str(self.root),
                accounts=[],
                dingtalk=DingTalkSettings(app_key="k", app_secret="s", users=[]),
            )
        )
        missing_account = self.bridge.start_lazada_bill_detail(self.payload())
        self.assertFalse(missing_account["ok"])
        self.assertTrue(missing_account["requires_account"])
        self.assertEqual(missing_account["vendor"], "ziniao")

        self.store.save(
            {
                "accounts": [
                    {
                        "id": "ziniao-a", "vendor": "ziniao", "name": "公司 A",
                        "username": "operator-a", "password": "private-a",
                        "extra": {"company": "公司 A"},
                    }
                ],
                "active_account_ids": {"ziniao": "ziniao-a"},
            }
        )
        self.store.save(
            config_store.AppSettings(
                output_dir=str(self.root),
                accounts=list(self.store.load().accounts),
                active_account_ids={"ziniao": "ziniao-a"},
                dingtalk=DingTalkSettings(app_key="k", app_secret="s", users=[]),
            )
        )
        missing_operator = self.bridge.start_lazada_bill_detail(self.payload())
        self.assertFalse(missing_operator["ok"])
        self.assertIn("操作人", missing_operator["error"])

        self.store.save(
            config_store.AppSettings(
                output_dir=str(self.root),
                accounts=list(self.store.load().accounts),
                active_account_ids={"ziniao": "ziniao-a"},
                dingtalk=DingTalkSettings(
                    app_key="k", app_secret="s", users=[DingTalkUserBinding("操作人甲", "u")]
                ),
            )
        )
        unknown_operator = self.bridge.start_lazada_bill_detail(
            self.payload(dingtalk_operator_name="不存在的人")
        )
        self.assertFalse(unknown_operator["ok"])
        self.assertIn("不在钉钉用户配置表中", unknown_operator["error"])

        self.store.save(
            config_store.AppSettings(
                output_dir=str(self.root),
                accounts=list(self.store.load().accounts),
                active_account_ids={"ziniao": "ziniao-a"},
                dingtalk=DingTalkSettings(app_key="", app_secret="", users=[]),
            )
        )
        missing_creds = self.bridge.start_lazada_bill_detail(
            self.payload(dingtalk_operator_name="操作人甲")
        )
        self.assertFalse(missing_creds["ok"])
        self.assertIn("AppKey", missing_creds["error"])
        self.bridge.tasks.start.assert_not_called()

    def test_start_rejects_invalid_payload_before_starting_task(self) -> None:
        cases = (
            ({"country": "SG"}, "请选择支持的国家"),
            ({"start_date": "2026-09-01", "end_date": "2026-08-01"}, "开始日期不能晚于结束日期"),
            ({"store_names": " "}, "请输入店铺名称"),
        )
        for extra, message in cases:
            with self.subTest(extra=extra):
                result = self.bridge.start_lazada_bill_detail(
                    self.payload(dingtalk_operator_name="操作人甲", **extra)
                )
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])
        self.bridge.tasks.start.assert_not_called()

    def test_list_sheets_uses_shared_document_and_operator(self) -> None:
        self.store.save(
            {
                "lazada_bill_workbook_id": "saved-shared-workbook",
                "dingtalk": {
                    "app_key": "fixture-app-key",
                    "app_secret": "fixture-app-secret",
                    "users": [{"name": "操作人甲", "user_id": "fixture-user-a"}],
                },
            }
        )
        sheets = [{"name": "26年10月", "id": "sheet-1"}, {"name": "26年9月", "id": "sheet-2"}]
        with patch.object(service, "list_workbook_sheets", return_value=sheets) as listing:
            result = self.bridge.list_lazada_bill_sheets(
                {"country": "TH", "dingtalk_operator_name": "操作人甲"}
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sheet_names"], ["26年10月", "26年9月"])
        self.assertEqual(result["workbook_id"], "saved-shared-workbook")
        self.assertEqual(result["preferred_sheet_name"], "", "文档里没有同名 Sheet 时留空")
        self.assertEqual(result["country_name"], "泰国")
        self.assertEqual(result["sheets"], sheets)
        self.assertEqual(
            listing.call_args.kwargs,
            {
                "app_key": "fixture-app-key",
                "app_secret": "fixture-app-secret",
                "user_id": "fixture-user-a",
            },
        )
        self.assertEqual(listing.call_args.args[0], "saved-shared-workbook")
        self.assert_no_credentials(result)
        self.network.assert_not_called()

    def test_list_sheets_falls_back_to_default_document_and_reports_errors(self) -> None:
        with patch.object(service, "list_workbook_sheets", return_value=[]) as listing:
            default = self.bridge.list_lazada_bill_sheets({"country": "PH"})
        self.assertTrue(default["ok"], default)
        self.assertEqual(listing.call_args.args[0], service.DEFAULT_WORKBOOK_ID)
        self.assertEqual(default["sheet_names"], [])

        unknown = self.bridge.list_lazada_bill_sheets({"country": "SG"})
        self.assertFalse(unknown["ok"])
        self.assertIn("请选择支持的国家", unknown["error"])

        self.store.save({"dingtalk": {"app_key": "", "app_secret": "", "users": []}})
        without_credentials = self.bridge.list_lazada_bill_sheets({"country": "TH"})
        self.assertFalse(without_credentials["ok"])
        self.assertTrue(
            "AppKey" in without_credentials["error"] or "操作人" in without_credentials["error"]
        )

    def test_preferred_sheet_defaults_to_the_country_named_sheet(self) -> None:
        names = ["26年10月", "泰国", "菲律宾"]
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(names, service.COUNTRY_PROFILES["PH"]), "菲律宾"
        )
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(names, service.COUNTRY_PROFILES["TH"]), "泰国"
        )
        # 旧脚本里马来西亚的默认工作表名是「马来」（不是「马来西亚」）。
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(["26年10月", "马来"], service.COUNTRY_PROFILES["MY"]),
            "马来",
        )
        self.assertEqual(
            service.sheet_name_aliases(service.COUNTRY_PROFILES["MY"]), ("马来", "马来西亚")
        )
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(
                ["26年10月", "马来西亚"], service.COUNTRY_PROFILES["MY"]
            ),
            "马来西亚",
        )
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(["26年10月"], service.COUNTRY_PROFILES["TH"]), ""
        )
        self.assertEqual(
            app_bridge._preferred_bill_sheet_name(["2026 泰国账单"], service.COUNTRY_PROFILES["TH"]),
            "2026 泰国账单",
        )
        with patch.object(
            service,
            "list_workbook_sheets",
            return_value=[{"name": name, "id": name} for name in names],
        ):
            result = self.bridge.list_lazada_bill_sheets({"country": "菲律宾"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["country"], "PH")
        self.assertEqual(result["preferred_sheet_name"], "菲律宾")
        self.assertEqual(result["sheet_names"], names)

    def test_tool_registry_lists_the_module(self) -> None:
        with patch.object(app_bridge, "get_group_options", return_value=[]):
            info = self.bridge.get_app_info()
        keys = [item["key"] for item in info["tools"]]
        self.assertIn("lazada_bill_detail", keys)
        entry = next(item for item in info["tools"] if item["key"] == "lazada_bill_detail")
        self.assertEqual(entry["vendor"], "ziniao")
        self.assertEqual(entry["status"], "ready")
        self.assertIn("收支", entry["name"])


if __name__ == "__main__":
    unittest.main()

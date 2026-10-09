from __future__ import annotations

import tempfile
import unittest
import json
import sys
import csv
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from backend.config_store import BoundAccount, ConfigStore, DEFAULT_UPDATE_MANIFEST_URL
from backend.core.mabang_client import MabangApiError
from backend.services.group_sales_report import (
    SalesGroup,
    export_group_csv,
    get_group_options,
    normalize_sales_csv,
    parse_group_options,
    validate_group_sales_payload,
)


class GroupSalesReportServiceTests(unittest.TestCase):
    @staticmethod
    def _sales_row(content: bytes) -> dict[str, str]:
        reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
        return next(reader)

    def test_normalize_sales_csv_recalculates_daily_average_for_selected_period(self) -> None:
        content = (
            "SKU,销售数量,平均单天销量,收入-订单金额\n"
            "hqsG2001,3421,110.35,38284.7828\n"
        ).encode("utf-8")

        thirty_day_row = self._sales_row(normalize_sales_csv(content, 30))
        thirty_one_day_row = self._sales_row(normalize_sales_csv(content, 31))

        self.assertEqual(thirty_day_row["销售数量"], "3421")
        self.assertEqual(thirty_day_row["平均单天销量"], "114.03\t")
        self.assertEqual(thirty_one_day_row["平均单天销量"], "110.35\t")

    def test_normalize_sales_csv_recalculates_zero_and_total_rows(self) -> None:
        content = (
            "SKU,销售数量,平均单天销量\n"
            "zero-sku,0,99.99\n"
            "合计,205846,1.23\n"
        ).encode("utf-8")
        reader = csv.DictReader(StringIO(normalize_sales_csv(content, 30).decode("utf-8-sig")))
        rows = list(reader)

        self.assertEqual(rows[0]["平均单天销量"], "0.00\t")
        self.assertEqual(rows[1]["平均单天销量"], "6861.53\t")

    def test_normalize_sales_csv_uses_half_up_rounding_and_skips_blank_rows(self) -> None:
        content = (
            "SKU,销售数量,平均单天销量\n"
            "\n"
            "one-item,1,0\n"
        ).encode("utf-8")
        normalized = normalize_sales_csv(content, 8)
        rows = list(csv.DictReader(StringIO(normalized.decode("utf-8-sig"))))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["平均单天销量"], "0.13\t")

    def test_normalize_sales_csv_requires_numeric_quantity(self) -> None:
        invalid_quantity = "SKU,销售数量\ninvalid,--\n".encode("utf-8")
        with self.assertRaisesRegex(MabangApiError, "销售数量不是有效数字"):
            normalize_sales_csv(invalid_quantity, 30)

        for value in ("", "NaN", "Infinity", "-Infinity", "1E+999999"):
            with self.subTest(value=value):
                content = f"SKU,销售数量\ninvalid,{value}\n".encode("utf-8")
                with self.assertRaises(MabangApiError):
                    normalize_sales_csv(content, 30)

    def test_normalize_sales_csv_requires_quantity_header(self) -> None:
        with self.assertRaisesRegex(MabangApiError, "缺少必需列.*销售数量"):
            normalize_sales_csv("SKU,平均单天销量\nabc,1\n".encode("utf-8"), 30)

    def test_normalize_sales_csv_rejects_non_positive_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "统计天数"):
            normalize_sales_csv(b"SKU\n", 0)

    def test_export_group_csv_recalculates_average_without_pagination(self) -> None:
        class ExportOnlyClient:
            def __init__(self) -> None:
                self.download_calls = 0

            def download_product_sales_report_csv(self, **_kwargs):
                self.download_calls += 1
                return SimpleNamespace(
                    content=(
                        "SKU,销售数量,平均单天销量\n"
                        "hqsG2001,3421,110.35\n"
                    ).encode("utf-8")
                )

        client = ExportOnlyClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            output_path = export_group_csv(
                client,
                output_dir,
                SalesGroup("菲S-雷莹莹团队", "1057656"),
                "20260701_20260730",
                date(2026, 7, 1),
                date(2026, 7, 30),
            )

            self.assertEqual(client.download_calls, 1)
            self.assertEqual(self._sales_row(output_path.read_bytes())["平均单天销量"], "114.03\t")

    def test_validate_group_sales_payload_accepts_known_group(self) -> None:
        group = get_group_options()[0]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "backend.services.group_sales_report.get_group_options", return_value=[group]
        ):
            job = validate_group_sales_payload(
                {
                    "username": "u",
                    "password": "p",
                    "group_ids": [group["id"]],
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-18",
                    "output_dir": temp_dir,
                }
            )
        self.assertEqual(job.groups[0].id, group["id"])

    def test_live_options_include_every_platform_and_group_from_report_page(self) -> None:
        html = '''<html>
          <label><input name="shopLabelIds[]" value="new-ph"><span>菲S-新增小组</span></label>
          <label><input name="shopLabelIds[]" value="new-tk"><span>tk菲律宾</span></label>
          <label><input name="shopLabelIds[]" value="new-my"><span>lazada马来</span></label>
          <label><input name="shopLabelIds[]" value="new-ph"><span>重复分类</span></label>
          <label><input name="shopLabelIds[]" value=""><span>全部</span></label>
          <label><input name="shopIdMultiple[]" value="shop"><span>店铺</span></label>
        </html>'''
        with patch("backend.services.group_sales_report.MabangClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.client.get.return_value = httpx.Response(
                200, text=html, request=httpx.Request("GET", "https://example.test/index.php")
            )
            options = get_group_options("bound-user", "bound-password")
        client.login.assert_called_once_with("bound-user", "bound-password")
        self.assertEqual([item["id"] for item in options], ["new-ph", "new-tk", "new-my"])
        self.assertEqual([item["name"] for item in options], ["菲S-新增小组", "tk菲律宾", "lazada马来"])

    def test_category_parser_handles_select_and_rejects_login_or_permission_page(self) -> None:
        groups = parse_group_options('''<select name="shopLabelIds[]">
            <option value="">全部</option><option value="123">新分类 &amp; 分组</option>
        </select>''')
        self.assertEqual(groups, (SalesGroup("新分类 & 分组", "123"),))
        with self.assertRaisesRegex(MabangApiError, "未能读取马帮自定义分类"):
            parse_group_options('<form><input name="username"></form>')

    def test_validation_accepts_new_categories_and_rejects_invisible_ids(self) -> None:
        payload = {
            "username": "u", "password": "p", "group_ids": ["new", "new"],
            "start_date": "2026-09-01", "end_date": "2026-09-20", "output_dir": str(Path.cwd()),
        }
        with patch("backend.services.group_sales_report.get_group_options", return_value=[
            {"id": "new", "name": "新增分类", "shop_label_id": "new"},
        ]) as options:
            job = validate_group_sales_payload(payload)
            self.assertEqual(job.groups, (SalesGroup("新增分类", "new"),))
            options.assert_called_once_with("u", "p")
            with self.assertRaisesRegex(ValueError, "自定义分类不存在或当前账号不可见"):
                validate_group_sales_payload({**payload, "group_ids": ["unknown"]})

    def test_validate_group_sales_payload_requires_group(self) -> None:
        with self.assertRaises(ValueError):
            validate_group_sales_payload(
                {
                    "username": "u",
                    "password": "p",
                    "group_ids": [],
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-18",
                    "output_dir": str(Path.cwd()),
                }
            )

    def test_config_store_round_trips_accounts_and_sales_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = store.save(
                {
                    "output_dir": temp_dir,
                    "sales_group_ids": ["1057656", "1057656", "1047454"],
                    "accounts": [
                        BoundAccount("account-1", "mabang", "主账号", "u", "p")
                    ],
                    "active_account_ids": {"mabang": "account-1"},
                }
            )
            loaded = store.load()
        self.assertEqual(saved.accounts[0].password, "p")
        self.assertEqual(loaded.accounts[0].password, "p")
        self.assertEqual(loaded.active_account_ids, {"mabang": "account-1"})
        self.assertEqual(loaded.sales_group_ids, ["1057656", "1047454"])

    def test_config_store_migrates_legacy_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            config_path.write_text(
                json.dumps({"username": "legacy-user", "password": "legacy-pass"}),
                encoding="utf-8",
            )
            loaded = ConfigStore(config_path).load()
        self.assertEqual(len(loaded.accounts), 1)
        self.assertEqual(loaded.accounts[0].vendor, "mabang")
        self.assertEqual(loaded.accounts[0].username, "legacy-user")
        self.assertEqual(loaded.active_account_ids, {"mabang": "legacy-mabang"})

    def test_config_store_migrates_legacy_update_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = store.save(
                {"update_manifest_url": "http://47.112.20.68/hqyl/latest.json"}
            )
        self.assertEqual(saved.update_manifest_url, DEFAULT_UPDATE_MANIFEST_URL)

    def test_config_store_preserves_saved_captcha_password_on_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            store.save({"captcha_username": "first-user", "captcha_password": "secret"})
            store.save({"captcha_username": "second-user"})
            loaded = store.load()
        self.assertEqual(loaded.captcha_username, "second-user")
        self.assertEqual(loaded.captcha_password, "secret")

    def test_config_store_seeds_dingtalk_user_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            user_map = Path(temp_dir) / "user_map.yaml"
            user_map.write_text("users:\n  Example User: example-user-id\n", encoding="utf-8")
            with patch("backend.config_store._default_user_map_candidates", return_value=[user_map]):
                loaded = ConfigStore(Path(temp_dir) / "settings.json").load()
        self.assertEqual([(user.name, user.user_id) for user in loaded.dingtalk.users], [("Example User", "example-user-id")])

    def test_config_store_preserves_dingtalk_secret_on_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            store.save({"dingtalk": {"app_key": "app-key", "app_secret": "app-secret", "users": [{"name": "王小妹", "user_id": "user-id"}]}})
            store.save({"dingtalk": {"app_key": "new-key"}})
            loaded = store.load()
        self.assertEqual(loaded.dingtalk.app_key, "new-key")
        self.assertEqual(loaded.dingtalk.app_secret, "app-secret")
        self.assertEqual(loaded.dingtalk.users[0].name, "王小妹")

    def test_config_store_migrates_dingtalk_credentials_from_old_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "HQYLAutomation"
            legacy_config = install_root / "_internal" / "config" / "config.yaml"
            legacy_config.parent.mkdir(parents=True)
            legacy_config.write_text(
                "dingtalk:\n"
                "  app:\n"
                "    rpa:\n"
                "      app_key: legacy-app-key\n"
                "      app_secret: legacy-app-secret\n",
                encoding="utf-8",
            )
            config_path = Path(temp_dir) / "AppData" / "settings.json"
            config_path.parent.mkdir(parents=True)
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(install_root / "HQYLAutomation.exe")),
            ):
                loaded = ConfigStore(config_path).load()
                persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(loaded.dingtalk.app_key, "legacy-app-key")
        self.assertEqual(loaded.dingtalk.app_secret, "legacy-app-secret")
        self.assertEqual(persisted["dingtalk"]["app_key"], "legacy-app-key")
        self.assertEqual(persisted["dingtalk"]["app_secret"], "legacy-app-secret")

    def test_config_store_keeps_saved_dingtalk_credentials_during_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "HQYLAutomation"
            legacy_config = install_root / "_internal" / "config" / "config.yaml"
            legacy_config.parent.mkdir(parents=True)
            legacy_config.write_text(
                "dingtalk:\n"
                "  app:\n"
                "    rpa:\n"
                "      app_key: legacy-app-key\n"
                "      app_secret: legacy-app-secret\n",
                encoding="utf-8",
            )
            config_path = Path(temp_dir) / "AppData" / "settings.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "dingtalk": {
                            "app_key": "saved-app-key",
                            "app_secret": "saved-app-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(install_root / "HQYLAutomation.exe")),
            ):
                loaded = ConfigStore(config_path).load()

        self.assertEqual(loaded.dingtalk.app_key, "saved-app-key")
        self.assertEqual(loaded.dingtalk.app_secret, "saved-app-secret")



if __name__ == "__main__":
    unittest.main()

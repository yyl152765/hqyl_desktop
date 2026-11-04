"""按需探测 TEMU 活动报名导出（真实紫鸟会话，只读）。

用法：
  python scripts/probe_temu_activity_export.py --list
  python scripts/probe_temu_activity_export.py --store 店铺名
  python scripts/probe_temu_activity_export.py --store 店铺名 --export
  python scripts/probe_temu_activity_export.py --store 店铺名 --sites PH,TH

--list 只读店铺目录，不打开任何店铺；--store 打开该店并读取活动报名页结构；
--export 额外完整导出一份模板到 outputs/temu_activity_probe/。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Electron 客户端（紫鸟）会把 ELECTRON_RUN_AS_NODE=1 当成 Node 解释器，直接退出；
# NODE_OPTIONS 里的 --require 也会被注入。从 agent 终端里启动客户端时必须先清掉。
for _name in ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "ELECTRON_NO_ATTACH_CONSOLE"):
    os.environ.pop(_name, None)

from backend.config_store import ConfigStore  # noqa: E402
from backend.core.ziniao_browser import ZiniaoBrowser, account_profile  # noqa: E402
from backend.services import temu_activity_export as export  # noqa: E402
from backend.services import temu_activity_page as page  # noqa: E402
from backend.services.temu_activity_workbook import inspect_export  # noqa: E402


def ziniao_request() -> dict:
    settings = ConfigStore().load()
    account_id = settings.active_account_ids.get("ziniao") or ""
    account = next((item for item in settings.accounts
                    if item.vendor == "ziniao" and item.id == account_id), None)
    if account is None:
        raise SystemExit("未找到已绑定的紫鸟账号，请先在桌面端「设置 → 紫鸟账号」绑定")
    return {"account_id": account.id, "company": (account.extra or {}).get("company") or account.name,
            "username": account.username, "password": account.password}


def pick_store(stores: list[dict], name: str) -> dict:
    matched = export.resolve_stores([name], stores)
    if not matched["can_start"]:
        raise SystemExit("；".join(f"{item['name']}：{item['error']}" for item in matched["issues"]))
    return matched["stores"][0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="探测 TEMU 活动报名导出页面")
    parser.add_argument("--list", action="store_true", help="只列出 TEMU 店铺，不打开店铺")
    parser.add_argument("--store", default="", help="要打开的店铺名（完整名或唯一片段）")
    parser.add_argument("--sites", default="PH", help="逗号分隔的站点，默认 PH")
    parser.add_argument("--export", action="store_true", help="完整导出一份模板到 outputs/temu_activity_probe")
    parser.add_argument("--prepare", action="store_true", help="先执行紫鸟自动化模式准备（会重启客户端）")
    arguments = parser.parse_args(argv)

    request = ziniao_request()
    sites = [item.strip().upper() for item in arguments.sites.split(",") if item.strip()]
    profile = account_profile(request)
    print(f"紫鸟账号：{request['username']}（{request['company']}）", flush=True)

    with ZiniaoBrowser(request) as browser:
        if arguments.prepare:
            browser.prepare_task(progress=lambda message: print(message, flush=True))
        else:
            browser.ready()
        stores = browser.list_stores()
        temu = [store for store in stores if "temu" in str(store.get("platform_name") or "").casefold()]
        print(f"店铺总数 {len(stores)}，其中 TEMU {len(temu)} 家", flush=True)
        if arguments.list or not arguments.store:
            for store in temu[:200]:
                print(f"  [{store['store_id']}] {store['store_name']}", flush=True)
            return 0

        store = pick_store(temu, arguments.store)
        gateway = page.TemuActivityGateway(profile, browser=browser)
        gateway.preflight([{**store, "status": "pending"}])
        if not arguments.export:
            result = gateway.inspect(store, sites)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0

        output = ROOT / "outputs" / "temu_activity_probe"
        raw_dir = output / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        print(f"开始导出：{store['store_name']} / {sites}", flush=True)
        result = gateway.export(store, raw_dir, sites=sites)
        report = inspect_export(result["path"])
        print(json.dumps({"file": result["path"], "evidence": result["evidence"], "report": report},
                         ensure_ascii=False, indent=2), flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

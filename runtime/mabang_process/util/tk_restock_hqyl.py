"""HQYL orchestration for TK restock exports with preserved country delivery paths."""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from philippines.hqyl_runtime import (
    RuntimeConfigurationError,
    add_dingtalk_delivery_arguments,
    atomic_copy_file,
    create_safe_logger,
    deliver_philippines_files,
    file_sha256,
    load_flow_config,
    load_hqyl_parameters,
    parse_run_date,
    prepare_dingtalk_file_delivery,
    resolve_asset_environment,
    resolve_output_directory,
    write_pipeline_success_manifest,
)
from util.mabang_client import MabangApiError, MabangClient
from util.warehouse_filenames import inventory_file_stems, preserve_inventory_filenames
from util.tk_restock_base import (
    _read_excel_with_text_skus,
    build_date_ranges,
    clean_sales_dataframe,
    load_and_filter_inventory,
    load_stock_label_dataframe,
    normalize_sku,
    validate_config,
    validate_output_workbook,
    write_output_workbook,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUEST_CONFIG_PATH = PROJECT_ROOT / "philippines/config/mabang-request.public.yaml"
# The same account serves all TK country labels; country-specific account keys
# must not accidentally select another login during local fallback.
MABANG_ASSETS = {
    "MABANG_USERNAME": "asset.mabang.philippines.username",
    "MABANG_PASSWORD": "asset.mabang.philippines.password",
}
LEGACY_ASSET_PATHS = {
    "MABANG_USERNAME": ("mabang.user_info.philippines.username",),
    "MABANG_PASSWORD": ("mabang.user_info.philippines.password",),
}


def parse_options(argv, environment):
    parameters = load_hqyl_parameters(environment)
    parser = argparse.ArgumentParser(description="生成 TK 备货数据并归档、交付共享盘")
    parser.add_argument("--date", default=parameters.get("date"))
    parser.add_argument("--output-dir", default=parameters.get("output_dir"))
    parser.add_argument("--shared-root", default=parameters.get("shared_root"))
    add_dingtalk_delivery_arguments(parser, parameters, default_send=False)
    options = parser.parse_args(argv)
    options.run_date = parse_run_date(options.date, default=date.today(), name="date")
    return options


def validate_flow_config(config, process_id):
    if config.get("process_id") != process_id:
        raise RuntimeConfigurationError("公开配置 process_id 不匹配")
    labels = validate_config(config)
    for key in ("country", "process_name", "output_file_name"):
        text = str(config.get(key) or "").strip()
        if not text or Path(text).name != text or text in {".", ".."} or any(c in text for c in "\\/:"):
            raise RuntimeConfigurationError(f"{key} 必须是不含目录的名称")
    if not str(config["output_file_name"]).endswith(".xlsx"):
        raise RuntimeConfigurationError("output_file_name 必须是 xlsx 文件")
    if not isinstance(config.get("filename_date_suffix"), bool):
        raise RuntimeConfigurationError("filename_date_suffix 必须是布尔值")
    if not isinstance(config.get("warehouse_mapping"), dict) or not config["warehouse_mapping"]:
        raise RuntimeConfigurationError("warehouse_mapping 必须是非空对象")
    for name, warehouse_id in config["warehouse_mapping"].items():
        if not name or any(c in name for c in "\\/:") or str(warehouse_id).strip() == "":
            raise RuntimeConfigurationError("warehouse_mapping 包含无效仓库名称或 ID")
    inventory_file_stems(config["warehouse_mapping"], config.get("inventory_file_stems"))
    if not isinstance(config.get("resolve_warehouse_names_by_id", False), bool):
        raise RuntimeConfigurationError("resolve_warehouse_names_by_id 必须是布尔值")
    expected_columns = ["库存SKU", "库存SKU中文名", "仓库名称", "仓库可用库存量", "仓库总在途量", "销量(7/28/42)"]
    if config.get("inventory_columns") != expected_columns:
        raise RuntimeConfigurationError("inventory_columns 与既有六列交付契约不一致")
    mabang = config.get("mabang") or {}
    if not str(mabang.get("base_url") or "").startswith("https://") or mabang.get("verify_ssl") is not True:
        raise RuntimeConfigurationError("马帮必须使用 HTTPS 和证书验证")
    if int(mabang.get("timeout_seconds") or 0) <= 0:
        raise RuntimeConfigurationError("mabang.timeout_seconds 必须大于 0")
    if not set(config.get("optional_stock_labels") or []).issubset(labels):
        raise RuntimeConfigurationError("optional_stock_labels 必须来自 stock_labels")
    return labels


def _shared_directory(config, options, environment):
    root_value = options.shared_root or environment.get("TK_SHARED_ROOT") or config.get("shared_root")
    if not root_value or not Path(root_value).is_absolute():
        raise RuntimeConfigurationError("shared_root 必须是共享盘绝对路径")
    return (Path(root_value) / config["country"] / config["process_name"] /
            "输出文件" / options.run_date.strftime("%Y%m%d"))


def final_filename(config, run_date):
    filename = config["output_file_name"]
    if config["filename_date_suffix"]:
        return f"{Path(filename).stem}{run_date.month}.{run_date.day}.xlsx"
    return filename


def _export_inventory_files(client, config, output_dir, logger):
    """Bind exports to current ERP names while keeping historical delivery names."""
    mapping = config["warehouse_mapping"]
    stems = inventory_file_stems(mapping, config.get("inventory_file_stems"))
    if not config.get("resolve_warehouse_names_by_id", False):
        paths = [Path(path) for path in client.export_realtime_inventory_warehouses(
            mapping, output_dir, skip_existing=True,
        )]
        expected = {f"{name}.xlsx" for name in mapping}
        if {path.name for path in paths} != expected or len(paths) != len(expected):
            raise RuntimeConfigurationError("仓库导出文件清单与配置不一致，禁止遗漏仓库")
        return preserve_inventory_filenames(paths, stems)

    requested = {str(identity).strip() for identity in mapping.values()}
    # Management-directory filters do not define realtime inventory access.
    catalog = client.list_inventory_warehouses()
    if not isinstance(catalog, list):
        raise RuntimeConfigurationError("马帮实时库存可选仓库列表必须是列表")
    current_names = {}
    for row in catalog:
        if not isinstance(row, dict) or row.get("id") is None or isinstance(row.get("id"), bool):
            raise RuntimeConfigurationError("马帮仓库列表包含无效 ID")
        identity = str(row["id"]).strip()
        if not identity:
            raise RuntimeConfigurationError("马帮仓库列表包含空 ID")
        if identity not in requested:
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeConfigurationError(f"马帮仓库名称为空: ID={identity}")
        name = name.strip()
        if identity in current_names and current_names[identity] != name:
            raise RuntimeConfigurationError(f"同一仓库 ID 返回多个名称: ID={identity}")
        current_names[identity] = name
    missing = requested - set(current_names)
    if missing:
        targets = [f"{identity}（{'、'.join(name for name, value in mapping.items() if str(value).strip() == identity)}）"
                   for identity in sorted(missing)]
        raise RuntimeConfigurationError(
            "当前账号实时库存页面不可选择配置仓库: " + ", ".join(targets)
            + "；请核对仓库状态和该账号仓库权限，不能按零库存替代"
        )
    if len({name.casefold() for name in current_names.values()}) != len(current_names):
        raise RuntimeConfigurationError("不同仓库 ID 返回相同名称，禁止覆盖库存文件")
    # Preserve the first occurrence of each ID without changing configured scope.
    export_mapping = {}
    for identity in mapping.values():
        identity = str(identity).strip()
        export_mapping.setdefault(current_names[identity], identity)
    inventory_file_stems(export_mapping)
    logger.info("按 ID 核对仓库名称: %s 个仓库，保留 %s 个交付文件", len(export_mapping), len(mapping))
    result = []
    # Isolate download names from delivery names, including renamed-name collisions.
    with TemporaryDirectory(prefix="tk-inventory-", dir=output_dir) as temporary:
        download_dir = Path(temporary)
        paths = [Path(path) for path in client.export_realtime_inventory_warehouses(
            export_mapping, download_dir, skip_existing=False,
        )]
        expected = {f"{name}.xlsx" for name in export_mapping}
        if ({path.name for path in paths} != expected or len(paths) != len(expected)
                or any(path.parent.resolve() != download_dir.resolve() for path in paths)):
            raise RuntimeConfigurationError("仓库导出文件清单与配置 ID 不一致，禁止遗漏仓库")
        exports = {path.stem: path for path in paths}
        for historical_name, identity in mapping.items():
            source = exports[current_names[str(identity).strip()]]
            target = atomic_copy_file(source, output_dir / f"{stems[historical_name]}.xlsx")
            if file_sha256(source) != file_sha256(target):
                raise RuntimeConfigurationError(f"仓库别名文件复制校验失败: {target.name}")
            result.append(Path(target))
    return result


def _publish_exports(result, files, shared_directory):
    """Copy the explicit completed-export list, excluding logs and scratch files."""
    shared = []
    for source in files:
        target = atomic_copy_file(source, shared_directory / source.name)
        if file_sha256(source) != file_sha256(target):
            raise RuntimeError(f"共享盘交付校验失败: {source.name}")
        shared.append(str(target))
    result["shared_output_directory"] = str(shared_directory)
    result["shared_output_files"] = shared
    result["metrics"]["shared_output_count"] = len(shared)
    return result


def run_flow(public_config_path, process_id, argv=None, *, environ=None, client_factory=None, transport=None):
    environment = dict(os.environ if environ is None else environ)
    options = parse_options(argv, environment)
    config = load_flow_config(public_config_path, flow_id=process_id, environment=environment)
    labels = validate_flow_config(config, process_id)
    request_config = load_flow_config(REQUEST_CONFIG_PATH, environment=environment)
    template = request_config.get("reports_stockShopReports") or {}
    if not isinstance(template.get("params"), dict) or not isinstance(template.get("form_data"), dict):
        raise RuntimeConfigurationError("公开请求配置缺少销量报表请求模板")
    if not str(request_config.get("url") or "").startswith("https://"):
        raise RuntimeConfigurationError("公开请求配置 url 必须使用 HTTPS")
    context = prepare_dingtalk_file_delivery(
        process_id, options.send, options.recipients, environment=environment,
        legacy_recipients_config=Path(public_config_path).with_name(
            Path(public_config_path).name.replace(".public.yaml", ".yaml")),
    )
    assets = resolve_asset_environment(
        MABANG_ASSETS, environment=environment, legacy_asset_paths=LEGACY_ASSET_PATHS,
        required=MABANG_ASSETS,
    )
    shared_directory = _shared_directory(config, options, environment)
    output_dir = resolve_output_directory(
        PROJECT_ROOT / "outputs" / process_id / options.run_date.strftime("%Y%m%d"),
        requested=options.output_dir, environment=environment,
    )
    secrets = list(assets.values())
    if context is not None:
        secrets.extend([context.app_key, context.app_secret, context.sender_user_id,
                        context.space_id, context.parent_id, *context.recipient_user_ids])
    logger = create_safe_logger(process_id, output_dir / "logs", secrets=secrets)
    country = config["country"]
    ranges = build_date_ranges(datetime.combine(options.run_date, datetime.min.time()), **config["periods"])
    logger.info("流程启动: process_id=%s date=%s", process_id, options.run_date)
    logger.info("销量区间: weekly=%s~%s monthly=%s~%s",
                ranges["weekly"]["start"], ranges["weekly"]["end"],
                ranges["monthly"]["start"], ranges["monthly"]["end"])
    files = []
    label_frames = {}
    client = (client_factory or MabangClient)(
        config["mabang"]["base_url"], timeout_seconds=config["mabang"]["timeout_seconds"], verify_ssl=True,
    )
    try:
        client.login(assets["MABANG_USERNAME"], assets["MABANG_PASSWORD"])
        for label in labels:
            path = output_dir / f"{label}.xlsx"
            logger.info("导出库存分类: %s", label)
            try:
                client.export_stock_label_skus(label_name=label, output_path=path)
            except MabangApiError as exc:
                # Preserve the old optional-sheet behavior only for a confirmed
                # missing category. Network/API/parsing failures remain failures.
                if (label not in config.get("optional_stock_labels", []) or
                        str(exc) != f"查询ID失败: 未找到库存名称{label}"):
                    raise
                logger.warning("可选库存分类不存在，保留空工作表: %s", label)
                label_frames[label] = pd.DataFrame(columns=["库存SKU"])
                continue
            label_frames[label] = load_stock_label_dataframe(path)
            files.append(path)
        sales_paths = {}
        for period, days in (("weekly", 7), ("monthly", 28)):
            path = output_dir / f"TK{country}_{days}天销量.xlsx"
            form = deepcopy(template["form_data"])
            form.update({"timeStart": f"{ranges[period]['start']} 00:00:00",
                         "timeEnd": f"{ranges[period]['end']} 23:59:59",
                         "shopLabelIds[]": str(config["sales_label"]["label_id"]), "moneyType": "US"})
            logger.info("导出近 %s 天销量", days)
            client.export_sales_report_by_product_all(
                url=request_config["url"], params=dict(template["params"]), form_data=form,
                output_path=path, platform_or_shop="stockShop", fallback_to_pagination=True,
                preserve_export_columns=True,
            )
            sales_paths[period] = path
            files.append(path)
        logger.info("导出 %s 个仓库库存", len(config["warehouse_mapping"]))
        inventory_paths = _export_inventory_files(client, config, output_dir, logger)
        files.extend(inventory_paths)
    finally:
        client.close()

    weekly, weekly_invalid = clean_sales_dataframe(_read_excel_with_text_skus(sales_paths["weekly"]))
    monthly, monthly_invalid = clean_sales_dataframe(_read_excel_with_text_skus(sales_paths["monthly"]))
    weekly_skus = set(weekly["SKU"].apply(normalize_sku)) - {""}
    logger.info("校验库存新旧销量结构并匹配 %s 个 SKU", len(weekly_skus))
    inventory, duplicates, warehouses = load_and_filter_inventory(
        inventory_paths, weekly_skus, config["inventory_columns"],
    )
    final_file = output_dir / final_filename(config, options.run_date)
    write_output_workbook(country, weekly, monthly, inventory, final_file, label_frames, labels)
    validate_output_workbook(country, final_file, labels)
    files.append(final_file)
    metrics = {"weekly_sales_rows": len(weekly), "monthly_sales_rows": len(monthly),
               "weekly_unique_sku_count": len(weekly_skus), "inventory_row_count": len(inventory),
               "inventory_warehouse_count": warehouses, "duplicate_inventory_row_count": duplicates,
               "invalid_sales_quantity_count": weekly_invalid + monthly_invalid,
               "unmatched_inventory_sku_count": len(weekly_skus - set(inventory["库存SKU"].apply(normalize_sku))),
               "stock_label_sheet_count": len(labels), "stock_label_sku_count": sum(map(len, label_frames.values())),
               "output_file_count": 1}
    result = {"process_id": process_id, "processed_count": len(weekly_skus), "output_file_count": 1,
              "output_directory": str(output_dir), "output_files": [str(path) for path in files],
              "final_file": str(final_file), "metrics": metrics}
    logger.info("最终工作簿校验通过，交付共享盘: %s", final_file.name)
    result = _publish_exports(result, files, shared_directory)
    result = deliver_philippines_files(
        result, context, [final_file], process_id, options.run_date, environment=environment, transport=transport,
    )
    return write_pipeline_success_manifest(
        result, process_id=process_id, run_date=options.run_date, artifacts={"tk_restock": final_file},
        environment=environment,
    )


def run_cli(main, argv=None):
    result = main(argv)
    print(json.dumps(result, ensure_ascii=False))
    return 0

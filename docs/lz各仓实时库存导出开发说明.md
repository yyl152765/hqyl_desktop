# lz 各仓实时库存导出 开发说明

对应上游提交 `c76e4578`（Add LZ warehouse inventory exports and scoped output directories）。

## 使用方式

1. 侧栏进入「马帮」分组 → 「lz各仓实时库存」（开发入口：`python launcher/main.py --page=lz-warehouse-realtime-inventory`）。
2. 页面加载时自动读取本功能绑定的马帮账号；未绑定会提示先到「账号管理」绑定 `mabang` 账号。
3. 「仓库多选」里选目标仓库（默认预选泰国 TH04 / TH05 / TH06），支持搜索、刷新、全选当前结果。
4. 选好输出目录后点「选择仓库并导出」。
5. 任务日志与结果面板展示每仓文件、记录数、导出分页数、失败明细。

## 参数表

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `warehouse_names` | 前端多选 | 至少选 1 个仓库；重复名称会去重，顺序按选择顺序 |
| `output_dir` | 前端目录选择 | 留空则用默认目录；保存后写入 `tool_output_dirs["lz_warehouse_inventory"]` |
| `username` / `password` | 桥接层注入 | 由 `_lz_inventory_account()` 从绑定账号取出，前端不传 |

桥接方法（`backend/app_bridge.py`）：

- `get_lz_warehouse_inventory_info()` —— 返回账号、可选仓库列表和当前输出目录（含 `default_output_dir`）。
- `get_lz_warehouse_inventory_warehouses()` —— 实时拉取马帮库存查询页的仓库列表。
- `start_lz_warehouse_inventory(payload)` —— 校验入参后以 `tool="lz_warehouse_inventory"` 启动长任务。

## 口径

- 走马帮「商品 → 库存查询」同一套接口：搜索 `warehouse.searchwarehousestock`、翻页 `warehouse.getSearchWarehouseStockPage`、导出 `warehouse.doexportwarehousestock`。
- **每个仓库一个文件**，各自使用独立的搜索 token，不做跨仓合并。
- 业务行不过滤、不去重，**零库存行也保留**。
- 单次导出分页 `EXPORT_PAGE_SIZE = 10000`，最多 `MAX_EXPORT_PAGES = 1000` 页。
- 结果面板预览只取每仓前 100 行（`PREVIEW_ROW_LIMIT`），并给出 `preview_truncated`。
- `record_count` 是导出后实际行数，`source_record_count` 是导出前源记录数；`is_complete` 为「无任何仓库失败」。
- 输出目录按工具隔离：默认 `桌面/log/lz各仓库存`，不跟随其他功能最后选择的目录（见 `backend/output_paths.py`）。

## 输出结构

```
<output_dir>/
  <仓库名>.xlsx           # 每仓一份，表头与马帮导出一致，首行冻结、按列宽自适应
```

任务结果对象（`start_lz_warehouse_inventory` 返回 / 任务 `applyResult`）：

| 字段 | 说明 |
| --- | --- |
| `output_dir` / `output_root` | 本次输出根目录 |
| `output_files` | 全部仓库文件路径；`output_file` 为第一个 |
| `warehouse_counts` | `{仓库名: 行数}` |
| `record_count` / `source_record_count` / `export_page_count` | 汇总行数、源行数、分页数 |
| `selected_warehouse_count` | 用户勾选的仓库数（与成功数可能不同） |
| `preview_columns` / `rows` / `preview_limit` / `preview_truncated` | 预览列与行 |
| `failures` | `[{warehouse_name, error}, ...]` |
| `is_complete` / `completion_message` | 完成度与提示语 |

## 关联改动：独立输出目录

`backend/output_paths.py` 新增 `SUPPORTED_TOOLS = {lazada_ads_data, lazada_bill_detail, lz_warehouse_inventory}`：

- `directory_scope_key(tool, country, kind)` —— 生成 `tool:country:kind` 形式的存储键，`kind` 仅 `output` / `screenshot`，且 `screenshot` 只对 `lazada_bill_detail` 开放。
- `default_output_directories(tool, country)` —— 广告数据固定 `Desktop/log/lz泰国广告数据`；lz 库存固定 `Desktop/log/lz各仓库存`；后台收支按国家生成 `Lazada{国家}账单明细` 与 `Lazada{国家}账单明细截图`。
- `resolve_output_directories(settings, tool, country)` —— 读取 `settings.tool_output_dirs` 覆盖值，返回实际目录与 `default_*` 兜底值。
- 国家码支持 `TH/PH/MY/ID/VN` 及中文名，非法国家或工具直接抛 `ValueError`。

`backend/config_store.py` 新增 `tool_output_dirs` 字段持久化各作用域的自定义目录。

## 限制与注意事项

- 必须在「账号管理」绑定 `mabang` 账号；未绑定时前端会引导跳转，不进入任务。
- 仓库名必须能在马帮库存查询页匹配到 ID，匹配不到会在 `failures` 里给出失败原因，其余仓库照常导出。
- 单仓超过 1000 万行（10000 × 1000）会被分页上限截断，属预期保护，不静默丢数据。
- 预览只有前 100 行，判断完整结果请看导出文件本身。
- `tool_output_dirs` 的键含国家和目录类型，切国家只影响后台收支，广告与库存目录不受影响。

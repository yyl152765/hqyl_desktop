# BS 商品ID查询

## 使用方式

入口：工作台的「BS 商品ID查询」，或侧栏 BigSeller → Shopee 商品 → BS 商品ID查询。

1. 在设置中绑定并选择 BigSeller 账号。账号不从页面参数直接读取，也不硬编码到业务代码。
2. 配置已有的图形验证码服务账号。登录复用 `bigseller_sync` 的配置解析和 `bigseller_request_util`，新模块不会打印登录组件的验证码或原始异常。
3. 输入 SKU，选择输出目录，开始查询。支持换行、英文/中文逗号和分号、Tab 分隔；去掉首尾空格，保留内部空格和大小写；重复项按首次出现顺序去重。上限为 5000 个不同 SKU。
4. 查看逐条进度和结果，使用「打开 Excel」或「打开输出目录」。切换页面再返回会恢复本模块最近任务。

从 **0.2.42** 起，搜索字段固定为 **SKU**，选择 **Fuzzy Search**，支持输入子 SKU。当前范围固定为 **Shopee 在售商品、当前账号全部可访问店铺**。不会触发商品/库存同步或其他业务写入。

## 查询与导出契约（0.2.42 起）

请求为 POST `/api/v1/product/listing/shopee/pageList.json`，正文：

```json
{
  "searchType": "sku",
  "searchContent": "输入的 SKU",
  "inquireType": 0,
  "shopeeStatus": "live",
  "status": "active",
  "orderBy": "views",
  "desc": true,
  "pageNo": 1,
  "pageSize": 50,
  "timeType": "create_time",
  "startDateStr": "",
  "endDateStr": ""
}
```

分页响应采用 `data.page.rows`。只读取服务端返回的第一行，不对店铺分组、不自行改写相同 Views 的顺序。Item ID 当前实现取首行 `itemId`，绝不退回 BS 内部 `id`、`productId` 或 `listingId`，首行缺少有效 Item ID 时记为失败，不选择第二行。

**版本差异**：BS 的 Parent SKU 选项值为 `parentSku`，SKU 选项值为 `sku`，两者是不同搜索字段。`0.2.41` 按当时需求使用 Parent SKU；`0.2.42` 按用户最新要求改为 **SKU 模糊搜索**，支持子 SKU 输入，当前正式请求使用 `searchType="sku"`。Fuzzy Search 仍为 `inquireType=0`，Views 仍用 `orderBy="views"`、`desc=true`，Item ID 仍取首行 `itemId`。这些选项和值已在之前核对的 BS 页面公开脚本中区分，来源：[Shopee 页面脚本（2026-08）](https://cdn.bigseller.pro/bs-web/2026-08/60675.chunk.98ddce.js)。

Excel 工作表名为 `SKU-Item ID`，严格只有 `SKU`、`Item ID` 两列，均保存为文本，保留前导零和长数字，输入以 `=` 开头时也不会成为公式。未匹配、失败、因登录/权限/限流停止而未执行的 SKU 均保留，Item ID 留空。页面与任务日志分别标明原因；页面最多预览 500 行，每页 50 行，Excel 包含全部输入结果。每次导出采用唯一文件名，避免覆盖上次结果。

## 失败处理

- 只有明确成功且 `data.page.rows=[]` 才算未匹配，接口错误或响应结构变化算失败。
- 网络超时、连接失败和服务端临时错误最多重试 3 次。
- 登录在查询期间失效时，全批次最多重新登录一次。
- 登录仍失败、权限不足或平台限流时停止后续请求；保留所有未完成 SKU，方便重试。
- 部分/全部查询失败会在页面显示「部分失败」/「查询失败」，不能当成没有匹配商品。
- 初次登录失败或验证码配置缺失会直接使任务失败，不导出误导性的空结果。

## 代码位置

| 部分 | 文件 |
| --- | --- |
| 参数、查询、登录包装、Excel | `backend/services/bigseller_item_id_query.py` |
| 桌面桥接 | `backend/app_bridge.py` 的 `start_bigseller_item_id_query` |
| 页面 | `frontend/pages/bigseller-item-id-query.html` |
| 页面行为 | `frontend/assets/pages/bigseller-item-id-query.js` |
| 工作台、侧栏与任务进度 hook | `frontend/assets/pages/dashboard.js`、`frontend/assets/common.js` |
| 直接启动入口 | `launcher/main.py` 的 `--page=bigseller-item-id-query` |

桌面桥接传入 `{sku_text, output_dir}`，后端注入选定账号和验证码服务配置。任务标识为 `bigseller_item_id_query`，只在任务上下文保存 SKU 数，不保存密码或整个请求。任务结果返回 `sku_count`、`matched_count`、`not_found_count`、`failed_count`、`rows`、`preview_limited`、`output_file`、`output_dir`。

## 当前版本验证状态（0.2.42）

- 正式搜索契约为 `sku / inquireType=0 / views / desc=true / pageNo=1`，完整保留输入的子 SKU，不裁剪后缀。
- 431 项测试、179 项子测试通过。使用未被临时改写的正式服务重新查询用户提供的 10 个 SKU，10 个全部命中、0 未匹配、0 失败；与独立 SKU 补查结果一致，正式 Excel 读回验证通过。
- 0.2.42 安装包与两个 ZIP 已生成；冻结程序隔离自检退出 0，包内 SKU 搜索契约、前端文件、公开配置、版本及哈希校验通过。尚未上传线上更新源。
- 验收记录位于 `acceptance_outputs/bs_item_id_query_0_2_42_20260826/`；发布产物与 SHA-256 见 [0.2.42 发布说明](发布说明_0.2.42.md)。

## 历史验证记录（0.2.41，2026-08-26）

以下记录保留旧版本 **Parent SKU** 搜索的开发及打包证据，仅对应 `0.2.41`，不代表 `0.2.42` 的 SKU 搜索验收结果。

- 本地完整功能与回归测试：425 项测试、164 项子测试通过（包含限流保护、桥接上下文、验证码配置优先级）。
- 新模块覆盖输入去重、首条优先、内部 ID 排除、失败与未匹配区分、网络重试、登录恢复、账号隔离、Excel 读回和公式安全、500 行预览上限及前端事件行为。
- 浏览器已检查页面布局、演示批量输入、去重和结果预览；演示明确标记、不发真实商品请求、不伪造输出文件。
- **真实接口验收通过**：用户授权后使用项目现有登录组件登录 BS，确认响应同时包含平台 `itemId` 和内部 `id`。根据页面脚本发现并修正 `searchType` 为 `parentSku`，随后重新执行完整验收：3 个从该账号商品列表选取的实际 SKU 命中，1 个唯一无匹配测试项返回空结果，0 失败。验证每次返回的 Views 非递增、服务输出等于返回列表首条 Item ID，并对生成的 Excel 读回，确认仅两列、4 个结果行、文本格式与空白未匹配值正确。
- 真实验收文件与脱敏检查结果位于 `acceptance_outputs/bs_item_id_query_20260826/`。该目录不含账号密码、Cookie 或原始登录响应。账号密码仅通过临时隐藏输入传入进程内存，未写入源码或验收日志，未自动绑定到应用设置。
- BS 浏览器页签连接不稳定，真实业务验收通过正式服务的请求登录和查询完成，并用 BS 页面公开源码核对选项；未宣称完成 BS 网页逐项点击验收。
- 后续已完成 `0.2.41` 本地发版打包：431 项测试、179 项子测试通过，冻结程序离线自检及发布包校验通过；尚未上传线上更新源。产物及校验值见 [0.2.41 发布说明](发布说明_0.2.41.md)。

运行测试（使用已安装项目依赖的 Python）：

```powershell
python -m pytest tests -q
```

源码开发直接启动：

```powershell
python desktop_entry.py --page=bigseller-item-id-query
```

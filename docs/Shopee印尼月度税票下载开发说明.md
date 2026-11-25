# Shopee 印尼月度账单与税票下载

入口：桌面平台 → 紫鸟流程 → 印尼月度账单与税票下载。

## 下载范围

按历史完整自然月下载 **My Income → Released** 的收入账单 Excel，以及 **My Tax Invoices** 中每一行的两类 PDF：

| 页面动作 | 文件内容 | 归档目录 |
| --- | --- | --- |
| Released → Export / My Reports | 整月收入账单 XLSX | `Income_Report` |
| Master Invoice | 服务费账单 | `Master_Invoice` |
| Tax Invoice | Faktur Pajak 税务发票 | `Tax_Invoice` |

不下载 Income Statements 和 Itemised Billing Excel，不提取或修改 PDF 内的税号、金额等内容。每行必须同时存在两种 PDF 动作，缺失、重复或禁用时报告失败，避免把不完整结果显示为完成。

## 使用方法

1. 在设置中绑定并选择有 Shopee 印尼店铺权限的**紫鸟账号**。马帮账号和开发用 CLI profile 不属于桌面运行端的紫鸟绑定。
2. 选择账单月份（默认上一个完整自然月），每行填写一个完整紫鸟店铺名或简称，例如 `印尼001`。
3. 确认紫鸟 V6 客户端、驱动目录与输出目录，点击“开始下载”。店铺预览是可选操作；正式启动时重新读取当前账号的店铺并核对身份。
4. 完成后点击“打开批次目录”。失败店铺可重试；已成功店铺的 Excel 和 PDF 会先复核账期、完整性与哈希，通过后跳过。

开始任务会按现有紫鸟运行规范重启所选客户端进入 WebDriver 模式，并按名单逐店运行。登录态有效时先进入收入报表历史页；密码验证弹窗已由紫鸟自动填充时自动点击 Verify。缺少填充、验证码或其他人工验证会给出明确错误。

同一店号匹配到多个店铺时须改填完整名称；不会凭店号跨国家或跨平台选择店铺。当前月份和未来月份不能启动下载。

## 输出与失败恢复

默认根目录为 `桌面/log/Shopee印尼月度税票`，该功能独立保存输出目录偏好。

```text
<输出根目录>/<YYYY-MM>/<时间戳>_<批次标识>/
  manifest.json
  logs/run.log
  invoices/<序号>_<店铺简称>/attempt_<次数>/
    Income_Report/Income.released.id.YYYYMMDD_YYYYMMDD.xlsx
    Master_Invoice/row_001/<原始文件名>.pdf
    Tax_Invoice/row_001/<原始文件名>.pdf
```

清单记录所选月份、店铺身份、账单行、文件类型、文件大小、SHA-256 和失败原因。下载目录按动作隔离，等待临时文件消失、文件大小稳定，并检查 `%PDF-` 文件头和 `%%EOF` 结束标记后才计入成功。保持原文件名，缩短上级目录以兼容 Windows 路径限制。

当税票页明确显示 `No receipt found` 时，仍必须取得有效的整月收入 Excel，才将该店记为成功。只显示空表或尚未加载完成不会判定为无数据。下载部分成功后遇到错误，逐份保留已验证的 Excel/PDF；重试使用新的尝试目录，不覆盖原文件。其他账号不能恢复该账号的批次。

收入报表先检查完整 My Reports → Income 历史（包括分页和已经下载过的条目），按精确文件名 `Income.released.id.YYYYMMDD_YYYYMMDD.xlsx` 匹配目标完整自然月，不按报表申请时间匹配。只检查 Latest Reports 不能证明历史不存在。发现 Download 即下载；发现 Processing 则最多等待30分钟，每20秒刷新历史并重新定位文件，不重复 Export。只有完整历史确认缺失，才跳转到该月首末日期，复核 Released、URL 和页面日期后点击一次 Export。超过100页、分页不稳定、未知空态或未知状态会报错；重试仍从历史开始。

Excel 校验包括文件头、完整 ZIP/CRC、Summary 和 Adjustment 内部起止日期、印尼盾 Rp、Income/Seller Fee 表头。支持大店的连续编号分表（例如 `Income - 1/2/3`），不以工作簿错误的 A1:A1 维度判为空。清单记录收入报表来源（历史复用/新建）、账期和 SHA-256。每店完成条件为一份整月 XLSX，加每行两份 PDF。

批次清单升级为 v2。v1 的仅税票完成记录会显示需要补下载，不能跳过；首次重试保留 `manifest.tax_only_v1.json` 和旧文件，再写入新尝试结果。

## 实现与开发规则

- 项目规则见根目录 `AGENTS.md`：开发探查、定位和下载验证使用 `ziniao-cli`；正式执行复用 `backend/core/ziniao_browser.py` 的紫鸟 WebDriver，不依赖 CLI、CLI profile 或 ZClaw Bridge。
- `backend/services/shopee_id_income_report.py`：整月报表匹配、历史分页、等待生成、Excel 下载与完整性校验。
- `backend/services/shopee_id_invoice_gateway.py`：实页状态、密码验证、月份选择、账单动作定位、PDF 下载与店铺关闭。
- `backend/services/shopee_id_monthly_invoice.py`：月份与店铺匹配、批次清单、文件验证、失败重试。
- `backend/app_bridge.py`：账号绑定、独占任务槽、客户端准备和前端接口。
- `frontend/pages/shopee-id-monthly-invoice.html` 与对应 JS：月份、店铺名单、日志、结果和重试。

正式下载网址为 `https://seller.shopee.co.id/portal/finance/income/invoice`。月份支持英语和印尼语名称；动作名称按当前实页的 `Master Invoice` 与 `Tax Invoice` 识别。税票页尚未适配其他动作语言或分页列表：税票页出现分页控件、多个表格或无法唯一定位时明确失败，防止遗漏。真实账号配置、带签名的下载地址与账单样本只保存于本机，不进入仓库和安装包。

## 1.0.7 税票功能基线验证（2026-11-25）

- CLI 实页：历史月份 3 行，共下载 3 份服务费账单和 3 份税务发票；6 份 PDF 均可解析为单页，代表样本已渲染核对。另验证了空月份的 `No receipt found`。
- 自动检查：新增功能与输出目录、桌面入口、前端资源、导航相关 124 项检查通过。覆盖错月、错误站点、歧义店号、密码填充、暂存空态、分页拒绝、不完整下载、部分失败、关闭失败和跨账号重试。
- 页面预览：本地 Edge 无界面浏览器完成账号加载、月份默认值、匹配预览、启动和结果展示；没有 JavaScript 页面错误。预览数据为合成数据，不作为真实下载验收。
- 真实 WebDriver：使用桌面平台已绑定紫鸟账号，经 `AppBridge.start_shopee_id_monthly_invoice` 启动独占任务，完成客户端切换至自动化模式、店铺身份核验、2026 年 10 月选择、6 份 PDF 下载、清单落盘和店铺关闭；任务返回 `success`，批次返回 `complete`。6 份文件均重新解析并核对 SHA-256。首次切换时共享客户端保护检测到关闭期间新建子进程并停止；确认原店铺已退出后重新启动成功，未绕过进程归属校验。

回归命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_shopee_id_monthly_invoice tests.test_shopee_id_invoice_gateway tests.test_shopee_id_monthly_bridge tests.test_output_paths tests.test_tool_output_directory_isolation tests.test_launcher_pages tests.test_frontend_integrity tests.test_sidebar_preferences tests.test_sidebar_navigation
```

## 整月收入报表扩展验证（2026-11-25）

- CLI 实页：一店复用已有整月报表并验证重新下载；另一店扫描3页完整历史确认缺失后只提交一次 Export，待生成完成再下载。两份 XLSX 均通过内部账期、币种、工作表和 ZIP 校验；大店样本包含3张 Income 分表和2张 Seller Fee 分表。真实样本及具体店铺信息仅保存本机。
- 重点自动检查覆盖历史第2页复用、Processing刷新、完整历史缺失后单次导出、超时不重复导出、错误区间、暂存空表、分页上限、损坏 ZIP、错账期、错币种、错误维度、编号分表、Excel 哈希重验、旧批次迁移及部分文件保留。
- 真实 WebDriver：通过平台已绑定账号完成一店2026年10月的一份收入 Excel、三份 Master Invoice 和三份 Tax Invoice，批次完成、文件哈希与 CLI 下载一致。另一店收入 Excel 下载通过，税票页缺少 Tax Invoice 按钮时保留已验证收入文件并报告不完整。一次税票页连接中断后重试完成，客户端启动仍沿用原有进程归属保护。
- 发版回归：523项测试、585项子用例通过，无失败或跳过。CLI 与 WebDriver 验收结果分别记录；冻结安装包检查结果记录于发布验证清单，不将旧版六份 PDF 验收计作本次收入报表验收。

收入扩展回归：在上方命令中追加 `tests.test_shopee_id_income_report`。

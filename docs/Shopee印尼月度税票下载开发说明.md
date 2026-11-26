# Shopee 印尼月度账单与税票下载

入口：桌面平台 → 紫鸟流程 → 印尼月度账单与税票下载。

## 下载范围

按历史完整自然月下载 **My Income → Released** 的收入账单 Excel，以及 **My Tax Invoices** 中每一行的两类 PDF：

| 页面动作 | 文件内容 | 归档目录 |
| --- | --- | --- |
| Released → Export / My Reports | 整月收入账单 XLSX | `Income_Report` |
| Master Invoice | 服务费账单 | `Master_Invoice` |
| Tax Invoice | Faktur Pajak 税务发票 | `Tax_Invoice` |

不下载 Income Statements 和 Itemised Billing Excel，不提取或修改 PDF 内的税号、金额等内容。每行须有唯一且可点击的 Master Invoice。页面未提供 Tax Invoice 时，先保存可用账单并标记“平台未提供税票”，不计为文件齐全；重复、禁用、未知按钮或异常结构仍报告失败。

## 使用方法

1. 在设置中绑定并选择有 Shopee 印尼店铺权限的**紫鸟账号**。马帮账号和开发用 CLI profile 不属于桌面运行端的紫鸟绑定。
2. 选择账单月份（默认上一个完整自然月）。店铺范围默认为“当前紫鸟账号下全部印尼 Shopee 店铺”，无需填写名单或拆分批次。也可切换到“只处理指定店铺”，每行填写完整名称或简称，例如 `印尼001`；指定名单也不再限制200家。
3. 确认紫鸟 V6 客户端、驱动目录与输出目录，点击“开始处理全部店铺”。“查看全部店铺”是可选预览；正式启动时重新读取当前绑定账号的完整店铺列表，按平台与站点筛选，保存稳定 ID 和名称后逐店执行。
4. 完成后点击“打开批次目录”。点击“重试失败 / 补查税票”可继续失败店铺并重新检查平台缺失税票；已成功店铺的 Excel 和 PDF 会先复核账期、完整性与哈希，通过后跳过。

开始任务会按现有紫鸟运行规范重启所选客户端进入 WebDriver 模式，并按名单逐店运行。登录态有效时先进入收入报表历史页；密码验证弹窗已由紫鸟自动填充时自动点击 Verify / Verifikasi。缺少填充、验证码或其他人工验证会给出明确错误。

指定名单模式下，同一店号匹配到多个店铺时须改填完整名称；不会凭店号跨国家或跨平台选择店铺。全部店铺模式按稳定 ID 区分店铺，排除非 Shopee、非印尼或身份信息冲突的记录；没有可确认店铺时停止并提示。当前月份和未来月份不能启动下载。

每批开始时固定店铺范围、账号和月份；运行中新增店铺不会加入本批，下一次新建任务才会读取。重试沿用原批名单，只继续失败、中断或待补税票的店铺；成功文件须通过原有完整性校验才能跳过。切换账号后预览和恢复入口随之更新，不能用其他账号重试本批。

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

当税票页明确显示 `No receipt found` / `Tidak ada faktur` 时，仍会独立核对整月收入报表。完整历史没有目标月后，先核对 Released、完整日期和收入明细，再提交一次 Export；只有新出现 `No data is available for download.`，且点击前后目标月明细均明确为空，才能确认无收入报表。仍会检查当月 My Tax Invoices：两类均为空时显示“当月无账单，已跳过”，继续下一家；有 PDF 时正常下载。空明细可能仍有调整项，因此仅空表、缺历史记录、超时或窗口关闭都不能判为无账单。下载部分成功后遇到错误，逐份保留已验证的 Excel/PDF；重试使用新的尝试目录，不覆盖原文件。其他账号不能恢复该账号的批次。

收入报表先检查完整 My Reports → Income 历史（包括分页和已经下载过的条目），按文件名 `Income.released.id.YYYYMMDD_YYYYMMDD.xlsx` 或印尼语 `Income.sudah dilepas.id.YYYYMMDD_YYYYMMDD.xlsx` 精确匹配目标完整自然月，不按报表申请时间匹配。只检查 Latest Reports 不能证明历史不存在。发现 Download 即下载；发现 Processing / Sedang Diproses 则最多等待30分钟，每20秒刷新历史并重新定位文件，不重复 Export。只有完整历史确认缺失，才跳转到该月首末日期，复核 Released、URL 和页面日期后点击一次 Export。超过100页、分页不稳定、未知空态或未知状态会报错；重试仍从历史开始。

Excel 校验包括文件头、完整 ZIP/CRC、Summary 内部起止日期、印尼盾 Rp、Income（印尼语 Penghasilan）/Seller Fee 表头；Adjustment 页允许平台省略，存在时仍严格核对账期和币种。支持实页核对的英文及印尼语标签。支持大店的连续编号分表（例如 `Income - 1/2/3`），不以工作簿错误的 A1:A1 维度判为空。清单记录收入报表来源（历史复用/新建）、账期和 SHA-256。通常每店文件齐全条件为一份整月 XLSX，加每行两份 PDF；收入报表经上述严格证据确认为空时允许零份 XLSX，其他文件仍按实际行逐份校验；缺 Tax Invoice 的店铺单独显示待补状态，保留页面按钮证据和已有文件。

批次清单升级为 v4，新增 `empty_month` 终态和对应月份的收入空数据证据。同批重试会复核证据后跳过已确认无账单的店铺；新建批次重新查询。可继续 v2/v3 批次，首次写入前分别备份为 `manifest.v2.json` / `manifest.v3.json`；已完整成功的店铺校验后跳过。v1 的仅税票完成记录会显示需要补下载，不能跳过；首次重试保留 `manifest.tax_only_v1.json` 和旧文件，再写入新尝试结果。

清单原子替换遇到 Windows 临时占用时有限重试，避免进度读取导致整批中断；持续无写权限仍报错，旧清单和已序列化的临时清单保留。

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

## 全部印尼店铺模式验证（1.0.9，2026-11-25）

- 开发侧通过 `ziniao-cli store list --all` 读取完整1866家店铺；桌面侧通过已绑定账号的 `ZiniaoBrowser` 只读预览核对，两侧筛选出的211家 Shopee 印尼店铺，其稳定 ID、名称和平台集合完全一致。未启动211家店铺的真实全量下载。
- 合成211店批次验证全部逐店执行，第201家失败不阻断后续店；重试只执行这一家，通过文件完整性校验后已完成店铺跳过。覆盖250个指定店名、空列表、站点冲突、重复 ID、旧清单兼容及跨账号拒绝。
- 实际 Edge 无界面页面验证默认全部模式、指定名单切换、预览、启动及结果；使用明确的演示数据，无页面 JavaScript 错误。
- 533项测试与597项子用例通过，无失败或跳过；按批次运行，每批进程树内存上限1800 MiB，最高峰值1223.5 MiB。Antigravity `gemini-4-argon-high` 对范围、账号和重试行为作只读复核，无新增可执行问题，未切换模型。
- 下载网关与收入报表模块保持1.0.8代码，沿用前节真实下载基线；当前版本新增验收针对完整店铺选择、批次执行和恢复，不将模拟211店执行描述为真实下载验收。

全部店铺回归：在前述命令中追加 `tests.test_shopee_id_all_stores`。

## 兼容修复验证（1.0.10，2026-11-26）

- CLI 实页确认缺 Tax Invoice 的账单行、无 Adjustment 页的有效 XLSX、印尼语 Verifikasi / Sudah Dilepas / Sedang Diproses、文件名和工作簿标签；完整历史缺少目标月后仅点击一次 Export 并下载验证。
- 当前源码的正式 WebDriver 在 4 家代表店下载 4 份收入 Excel、9 份服务费账单和 3 份税票。3 家平台未提供税票，1 家文件齐全，下载失败店铺为 0；缺失税票保持待补状态。3 份 Excel 和 8 份 PDF 的 SHA-256 与 CLI 下载相同。
- 合成用例覆盖平台缺税票与异常按钮的区别、印尼语历史复用与仅导出一次、工作簿账期和分表、旧 v2 批次恢复、补查后转为完整成功、Windows 文件读取占用与持续无权限。前端实际任务渲染显示待补税票与可用的重试入口。
- 未执行 211 家真实全量下载，冻结安装包未重新连接店铺，也未在员工电脑执行安装或恢复原批次。安装包通过离线冻结自检、源码与载荷校验后发布。


## 无账单店铺验证（1.0.11，2026-11-26）

- CLI 实页确认：完整收入历史为空，目标自然月收入明细为空，单次 Export 返回 `No data is available for download.`；目标月税票列表显示 `No receipt found`。Export 按钮在空月份仍可点击，不能仅靠按钮状态判断。
- 正式 WebDriver 通过桌面任务入口验证三种代表店：一店当月无账单自动跳过，下一店正常下载，另一店保留平台缺税票状态；共校验 2 份收入 Excel、5 份账单 PDF 和 3 份税票 PDF，失败店铺为 0。
- 合成用例覆盖空明细仍能导出有调整项报表、历史优先、Processing 等待、不重复 Export、旧提示、错月、加载中、窗口手动关闭、仅收入为空但有 PDF、两类为空后继续下一家、同批重试和旧清单恢复。界面分别显示无账单、待补税票和失败数量。
- 不新增手动关闭后的自动重连；连接故障仍需重试，不能转换为无账单。未执行全部 211 店真实下载，冻结包与员工电脑验证范围见发布清单。

# Lazada 后台账单明细（五国聚合）开发说明

## 1. 背景与目标

原项目 `yingdao/Lazada` 下用「1 个核心脚本 + 4 个国家包装脚本」采集 Lazada 后台账单明细：

```text
泰国lzd后台账单明细.py        # 核心（约 2900 行）
菲律宾lzd后台账单明细.py      # 覆盖 SITE_NAME/CONFIG_KEY/CURRENCY_PATTERN/FINANCE_URL/SELLER_HOST_PAIRS/工作簿/工作表/截图目录
马来lzd后台账单明细.py
印尼lzd后台账单明细.py        # 额外：PREFERRED_LANGUAGE=简体中文
越南lzd后台账单明细.py        # 额外：DEFAULT_DWS_NODE（图片文档与文本工作簿不同）
```

本次把五国聚合为一个桌面平台模块，差异全部收敛为「国家档案」，其余能力复用桌面平台既有实现。

| 原脚本 | 桌面平台对应实现 |
| --- | --- |
| `config/ziniao.yml` 紫鸟账号 | 设置中绑定的紫鸟账号（`_payload_with_account(payload, "ziniao")`） |
| `config` 中钉钉 app_key/app_secret/user_id | 设置中的钉钉应用凭证与操作人姓名 → unionId |
| 各国固定 `DEFAULT_SHEET_NAME` | 前端「指定 Sheet」下拉：从共用钉钉文档读取 Sheet 列表，默认优先选中与国家同名的 Sheet（如选“菲律宾”就选表名“菲律宾”） |
| `--date` 固定取上月 | 前端「账单开始/结束日期」，留空默认上月 1 日至上月末 |
| `DEFAULT_OUTPUT_DIR` 截图目录 | 前端「截图保存位置」根目录（默认 `桌面\log`），按国家与账单区间自动建文件夹 |
| 各国写死 `DEFAULT_WORKBOOK_ID` | 前端「钉钉文档 ID」：五国共用一本、全局保存，切换国家不变（旧 ID 仅历史参考） |
| `dws` 写钉钉 F 列图片 | 保留原逻辑（默认写入同一文档），且只允许写 F 列或其后；本机未安装 `dws` 时降级为「仅本地截图」 |
| 钉钉文本读写 | `backend/core/dingtalk_workbook.py`（doc_1_0 SDK） |
| 紫鸟浏览器/CDP | `ZiniaoPlaywrightRuntime` + `ZiniaoClient` |

## 2. 交付文件

后端：

- `backend/services/lazada_bill_detail.py`：国家档案、请求校验、钉钉表格适配、写入与回读校验、F 列图片上传（`dws`）、运行器。
- `backend/services/lazada_bill_page.py`：进站主机判定、紫鸟启动页入店、登录（唯一凭据容器/唯一登录控件、注册页回标准登录页）、进入「我的收入 / 收入详情」、设置取数区间、读取三项指标、保存截图。
- `backend/config_store.py`：新增 `lazada_bill_client_path`、`lazada_bill_screenshot_dir`、`lazada_bill_workbook_id`、`lazada_bill_dws_node`（后两者为五国共用的全局值）。
- `backend/app_bridge.py`：`get_lazada_bill_detail_info`、`save_lazada_bill_preferences`、`start_lazada_bill_detail` 与工具条目。

前端：

- `frontend/pages/lazada-bill-detail.html`
- `frontend/assets/pages/lazada-bill-detail.js`
- `frontend/assets/common.js`：侧栏条目（紫鸟流程 / Lazada 账单）与预览桥（`?preview=1&account=ziniao` 可直接演示）。
- `frontend/assets/pages/dashboard.js`、`launcher/main.py`：工作台入口与 `--page=lazada-bill-detail`。

## 3. 数据与写入约定

- 指定 Sheet：前端下拉，由 `list_lazada_bill_sheets`（国家 + 共用钉钉文档 ID）读取该文档的 Sheet 列表，默认优先选中与国家同名的 Sheet（泰国/菲律宾/马来/印尼/越南），其次当前月份 Sheet，最后保留用户上次选择；读取失败时退回「默认（当前月份）」，运行仍可继续。
- 钉钉文档 ID：五国共用一本（`lazada_bill_workbook_id`，全局保存，默认 `np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l`），切换国家不会改变该值；国家只决定站点入口、币种、语言与目标 Sheet。各国旧脚本写死的文档 ID 仅作历史参考（`CountryProfile.legacy_workbook_id`）。
- 图片节点：截图默认写入同一文档的 F 列，页面不再单独填写图片节点；若确实需要写到另一个文档，可在配置中设置 `lazada_bill_dws_node`。
- 行匹配：A 列店铺名，先做规范化精确匹配；失败时用「唯一编号 + 中文尾缀」别名；歧义、重复、行号被占用一律跳过并记录。
- 写入列：`B=总金额`、`C=收入`、`D=扣减项`（一次写入 B:D 并用 `General` 数值格式），写入后立即回读校验，写入本身绝不重试。
- 已有数据：B:D 三项均有值 → 状态 `skipped`，不覆盖。
- 图片：固定写入同一文档的 `F` 列（`validate_image_column` 显式拒绝 A～E 列），F1 表头必须是 `图片`，且 B:D 已完整写入才允许写图；单元格已有图片 → 跳过，已有其它内容 → 拒绝覆盖。

## 4. 进站与登录规则（本土店 / 跨境店）

进站与登录对齐原脚本（含 `泰国lzd违规下架处理.perf-before.py`）的成熟规则。两者差异**只在进站主机与登录入口**，
其余（收入详情、日期区间、指标、截图）完全一致，所以五国共用一个模块、只靠 `COUNTRY_PROFILES` 区分。

### 4.1 进站主机

| 优先级 | 会话当前地址 | 使用主机 |
| --- | --- | --- |
| 1 | 已是本国的本地站或跨境站 | 沿用会话主机（紫鸟启动页会把该店开在它自己的站点上，最可靠） |
| 2 | GSP（`gsp.lazada-seller.cn` / `gsp.lazada.com`） | 本国**跨境站**（`sellercenter-<cc>.lazada-seller.cn`） |
| 3 | 其它（店铺名含「跨境」） | 本国**跨境站** |
| 4 | 其它 | 本国**本地站**（`sellercenter.lazada.<cc>`） |

- 会话里已有的主机永远优先，第 3/4 档只在紫鸟启动页没进站时兜底；这也是「本地店不会被打到跨境站」的关键。
- 目标地址只复制主机，**绝不把登录页的路径、参数带进账单页**；业务参数一律用配置里的。
- 点完紫鸟启动页后会**先无条件补一次登录**（不在登录页时是空操作），再进账单地址——这样本土店落在本地站
  登录页、跨境店落在 GSP 密码登录页都能第一时间提交预填凭据（与参考脚本 `open_violation_list` 一致）。

### 4.2 本土店（本地站）登录

1. 会话可能在 `https://sellercenter.lazada.<cc>/app/seller/login?login=1&redirect_uri=…`（紫鸟已预填）；
2. 也可能被本地站导到 `/apps/register/index`（「注册 Lazada 卖家」页）——这是最常见的失败来源。
   此时先点页面上的登录入口，点不通才回退标准登录地址：
   - 该入口既可能**嵌在 iframe 里**，文案也常是「已有账号？点击这里登录」而不是孤立的「登录」二字，
     所以点击顺序是：**主文档 + 各子框架的精确匹配 → 退化为链接/按钮的「包含」匹配**；
   - 点不到、或点完仍停在注册页，则回退该主机标准登录地址
     `https://<主机>/app/seller/login?login=1&redirect_uri=<账单页>`（该地址会触发紫鸟自动填表）；
   - 「点入口 → 回退标准地址」**最多两轮**；第二轮回退后仍停在注册页，直接报
     `本地站注册页面未提供可用的登录入口，需人工登录`，不再用误导性的「密码登录页未填好」；
3. **标准登录地址本身也可能被弹回注册页**（实测泰国本土店 `张萌-LZ泰国企业048-TH007-半运营` 如此）。
   进入预填等待后若发现页面又回到注册页，会再点一次注册页登录入口（全流程只允许一次），点通后继续预填；
4. 预填一直没出现时，只重试一次标准登录地址，绝不自己输入凭据。

### 4.3 跨境店（GSP）登录

1. 会话先落在 `https://gsp.lazada-seller.cn/page/login`（GSP 共享密码登录页），必须先在这里提交一次；
2. 提交后可能**停在 GSP 落地面**（`gsp.lazada-seller.cn` 的非登录页）而不是卖家中心——此时再进一次目标国家账单地址；
3. 两次进站后仍未到卖家中心 → 判定失败并说明原因。

### 4.4 共同的保守判定（绝不代替人工）

- 只认「唯一的凭据容器 + 唯一的登录控件」：密码框已填、账号框已填、可交互密码框唯一、控件与字段同属一个容器
  （无 `form` 时取最近的含账号框的父容器）；出现两个候选卡片或两个可交互密码框直接停止并说明原因。
- 每次采集尝试只提交一次浏览器预填的账号密码；不自动输入、不处理验证码/短信 OTP（检出验证码即停止）。
- 一次运行内每店最多 **3 次**采集机会，每次重试都是**全新的登录流程**（独立 `login_state`），
  不会把登录机会耗在第一次失败上；但以下情况**不重试**：
  - `login_required` / `verification_required`（需人工登录或过验证码）；
  - 浏览器会话已断开（`browser_failed`）；
  - 钉钉 B:D 已经提交过写入（`write_failed`，绝不重复采集/写入）。

### 4.5 失败分类与截图

| 状态 | 含义 | 是否保存失败截图 |
| --- | --- | --- |
| `login_required` | 需人工登录 / 账号密码被拒 / 跳转超时 | 否 |
| `verification_required` | 出现验证码或短信 OTP | 否 |
| `browser_failed` | 浏览器会话/页面已断开 | 是（非认证页时） |
| `write_failed` | B:D 已提交写入但回读未确认 | 是（非认证页时） |
| `failed` | 其它采集失败（已重试 3 次） | 是（非认证页时） |

认证页（登录/注册）**一律不保存截图**：页面可能含预填账号或已展开的密码。注意：这里的「认证页」判定同样基于 `last_page`，不是初始标签页。

排查日志时重点看这几行：`打开收入页（第 N/2 次）：<主机/路径>`、`已进入账单站点：<主机/路径>`、
`已点击注册页登录入口`、`注册页未找到可点击的登录入口`、`已提交浏览器预填的密码登录`、
`第 N/3 次采集失败`、`截图取用页面：<主机/路径>`。

**日志不重复**：同一家店的失败原因只记一次（`…；不再店内重试` 那条）。收尾的「店名：<结果>」汇总行
只在本次尝试没有记录过原因时才补，避免同一条消息刷两遍。

### 4.6 开店落地等待（登录提速）

紫鸟 `open_browser` 打开店铺后，程序要等「店铺真正落地」再进入登录流程。此前**只认跨境 GSP 页面**：
本土店（本地卖家中心 `sellercenter.lazada.<cc>`）永远等不到 GSP，导致「打开店铺 → 已提交登录」之间
每次白等约 60 秒（20s + 40s 两段等待被吃满）。

现在改为同时接受两类落地页面，落地即返回，不再空等：

| 店铺类型 | 落地域名 | 判定 |
| --- | --- | --- |
| 跨境 GSP | `gsp.lazada-seller.cn` / `gsp.lazada.com` | `_url_matches_gsp` |
| 跨境卖家中心 | `sellercenter-<cc>.lazada-seller.cn` | `.lazada-seller.cn` 后缀 |
| 本地卖家中心 | `sellercenter.lazada.co.th` / `.com.ph` / `.com.my` / `.co.id` / `.vn` | `LOCAL_STORE_FRONT_SUFFIXES` 白名单 |

- 域名判定**精确锚定**：`sellercenter.*` 必须命中真实的本土站后缀白名单，绝不把 `*.evil.example` 之类当店铺页。
- 落地等待预算从 `20s + 40s` 收紧为 `12s + 20s`（`STORE_PAGE_WAIT_SECONDS` /
  `STORE_PAGE_WAIT_AFTER_LAUNCH_SECONDS`），只在「一直不落地」时才被吃满。
- 登录页预填等待也从 15s 收紧：`LOGIN_PREFILL_TIMEOUT = 8s`、新增 `REGISTER_ENTRY_TIMEOUT = 5s`
  （注册页登录入口点不到就快速回退标准登录地址，不再先干等 10s）。

## 5. 截图目录

- 前端「截图保存位置」是**根目录**，默认 `桌面\log`（`default_screenshot_root()`）。
- 实际落地目录：`<根目录>\Lazada<国家名>账单明细截图\<开始日期>到<结束日期>`，例如
  `C:\Users\Mayn\Desktop\log\Lazada泰国账单明细截图\2026-09-01到2026-09-30`。
- 成功截图为 `<店铺名>.jpg`；失败截图放在同一目录的 `debug` 子目录，文件名追加 `_failed_HHMMSS`。
- 钉钉 F 列图片直接使用该文件，不再按日期再套一层目录。
- **钉钉 F 列图片依赖外部命令 `dws`**（读写钉钉文档的 CLI，不在本工程内，必须由使用者 PATH 提供）。
  服务提供 `dws_path()` / `dws_available()`：缺 dws 时**不阻塞任务**，截图仍保存到本地，
  结果里 `image_sync_available=false` 且 `image_sync_note` 给出安装指引（含 PATH 说明与本机示例路径）。
  页面在**运行前**就会显示橙色告警条「未检测到 dws 命令：钉钉 F 列图片将不会写入」，
  `info` 接口相应返回 `dws_available` / `dws_path` / `dws_hint`。
- **截图取「真正采集到数据的那一页」**：紫鸟/Lazada 登录常会另开标签页，初始标签可能还停在注册页/登录页。
  采集动作把最终页面记在 `PlaywrightLazadaBillPageActions.last_page`，运行器统一用它做截图与认证页判定；
  日志会打印 `截图取用页面：<主机/路径>`，若发现截错页先看这一行。

## 6. 国家档案

| 代码 | 国家 | 本地站 | 跨境站 | 币种 | 语言 | 「指定 Sheet」默认名 | 旧脚本写死的文档 ID（仅历史参考） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TH | 泰国 | sellercenter.lazada.co.th | sellercenter-th.lazada-seller.cn | ฿/THB | 页面默认 | 泰国 | `14lgGw3P8vvv7b2gCgg3PkQr85daZ90D` |
| PH | 菲律宾 | sellercenter.lazada.com.ph | sellercenter-ph.lazada-seller.cn | ₱/PHP | 页面默认 | 菲律宾 | `np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l` |
| MY | 马来西亚 | sellercenter.lazada.com.my | sellercenter-my.lazada-seller.cn | RM/MYR | 页面默认 | 马来（也接受「马来西亚」） | `np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l` |
| ID | 印尼 | sellercenter.lazada.co.id | sellercenter-id.lazada-seller.cn | Rp/IDR | 简体中文 | 印尼（也接受「印度尼西亚」） | `np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l` |
| VN | 越南 | sellercenter.lazada.vn | sellercenter-vn.lazada-seller.cn | ₫/VND | 简体中文 | 越南 | `pLdn55X2E5o4yno8` |

- 「指定 Sheet」默认名来自旧脚本各国包装文件的 `DEFAULT_SHEET_NAME`——**马来西亚的表叫「马来」而不是「马来西亚」**，
  所以下拉默认选中按别名匹配（见 `SHEET_NAME_ALIASES` / `sheet_name_aliases`）。
- 运行时统一使用前端填写的共用文档 ID（默认 `np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l`）；上表最后一列只用于追溯旧脚本行为，不会再被自动带入。

## 7. 任务结果与状态

每个店铺结果包含：目标/实际店铺名、国家、账单区间、钉钉行号、总金额/收入/扣减项、写入区间、状态、说明、本地截图路径、F 列图片状态与单元格。

状态取值：`success`（成功）、`skipped`（B:D 已有数据）、`unmatched_store`（钉钉 A 列或紫鸟未唯一匹配）、
`login_required` / `verification_required`（需人工登录或验证码）、`browser_failed`（会话断开）、
`write_failed`（已提交写入但回读未确认）、`failed`（其它采集失败，已重试 3 次）。
汇总行会在有需要时补上「（需人工登录/验证 N；浏览器会话不可用 N）」。

运行器同时输出 `run.log` 与 `result.json` 到 `<输出目录>\lazada_bill_detail\<时间戳_随机>`；截图按第 5 节的国家与区间目录落盘，失败店铺截图在 `debug` 子目录（认证页不保存截图）。

## 8. 验收与测试

```powershell
cd D:\py\hqyl_desktop
python -m unittest tests.test_lazada_bill_detail tests.test_lazada_bill_bridge tests.test_lazada_bill_frontend tests.test_lazada_bill_login
python -m unittest tests.test_frontend_integrity tests.test_launcher_pages
```

覆盖点：五国档案、校验与默认值（上月区间/当前月份 Sheet）、Sheet 下拉读取与「马来」等同名优先、进站主机选择（本地站/跨境站/GSP/会话优先）、本土店注册页回退标准登录地址、跨境店 GSP 落地面二次进站、单店 3 次采集与独立 login_state、登录失败不重试、认证页不存截图、登录卡片唯一性判定（合成页面 + 真实 Chromium）、截图根目录与国家区间目录、F 列限制（A～E 拒绝）、店铺与浏览器匹配的歧义保护、B:D 写入与回读、已有数据跳过、截图落盘、无 `dws` 降级（含运行前检测与安装提示）、截图取「真正采集到数据的那一页」、F 列写入失败不影响已写入数据、凭据脱敏、桥接层账号/钉钉/偏好注入、前端 DOM/导航/预览桥契约；另有一次真实浏览器预览冒烟（`?preview=1&account=ziniao`，0 page error）。

## 9. 遗留事项

- 真实店铺联调按用户环境逐步验收：已按 `黄金娟-LZ马来001`（本地站店铺）与 `泰国lzd违规下架处理.py` 的规则修正进站主机、本土店注册页回退与跨境店 GSP 二次进站；日期控件选择器与 F 列写图仍需在装有紫鸟客户端（与 `dws`）的机器上复跑确认。
- 钉钉 F 列图片依赖本机 `dws` 命令（本机实测 `dws v1.0.51` 可用）：未安装时页面运行前显示橙色告警条、结果中给出「仅本地截图」说明并附 PATH 安装指引；如需指定非 PATH 目录，可后续加 `dws` 路径配置。
- 日期控件适配沿用原脚本的 Next/rc 日历选择器与「输入 + 回车」兜底；若站点改版需复核 `lazada_bill_page.set_date_range`。

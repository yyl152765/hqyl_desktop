# 寰球云联自动化平台

`hqyl_desktop` 是寰球云联内部自动化桌面平台。界面为 H5 风格桌面客户端，所需马帮、紫鸟业务运行时已迁入本仓库的 `runtime/`，可独立运行和构建。

**最新代码固定从 [yyl152765/hqyl_desktop](https://github.com/yyl152765/hqyl_desktop.git) 的 `main` 分支获取。** 本机原工作区 `D:\hqyl_project\hqyl_desktop` 位于父仓库 `D:\hqyl_project` 内；父仓库 `hqyl_project` 的 `origin` 拉取结果不能代表本桌面项目已同步最新代码。更新约定见 [AGENTS.md](AGENTS.md)。

当前样例已接入：

- 马帮：SKU 采购日志查询
- 马帮：订单批量查询与 SKU 汇总（导入或粘贴订单编号，按「批量查询 → 按订单编号」接口检索，拉取订单商品明细，汇总唯一 SKU 清单、订单明细与未查询到订单；每批 2000 个、超额自动分批，无需浏览器）
- 马帮：lz各仓实时库存（多选指定账号有权限的仓库，接口查询并为每仓独立生成 Excel，仓内全部分页自动合并，保留原生字段和零库存记录；默认勾选泰国 TH04、TH05、TH06 仓）
- 马帮：菲律宾各组商品销量报表导出
- 马帮：菲律宾取消订单同步退款，预览后确认同步，并导出马帮回执与复查结果
- 马帮：商品仓库报表按月导出，按分类和仓库筛选
- 马帮：批量添加开发员（预览员工匹配，追加岗位并设置按商品父目录查看）
- 马帮：TEMU发货渠道更改（导入 Excel，按已开启渠道顺序设置长宽高和申报重量，关闭渠道跳过，保存核验及失败继续）
- 财务：越南 KEC 对账（迁移旧版业务流程，使用平台马帮账号；支持新旧报价布局、实际体积仓租、分档操作费与第20件起的增量费率、退件及装卸核对）
- 紫鸟：Shopee 多站点广告充值
- 紫鸟：Shopee 印尼月度账单与税票下载（默认遍历当前紫鸟账号下全部印尼 Shopee 店铺，无需输入或拆分名单；优先复用历史 Released 整月收入 Excel，不存在才导出；同时下载全部 Master Invoice 服务费账单与 Tax Invoice 税务发票 PDF，兼容英文与印尼语，缺税票时先保存账单并标记待补，支持失败重试和补查；详见 [开发与验收说明](docs/Shopee印尼月度税票下载开发说明.md)）
- 紫鸟：TEMU 在售商品导出（试运行；选店、原文件归档、汇总及失败重试已实现，旧 CLI 通道两店验证通过，新账号通道待实测）
- 紫鸟：TEMU 财务报表导出（按自定义日期导出列表与账务详情，下载全球财务明细；每个 sheet 首列添加完整紫鸟店铺名，同名 sheet 跨店合并，兼容两行合并表头；详见 [开发与验收说明](docs/temu_financial_export.md)）
- 紫鸟：TEMU 面单费与退货退款导出（按店铺归档原文件并生成汇总，支持失败重试；详见 [开发说明](docs/temu_fee_refund_export.md)）
- 紫鸟：TEMU 活动报名导出（第一阶段：按店铺批量导出活动申报模板，排除清仓主题、遍历全部分页全选商品，并按站点、活动主题与报价/库存行数复核；不提交报名，页面元素待实机校准）
- 紫鸟：TEMU 余额统计（试运行；选择订单创建月份，读取当前账户总金额，导出金额和原始截图）
- 紫鸟：Lazada 余额统计（试运行；多站点收入、余额、广告余额及最新提现状态，支持失败重试）
- 紫鸟：Lazada 广告月报（完整自然月的广告金额、联盟花费、优惠券金额和真实花费；每店嵌入三张原始截图，失败项可重试；使用本机已授权的 `ziniao-cli`，逐店采集且同时打开的店铺总数最多三个。泰国三店已实测，其他站点待实店验收。）
- 紫鸟：泰国 Lazada 广告数据（指定店铺、Sheet 和日期，采集广告费及业绩并回填钉钉表格；默认采集昨天）
- 紫鸟：Lazada 后台收支数据（泰国、菲律宾、马来、印尼、越南；五国共用一本钉钉文档，「指定 Sheet」按国家读取并优先选中同名 Sheet，按账单区间采集总金额/收入/扣减项写入 B:D，截图按国家与区间落盘，钉钉截图只写同一文档的 F 列）
- Excel：通用折扣筛选工具（纯本地，无需账号；批量导入最多100份 `.xlsx` / `.xlsm`，按最低折扣率筛选后，可选导入 SKU 价格表，对匹配且活动价低于参考价的行再筛除；重复 SKU 价格冲突会提示修正。另存结果、审核明细及 ZIP，源文件不修改；详见 [使用说明](docs/Lazada菲律宾活动表格处理.md)）
- BigSeller：BS 商品ID查询（批量 Parent SKU 模糊搜索、Views 降序首条、两列 Excel 导出）
- BigSeller：滞销SKU爆款对标（读取 Excel，选择浏览量或销量，逐行记录第一店铺和失败原因）
- BigSeller：新品认领时间查询（导入 Excel，按主 SKU 精确查询 Shopee 在售商品，回填最早创建商品的店铺与时间，支持失败续查）
- pywebview 桌面壳
- 静态 H5 控制台页面
- Python 后台任务与实时日志
- 页面切换后按模块恢复最近任务及日志
- 更新中心：支持远程发布清单检查
- PyInstaller / Inno Setup 打包脚本

广告数据、后台收支和 lz 库存使用独立目录配置，不再跟随其他功能最后选择的输出目录。默认位于当前用户的 `桌面/log`：广告为 `lz泰国广告数据`，库存为 `lz各仓库存`；后台收支按国家使用 `Lazada{国家}账单明细` 和 `Lazada{国家}账单明细截图`，切换国家立即更新，自定义目录也分别保存。截图目录下仅追加账单日期区间，任务日志和结果按运行时间分目录保存。

## 当前版本

当前源码版本为 **1.0.10**，内置中台广告充值连接，公开更新源使用中台 OSS。本仓库已将所需业务运行时迁入 `runtime/`，可以独立构建；源码隔离改造的提交不代表已自动发布新安装包。正式发版以程序版本、构建清单及线上更新清单为准。

## 目录结构

```text
hqyl_desktop/
  backend/                 # Python 后端与业务服务
    core/                  # 通用客户端、工具类
    services/              # 按业务拆分的自动化服务
  frontend/                # H5 界面
    pages/                 # 每个流程一个独立 HTML 页面
    assets/common.js       # 导航、账号、任务恢复等共享能力
    assets/pages/          # 每个页面独立的业务脚本
  launcher/                # 桌面入口
  docs/                    # 设计与迁移文档
  packaging/               # 安装包脚本
  runtime/                 # 本项目维护的业务运行时与公共配置模板
  scripts/                 # 开发、构建脚本
  tests/                   # 单元测试
```

## 同事拉取与更新

首次拉取公开源码：

```powershell
git clone https://github.com/yyl152765/hqyl_desktop.git
cd hqyl_desktop
```

以后在独立克隆的桌面仓库目录更新，先确认 Git 根目录及 `origin` 地址：

```powershell
git rev-parse --show-toplevel
git remote -v
# 确认 origin 为 yyl152765/hqyl_desktop.git 后执行：
git pull --ff-only origin main
```

对于当前嵌在 `D:\hqyl_project` 下的工作区，应从上述独立桌面仓库获取更新，再审查合入本目录并保留本地修复。不要将父仓库的 `origin` 改写为桌面仓库，也不要以父仓库的 `Already up to date` 判断桌面代码已最新。更新前先保护本地修改，按实际差异合并后测试、打包。

公开仓库包含桌面端源码、测试和开发文档；运行账号、密码、Cookie、内部人员映射、业务导出文件、验收附件及安装包不随源码分发。首次运行请在「设置」中绑定自己的账号，钉钉人员信息也需自行配置。`backend/resources/user_map.yaml` 仅为空模板，不要将真实人员映射提交到公开仓库。

## 本地运行

在 Windows 上安装 Python 3.12 和 Git，然后在克隆得到的 `hqyl_desktop` 目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\dev.ps1
```

如果 PowerShell 限制脚本执行，可以直接运行 `.\.venv\Scripts\python.exe desktop_entry.py`。

### 项目内的业务运行时

桌面端所需依赖已经迁入本仓库，**运行和打包都不再读取同级的 `mabang_process` 或 `superbrowser_process` 项目**。无需启动其他项目，也无需另行克隆它们。

```text
hqyl_desktop/
  runtime/
    mabang_process/
      util/                         # BigSeller 等功能使用的工具代码
      vietnam/config/               # BigSeller 公共配置模板
    superbrowser_process/
      config/ implement/ main/ util/ # 五国充值、紫鸟等业务运行时
      main/shopee/config/             # 五国充值公共配置模板
```

这些代码来自已验收的桌面安装包构建输入，现由本仓库维护。`runtime` 中的文件必须随 Git 提交，不能当作缓存删除。旧的 `HQYL_MABANG_*`、`HQYL_SUPERBROWSER_*`、`SUPERBROWSER_PROCESS_ROOT` 路径设置不再用于加载外部源码。马帮/钉钉账号、紫鸟客户端和网络访问等业务运行条件仍然需要；源码隔离不代表业务可以离线运行。

源码中只保留公共配置模板。用户账号仍从桌面「设置」读取；充值站点的内部表格配置从中台读取。真实数据库连接、Cookie、人员映射不提交到公开 Git。

## 构建 EXE 和安装包

### 1. 准备独立桌面仓库和环境

请使用上文核对过 `origin` 的独立桌面仓库；本机已有检出位于 `D:\hqyl_worktrees\desktop-release-20261120`。不要直接在包含其他并行修改的父仓库里执行整仓提交或覆盖源码。

构建需要 Windows、Python 3.12、Git；生成安装器还需要 Inno Setup 6。在桌面仓库根目录执行一次：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

普通安装用户只需要最终安装包，不需要 Python、Git、这些源码目录或 Inno Setup。

### 2. 准备安装包内置的充值连接

要得到“安装后直接充值、无需用户填写数据库”的包，构建时必须提供已授权的连接文件：

```powershell
# 本机已有的连接文件；其他构建电脑需通过公司内部渠道取得自己的授权副本。
$env:HQYL_ADS_DATABASE_RESOURCE = Join-Path $env:LOCALAPPDATA 'HQYL\private-releases\1.0.6\ads_database.json'
if (-not (Test-Path -LiteralPath $env:HQYL_ADS_DATABASE_RESOURCE -PathType Leaf)) {
    throw '缺少安装包内置充值连接文件，请先准备授权配置。'
}
```

文件名必须是 `ads_database.json`，使用绝对路径，放在源码目录之外且不能被 Git 跟踪。JSON 需包含 `host`、`port`（字符串）、`dbname`（`hrms`）、`user`、`password`，并沿用已验收的 `sslmode`。这里不记录真实连接值。

`HQYL_ADS_DATABASE_RESOURCE` 仅在打包时使用；安装用户不需要设置它。没有提供该文件也能构建程序，但安装包不会带数据库连接，不能当成“免配置充值版”交付。单独设置这台电脑的充值环境变量，不等于把配置放进了安装包。

### 3. 一键构建

在同一个 PowerShell 窗口、仓库根目录执行：

双击项目根目录的 `一键生成更新包.cmd`，即可执行完整构建并输出四个更新文件和桌面 RAR。缺少依赖时窗口会显示原因并保留，补齐后重新双击即可。也可以在 PowerShell 中执行：

```powershell
.\scripts\build.ps1
```

若 PowerShell 的执行策略阻止运行，可用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

脚本依次安装 Python 依赖、从本项目 `runtime` 生成公共配置、暂存本项目运行时代码、执行 PyInstaller，并在找到 Inno Setup 时制作安装器。不会访问其他业务项目的源码；不会执行充值，也不会上传 OSS。

输出位置：

| 输出 | 路径 |
|---|---|
| 可运行程序及全部配套文件 | `dist/HQYLAutomation/` |
| 安装包 | `release/HQYLAutomationSetup_<版本>.exe` |
| 自动生成的公共配置 | `build/public_runtime_config/` |
| 自动生成的运行时代码副本 | `build/runtime/superbrowser_source/` |

必须保留整个 `dist/HQYLAutomation` 目录，不能只复制 EXE。构建前会检查 Inno Setup 6 和 WinRAR；缺少工具时停止并说明原因。

更新业务代码时修改 `backend` 或 `runtime` 的受控源码，不要改 `build` 下的生成副本。`scripts/public_mabang_inputs.json` 是马帮工具代码的校验清单，修改这些工具文件时需在审查后同步校验值，不能直接关闭校验。

### 4. 验收与正式发版

```powershell
& .\dist\HQYLAutomation\HQYLAutomation.exe --self-check
& .\dist\HQYLAutomation\HQYLAutomation.exe --check-ads-database
Get-FileHash -LiteralPath .\release\HQYLAutomationSetup_1.0.6.exe -Algorithm SHA256
```

最后一行的版本号应与本次源码版本一致。`--self-check` 检查打包运行时；`--check-ads-database` 实际连接并只读检查中台表和切换状态，不执行充值。验收“内置连接”时，应在没有预设 `HQYL_GUARD_*` 或 `PGPASSWORD` 的新进程/测试电脑上运行，避免误用构建机现有配置。

正式发新版本前，统一更新 `backend/app_bridge.py`、`frontend/assets/common.js`、`packaging/hqyl_automation.iss`、`scripts/package_release.py` 和 `scripts/stage_superbrowser_source.py` 中的版本号，再构建并验证安装包。已公开发布的版本不覆盖为另一个二进制文件。

完整构建会自动生成 `release/HQYLAutomation_<版本>_BUILD_MANIFEST.json`，记录实际源码提交、构建模式、离线自检、安装包大小和 SHA-256；未执行的回归、真实业务或安装验收不会标为通过。正式发布前仍须审查清单与凭据包含情况。

准备好安装包和构建清单后，可整理上传文件（示例版本为 1.0.6，新发版必须换新版本号）：

```powershell
.\.venv\Scripts\python.exe scripts\package_release.py 1.0.6
```

单独整理已有安装包不会重新编译源码。同名旧产物先备份；已有构建清单必须与安装包一致，缺少清单时明确记录构建验证未知。完整更新目录包含：

```text
HQYLAutomationSetup_<版本>.exe
HQYLAutomation_<版本>_BUILD_MANIFEST.json
SHA256.txt
latest.json
```

通过中台现有上传授权发布到 `oss://hq-middleground/自动化平台/`：先上传安装包和构建清单并读回核对，再更新 `SHA256.txt`，最后更新 `latest.json`。从免登录更新入口重新下载整包并比对 SHA-256 后才算发布完成。`package_release.py` 只整理文件，不会自动上传；生成的旧 IIS 部署脚本不用于当前 OSS 更新流程。

内置数据库连接的包必须如实记录凭据包含情况，沿用该版本明确授权的发布流程；不能把校验报告改成“不含凭据”来绕过发布检查。公开 Git 仍不提交连接文件或安装包。

- 更新源：<https://hrms.whhqyl.com.cn/desktop-updates/latest.json>
- 同事直接下载：<https://hrms.whhqyl.com.cn/desktop-updates/latest.json?download=1>

`.git/release-work/` 下的历史验收、上传脚本只存在于维护机，不属于公开仓库，也不是新克隆项目的打包前提。

### 5. 完整更新包与精简构建

每次更新代码并完成验证后运行上面的命令。脚本自动核对后端、前端、安装脚本的版本号，完成 PyInstaller / Inno Setup 构建，再生成与 `upload_1.0.5.rar` 相同结构的四个文件。需要 Python 依赖、项目 `runtime` 下随附的运行依赖、Inno Setup 6 和 WinRAR；缺少时会在构建前说明并停止。

可先检查环境，或指定更新说明、压缩包输出目录：

```powershell
.\scripts\build.ps1 -Check
.\scripts\build.ps1 -Notes "本次更新内容" -OutputDirectory "$env:USERPROFILE\Desktop"
```

Python 依赖已安装时可加 `-SkipDependencyInstall`。版本号取当前项目值，不固定为 `1.0.5`；例如当前版本生成：

```text
release\upload_1.0.6\
  HQYLAutomationSetup_1.0.6.exe
  HQYLAutomation_1.0.6_BUILD_MANIFEST.json
  latest.json
  SHA256.txt
```

默认在系统桌面生成 `upload_1.0.6.rar`，RAR 内含上述 `upload_1.0.6` 文件夹。压缩后自动做完整性检查，安装包 SHA-256 与两个 JSON、校验文件保持一致。同名旧产物先保留为带时间戳的 `.bak`；压缩失败不替换旧 RAR。冻结程序仍在 `dist\HQYLAutomation`，原有两个 ZIP 分发包仍在 `release`。

需要精简构建时，双击 `一键生成精简更新包.cmd`，或执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1 -SkipInternalDependencies
```

精简版仍从当前源码生成新 EXE 和安装包。工作台、导航和接口会禁用 Shopee 充值、全部 BigSeller 查询/同步、Lazada 提现与余额统计，以及 TEMU 财务/费用退款/活动/在售导出与余额统计。其他功能保留，包括马帮工具、Lazada 月报/广告/账单/活动导出、TEMU 物流渠道和本地表格处理；业务运行仍需对应账号和本机环境。完整版构建使用本项目 `runtime` 下的运行组件。两种模式都会先执行冻结程序的离线自检，再生成安装包；构建清单记录模式、跳过项和自检结果，不将离线自检标为真实业务或安装验收通过。

若只是重新整理已有的安装包（安装包放在 `release`），可单独执行：

```powershell
python scripts\package_release.py --rar-output-dir "$env:USERPROFILE\Desktop" --notes "本次更新内容"
```

该命令不重新编译程序。存在构建清单时必须与安装包版本、大小和哈希匹配；缺少清单时生成清单，但明确记录源码与构建验证未知。`--fresh-build` 仅供 `build.ps1` 在新构建成功后使用，记录实际工作区提交、未提交修改状态与本地上游引用；不把本地引用当作已经联网核验的最新提交，不复用旧版测试结论。

此流程生成本地交付文件；正式发布时按 `AGENTS.md` 的 OSS 顺序上传。

## 配置

应用会保存用户常用配置，避免每次打开都重新输入：

- 用户名
- 密码
- 输出目录
- 每页查询数量
- 菲律宾销量报表上次选择的自定义分类
- 更新源地址

配置文件位置：

```text
%APPDATA%\HQYLAutomation\settings.json
```
该文件位于当前 Windows 用户目录下，请不要提交或共享。

## 菲律宾各组销量报表

进入页面后，自动读取当前马帮账号可见的全部店铺自定义分类，可按分类名称或 ID 搜索并勾选多个分类。切换搜索词会保留已选项；“全选搜索结果”只追加当前匹配的分类，“清空选择”清除全部已选项。点击“重新加载”可更新马帮新增的分类，导出时保存本次选择。付款时间、CSV 明细及汇总 Excel 的统计规则保持不变。

## 电子税务局申报

从「财务流程 → 电子税务局申报」进入，选择本地账号 Excel（B 列为单位名称，D 列为申报密码）和 Windows 扣缴端启动程序 `EPEvenue_SH.exe`。默认只导出单位申报记录，不生成工资、不算税、不发送申报。正式申报需要切换模式，并在核对客户端所属期和公司范围后勾选确认。导出月份只影响记录查询，不改变客户端正在申报的所属期。

启动前请保存并关闭已打开的扣缴端，运行时保持桌面解锁且不要操作鼠标键盘。记录默认归档到系统桌面 `财务报税/<上月>`；同范围完整文件会跳过。结果表与日志按批次保存在归档目录下的 `运行结果`。账号表不会复制到项目或写入平台配置。

开发入口：`python desktop_entry.py --page=tax-declaration`。独立脚本副本位于 `backend/services/tax_declaration.py`；页面与后台任务接入已离线验证，未在真实扣缴端提交申报。

## TEMU 在售商品导出

从「紫鸟流程 → 在售商品导出」进入。与广告充值一致，使用「设置 → 紫鸟账号」中选中的公司、账号和密码登录紫鸟自动化客户端；无需单独安装或授权 CLI。客户端和驱动目录默认自动发现。每行填写一个完整店铺名，填写输出目录后直接点击「开始导出」，后台自动读取和匹配店铺，无需勾选、刷新或预览。固定一次处理一家店，确认当前店铺关闭后才打开下一家；关闭失败会暂停后续店铺。汇总第一列为紫鸟店铺名，保留全部在售 SKU，包括已生效、已作废及样例隐藏行；原始文件按批次单独归档，失败后可继续和重试。

开发入口：`.\.venv\Scripts\python.exe desktop_entry.py --page=temu-on-sale-export`。0.2.60 起取消手动预览。0.2.61 修复 TEMU 登录衔接、异步待办弹窗和 Windows 店铺关闭确认，平台绑定账号通道已完成泰国009与菲律宾021两店实测，共 15,313 行逐行核验通过，同时验证了失败店铺重试及单店串行关闭。不允许跨账号查看或重试批次；旧 CLI 批次保留文件，安装包不携带账号凭据。详见 [TEMU在售商品导出开发说明](docs/TEMU在售商品导出开发说明.md)。

## TEMU 财务报表导出

从「紫鸟流程 → 财务报表导出」进入，填写店铺名和自定义日期范围。程序在对账中心选择「导出列表 + 账务详情」，只下载「财务明细（全球）」，保留原始文件与逐店工作簿，并按同名 sheet 汇总到一个 Excel。每个 sheet 的数据首列为完整紫鸟店铺名，部分失败的汇总明确标注「不完整」。

开发入口：`.\.venv\Scripts\python.exe desktop_entry.py --page=temu-financial-export`。真实店铺下载验收尚未完成；操作与验证范围见 [TEMU 财务报表导出](docs/temu_financial_export.md)。

## TEMU 活动报名导出

从「紫鸟流程 → 活动报名导出」进入，使用当前选中的紫鸟账号。每行填一个店铺名（完整名或能唯一匹配的片段），勾选本次站点（菲律宾 / 马来西亚 / 泰国），确认运行日期与输出目录后点「开始导出」。每个店铺导出一份平台「批量报名活动」的活动申报模板：自动选限时秒杀、官方大促、专题活动，专题主题先全选再排除名称含「清仓」等关键词的项，商品切到每页 100 条后遍历全部分页全选并核对已选数与平台总数。导出后重新打开文件核验工作表、表头、站点覆盖和报价/库存行数，四类计数分开显示。

本功能只导出，不提交报名、不改价，重复运行安全；同一批次内已成功的店铺不会重跑，失败店铺可单独重试。第一次在真实环境使用前，先点「检查页面结构」核对页面标签与活动类型，标签集集中在 `backend/services/temu_activity_page.py` 顶部。

开发入口：`.\.venv\Scripts\python.exe desktop_entry.py --page=temu-activity-export`。详见 [TEMU活动报名导出开发说明](docs/TEMU活动报名导出开发说明.md)。

## 泰国 Lazada 广告数据

从「紫鸟流程 → 广告数据」进入，使用「设置」中当前选中的紫鸟账号，以及钉钉 AppKey、AppSecret 和操作人配置。每行填写一个完整店铺名，指定钉钉工作簿、Sheet 和数据日期后点击「开始采集」。原「月度账单下载」仍保持独立。

紫鸟客户端路径会从本机安装信息自动识别，优先 V6，也可手动选择程序。广告数据与月度账单的浏览器启动使用项目内的紫鸟连接代码，无需旁边放置 `superbrowser_process` 源码目录。

广告页选择客户端后立即保存；手动修改客户端路径或文档 ID／链接后，离开输入框自动保存，页面会显示保存状态。下次打开时优先恢复本机配置中的这两项，点击「恢复默认值」也会保留它们。Sheet、日期和店铺不会随这两项一起保存。

Sheet 和指定日期均可留空：Sheet 留空使用昨天所在月份，格式如 `26年10月`；日期留空使用昨天。填写时使用指定的工作表名和实际采集日期，不会再减一天。补历史数据时请同时选择对应的 Sheet。流程会校验表头日期与「广告费/业绩」两列（指标行自动定位，兼容表格改版），匹配 K 列店铺后采集，只填写待填单元格，保留已有数据，并回读验证写入结果。未匹配或名称有歧义的店铺会显示失败原因。钉钉文档接口出现 `ServiceUnavailable`(503) 等临时错误时会自动重试，最多 5 次（1/2/4/8 秒退避），重试信息会写进运行日志。

广告报表沿用原脚本限制，只支持昨天及以前的日期（默认即取昨天）；选择今天时可采集当日业绩，广告费会提示次日补采，不会写成零。

前端位于 `frontend/pages/lazada-ads-data.html` 和 `frontend/assets/pages/lazada-ads-data.js`，服务位于 `backend/services/lazada_ads_data.py`，原脚本的页面取数逻辑整理在 `backend/services/lazada_ads_page.py`。开发入口：`python desktop_entry.py --page=lazada-ads-data`。本地结果目录保存各店结果 JSON 和日志。

## TEMU发货渠道更改

从「马帮流程 → TEMU发货渠道更改」进入，选择马帮账号和每日随机长宽高 `.xlsx`，查询预览已开启（ON）渠道与 Excel 的对应关系后批量设置。已关闭（OFF）的渠道跳过，不占用 Excel 行；只按已开启渠道数检查数据是否足够。支持保存后核验、逐行进度和失败继续；数据不足时整批不可执行。每日公式必须已经计算并保存，本次任务会固定导入数据，避免重新分配。

开发入口：`python desktop_entry.py --page=temu-shipping-channel`。使用规则及真实页面联调范围见 [TEMU发货渠道更改开发说明](docs/TEMU发货渠道更改开发说明.md)。

## BS 商品ID查询

从工作台或侧栏 BigSeller → BS 商品ID查询进入。绑定 BigSeller 账号并配置现有图形验证码服务后，粘贴 SKU、选择输出目录，点击“开始查询并导出”。SKU 支持换行、逗号、分号和 Tab 分隔，按首次输入顺序去重，单次最多 5000 个。

从 `0.2.42` 起，搜索字段改为 **SKU**，使用 **Fuzzy Search**，支持输入子 SKU；查询范围仍为当前账号可访问的 Shopee 在售商品，按 **Views 降序**取第一条。Excel 只有 `SKU`、`Item ID` 两列；未匹配或查询失败的 SKU 保留，Item ID 留空，区别和原因在页面与日志中显示。`0.2.41` 的 Parent SKU 搜索行为仅适用于旧版本。

开发说明及真实平台验收状态见 [BS商品ID查询开发说明](docs/BS商品ID查询开发说明.md)。

## 滞销SKU爆款对标

从工作台或侧栏 BigSeller → 滞销SKU爆款对标进入，选择 Excel 文件、工作表、浏览量或销量、输出目录后开始。沿用旧版的 Shopee 在售 SKU 模糊搜索及每店单商品最大值口径；销量为 BigSeller 商品列表的销量，不是月销量或店铺汇总。支持完整分页、限流退避，并将未匹配和查询失败分别写入结果，源文件不会覆盖。

失败 SKU 会等待 30、60、120 秒再补查最多 3 轮，服务要求更长冷却时优先遵守。每项结果即时保存进度，中断后可恢复；只补查未完成项。仍有失败时任务和 Excel 均标记“结果不完整”，附待补查清单及继续入口，全部确认并导出成功才标记完成。

`.xlsx` 保留原表、格式和公式，新增所选指标的结果列及对标明细；`.xls` 会提示转换限制。已处理的表可再次运行，同一指标更新，另一指标保留。开发入口：`python desktop_entry.py --page=bigseller-sku-benchmark`。诊断、参数、验证情况见 [滞销SKU爆款对标开发说明](docs/滞销SKU爆款对标开发说明.md)。

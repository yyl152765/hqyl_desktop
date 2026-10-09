# 寰球云联自动化平台

`hqyl_desktop` 是寰球云联内部自动化桌面平台。目标是把 `mabang_process` 等现有 Python 自动化脚本逐步迁移成统一的 H5 风格桌面客户端，并为后续紫鸟 WebDriver 相关流程预留接入边界。

当前样例已接入：

- 马帮：SKU 采购日志查询
- 马帮：菲律宾各组商品销量报表导出
- 马帮：批量添加开发员（预览员工匹配，追加岗位并设置按商品父目录查看）
- 马帮：TEMU发货渠道更改（导入 Excel，按已开启渠道顺序设置长宽高和申报重量，关闭渠道跳过，保存核验及失败继续）
- 紫鸟：Shopee 多站点广告充值
- 紫鸟：TEMU 在售商品导出（试运行；选店、原文件归档、汇总及失败重试已实现，旧 CLI 通道两店验证通过，新账号通道待实测）
- 紫鸟：TEMU 余额统计（试运行；选择订单创建月份，读取当前账户总金额，导出金额和原始截图）
- 紫鸟：Lazada 余额统计（试运行；多站点收入、余额、广告余额及最新提现状态，支持失败重试）
- BigSeller：BS 商品ID查询（批量 Parent SKU 模糊搜索、Views 降序首条、两列 Excel 导出）
- BigSeller：滞销SKU爆款对标（读取 Excel，选择浏览量或销量，逐行记录第一店铺和失败原因）
- BigSeller：新品认领时间查询（导入 Excel，按主 SKU 精确查询 Shopee 在售商品，回填最早创建商品的店铺与时间，支持失败续查）
- pywebview 桌面壳
- 静态 H5 控制台页面
- Python 后台任务与实时日志
- 页面切换后按模块恢复最近任务及日志
- 更新中心：支持远程发布清单检查
- PyInstaller / Inno Setup 打包脚本

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
  scripts/                 # 开发、构建脚本
  tests/                   # 单元测试
```

## 同事拉取与更新

首次拉取公开源码：

```powershell
git clone https://github.com/yyl152765/hqyl_desktop.git
cd hqyl_desktop
```

以后在本仓库目录更新：

```powershell
git pull --ff-only origin main
```

更新前请先提交或暂存自己的修改；如提示分支已分叉，请先与维护者确认合并方式。

公开仓库包含桌面端源码、测试和开发文档；运行账号、密码、Cookie、内部人员映射、业务导出文件、验收附件及安装包不随源码分发。首次运行请在「设置」中绑定自己的账号，钉钉人员信息也需自行配置。`backend/resources/user_map.yaml` 仅为空模板，不要将真实人员映射提交到公开仓库。

## 本地运行

在 Windows 上安装 Python 3.12 和 Git，然后在克隆得到的 `hqyl_desktop` 目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\dev.ps1
```

如果 PowerShell 限制脚本执行，可以直接运行 `.\.venv\Scripts\python.exe desktop_entry.py`。

### 完整业务功能所需依赖

本仓库可以加载桌面界面，部分业务仍会延迟加载内部项目。需要完整业务功能或构建 EXE 时，请向项目维护者获取**匹配当前桌面版本**的内部源码和配置，并保持以下同级结构：

```text
workspace/
  hqyl_desktop/           # 本仓库
  mabang_process/         # 内部依赖，不在本公开仓库中
  superbrowser_process/   # 内部依赖，不在本公开仓库中
```

- BigSeller 相关功能依赖 `mabang_process/util/bigseller_request_util.py`；基础参数配置位于 `mabang_process/vietnam/config/BigSeller库存同步.yaml`。
- Shopee 广告充值、紫鸟账号通道的 TEMU 导出和余额统计、Lazada 相关紫鸟流程依赖 `superbrowser_process`，并需要在本机安装、配置紫鸟客户端和驱动。
- EXE 构建依赖 `mabang_process/util/`、上述 BigSeller YAML，以及 `superbrowser_process/main/shopee/config/` 中印尼、泰国、菲律宾、越南、马来五个站点的广告充值 YAML。`scripts/build_public_runtime_configs.py` 会从同级目录读取它们。

内部依赖和真实配置请通过公司授权渠道获取，不要上传到本公开仓库。只拉取本仓库无法完成全部业务流程或 EXE 构建；`desktop_entry.py --self-check` 同样需要这些依赖。

## 构建 EXE

```powershell
cd D:\hqyl_project\hqyl_desktop
.\scripts\build.ps1
```

构建成功后，EXE 目录位于：

```text
D:\hqyl_project\hqyl_desktop\dist\HQYLAutomation
```

如果本机安装了 Inno Setup，脚本会继续生成安装包：

```text
D:\hqyl_project\hqyl_desktop\release\HQYLAutomationSetup_0.2.66.exe
```

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

## TEMU 在售商品导出

从「紫鸟流程 → 在售商品导出」进入。与广告充值一致，使用「设置 → 紫鸟账号」中选中的公司、账号和密码登录紫鸟自动化客户端；无需单独安装或授权 CLI。客户端和驱动目录默认自动发现。每行填写一个完整店铺名，填写输出目录后直接点击「开始导出」，后台自动读取和匹配店铺，无需勾选、刷新或预览。固定一次处理一家店，确认当前店铺关闭后才打开下一家；关闭失败会暂停后续店铺。汇总第一列为紫鸟店铺名，保留全部在售 SKU，包括已生效、已作废及样例隐藏行；原始文件按批次单独归档，失败后可继续和重试。

开发入口：`.\.venv\Scripts\python.exe desktop_entry.py --page=temu-on-sale-export`。0.2.60 起取消手动预览。0.2.61 修复 TEMU 登录衔接、异步待办弹窗和 Windows 店铺关闭确认，平台绑定账号通道已完成泰国009与菲律宾021两店实测，共 15,313 行逐行核验通过，同时验证了失败店铺重试及单店串行关闭。不允许跨账号查看或重试批次；旧 CLI 批次保留文件，安装包不携带账号凭据。详见 [TEMU在售商品导出开发说明](docs/TEMU在售商品导出开发说明.md)。

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

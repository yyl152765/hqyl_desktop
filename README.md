# 寰球云联自动化平台

`hqyl_desktop` 是寰球云联内部自动化桌面平台。目标是把 `mabang_process` 等现有 Python 自动化脚本逐步迁移成统一的 H5 风格桌面客户端，并为后续紫鸟 WebDriver 相关流程预留接入边界。

当前样例已接入：

- 马帮：SKU 采购日志查询
- 马帮：菲律宾各组商品销量报表导出
- 紫鸟：Shopee 多站点广告充值
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

## 本地运行

```powershell
cd D:\hqyl_project\hqyl_desktop
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\dev.ps1
```

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
D:\hqyl_project\hqyl_desktop\release\HQYLAutomationSetup_0.2.0.exe
```

## 配置

应用会保存用户常用配置，避免每次打开都重新输入：

- 用户名
- 密码
- 输出目录
- 每页查询数量
- 菲律宾销量报表上次选择的小组
- 更新源地址

配置文件位置：

```text
%APPDATA%\HQYLAutomation\settings.json
```
该文件位于当前 Windows 用户目录下，请不要提交或共享。

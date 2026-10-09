# BigSeller 登录空白字符问题修复说明

日期：2026-09-05

## 问题与根因

销量对标在登录阶段停止，全部 3721 个 SKU 的查询次数为 0。界面只显示“账号、权限或登录配置需要处理”，没有显示已脱敏的具体暂停原因。

10:25 的任务使用了与 09:32 任务不同的账号。新任务的账号名称正确，但客户端保存的密码带有首尾空白；去掉这些空白后，与用户提供的密码一致。使用原存储值提交登录时，服务端返回“账号或密码错误”；仅去除密码首尾空白后，登录、登录态校验和销量列表查询均成功。

官网表单对账号和密码执行 `trim()`，客户端此前将密码中的首尾空白原样加密提交，导致网页与客户端行为不同。含 `@` 的员工账号在官网也使用 `authType=email`，本次不修改账号类型、加密算法或登录端点。

公开网页依据：[BigSeller 登录组件](https://bs-s1.dianxiaomi.com/bs-web-ssr/2026-08/_nuxt/V_JmazsA.js)，于 2026-09-05 核对。这里不记录真实账号、密码、验证码、Cookie 或请求体。

## 修复内容

- `backend/services/bigseller_sync.py`：构造实际登录配置时清理 BigSeller 账号、密码的首尾空白，保留内部空格；不修改验证码环境变量优先级，也不改变其他平台的密码规则。
- `backend/services/bigseller_item_id_query.py`：将已知的“账号或密码错误”“账号已停用”映射为固定、可操作的安全提示。未知异常仍使用通用提示，旧日志仍禁用，避免泄露验证码或凭据。
- `backend/services/bigseller_sku_benchmark.py`：停止时打印已脱敏的具体暂停原因，继续保存进度和未完成结果。

当前选中 BigSeller 账号已清理存储密码的多余首尾空白。采用原子写入并验证其他配置未变；账号名称、账号 ID 和任务进度文件均未改变，因此当前客户端下一次任务读取配置即可使用正确值，无需等待安装包更新。源码防护与错误提示增强将在后续构建中生效。

## 验证与恢复

- 单次原参数诊断：验证码获取和打码成功，登录返回失败。
- 单次规范化参数验证：登录返回成功，登录态校验成功；对源表首个 SKU 执行销量列表只读查询，第一页返回 11 条商品记录。
- 清理存储配置后，不加临时参数重新验证：登录、登录态校验及同一销量列表查询再次成功。
- 自动回归：`tests/test_bigseller_item_id_query.py` 和 `tests/test_bigseller_sku_benchmark.py`，49 项通过，65 个子测试通过。覆盖首尾空白清理、内部空格保留、已知和未知异常脱敏、登录失败后显示原因及保留全部待补查项。
- 共享登录配置的同步服务回归：`tests/test_bigseller_sync_service.py`，3 项通过。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_bigseller_item_id_query.py tests/test_bigseller_sku_benchmark.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_bigseller_sync_service.py -q
```

继续使用相同账号、原源文件、Sheet1 和“销量”指标，选择 10:25:20 建立的进度文件恢复。不要选择 09:32 旧账号对应的进度文件，也不要把本次“结果不完整”导出文件替换成恢复时的源文件。此次验证只确认登录及一次只读查询，没有自动重跑全部 3721 个 SKU。

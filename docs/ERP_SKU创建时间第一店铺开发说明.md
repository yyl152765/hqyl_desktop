# ERP SKU 创建时间第一店铺

入口：侧栏「ERP 数据库 → ERP SKU 创建时间第一店铺」。直连 ERP PostgreSQL **只读**查询，不需要 ERP 账号登录、紫鸟或浏览器。设计依据为 `superbrowser_process` 的《ERP 按 SKU 查询创建时间第一店铺：开发文档》（2026-11-27）。

## 业务口径

- Shopee，**全部商品状态**，**包含 ERP 仍留存的商品/变体归档记录**；不增加“在售”“未归档”过滤。
- 完整 SKU 精确匹配、区分大小写，只清理两端空白。有变体商品匹配 `model_sku`；明确无变体（`has_model` 为假）匹配 `item_sku`；`has_model` 未知则记为数据待核验。
- 先求每家店铺最早的有效商品平台创建时间（`shopee_item_base_info.create_time`），再选所有店铺中最早者。并列时按 `shop_id`、`item_id`、`model_id` 升序稳定展示一家，并输出并列数和并列店铺。
- 时间是 ERP 存储的 Asia/Shanghai 业务本地时间，不额外换算；不是店铺账号创建时间，也不是同步到 ERP 的时间。
- 只要该 SKU 存在具备有效创建时间（2010 年至今且不晚于当前时间 1 天以上）和店铺资料的候选记录，即按有效候选正常选出第一店铺并回填；若同时存在少量历史无主表/无时间的孤儿变体记录，在说明中备注“另有 X 条缺基础商品/时间/店铺资料的历史记录未参与排名”。只有当匹配的记录**全部**缺少有效创建时间或店铺资料（无任何有效候选）时，才记为「数据待核验」并留空“创建时间第一店铺”。
- 连接失败、超时、权限错误是「查询失败」；取消或中止后的剩余项是「未执行」。它们都不会被写成“未找到”。
- 「ERP留存范围未找到」只表示 ERP 当前留存且授权范围内无匹配，不代表该 SKU 从未存在。
- 任何待核验、失败、未执行项存在时，工作簿文件名带“结果不完整”，任务也标为不完整。

核心 SQL 见 `backend/services/erp_sku_earliest_store_repository.py` 的 `CORE_SQL`，在设计文档第 5 节已验证 SQL 的基础上增加：授权/店铺/站点过滤（在排名**之前**，即 `enriched` 阶段）、并列店铺清单（`LATERAL`，不额外发起查询）。所有值均通过 `psycopg2` 参数绑定。

## 连接、授权与安全

授权安装包从本地私有文件注入 ERP 只读连接，员工无需配置环境变量即可直接查询。公开源码不包含连接值，未注入资源的构建必须显式配置 `ERP_SKU_DB_*`；若显式设置了连接覆盖变量或 `ERP_SKU_AUTHORIZED_SHOPS`，缺项或格式错误时直接拒绝（fail-closed）：

| 变量 | 说明 |
| --- | --- |
| `ERP_SKU_DB_HOST` / `ERP_SKU_DB_DATABASE` / `ERP_SKU_DB_USERNAME` / `ERP_SKU_DB_PASSWORD` | 授权安装包使用注入的私有连接；显式覆盖时四项必填 |
| `ERP_SKU_DB_PORT` | 默认 5432 |
| `ERP_SKU_DB_SSLMODE` | 使用私有资源中明确指定的策略；显式环境变量未指定时默认 `require`。仅接受 `require`/`verify-ca`/`verify-full`/`disable`，TLS 握手失败时**不会自动降级** |
| `ERP_SKU_AUTHORIZED_SHOPS` | 默认内置 `*`（公司级）；可显式配置 `*` 或以逗号分隔的 Shop ID，显式设置为空/非数字时拒绝查询 |

界面的站点/店铺筛选只与授权范围取交集，不构成授权依据。会话参数：只读事务、`statement_timeout=15000ms`、`lock_timeout=1000ms`、`idle_in_transaction_session_timeout=10000ms`、`connect_timeout=10s`、`application_name=hqyl_shopee_sku_earliest_store`；单连接顺序处理，每批 50 个 SKU，批次超时会二分重试，单个 SKU 超时记失败。仅对可恢复的连接中断做一次受控重连；认证、权限、SQL 结构错误不重试，并停止后续批次。

密码、连接串不进入日志、进度文件、`summary.json`、结果工作簿和任务上下文；异常只保留安全错误码、SQLSTATE 和业务说明。

构建时用 `HQYL_ERP_SKU_DATABASE_RESOURCE` 指定源码目录外、未被 Git 跟踪的 `erp_sku_database.json`，包含 `host/database/username/password`，可含 `port/sslmode`。资源必须经管理员授权，不能提交到公开源码仓库；构建前会验证文件来源及配置格式。

## 输入与输出

- 输入二选一：多行文本 SKU，或 `.xlsx`/`.xls`。前 10 行识别「库存SKU编号」「库存SKU」「SKU」（按此优先级，不使用主SKU/Parent SKU），优先工作表「动销总表」。保留前导零；公式必须有缓存值；错误、布尔、日期单元格不当作 SKU；同格多行 SKU 分别查询。最多 10000 个不同 SKU（产品保护上限，不代表已压测）。
- 每次运行在输出目录下生成独立目录 `ERP创建时间对标_<时间>_<唯一后缀>/`，含：结果工作簿、`summary.json`、`process.log`、`进度_*.sqlite3`。源文件不会被改写。
- 原表追加「创建时间第一店铺」「最早平台创建时间」「创建时间对标状态」「创建时间对标说明」四列（带归属标记，重跑不重复堆叠；不占用已有空表头列），并新增「ERP创建时间对标明细」「ERP创建时间对标汇总」；同名用户工作表不会被覆盖，改用 `_2` 后缀。标识类字段写为文本，防公式注入与长数字精度丢失。
- 明细包含：输入/匹配 SKU、匹配来源、Shop ID、店名、站点、Item/Model ID、平台创建时间、商品状态、商品/变体归档、匹配商品数、匹配店铺数、并列数与并列店铺、基础/变体采集时间、查询时间、状态与说明。数据时效以采集时间为准，查询时间不代表平台数据刚刷新。

## 恢复

进度文件的身份绑定：输入指纹、工作表、SKU 集合、平台、匹配方式、状态/归档策略、站点/店铺筛选、授权范围摘要、查询逻辑版本。恢复时身份不一致即拒绝；仅补查失败/未执行项；已确认项沿用并在汇总中注明批次查询时间不同。要最新结果请新建查询。导出失败时进度保留，可重新导出。

## 代码

- `backend/services/erp_sku_earliest_store_repository.py`：配置、授权范围、只读查询、错误脱敏。
- `..._checkpoint.py`：SQLite 进度，写入前校验行结构。
- `..._workbook.py`：输入识别、回填、明细与汇总。
- `..._service.py`：批次编排、结果判定、取消、导出，可注入 `repository_factory`/`cancel`/`clock`。
- `backend/app_bridge.py`：`inspect_erp_sku_file`、`choose_erp_sku_file`、`choose_erp_sku_checkpoint`、`start_erp_sku_earliest_store`。
- 页面：`frontend/pages/erp-sku-earliest-store.html`、`frontend/assets/pages/erp-sku-earliest-store.js`。

启动：`.\.venv\Scripts\python.exe desktop_entry.py --page=erp-sku-earliest-store`

## 验证状态

- 已完成：`tests/test_erp_sku_earliest_store.py` 23 项自动化单元测试（假连接/假仓库，不连接生产库）：内置连接与环境变量覆盖、参数绑定、授权取交集与失败关闭、错误脱敏与受控重连、连接清理、结果与输入一致性、各状态判定与并列、超时二分、致命错误停止、取消、恢复身份校验、表头优先级、前导零、重复 SKU、公式注入、源文件不变、重跑不堆叠、同名工作表保护。
- 已完成真实 ERP 库只读联调，覆盖正常匹配、资料不完整和未命中；业务样本仅保留本地。
- **尚未完成**：1/100/1000/10000 SKU 分规模性能与执行计划复核；两次确定性构建与发布包检查。本期未打包发布。
- 本期不含：Lazada/TikTok、按店铺账号注册时间排名、SKU 修改轨迹、`contains` 模糊匹配、父 SKU 查询。

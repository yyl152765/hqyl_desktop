# EchoTik 商品达人采集开发方案

## 1. 背景与目标

需要在现有桌面工具中新增一个 EchoTik 数据采集功能。

用户在前端输入商品关键词后，系统自动登录 EchoTik，固定国家为泰国，进入商品库搜索该关键词，采集搜索结果中的全部商品，并逐个进入商品详情数据接口采集完整达人列表。采集完成后，用户可以导出 Excel。

目标站点：

```text
https://echotik.live/products
```

固定业务条件：

```text
国家：泰国
模块：数据分析 -> 商品 -> 商品库
搜索对象：商品
搜索方式：关键词搜索
```

关键词示例：

```text
ลิปแมตต์สีชัด
```

## 2. 账号与安全要求

账号、密码、Cookie、Token、签名密钥等敏感信息不得写入前端代码，也不得提交到仓库。

后端通过环境变量读取账号配置：

```env
ECHOTIK_EMAIL=账号
ECHOTIK_PASSWORD=密码
ECHOTIK_COUNTRY=TH
```

如果本地已有统一配置文件或应用设置模块，优先接入现有配置体系；否则使用 `.env` 或后端启动配置读取。

## 3. 总体技术要求

优先使用 HTTP 请求完成采集流程，不使用浏览器自动点击作为主流程。

开发前需要通过浏览器 DevTools Network 抓包确认真实接口，至少确认以下内容：

```text
1. 登录接口
2. 登录后的鉴权方式，例如 Cookie、Authorization、Token
3. 国家或站点切换接口，固定为泰国
4. 商品库搜索接口
5. 商品列表分页参数
6. 商品详情接口
7. 商品详情页达人列表接口
8. 达人列表分页参数
9. 是否存在 sign、timestamp、nonce、加密参数或前端生成签名
10. 登录态过期后的接口返回特征
```

如果接口存在签名、加密、动态 Token，需要分析 EchoTik 前端 JS 中的生成逻辑，并在后端请求层复现。不要依赖人工复制 Cookie 作为长期方案。

## 4. 功能范围

### 4.1 采集入口

前端新增一个页面或功能区，名称建议：

```text
EchoTik 商品达人采集
```

页面只需要用户输入关键词，不需要国家选择，因为国家永远固定为泰国。

### 4.2 商品采集

根据用户输入关键词请求商品库搜索接口。

采集规则：

```text
1. 采集该关键词搜索结果中的全部商品
2. 商品列表必须自动分页
3. 一直采集到接口明确没有下一页，或返回列表为空
4. 不允许只采集第一页
5. 不允许只采集固定数量商品
```

### 4.3 达人采集

对每一个商品请求商品详情页中的达人列表接口。

采集规则：

```text
1. 每个商品都需要采集达人列表
2. 达人列表必须自动分页
3. 一直采集到接口明确没有下一页，或返回列表为空
4. 单个商品达人采集失败时记录失败原因，并继续采集下一个商品
5. 整体任务不能因为单个商品失败直接中断
```

### 4.4 导出 Excel

采集完成后导出 `.xlsx` 文件。

Excel 至少包含两个 Sheet：

```text
1. Products：商品列表
2. Creators：达人列表
```

## 5. 前端需求

### 5.1 页面元素

前端页面需要包含：

```text
1. 关键词输入框
2. 开始采集按钮
3. 任务状态展示
4. 采集进度展示
5. 采集日志展示
6. 结果预览表格
7. Excel 导出按钮
```

### 5.2 任务状态

任务状态建议使用以下枚举：

```text
idle：等待中
logging_in：登录中
searching：搜索商品中
collecting_products：采集商品中
collecting_creators：采集达人中
completed：已完成
failed：失败
cancelled：已取消，可选
```

如果现有项目已有任务状态规范，优先沿用现有规范。

### 5.3 进度字段

前端进度展示建议包含：

```text
当前关键词
商品总数
已采集商品数
当前采集商品名称
当前商品达人总数
当前商品已采集达人数
全部已采集达人数
当前分页信息
任务状态
最新日志消息
开始时间
结束时间
```

### 5.4 前端交互规则

```text
1. 关键词为空时不能开始采集
2. 采集中禁用重复提交
3. 采集完成后显示导出按钮
4. 采集失败时展示明确错误信息
5. 结果表格可先展示部分数据，不要求一次性渲染全部大数据
6. 长任务需要轮询后端任务状态，避免页面请求超时
```

## 6. 后端接口设计

建议采用异步任务模式，避免采集耗时过长导致单次 HTTP 请求超时。

### 6.1 创建采集任务

```http
POST /api/echotik/collect
Content-Type: application/json

{
  "keyword": "ลิปแมตต์สีชัด"
}
```

响应示例：

```json
{
  "jobId": "echotik_20260709_001",
  "status": "running"
}
```

### 6.2 查询任务状态

```http
GET /api/echotik/jobs/{jobId}
```

响应示例：

```json
{
  "jobId": "echotik_20260709_001",
  "status": "collecting_creators",
  "keyword": "ลิปแมตต์สีชัด",
  "totalProducts": 120,
  "collectedProducts": 35,
  "totalCreators": 2400,
  "collectedCreators": 680,
  "currentProductId": "1734400948172129805",
  "currentProductTitle": "商品名称",
  "currentProductCreatorTotal": 96,
  "currentProductCreatorCollected": 40,
  "message": "正在采集达人列表第 3 页",
  "startedAt": "2026-07-09T10:00:00+08:00",
  "finishedAt": null,
  "error": null
}
```

### 6.3 查询任务结果预览

可选接口，用于前端表格预览：

```http
GET /api/echotik/jobs/{jobId}/results?type=creators&page=1&pageSize=50
```

响应示例：

```json
{
  "items": [],
  "page": 1,
  "pageSize": 50,
  "total": 0
}
```

### 6.4 导出 Excel

```http
GET /api/echotik/jobs/{jobId}/export
```

响应：

```text
返回 .xlsx 文件
```

### 6.5 取消任务

可选接口：

```http
POST /api/echotik/jobs/{jobId}/cancel
```

## 7. 数据字段设计

以下字段是业务字段，真实接口字段名以 EchoTik 抓包结果为准。开发时需要在适配层完成字段映射。

### 7.1 Products Sheet 字段

```text
keyword
product_id
product_title
product_url
product_image
shop_id
shop_name
price
currency
recent_7d_sales
recent_7d_gmv
total_sales
total_gmv
creator_count
video_count
category
raw_category
collected_at
error
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| keyword | 用户输入的搜索关键词 |
| product_id | 商品 ID |
| product_title | 商品标题 |
| product_url | EchoTik 商品详情页地址 |
| product_image | 商品图片地址 |
| shop_id | 店铺 ID，接口有则保存 |
| shop_name | 店铺名称 |
| price | 商品价格 |
| currency | 币种，泰国通常为 THB |
| recent_7d_sales | 近 7 天销量 |
| recent_7d_gmv | 近 7 天 GMV |
| total_sales | 总销量 |
| total_gmv | 总 GMV |
| creator_count | 带货达人数 |
| video_count | 带货视频数 |
| category | 主要类目 |
| raw_category | 接口返回的完整类目结构或类目文本 |
| collected_at | 采集时间 |
| error | 该商品采集失败原因，没有失败则为空 |

### 7.2 Creators Sheet 字段

```text
keyword
product_id
product_title
creator_id
creator_name
creator_avatar
country
fans_count
likes_count
creator_categories
product_gmv
video_count
video_play_count
live_count
live_view_count
creator_url
collected_at
error
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| keyword | 用户输入的搜索关键词 |
| product_id | 关联商品 ID |
| product_title | 关联商品标题 |
| creator_id | 达人 ID，接口有则保存 |
| creator_name | 达人昵称 |
| creator_avatar | 达人头像 |
| country | 达人国家或地区 |
| fans_count | 粉丝数 |
| likes_count | 点赞数 |
| creator_categories | 达人相关类目 |
| product_gmv | 该达人对当前商品贡献的带货 GMV |
| video_count | 带货视频数 |
| video_play_count | 视频总播放数 |
| live_count | 带货直播数 |
| live_view_count | 直播总观看人次 |
| creator_url | 达人详情页或主页地址 |
| collected_at | 采集时间 |
| error | 该达人行数据异常说明，没有异常则为空 |

## 8. 核心采集流程

```text
1. 前端提交关键词
2. 后端创建异步采集任务
3. 后端读取 EchoTik 账号配置
4. 请求登录接口，保存登录态
5. 固定切换或设置国家为泰国
6. 请求商品库搜索接口第一页
7. 根据接口分页规则循环采集全部商品
8. 保存商品列表到任务结果
9. 遍历商品列表
10. 对每个商品请求达人列表第一页
11. 根据接口分页规则循环采集该商品全部达人
12. 保存达人数据到任务结果
13. 任务状态更新为 completed
14. 前端允许用户导出 Excel
```

## 9. 分页规则

分页逻辑必须以真实接口返回为准。

常见停止条件：

```text
1. hasNext 为 false
2. nextCursor 为空
3. page * pageSize >= total
4. 当前页返回列表为空
5. 接口返回明确的最后一页标记
```

不要写死页数。

不要假设只有 page/pageSize，也可能是 cursor、offset、search_after 等分页方式。

## 10. 请求限速与重试

建议默认限速：

```text
商品列表分页：每页间隔 500ms - 1000ms
达人列表分页：每页间隔 800ms - 1500ms
商品之间：间隔 1000ms 左右
```

失败重试建议：

```text
1. 单个请求最多重试 3 次
2. 超时、502、503、504 可重试
3. 401 或登录过期时先重新登录，再重试当前请求
4. 403、风控、验证码等情况需要记录错误并停止任务
5. 每次重试使用递增等待时间
```

## 11. 登录态管理

后端需要封装 EchoTikClient 或类似服务类，集中管理：

```text
1. 登录
2. Cookie/Token 保存
3. 国家设置为泰国
4. 请求头构造
5. 登录态过期检测
6. 自动重新登录
7. 接口错误统一处理
```

请求头尽量还原浏览器请求，例如：

```text
User-Agent
Accept
Accept-Language
Content-Type
Origin
Referer
Cookie
Authorization
```

具体请求头以 DevTools Network 抓包为准。

## 12. 数据存储建议

采集任务可能耗时较长且数据量较大，不建议只存在前端内存。

可选方案：

```text
1. 如果项目已有本地数据库，优先保存到现有数据库
2. 如果项目暂无数据库，可保存为任务级 JSON 文件或 SQLite 表
3. Excel 导出时从后端持久化结果生成
```

建议任务结果至少包含：

```text
任务元信息
商品列表
达人列表
失败商品列表
失败请求日志
```

## 13. Excel 导出要求

Excel 文件名建议：

```text
EchoTik_商品达人采集_{keyword}_{yyyyMMdd_HHmmss}.xlsx
```

Excel 内容要求：

```text
1. 包含 Products 和 Creators 两个 Sheet
2. 表头使用中文或英文字段均可，但需要清晰
3. 数值字段保持为数字，方便筛选和计算
4. URL 字段保留完整链接
5. 如果某个商品采集达人失败，Products Sheet 的 error 字段需要记录原因
```

## 14. 异常处理

必须处理以下异常：

```text
1. 关键词为空
2. 登录失败
3. 国家切换失败
4. 搜索商品失败
5. 商品列表分页失败
6. 商品详情或达人列表失败
7. 单个商品达人采集失败
8. 登录态过期
9. 接口限流
10. 网络超时
11. Excel 生成失败
12. 任务被用户取消
```

异常处理原则：

```text
1. 致命错误更新任务状态为 failed
2. 单个商品错误记录到该商品 error 字段，并继续后续商品
3. 可重试错误按重试策略处理
4. 所有错误需要写入任务日志，便于前端展示和排查
```

## 15. 推荐代码结构

具体目录以现有项目结构为准。建议后端拆分为：

```text
echotik_client：封装 EchoTik 登录、请求、分页
echotik_collector：封装商品和达人采集流程
echotik_jobs：封装异步任务状态、进度、取消
echotik_exporter：封装 Excel 导出
echotik_models：定义任务、商品、达人数据结构
```

前端建议拆分为：

```text
EchoTikCollectPage：页面容器
KeywordForm：关键词输入和提交
JobProgress：任务状态和进度
JobLogs：日志展示
ResultPreviewTable：结果预览
ExportButton：导出入口
```

## 16. 验收标准

功能验收：

```text
1. 前端可以输入关键词并启动采集
2. 国家固定为泰国，用户不可修改
3. 后端可以自动登录 EchoTik
4. 商品库搜索能返回该关键词下的商品列表
5. 商品列表可以完整分页采集，不只采集第一页
6. 每个商品可以采集完整达人列表
7. 达人列表可以完整分页采集，不只采集第一页
8. 采集过程中前端可以看到实时进度
9. 单个商品失败不会中断全部任务
10. 采集完成后可以导出 Excel
11. Excel 至少包含 Products 和 Creators 两个 Sheet
12. 账号密码不出现在前端和仓库代码中
```

技术验收：

```text
1. 主采集流程基于 HTTP 请求
2. 代码中有统一的 EchoTik 请求客户端
3. 登录态过期可以自动重新登录或给出明确错误
4. 分页逻辑不写死页数
5. 请求失败有重试和日志
6. 大数据量结果不会导致前端卡死
7. 导出文件字段完整且可读
```

## 17. 开发注意事项

```text
1. 所有接口地址、请求参数、响应字段以 DevTools Network 抓包为准
2. 本文档中的字段名是业务字段，不代表 EchoTik 真实接口字段名
3. 如果 EchoTik 有风控、验证码或二次验证，需要记录阻塞点，不要绕过未授权安全机制
4. 如果接口签名复杂，先单独完成接口探查和签名复现，再开发完整采集流程
5. 不要把测试账号、密码、Cookie、Token、抓包 Header 明文提交到仓库
6. 采集逻辑需要保留足够日志，方便定位分页、限流、登录态问题
```

## 18. 建议开发顺序

```text
1. 阅读现有前后端结构和任务/导出模块
2. 抓包确认 EchoTik 登录、商品搜索、达人列表接口
3. 实现 EchoTikClient，请求单页商品列表成功
4. 实现商品列表完整分页
5. 实现单个商品达人列表单页采集
6. 实现达人列表完整分页
7. 实现异步任务和进度查询接口
8. 实现 Excel 导出
9. 接入前端页面
10. 使用关键词 ลิปแมตต์สีชัด 做端到端验证
```

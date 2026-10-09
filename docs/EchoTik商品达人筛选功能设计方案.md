# EchoTik 商品达人筛选功能设计方案

## 1. 目标

在现有“关键词采集全部商品，再逐商品采集全部达人”的流程上增加筛选能力，让用户可以：

1. 先按商品类目、销量、播放量等条件缩小商品范围，减少后续商品详情和达人列表请求。
2. 再按达人带货类目、达人销量、视频播放量等条件保留目标达人。
3. 在页面、日志和 Excel 中看清“原始数量、筛选后数量、排除数量”，避免用户误以为数据漏采。

本次不改变以下既有规则：

- 国家固定为泰国。
- 支持多个关键词并按商品 ID 去重。
- 商品列表和单商品达人列表仍完整分页。
- Creators 工作表仍采用“商品 × 达人”一行的明细结构；同一达人关联多个商品时保留多行。

## 2. 当前能力与接口边界

### 2.1 当前桌面端

前端目前只提交：

- `keywords`
- `page_size`
- `output_dir`
- `account_id`

后端商品列表固定按 `total_sale_nd_cnt desc` 请求，随后无条件采集每个商品的全部达人。

### 2.2 EchoTik 当前接口能力

商品库 `/data/products` 原生支持以下服务端筛选参数：

- `product_categories`：商品类目
- `sales`：所选周期销量
- `total_sale_cnt`：累计销量
- `views_count`：商品相关视频播放量
- `videos_count`：商品相关视频数
- `related_influencers`：关联达人数
- `price`：价格
- `dateRange`：销量周期，支持 1、7、30、90 天

单商品达人列表 `/data/products/{product_id}/influencers` 返回的达人字段包括：

- `categories`：达人带货相关类目
- `sales`：EchoTik 达人销量口径
- `total_video_viewers` / `views`：视频总播放量
- `follower_count`：粉丝数
- `related_video`：关联视频数
- `product_ifl_gmv_amt`：该达人对当前商品贡献的 GMV
- `related_live` / `total_live_viewers`：关联直播数和直播观看量

该达人列表接口当前接受排序参数，但传入销量、播放量、粉丝数等区间参数不会缩小结果集。因此：

- 商品条件由 EchoTik 服务端先筛选，后端再做一次本地校验。
- 达人条件必须在完整分页采集后由本地筛选。

## 3. 筛选口径

### 3.1 商品筛选（第一期）

| 前端名称 | 内部字段 | EchoTik 参数/字段 | 默认值 |
| --- | --- | --- | --- |
| 商品类目 | `category_ids` | `product_categories` | 全部 |
| 销量周期 | `sales_period_days` | `dateRange` | 7 天 |
| 周期销量 | `period_sales` | `sales` / `total_sale_nd_cnt` | 不限 |
| 累计销量 | `total_sales` | `total_sale_cnt` | 不限 |
| 视频播放量 | `video_views` | `views_count` / `view_count` | 不限 |
| 关联视频数 | `video_count` | `videos_count` | 不限 |
| 关联达人数 | `creator_count` | `related_influencers` / `influencers_count` | 不限 |

第一期主界面默认展示前四项；视频数和关联达人数放在“更多商品条件”中。

### 3.2 达人筛选（第一期）

| 前端名称 | 内部字段 | EchoTik 字段 | 默认值 |
| --- | --- | --- | --- |
| 达人带货类目 | `category_ids` | `categories` / `category_product` | 全部 |
| 达人销量 | `sales` | `sales` | 不限 |
| 视频总播放量 | `video_play_count` | `total_video_viewers` / `views` | 不限 |
| 粉丝数 | `fans_count` | `follower_count` | 不限 |
| 关联视频数 | `video_count` | `related_video` | 不限 |
| 当前商品带货 GMV | `product_gmv` | `product_ifl_gmv_amt` | 不限 |

“达人销量”必须在帮助文字中标注为“EchoTik 返回的达人销量口径”，不能描述为当前商品销量；当前商品维度的贡献指标使用“当前商品带货 GMV”。

### 3.3 组合规则

- 不同筛选项之间使用 `AND`。
- 类目多选内部使用 `OR`，命中任一选中类目即保留。
- 区间上下界均包含边界值。
- 用户只填最小值时表示 `>= min`，只填最大值时表示 `<= max`。
- 某字段为空且对应筛选已启用时，该行不满足条件，并计入“指标缺失排除数”。
- 没有启用任何达人条件时，行为与当前版本一致，保留全部达人。

## 4. 前端设计

### 4.1 页面结构

保留现有账号栏、关键词、输出目录、日志和结果表，将采集表单调整为四个区块：

```text
┌ 采集范围 ───────────────────────────────────────────┐
│ 关键词（多行）             国家：泰国（固定）         │
├ 商品条件 · 先缩小商品范围 ───────────────────────────┤
│ 商品类目 [多选级联]   销量周期 [近7天 ▼]              │
│ 周期销量 [最小] - [最大]  累计销量 [最小] - [最大]    │
│ 视频播放 [最小] - [最大]  [展开更多商品条件]           │
├ 达人条件 · 采集后保留匹配达人 ───────────────────────┤
│ 带货类目 [多选]       达人销量 [最小] - [最大]         │
│ 视频播放 [最小] - [最大]  粉丝数 [最小] - [最大]      │
│ [展开更多达人条件]                                      │
├ 输出设置 ────────────────────────────────────────────┤
│ 输出目录 [...]            每页数量 [50]                │
│ 已选条件：类目 2 个 · 近7天销量≥100 · 播放量≥1万       │
│ [开始采集] [重置筛选] [打开输出文件]                   │
└───────────────────────────────────────────────────────┘
```

### 4.2 交互细节

1. 页面初始化时，根据当前 EchoTik 账号异步加载类目和官方推荐区间。
2. 类目加载失败时允许继续采集，类目控件禁用并提示“类目加载失败，可重试”；不能阻塞不带类目条件的任务。
3. 数值输入允许直接输入 `10000`，也允许输入 `1万`、`2.5万`、`1亿`，失焦后统一显示格式化值。
4. 最小值大于最大值时禁止提交，并将焦点定位到错误区间。
5. 有筛选条件时，在按钮上方显示条件摘要；无条件时显示“未设置筛选，将采集全部匹配商品和达人”。
6. 任务运行期间禁用所有筛选控件，防止页面显示条件与实际任务不一致。
7. “重置筛选”只清空筛选条件，不清空关键词、账号和输出目录。
8. 不把“每页数量”作为主要业务选项，移动到输出设置或高级设置中。

### 4.3 结果区

顶部统计卡调整为：

- 命中商品：商品服务端筛选并去重后的数量
- 原始达人：完整分页读取到的达人明细数
- 保留达人：本地筛选后写入 Creators 的数量
- 排除达人：`原始达人 - 保留达人`

结果表建议增加 `达人销量`、`视频总播放量`、`带货类目` 三列，并保留当前商品 GMV、粉丝数、关联视频数。

## 5. 前后端数据结构

### 5.1 启动任务请求

前端使用业务字段，不直接暴露 EchoTik 参数名：

```json
{
  "account_id": "echotik-account-id",
  "keywords": ["lip matte", "lip tint"],
  "output_dir": "D:/output",
  "page_size": 50,
  "filters": {
    "products": {
      "category_ids": ["category-id-1", "category-id-2"],
      "sales_period_days": 7,
      "period_sales": {"min": 100, "max": null},
      "total_sales": {"min": null, "max": null},
      "video_views": {"min": 10000, "max": null},
      "video_count": {"min": null, "max": null},
      "creator_count": {"min": null, "max": null}
    },
    "creators": {
      "category_ids": ["category-id-1"],
      "sales": {"min": 100, "max": null},
      "video_play_count": {"min": 10000, "max": null},
      "fans_count": {"min": null, "max": 500000},
      "video_count": {"min": 1, "max": null},
      "product_gmv": {"min": null, "max": null}
    }
  }
}
```

### 5.2 数据模型

新增不可变模型，避免在采集流程中到处读取原始字典：

```python
@dataclass(frozen=True)
class NumberRange:
    minimum: Decimal | None = None
    maximum: Decimal | None = None

@dataclass(frozen=True)
class ProductFilters:
    category_ids: tuple[str, ...] = ()
    sales_period_days: int = 7
    period_sales: NumberRange = NumberRange()
    total_sales: NumberRange = NumberRange()
    video_views: NumberRange = NumberRange()
    video_count: NumberRange = NumberRange()
    creator_count: NumberRange = NumberRange()

@dataclass(frozen=True)
class CreatorFilters:
    category_ids: tuple[str, ...] = ()
    sales: NumberRange = NumberRange()
    video_play_count: NumberRange = NumberRange()
    fans_count: NumberRange = NumberRange()
    video_count: NumberRange = NumberRange()
    product_gmv: NumberRange = NumberRange()
```

`EchoTikCollectJob` 增加 `product_filters` 和 `creator_filters`。

### 5.3 类目与筛选选项接口

在 pywebview bridge 增加：

```text
get_echotik_filter_options({ account_id })
```

返回：

```json
{
  "ok": true,
  "categories": [
    {"id": "...", "name": "Beauty", "children": []}
  ],
  "presets": {
    "period_sales": ["<100", "100-500", ">500"],
    "total_sales": ["<100", "100-500", ">500"],
    "video_views": ["<10000", "10000-100000", ">100000"]
  }
}
```

选项按“账号 + 国家”缓存 30 分钟，避免每次进入页面都重新登录和请求。

## 6. 后端采集流程

```text
校验请求
  → 登录 EchoTik
  → 将商品业务区间转换为 EchoTik 参数
  → 按关键词完整分页读取商品
  → 按商品 ID 去重并合并关键词
  → 本地复核商品条件
  → 对保留商品逐个完整分页读取达人
  → 标准化数值和类目
  → 本地执行达人条件
  → 写入商品、命中达人和筛选统计
  → 导出 Excel
```

### 6.1 商品服务端参数转换

统一用一个函数生成区间字符串：

- `{min: 100, max: null}` → `>100`（本地复核仍按 `>=100`）
- `{min: null, max: 500}` → `<500`（本地复核仍按 `<=500`）
- `{min: 100, max: 500}` → `100-500`

由于第三方接口对边界的解释可能是严格大于/小于，服务端参数只用于缩小候选集合时要避免漏数。推荐转换为稍宽的官方区间或仅使用官方预设，并始终执行本地精确判断。不能只依赖第三方过滤结果完成边界判断。

### 6.2 数值标准化

EchoTik 部分字段返回展示字符串，例如 `20.35万`、`14.76亿`、`฿6810.53`。新增统一解析函数：

```text
20.35万     → 203500
14.76亿     → 1476000000
1.2K        → 1200
3.4M        → 3400000
฿6,810.53   → 6810.53
空值/—      → None
```

筛选比较只能使用标准化数值，Excel 可以同时保留原始展示值和标准化数值。禁止直接用字符串比较。

### 6.3 统计

任务结果新增：

```json
{
  "product_candidate_count": 120,
  "product_count": 36,
  "creator_source_count": 6800,
  "creator_count": 412,
  "creator_filtered_count": 6388,
  "creator_missing_metric_count": 23,
  "failed_product_count": 1
}
```

日志至少包含：

- 每个关键词接口返回商品数和本地复核后保留数。
- 每个商品原始达人数、命中数、排除数。
- 最终筛选条件摘要和汇总数量。

## 7. Excel 调整

### 7.1 Products

保留现有字段，新增：

- `video_play_count`
- `source_creator_count`
- `matched_creator_count`
- `filtered_creator_count`

即使某商品没有命中达人，也保留 Products 行并令 `matched_creator_count=0`。

### 7.2 Creators

新增：

- `creator_sales`
- `creator_sales_value`
- `video_play_count_value`
- `fans_count_value`
- `product_gmv_value`
- `matched_filters`（用于说明命中的条件，可选）

现有展示字段继续保留，数值字段用于后续 Excel 排序和公式计算。

### 7.3 FilterSummary

新增 `FilterSummary` 工作表，记录：

- 采集时间、国家、关键词
- 商品筛选条件
- 达人筛选条件
- 候选/保留/排除数量
- 指标缺失排除数量

这样用户转发 Excel 后仍能知道该文件采用了什么口径。

## 8. 异常与兼容

1. 旧前端不传 `filters` 时，后端使用空筛选，行为必须与当前版本完全一致。
2. EchoTik 不识别某个商品服务端筛选参数时，记录警告并降级为完整采集后本地筛选；不能静默返回未筛选数据。
3. 类目 ID 失效时明确提示用户重新选择类目。
4. 达人筛选结果为 0 不属于任务失败，仍生成 Excel。
5. 单个商品达人采集失败时继续处理其他商品，并在 Products 的 `error` 中记录。
6. 所有筛选条件必须随任务对象固化；页面后续修改不能影响运行中的任务。

## 9. 需要修改的文件

- `frontend/pages/echotik-collect.html`
  - 增加商品筛选、达人筛选、条件摘要和筛选统计。
- `frontend/assets/pages/echotik-collect.js`
  - 加载筛选选项、管理表单状态、校验区间、组装 `filters` 请求、渲染新统计。
- `frontend/assets/app.css`
  - 增加筛选卡、区间输入、类目多选、摘要标签和响应式样式。
- `frontend/assets/common.js`
  - 补充预览 API 的筛选选项和筛选结果数据。
- `backend/app_bridge.py`
  - 增加筛选选项接口，并透传任务筛选结果统计。
- `backend/services/echotik_collector.py`
  - 增加筛选模型、参数映射、数值解析、本地过滤、统计和 Excel 字段。
- `tests/test_echotik_collector.py`
  - 增加筛选校验、数值解析、商品参数、达人过滤、空值和 Excel 测试。
- `tests/test_frontend_integrity.py`
  - 校验新增控件 ID 和脚本完整性。

## 10. 验收标准

1. 不设置筛选时，采集数量和现有版本一致。
2. 商品类目、周期销量、累计销量、播放量可单独或组合使用。
3. 达人类目、达人销量、视频播放量可单独或组合使用。
4. `1万`、`1亿`、`K`、`M`、货币符号等格式能正确转换和比较。
5. 商品筛选在请求商品详情和达人列表之前生效。
6. 达人列表仍完整分页，再执行本地过滤，不因排序提前停止导致漏数。
7. 页面能同时展示原始达人、保留达人和排除达人数量。
8. 筛选后 0 个达人仍能成功生成 Excel。
9. Excel 包含 Products、Creators、FilterSummary 三个工作表。
10. 旧请求结构继续可用，现有 EchoTik 回归测试全部通过。

## 11. 推荐开发顺序

1. 先实现数值解析、筛选模型和纯函数单元测试。
2. 接入商品服务端筛选并做本地复核。
3. 增加达人字段映射和本地筛选。
4. 调整 Excel 和任务统计。
5. 最后实现前端筛选控件、类目加载和结果展示。

第一期建议先完成用户明确提出的六项：商品类目、商品销量、商品播放量、达人类目、达人销量、达人播放量。粉丝数、视频数、GMV 的后端结构同时预留，前端可放入“更多条件”，避免后续再次调整请求结构。

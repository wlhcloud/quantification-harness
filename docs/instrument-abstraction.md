# Instrument 抽象设计（STOCK/ETF/Index 统一）

> 状态：设计稿（M1 收尾） · 原则：**只增不改**，股票链路零改动
> 关联：`AI_QUANT_ETF_SYSTEM_DESIGN.md` §26（统一 Instrument）、§46（Gap 迁移）

## 1. 目标

当前系统从证券主档到因子、回测、前端全部按"股票"建模（`security_master` 仅含 A 股，
`fund_type`/估值/财报因子对 ETF 无意义）。目标是把"标的"抽象为统一 Instrument，
让因子、回测、查询、前端可同时服务 **股票 / ETF / 指数**，且不破坏现有股票数据与代码。

## 2. 现状（股票专属点）

| 层 | 现状 | ETF 适配问题 |
|---|---|---|
| 主档 | `security_master`（5556 只 A 股，list_status 等） | 无 ETF/指数条目，字段股票化（industry/market 板块） |
| 行情 | `daily_bars`（按 code 存股票日线） | ETF 日线已入 `etf_daily_bars`，指数已入 `index_daily_bars` |
| 因子 | ROE/PE/PB/净利润同比等估值财报因子 | ETF 无财报，需动量/RS/趋势/量能/风险/流动性因子 |
| 池 | 股票池（`selection_candidates`） | 需 ETF 池（`etf_metadata.eligible=1`） |
| 回测 | 股票 T+1 月度/短期回测，按股票池换手 | ETF 需 Top3/5 组合回测、调仓成本模型 |
| 前端 | 个股页面、选股页面 | 需 ETF 列表/详情/排名页面 |

## 3. Instrument 抽象设计（新增，不动旧表）

### 3.1 主档：`instrument_master`（market.db 新增）

```sql
CREATE TABLE IF NOT EXISTS instrument_master (
  ts_code      TEXT PRIMARY KEY,      -- 510300.SH / 000001.SZ / 000300.SH
  name         TEXT NOT NULL,
  inst_type    TEXT NOT NULL,         -- stock | etf | index
  exchange     TEXT,                  -- SSE / SZSE
  list_date    TEXT,                  -- YYYYMMDD
  delist_date  TEXT,
  status       TEXT NOT NULL DEFAULT 'L',  -- L 上市 / D 退市 / P 暂停
  source       TEXT,                  -- 数据来源（security_master / fund_basic / index_basic）
  updated_at   TEXT NOT NULL
);
```

- `inst_type` 为一级分型；股票沿用 `security_master` 语义（industry/market 属股票扩展，不搬进主档）
- 数据来源：股票=security_master 同步结果；ETF=etf_metadata（eligible=1 为主，全量备查）；
  指数=index_basic（后续补同步）
- 主档只放"共性子集"，各类型专属字段留在各自表（`security_master` / `etf_metadata`），
  查询层按需 JOIN —— **避免一张大表塞满所有类型的专属列**

### 3.2 分层读写

```
写入侧（quant-sync 保持现状）
  stock: security_master (现状不动)
  etf:   etf_metadata + etf_daily_bars (M1 已落地)
  index: index_daily_bars (M1 已落地) + index_basic (待补)
  instrument_master: 由上述同步完成后聚合写入（M2 实施）

读取侧（data-query 新增统一视图）
  GET /api/v1/instruments?type=etf&q=...      -- 统一主档查询
  GET /api/v1/instruments/{ts_code}/bars      -- 按类型路由到对应行情表
```

### 3.3 各层改造点（分阶段，均为增量）

| 阶段 | 模块 | 动作 |
|---|---|---|
| M2 | data-query | 新增 `instrument_master` 聚合写入 + `/api/v1/instruments` 查询端点（股票/ETF/指数统一返回） |
| M2 | quant-engine | 因子注册表加 `inst_type` 维度：股票因子集 / ETF 因子集分开计算；池抽象（股票池 / ETF 池统一接口） |
| M2 | 回测 | 回测入口按 `inst_type` 路由：股票沿用 T+1 个股回测；ETF 新增 Top3/5 组合回测（成本/滑点复用现有模型） |
| M3 | quant-web | 前端通用 Instrument 页面（列表/详情/排名），ETF 与股票共用组件 |
| 不动 | quant-sync 股票链路 | `security_master` / `daily_bars` / 财报 / 分钟线 全部保持现状 |

## 4. 风险与边界

- **不迁移旧数据**：`security_master` 数据不搬入新表，`instrument_master` 从零开始由同步聚合，
  与旧表并存；查询层以新表为统一入口，旧接口保持可用
- **指数主档**：`index_basic` 尚未接入（M2 补），先用 `index_daily_bars` 中出现的 ts_code 兜底
- **Survivorship Bias**：ETF 池当前按"最新名单"回补历史日线（新 ETF 才有历史 K 线），
  历史回测需在 M3 引入"当时点可交易"过滤（etf_metadata 的 list_date/delist_date 已备）
- **多源一致性**：promax/rds/official 三通道字段差异由字典映射层处理（沿用 market.trade_calendar 模式）

## 5. 验收口径

- M2 完成后：`GET /api/v1/instruments?type=etf` 与 `?type=stock` 均返回正确主档；
  ETF 因子与股票因子同库共存、互不覆盖；ETF Top3/5 回测可跑通 260 日 Walk-Forward 且含成本
- 全程：旧测试（quant-sync 15 / data-query 6 / quant-engine 8）保持绿色

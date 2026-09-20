# DSH Quant 架构 v2（目标态）

> 状态：v2 已实施；PIT 研究数据与生产级成交模型持续完善中
> 根目录 `ARCHITECTURE.md` 为运行摘要，本文件为完整设计与约束。

## 0. 一句话

量化平台收敛为 **Python 独占数据/计算平面 + 一个 Agent 编排宿主 + 一个后台管理前端**。
Node 退出一切数据写入与计算，只保留 DSH Agent 编排；上游同步、因子/回测/训练全部迁入 Python。

## 1. 核心原则

1. **Python 独占写库**：所有 SQLite 原始库与派生库，唯一写入方是 Python 服务；Node 零写入。
2. **每项能力一个实现、一个入口**：同步只有 quant-sync，计算只有 quant-engine，查询只有 data-query。
3. **元数据归 Python**：数据集目录、数据源配置、字典映射、同步账本迁入 `data/meta.db`（替代 v1 的 Node `core.db`）。
4. **Agent 保留、UI 收敛**：DSH 保留为 Agent 编排宿主（后期对接 Agent）；内嵌 quant-ui 移除，`quant-web` 成为唯一后台管理控制台。
5. **契约先行**：服务间字段以 `contracts/` 下的 OpenAPI/JSON Schema 为准。

## 2. 服务清单（含端口重排）

| 平面 | 服务 | 技术 | 新端口 | 职责 |
|---|---|---|---|---|
| 交互面 | DSH 量化宿主 | Node + cordis | **3081** | Agent 编排层；不承载量化数据逻辑，无 quant-ui |
| 交互面 | quant-web 后台管理 | React + Vite | **3082** | 唯一后台管理控制台（数据中心/同步中心/字典治理/系统设置/因子/回测） |
| 数据面 | **quant-sync** | Python FastAPI | **9101** | 上游数据同步（日线/估值/财报/分钟）+ 字典归一化 + 落库 + 同步账本 |
| 计算面 | **quant-engine** | Python FastAPI | **9102** | 特征/因子挖掘/训练/回测/推理（job 化），写派生库与 artifacts |
| 查询面 | **data-query-service** | Python FastAPI | **9103** | 只读查询 API（market/minute/finance/factor/质量/元数据） |

> 说明：Python 后端共 3 个进程。其中 quant-sync 与 quant-engine 是本轮新规划的两个数据处理服务，data-query-service 为保留的只读查询服务。

端口编排约定：`308x` 交互面，`91xx` Python 后端面；`9101 同步 → 9102 计算 → 9103 查询`。

## 3. 数据所有权与库布局

| 数据 | 写入方 | 说明 |
|---|---|---|
| `data/market.db` | quant-sync | 日线、估值、证券主档、交易日历、历史证券状态、复权因子 |
| `data/finance.db` | quant-sync | 四类财报 |
| `data/minute.db` | quant-sync | 1m/5m 分钟线 |
| `data/meta.db` | quant-sync + quant-engine | 数据集目录、数据源配置、字典、工具路由、同步账本（替代 v1 core.db） |
| `data/factors.db` | quant-engine | 因子快照 |
| `data/backtest.db` | quant-engine | 回测结果 |
| `data/quant-engine.db` | quant-engine | job 账本 |
| `data/features/` | quant-engine | 派生特征 |
| `artifacts/` | quant-engine | 模型与报告 |

并发约定：`meta.db` 目前由 Python 服务使用 WAL + busy_timeout 协调；data-query 以 `mode=ro` + `PRAGMA query_only=ON` 打开全部库。扩展到多机部署前迁移 PostgreSQL。

安全约定：查询端永不返回上游 Token 原文；同步和计算服务的写接口通过
`QUANT_SYNC_API_KEY` / `QUANT_ENGINE_API_KEY` 保护。生产环境禁止使用空 Key。

## 4. 各服务职责明细

### 4.1 quant-sync（9101，替代 v1 的 tushare-gateway / tushare-data / finance-data / minute-data / minute-sync-service / minute-sync-client）

- 从 `meta.db` 读取 `data_sources`（rds/promax/official）与 `tool_routes` 决定取数链路与回退顺序。
- 按 `dictionary_mappings` 做上游字段归一化（含 scale 变换）。
- 落库：market / finance / minute（三套原始库），保留 `raw_json` 与 `source`。
- 同步账本：`sync_runs`（全市场分钟线断点续传等）。
- HTTP：`/health`、`/sync`（单标的）、`/sync-all`（全市场）、`/sync-all/retry`、`/sync-all/status`、`/sync-all/failures`、`/records`。
- 迁移后 DS 侧不再有 Node 同步插件。

### 4.2 quant-engine（9102，扩展 v1 骨架）

- 现有 job 基础设施（queued/running/complete/error/cancelled）沿用，落地 handler：
  `features` / `training` / `backtest` / `inference`（+ ETF 与股票 ML 系列，共 15 类）。
  > 2026-09-18：`factor_mining` 已随 legacy 因子选股退役删除（模块 `quant_engine/factors/` 一并移除，
  > `selection_candidates` 建表语句迁到 `stock_ml`）。legacy 月度回测在准入层硬阻断。
- 读原始库（market/finance/minute），写 `data/features`、`artifacts`、`factors.db`、`backtest.db`。
- HTTP：`/api/v1/jobs`（增删查/取消）、`/api/v1/health`。
- 替代 v1 的 Node `factor-selection`、`factor-backtest`；`quant-engine-client` 不再需要。

### 4.3 data-query-service（9103，保留）

- 只读 API：`/api/v1/datasets`、`/api/v1/datasets/minute/{freq}/quality|governance`、`/api/v1/minute-bars`、`/api/v1/securities`。
- 全库 `mode=ro` 打开；新增对 `meta.db`（元数据/数据源/字典）的只读端点，承接原 quant-core 的读侧。

### 4.4 DSH 宿主（3081，仅 Agent 编排）

- 保留 `@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-web-app` 等基础 bundle，移除 quant-ui 与全部量化插件。
- 作为未来 Agent 对接与任务编排的运行时；量化操作一律经 HTTP 调 Python 服务。

### 4.5 quant-web（3082，唯一后台管理控制台）

- 直连 Python 服务：读走 9103，同步走 9101，因子/回测走 9102（不再经 DSH `/api/quant` 代理）。
- 页面：数据中心、同步中心、分钟浏览器、字典治理、系统设置、因子研究、回测、T+1 选股。

## 5. 契约

- `contracts/openapi/quant-engine.yaml`：计算引擎 job 契约（沿用）。
- `contracts/schemas/job.schema.json`：job 结构（沿用）。
- 新增：`contracts/openapi/quant-sync.yaml`、`contracts/openapi/data-query.yaml`（同步/查询接口契约）。
- 模型配置统一：`config/factor-models.yaml`（选股/回测用）+ `config/{feature-sets,training,backtest}`（引擎用）。
  > ⚠️ **2026-09-20 更正**：`feature-sets/` 与 `training/` **从未接进代码**——
  > `pipeline.py` 只按字面 id（`daily-v1` / `lightgbm-t1`）工作，不加载这两个目录下的文件。
  > 只有 `backtest/t1-default.yaml` 是活的（`main.py` 的回测 handler 会加载）。
  > 详见 `docs/configuration.md` 的"活配置 vs 文档性配置"。
  > 2026-09-18 变更：legacy 因子选股（balanced / quality_value / trend_momentum / low_volatility /
  > alpha_aggressive）退役，`factor-models.yaml` 的 `models` 只剩 `ml_walkforward`（weights 为空，
  > 仅作登记；真实信号由 `stock_walkforward` 写入 `selection_candidates`）。`filters`/`factors` 保留，
  > 供 `factor_snapshots` 历史快照做只读诊断。每日链不再提交 `factor_mining`；legacy 月度回测
  > 在数据准入层被硬阻断（否则 `run_monthly` 会因空权重除零崩溃）。

## 6. 迁移路线（D5：允许换表/改接口一次到位）

| 阶段 | 内容 | 完成后状态 |
|---|---|---|
| **Phase 0** | 冻结现状、补契约（sync/query OpenAPI + meta 表结构） | 契约就绪 |
| **Phase 1** | 元数据迁移：core.db → meta.db（Python 侧持有），data-query 接只读元数据端点 | 元数据归 Python |
| **Phase 2** | quant-sync 落地（分钟线先行，再日线/估值/财报），替换 Node 同步栈 | 数据同步归 Python |
| **Phase 3** | quant-engine 落地五类 handler，替换 TS factor-selection / factor-backtest | 计算归 Python |
| **Phase 4** | quant-web 直连 Python；移除 quant-ui；DSH 仅留 Agent | 前端收敛 |
| **Phase 5** | 清理：删除 Node 量化插件与旧服务、临时脚本、端口重排、更新 `ARCHITECTURE.md` | v1 退役 |

## 7. 与 v1 的差异（删除清单）

删除/退役：
- Node 插件：`tushare-gateway`、`tushare-data`、`finance-data`、`minute-data`、`minute-sync-client`、`factor-selection`、`factor-backtest`、`quant-ui`、`quant-engine-client`（共 9 个量化插件，`task-board-api-compat` 视 task-board 去留而定）。
- Node 服务：`services/minute-sync-service`（其能力并入 quant-sync）。
- 库：`core.db`（并入 `meta.db`）。
- 全局 DSH 实例（3080）与内嵌量化视图。

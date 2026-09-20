# Quantification Harness Architecture (v2)

> 迁移完成后的目标态：Python 独占数据/计算平面，Node/DSH 仅保留 Agent 编排。
> 完整蓝图与迁移记录见 `docs/architecture-v2.md`。

## Runtime units

1. **DSH/Node（仅 Agent 编排，3081）** — 保留 dsh-base / dsh-web-app / task-board 基础 bundle；量化插件（quant-core / tushare-* / finance-data / minute-* / factor-* / quant-ui）已退役。
2. **quant-sync（Python，9101）** — 数据平面：上游同步（日线/估值/财报/分钟）、字典归一化、原始库落库、同步账本、数据源/路由/字典设置 CRUD。
3. **quant-engine（Python，9102）** — 计算平面：`factor_mining` 与 `backtest`（monthly / short）job，写派生库与 artifacts。
4. **data-query（Python，9103）** — 只读查询平面：数据集/质量/治理/分钟K线/证券 + 元数据 + 因子/选股/回测读端点。
5. **quant-web（React，3082）** — 唯一后台管理控制台，直连 9101 / 9102 / 9103。

## Data ownership

- **Python 独占写库**：原始库 `market.db` / `finance.db` / `minute.db` ← quant-sync；派生 `factors.db` / `backtest.db` / `quant-engine.db` + `data/features` + `artifacts` ← quant-engine；元数据 `meta.db`（数据集目录/数据源/字典/同步账本，替代 v1 core.db）← Python 侧。
- **Node 零写入**。
- 共享 HTTP 契约：`contracts/openapi/*.yaml`、`contracts/schemas/meta.sql`、`job.schema.json`。

## Ports

| 端口 | 服务 |
|---|---|
| 3081 | DSH 量化宿主（Agent 编排） |
| 3082 | quant-web 后台管理 |
| 9101 | quant-sync（同步） |
| 9102 | quant-engine（计算） |
| 9103 | data-query（只读查询） |

## 已退役（v1）

- Node 量化插件：quant-core、tushare-gateway、tushare-data、finance-data、minute-data、minute-sync-client、factor-selection、factor-backtest、quant-ui、quant-engine-client。
- Node 服务：`services/minute-sync-service`（并入 quant-sync）。
- 元数据库 `core.db` → `meta.db`。

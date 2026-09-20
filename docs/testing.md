# 测试与验证

## 怎么跑

```powershell
pwsh -File scripts/run_tests.ps1                    # 三个服务全部
pwsh -File scripts/run_tests.ps1 -Service quant-engine

cd apps/quant-web; npm test                         # 前端单测（node:test + tsx，零额外依赖）
cd apps/quant-web; npm run typecheck                # 前端类型检查
```

- 仓库**没有装 pytest**（`python -m pytest` 报 `No module named pytest`），但 `services/*/tests` 下全是
  标准库 `unittest` 用例，所以脚本用 `unittest discover`，无需额外依赖。
- 服务包位于 `services/<svc>/src`，未安装进 site-packages，脚本会为该次运行设置 `PYTHONPATH`。
- 测试数据全部建在临时目录，不触碰 `data/` 下的真实库（`data-query` 的历史测试除外，见下）。
- 前端单测用 `node:test` + `tsx`（`npm test`，glob 为 `src/*.test.ts` + `src/*.test.tsx`），零额外依赖；
  页面组件的**渲染冒烟**用 `react-dom/server` 的 `renderToStaticMarkup`（见下），交互仍需 jsdom/浏览器。

## 现有覆盖

| 服务 | 用例数 | 覆盖 |
|---|---|---|
| quant-engine | 113 | ETF 因子/研究/回测/walkforward、ETF 与股票模拟盘、job 仓储与重启恢复、**job 超时/取消/进度节流/同资源互斥**、数据准入、特征-训练-推理、月度指标、regime、**ETF 模拟盘账务**、**stock_ml 标签契约**、**stock-ml 配置体检**、**embargo 与标签口径审计**、**HTTP 层与 API Key 中间件**、**防未来函数黄金测试（ETF + 股票 ML）**、**辅助因子（行业/资金流）落库与新鲜度**、**V5 回测汇总 + publishTopN 候选池**、**每日链单实例锁与运行台账**、**数据库在线快照备份**、**脚本布局巡检 + 启动脚本 cmd 安全** |
| quant-sync | 60 | 账本持久化/重启恢复、凭据加解密、日线研究与分页、分钟线并发、ETF 池/日线/指数、交易日历、实时快照、**字典 transform 表达式**、**ETF 历史行情并发落库**、**多源回退与超时传参**、**分钟计数与超时传递**、**meta.db 自举**、**HTTP 层与 API Key 中间件**、**管理 key 优先级与 sync-config 深合并/死键** |
| data-query | 9（CI 上 1 个 skip） | 只读仓储与 ETF 查询、**动态模型探测走统一只读封装**；`test_repository.py` 的 `test_real_databases_are_readable` 属集成测试，无 `data/` 时自动 skip（否则 CI 必挂） |

## 修复回归用例（新增，用于锁住已修问题）

| 用例文件 | 锁住的行为 |
|---|---|
| `quant-engine/tests/test_etf_sim_ledger.py` | 模拟盘现金/持仓跨期结转、NAV 复利、卖出用 T+1 开盘价、无法定价的仓位不被丢弃 |
| `quant-engine/tests/test_stock_ml_label.py` | `forward_3` 由服务产出、标签计算口径、`label` 必须是服务产出的列 |
| `quant-engine/tests/test_stock_ml_config.py` | 配置键体检：`transactionCostBps` 之类代码不读取的键必须告警；仓库 yaml 不得含失效键 |
| `quant-engine/tests/test_jobs.py` | job 超时（协作退出 + 超时结果不采用）、进度写库节流、取消轮询缓存、外部取消 → `cancelled`、**同资源串行 / 异资源并行 / 等锁超时** |
| `quant-engine/tests/test_walkforward_embargo.py` | embargo 默认 0 且元数据如实报告重叠、embargo 过大显式报错、标签口径审计能发现除权失真且不改数据 |
| `quant-engine/tests/test_http_api.py` | HTTP 层：health/jobs CRUD、未知 job 类型 422、回测准入 409 与其风险原因 422、提交到终态、404、**API Key 只管写接口** |
| `quant-sync/tests/test_upstream_transform.py` | `apply_transform` 的布尔/缩放分支互不误判 |
| `quant-sync/tests/test_upstream_client.py` | `PendingError` 会尝试备用通道、全通道 pending 仍抛 PendingError、显式 `timeout_ms` 优先级 |
| `quant-sync/tests/test_minute_progress.py` | 失败只计 `failed`、`timeout_ms` 沿调用链传递 |
| `quant-sync/tests/test_etf_concurrency.py` | 并发落库不丢交易日、不报事务冲突；ETF 历史同步按调用传递超时 |
| `quant-sync/tests/test_meta_bootstrap.py` | meta.db 按契约自举、缺契约不建空库、幂等且不清空既有数据 |
| `quant-sync/tests/test_http_api.py` | HTTP 层：只读端点可用、参数校验 400、retry 404/409（锁语义）、**API Key 只管写接口** |
| `quant-engine/tests/test_no_lookahead.py` | **防未来函数黄金测试**：尾部截断后重算，ETF 与股票 ML 的历史因子必须**逐位相等**；反向对照确认标签会变（比较不空跑）；尾部价格冲击不影响更早的因子 |
| `quant-engine/tests/test_aux_factors.py` | 辅助因子（行业/资金流）表结构、参数化 SQL 绑定数量、原子换表（staging→DROP→RENAME）、`aux_factor_freshness` 缺失表时的容错 |
| `quant-engine/tests/test_v5_and_candidates.py` | `summarize_v5_backtest` 的口径（净值/年化/Sharpe/回撤、缺文件不炸）、`publishTopN` 候选池与 `backtest.topN` 持仓数解耦 |
| `quant-engine/tests/test_daily_chain_lock.py` | 每日链**单实例锁**：持锁时第二次进入被拒、残留锁（>6h）与损坏锁被接管、运行台账每次运行写一行且含 exitCode/durationSec |
| `quant-engine/tests/test_backup_databases.py` | 备份脚本：写入方连接未关时 `VACUUM INTO` 快照仍可打开且行数正确、快照只含 `.db`（不含 WAL/SHM）、轮转只留最近 N 份、`--dry-run` 不落盘、库缺失返回非 0 |
| `quant-engine/tests/test_script_layout.py` | 脚本布局巡检：文档/生产写到的 `scripts/x` 必须存在（防归档挪错）、归档目录文件必须在 README 列名、生产不得 import/执行归档脚本、服务不得 shell out、被链 import 的脚本必须留在 `scripts/` 根；**启动脚本 cmd 安全**：`data/start-910*.bat` 与 `.quant.env` 必须 ASCII+CRLF 且含 `QUANT_API_KEY` |
| `quant-engine/tests/test_settings_api_key.py` | 管理 key 收敛：`QUANT_API_KEY` 权威、旧名回退、**权威压过陈旧旧名**、全空时明确 `api_key=""` |
| `quant-sync/tests/test_settings_and_sync_config.py` | 同上（sync 侧）+ `sync-config.json` 与 `QUANT_SYNC_*` 默认值的**深合并**（部分覆盖不丢兄弟键）、坏 JSON 回退、未知键只告警不丢、仓库里真实配置文件无死键 |
| `apps/quant-web/src/jobPolling.test.ts` | 前端轮询决策：complete/error/cancelled 终态、错误文案回退、未知状态继续、总超时边界 |
| `apps/quant-web/src/stockMlPage.test.ts` | StockMlPage 拆分后抽出的纯函数：`fmtPct/fmtNum/dateText/fileBaseName/quoteUrl` 边界、特征中文名与股票名映射（含"分类里的特征都必须有中文名"）、**净值曲线 SVG 几何**（点数、首尾贴边、基准线越界不画、y 刻度递减、净值全相等不除零） |
| `apps/quant-web/src/stockMlTabs.test.tsx` | **页面组件渲染冒烟**（`react-dom/server`）：页面壳空态、回测与信号/特征与因子/训练配置/训练版本四个页签的关键文案与列——拆分后"渲染期不崩"的回归网（Modal/Drawer 走 portal，服务端渲染不支持，未纳入） |

## 缺口（尚未覆盖）

- **编排脚本是 PowerShell，进不了 unittest**：`scripts/ensure_services.ps1`、`scripts/register_tasks.ps1`
  用 `[Parser]::ParseFile` 做语法检查 + 手工验证留痕（日志文件即证据）。本次验证记录：
  看门狗自动重启了 health 未返回 `authEnabled` 的 9101/9102（重启后 `authEnabled=True`）；
  手工 `Stop-Process` 杀掉 9103 后，一轮看门狗内恢复（`read_only=true`）；`-DryRun` 不落盘。
- `stock_ml` 的全量生产数据 walkforward 仍需单独做 GPU soak test；单元测试已覆盖标签、配置、
  embargo、因子因果性、CUDA 训练进程隔离、发布候选来源一致性与公司行动记账。
- 前端：**交互仍无自动化测试**（无 jsdom/Playwright 断言脚本）。目前覆盖 = 纯函数单测 + 组件渲染冒烟；
  点击/轮询/弹窗交互只能靠 `npm run build` + 手工点页面。为此拆分 StockMlPage 时把可测的逻辑
  （净值曲线几何、格式化、映射表）都挪成了纯函数，而不是留在 JSX 的 IIFE 里。
- 股票 ML 正式链已使用复权标签和 `security_status_history` 的 PIT 股票池；状态表缺失记录按不合格处理。
  行业历史成员数据尚不具备 PIT 口径，因此正式配置暂不启用行业特征，避免当前行业分类回填历史。

## 测试隔离注意事项

- `quant-engine/tests/test_http_api.py`：导入 `quant_engine.main` 前设置 `QUANT_ENGINE_PROJECT_ROOT`
  到临时目录，模块级的 `JobRepository`/`settings` 全部落在临时目录，不碰真实 `data/`。
- `quant-sync/tests/test_http_api.py`：除 `QUANT_SYNC_PROJECT_ROOT` 外，还要
  `QUANT_SYNC_SCHEDULER_ENABLED=0`（否则导入时启动行情调度线程、交易时段会自动发起真实上游同步），
  并在临时根下放一份 `contracts/schemas/meta.sql` 供自举。
- 用例只走"校验即返回 / 无上游调用"的端点，测试进程不联网。

## CI

`.github/workflows/ci.yml`：`python` job 对三个服务做 `pip install -e ".[dev]"` + 与本地完全一致的
`unittest discover`；`web` job 做 `npm ci` + `npx tsc -b`。刻意使用与 `scripts/run_tests.ps1` 相同的命令，
避免"CI 跑的命令本地没人验证过"。

顺带修掉的两个 CI 阻碍：`quant-engine` 的 `pyproject.toml` 漏声明 `pandas`（源码直接 import，全新环境装不上）；
`data-query` 的集成测试无 `data/` 时自动 skip。

# 运维手册（索引 / 口径审计 / job 治理）

## 1. 索引：`scripts/ensure_indexes.py`

只读实测的现状：

| 库 | 表 | 问题 |
|---|---|---|
| `data/market.db` | `daily_bars`（13.3M 行） | 主键 `(code, trade_date)`，`WHERE trade_date=?` 走 **SCAN 全表扫描**（首页市场宽度统计每次都扫） |
| `data/minute.db` | `minute_bars`（1.29 亿行） | 主键 `(code, trade_time, freq)`，按 code+时间区间+freq 查询时 `freq` 只能做**残余过滤**（1m/5m 混读） |

```powershell
python scripts/ensure_indexes.py            # 默认 dry-run：只报告缺什么
python scripts/ensure_indexes.py --apply    # 真正建索引
python scripts/ensure_indexes.py --apply --db market
```

- 全部 `CREATE INDEX IF NOT EXISTS`，可重复执行；建索引期间持写锁，请在收盘后、9101/9102 空闲时执行并先备份。
- 复查方式：`EXPLAIN QUERY PLAN` 应不再出现 `SCAN daily_bars`。

## 2. 标签价格口径审计：`GET /api/v1/stock-ml/label-audit`（只读）

`stock_ml_factors` 的 `forward_*` 标签用 **未复权 close** 计算，而 `daily_bars` 存的是原始价
（月度回测需 JOIN `adjustment_factors` 才连续）。除权日会让标签失真。该端点只量化差异，**不修改任何数据**。

```powershell
# code_glob 用于取子集（大库上全量对比较慢）
curl "http://127.0.0.1:9102/api/v1/stock-ml/label-audit?label=forward_20&code_glob=0000*&threshold=0.005"
```

2026-09 实测（`code_glob=0000*`，threshold=50bp）：

| 标签 | 对比行数 | 超阈值 | 平均绝对差 | 最大差 |
|---|---|---|---|---|
| forward_3 | 49,321 | 2.49% | 0.19pp | 145pp |
| forward_5 | 49,176 | 4.08% | 0.31pp | 195pp |
| forward_20 | 48,387 | **14.61%** | 1.26pp | 378pp |

`abnormalMaxDiff=true` 表示存在 >50pp 的极端差异，通常指向复权因子异常或长期停牌，建议人工核查。
**当前按决策暂不切换口径**（保持历史基线），需要切换时改 `build_factors`：标签与价格类因子统一用
`close * adj_factor`，并重跑 walkforward。

## 3. purge/embargo：`walkforward.embargoDays`

`forward_N` 标签在 t 日用到 t+N 的价格；紧邻验证集起点的训练样本会把测试期信息带进训练。

- 默认 `embargoDays: 0` —— 与历史基线完全同口径，**数值不变**。
- 设为 20（forward_20 的持有期）可消除窗口重叠；`docs/etf-quant-mvp.md` 的历史结论需要重跑才可比。
- 每种配置可在 `config/stock-ml.yaml`（walkforward 段）或 `config/etf-quant.yaml`（walkforward 段）设置，
  也可通过 job 参数 `{"walkforward": {"embargoDays": 20}}` 传入。
- 结果元数据会带 `embargoDays / labelHorizonDays / labelOverlapDays`，未开启时 `labelOverlapDays=20` 即为如实告警。
- `embargoDays` 大于 `validationDays` 时会**明确报错**（而不是让 numpy 抛 2-D 错误），提示调小 embargo 或增大窗口。

## 4. job 治理语义（9102）

> 同资源互斥：`factors.db`（stock_ml_factors / stock_walkforward / stock_ml_backtest / stock_ml_predict /
> industry / money_flow）、`etf-quant.db`（etf_factors / research / train / backtest / walkforward）、
> `features`、`artifacts`、`backtest.db` 各自串行。原实现只有 worker 数限制，整表重建与 walkforward
> 读取并发时可能读到半成品。等待时间不计入 job 超时额度；等待超过 `resource_wait_timeout`（默认 1h）记 error 并提示。
>
> 注：`factor_mining` 已随 legacy 因子选股退役（2026-09-18），不再是 `factors.db` 的竞争者。

- **超时**：每类 job 有默认超时（见 `quant_engine/jobs.py: DEFAULT_JOB_TIMEOUTS`，如 `stock_walkforward` 2h、
  `etf_walkforward` 90min、`backtest` 1h）。超时无法强杀线程，因此实现为：
  1) 处理器轮询 `cancelled()` 时会拿到 `True` 并尽快退出；
  2) 若处理器整段跑完才返回，则状态记为 `error`、错误信息含"超时"，**结果不采用**（不会写回超时结果）。
- **取消**：`POST /api/v1/jobs/{id}/cancel` 写库 `cancel_requested=1`；处理器协作退出后状态为 `cancelled`。
  已接入检查点：`pipeline`（特征/训练/推理）、`backtest/monthly`、`backtest/short`、`factors.rebuild`、
  `etf_quant.run_factors`、`etf_quant.run_walkforward`、`stock_ml.build_factors`、`ranker.train_ranker`。
- **进度写库节流**：`progress()` 只在整数百分点变化或间隔 ≥1s 时落库（原先每次调用都新开连接 + commit，
  一次训练可产生数千次写事务）；取消标记按 1s 间隔缓存，避免紧凑循环打满 SQLite。
- **重启**：进程启动时 `recover_interrupted()` 把 `queued/running` 一律标为 `error`（不自动续跑）；
  多 uvicorn worker 部署会互相误标，需单 worker 运行。

## 5. 前端 job 轮询与请求超时

- `apps/quant-web/src/useJobPolling.ts`：统一的 job 轮询（原先 4 个页面各写一份 `setInterval`）。
  - 默认间隔 4s（股票 ML 训练页 5s）；**新增总超时 90 分钟**（原实现无总超时，job 卡在 `running` 会永久轮询）。
  - 终态处理：`complete → onComplete`、`error/cancelled → onError`、查询失败 → `onPollError`、超时 → `onTimeout`。
    其中 `cancelled` 原先被当成"未终结"继续轮询，现在会正确停止并提示。
  - 组件卸载时自动停止轮询。
- `api.ts` 的 `engineRequest` 现在带超时：默认 30s（超出抛"计算引擎响应超时"）；
  同步长请求单独放宽——`/stock-ml/backtest` 15 分钟、`/stock-ml/predict` 5 分钟。
  `dataApi.get` 本来就有 8s 超时；`syncRequest/settingsRequest` 仍无超时（后续可补）。
- 前端没有单元测试，改动后必须跑类型检查：`cd apps/quant-web && ../../node_modules/.bin/tsc -b`。

## 6. 服务启动与环境变量

> 四个配置面的完整清单、优先级与"禁止事项"见 **`docs/configuration.md`**（本节只讲启动与鉴权）。

| 变量 | 作用 |
|---|---|
| `QUANT_SYNC_PROJECT_ROOT` / `QUANT_ENGINE_PROJECT_ROOT` | 项目根（决定 `data/`、`config/`、`contracts/` 位置；测试用它做隔离） |
| `QUANT_SYNC_SCHEDULER_ENABLED=0` | 关闭行情调度线程（交易时段会自动发起真实上游同步；测试/CI 用） |
| `QUANT_API_KEY` | **管理 key 的唯一权威变量**。非空时**写接口**要求 `X-API-Key`（读接口始终开放）。9101/9102/每日链/看门狗都读它 |
| `QUANT_SYNC_API_KEY` / `QUANT_ENGINE_API_KEY` | key 收敛前的旧名字，**仅作兼容回退**（`QUANT_API_KEY` 为空时才生效）；新部署不要再用 |
| `QUANT_SYNC_CREDENTIAL_KEY` | 上游 token 的落库加密密钥（`enc:v1:` 前缀）。**一旦启用不可随意更换**，否则已加密 token 无法解密 |

### 当前启用状态（2026-09 已生效）

- 三个服务均只绑 `127.0.0.1`（此前手工启动用的是 `--host 0.0.0.0`）；启动脚本 `data/start-910{1,2,3}.bat`
  会先加载 `.quant.env` 再启动，因此 **key 与绑定都随脚本生效**。
- 写接口已启用鉴权：无 `X-API-Key` → 401，带正确 key → 进入处理器；读接口保持开放。
- 上游 token 已从**明文**迁移为 `enc:v1:` 加密存储（重启时由 `SettingsStore._migrate_credentials` 完成，
  明文值会被原样读入后重新加密）。诊断脚本 `scripts/get_tushare_token.py` 已改为从环境变量/`.quant.env`
  读取密钥。
- 前端 `apps/quant-web/.env` 写入同一个管理 key（`VITE_QUANT_API_KEY`，Vite 启动时内联进 bundle）；
  改 key 后必须重启 Vite。
- `scripts/daily_stock_chain.py` 会自行读取 `.quant.env` 并在写请求上带 `X-API-Key`
  （Windows 计划任务的环境里没有这些变量）。
- 9101/9102 的 `GET /api/v1/health` 会返回 `authEnabled`（是否真的加载到 key）与 `authSource`
  （key 来自哪个环境变量）——因为 key 为空时鉴权是**静默失效**的，光看服务活着看不出来。
  看门狗据此判断并用 `start-910N.bat` 重启（见 §9）。
- 管理 key 已收敛为**一个权威变量** `QUANT_API_KEY`（`.quant.env` 里只维护这一份），
  旧名 `QUANT_SYNC_API_KEY` / `QUANT_ENGINE_API_KEY` 仅作兼容回退。
- `.quant.env` **必须保持 ASCII + CRLF**：`start-910N.bat` 用 cmd 的 `for /f` 解析它，
  多字节字符/LF 行尾会让后面的变量被静默漏读（= 鉴权失效）。有 `test_script_layout.py::StartupScriptSafetyTest` 守住。
- 服务启动时会往自己的日志写一行 `[startup] ... authEnabled=... authSource=...`：
  排查"接口 401/403 异常"时先看这一行，而不是猜。

### 轮换 / 排障

1. 轮换管理 key：改 `.quant.env` 的 `QUANT_API_KEY`（**唯一一处**）→ 同步改 `apps/quant-web/.env`
   的 `VITE_QUANT_API_KEY`（前端 Vite 是构建时内联，见 `docs/configuration.md` 末节）→
   重启 9101/9102（`data\start-9101.bat` / `start-9102.bat`，或杀掉进程让看门狗拉起）与前端 Vite。
   验证：`GET /api/v1/health` 应显示 `authEnabled=true authSource=QUANT_API_KEY`。
2. **不要**在未备份的情况下更换 `QUANT_SYNC_CREDENTIAL_KEY`；确需更换要先解密再重新加密
   （`scripts/get_tushare_token.py` 可用来确认密钥是否匹配，末尾会打印解密结果）。
3. 401 排查顺序：请求是否带 `X-API-Key` → key 是否与服务进程环境一致（`.bat` 是否加载了 `.quant.env`）→
   是否只改了 `.quant.env` 而没重启服务。

## 7. 已清除的数据与停用开关（2026-09-12）

按使用方要求，**股票分钟线不再保留**：

| 项目 | 内容 |
|---|---|
| 清除内容 | `data/minute.db` 全库归零：5m 行情 129,408,271 行 / 5,556 只标的（20240820~20260904）、1m 723 行、以及 `minute_sync_days/segments/run_items/runs` 全部账本 |
| 结果 | 21.21 GB → 4 KB（表结构保留，各表 0 行）；磁盘释放约 21.2 GB。**无备份，不可恢复** |
| 未受影响 | 指数分钟 `market.db.index_minute_bars`（2,802 行）、日线/估值/财报/ETF/因子/回测/模拟盘、每日链（`daily_stock_chain.py` 本来就不含分钟同步） |
| 受影响 | 短线回测（`kind=short`）、data-query 的 `/minute-bars` 与 `/datasets/minute/*` 端点（返回空）、研究脚本 `scripts/research_archive/_minute_tp_sl_test.py` 等（已归档） |

两个开关（默认关闭，重启后生效）：

| 变量 | 默认 | 行为 |
|---|---|---|
| `QUANT_SYNC_MINUTE_ENABLED` | `0` | `0` 时 `/sync/minute`、`/sync/minute/all`、`/sync/minute/all/retry` 一律 **410**，防止误触发重新下载约 21GB |
| `QUANT_ENGINE_SHORT_BACKTEST_ENABLED` | `0` | `0` 时短线回测准入直接 `allowed=false`（`hardBlocked=true`），**带风险运行也无法绕过**（409）；月度回测不受影响 |

恢复方式（如需重新启用股票分钟线）：把两个变量设为 `1` → 重启 9101/9102 → 调用
`POST /api/v1/sync/minute/all?freq=5m&start_date=20240820` 全量回补（预计数小时、约 21GB）。

## 8. 股票策略四个页面的口径（2026-09-12 修复）

| 页面 | 修复内容 |
|---|---|
| T+1 选股 | 新增「模拟盘 run」下拉（原先写死最新 run，常落在刚创建、还没成交的择时盘上）；默认选 **ml_walkforward**；空仓原因提示（熊市空仓 / 等 T+1） |
| 股票ML模型 | 「回测 / 选股」由同步长请求改为 **job 化**（带进度，`stock_ml_backtest` / `stock_ml_predict`）；「选股预测」经确认为**只读预览**（`predict_with_model` 不写 `selection_candidates`，页面上原先的"会写回信号"说法已纠正） |
| 策略回测 | 「V5策略回测」页签不再写死数字：改为 `GET /api/v1/stock/technical-timing/backtest/latest?variant=long\|short`，读 `artifacts/stock-ml/technical_timing_v5*_backtest.csv` **现场计算**（含净值曲线、年化/Sharpe/回撤口径），并标注"非模拟盘台账" |
| 股票模拟盘 | 默认选中逻辑与选股页一致（`stockSimRun.pickDefaultRun`）；run 下拉显示"待T+1"标记；归档盘不出现在下拉里 |

配套后端约定：

- `config/stock-ml.yaml` 新增 `walkforward.publishTopN: 20`：写入 `selection_candidates` 的是**候选池 20 只**，
  实际持仓仍是 `backtest.topN=5`（T+1 选股页的筛选/统计与 V5 择时盘的 "Top20 候选池" 由此名副其实）。
  该值在下一次 `stock_ml_walkforward` / `stock_ml_factors` 运行时生效。
- 模拟盘 run 支持 `status='archived'`：`GET /api/v1/stock/sim/runs` 默认隐藏归档盘，
  `?include_archived=true` 可查看；归档只是不再展示，`sim_status`/`advance` 仍可用。
- 每日链的股票模拟盘推进改为**优先推进 ml_walkforward 盘**（原实现只推"最新 run"，
  而新建的择时盘会占住最新位置，导致 ML 盘信号长期停在旧日期）。
- 空仓是策略状态而不是故障：`stock_sim_daily.note` 会写明「熊市空仓」或「等待 T+1」，
  两个页面据此给出提示。




## 9. 编排地图（计划任务 / 看门狗 / 每日链锁）

编排原先分散在**三处**且任务定义只存在于注册表里，出现问题：`quant-engine-9102` 任务指向
`start-9102.bat`（会加载 `.quant.env`），而 `quant-quant-sync-9101` 用的是**内联命令**——
计划任务环境里没有 `QUANT_SYNC_API_KEY`，于是 `settings.api_key` 为空、鉴权**静默失效**
（`require_api_key` 在 key 为空时直接放行所有写请求）；`data-query-9103` 干脆**没有任何任务**，
重启机器后就永久少一个服务（服务任务是一次性任务，`RestartCount=0`，挂了不会自己回来）。

现在只保留**一份可重放的源码定义**：`scripts/register_tasks.ps1`（幂等，`-DryRun` 可先看计划）。

| 任务 | 触发 | 动作 | 说明 |
|---|---|---|---|
| `quant-quant-sync-9101` | 登录时 | `data\start-9101.bat` | 加载 `.quant.env`，只绑 `127.0.0.1` |
| `quant-engine-9102` | 登录时 | `data\start-9102.bat` | 同上 |
| `quant-data-query-9103` | 登录时 | `data\start-9103.bat` | **新增**（此前只有手工启动） |
| `quant-services-watchdog` | 每 5 分钟 + 登录时 | `scripts\ensure_services.ps1` | 服务自愈，见下 |
| `stock-factor-daily-poll` | 每 30 分钟 | `scripts\run-stock-chain.bat` | 每日链（`IgnoreNew` + 链内锁双保险） |

进程内还有 `MarketScheduler`（quant-sync，交易时段自动同步行情）与外部豆包任务
「ETF量化每日更新」（18:30）——它们都通过 HTTP 走服务，不直接写库，因此不在上表里。

### 看门狗：`scripts/ensure_services.ps1`

每 5 分钟对 9101/9102/9103 做三项检查，失败则杀掉该端口上的 **python** 进程并用
`data\start-910N.bat` 重新拉起（等待恢复上限 90 秒）：

1. `GET /api/v1/health` 可达且 `status=ok`（进程活着但卡死同样判失败）；
2. 若 `.quant.env` 声明了该服务的 API key，则 `health.authEnabled` 必须为 `true`
   ——**这是防"鉴权静默失效"的关键断言**，health 新增的 `authEnabled` 字段就是给它用的；
3. 端口被**非 python** 进程占用时只告警不动手（避免误杀）。

日志：`data/service-watchdog.log`；自身单实例锁：`data/.service-watchdog.lock`（残留 >20 分钟自动接管）。
退出码 0 = 本轮全好，1 = 本轮有过问题（计划任务里 1 只表示"这轮修过东西"，会被下次运行覆盖）。

```
pwsh -File scripts\ensure_services.ps1 -DryRun    # 只体检不重启
pwsh -File scripts\ensure_services.ps1            # 手工体检+自愈
```

### 每日链：单实例锁 + 运行台账

`scripts/daily_stock_chain.py` 由 30 分钟轮询、手工调用、豆包任务三处都可能触发，原先**没有任何重入保护**：

- 锁：`data/.daily-chain.lock`（`O_CREAT|O_EXCL`，写入 pid 与开始时间）。持锁期间再次进入直接
  `[SKIP]` 返回 0；锁超过 6 小时视为上次被强杀的残留并接管。
- 台账：`data/daily-chain-runs.jsonl`，每行 `{startedAt, finishedAt, durationSec, pid, exitCode}`
  ——用来核对"今天到底跑了几轮、有没有叠加"。

运维排查顺序：`data/daily-chain-runs.jsonl` 看是否漏跑 → `data/service-watchdog.log` 看服务是否
被重启过 → `data/quant-engine-9102.log` 看 job 报错。

## 10. 数据库备份：`scripts/backup_databases.py`

`data/` 下的库合计约 7.6 GB 且**不可再生**（分钟线那次清零就是无备份、不可恢复）。现在提供了
在线快照脚本：

```
python scripts\backup_databases.py                      # 全部 .db → data\backups\<时间戳>\，保留最近 2 份
python scripts\backup_databases.py --keep 3
python scripts\backup_databases.py --dest E:\quant-backup   # 换盘（推荐）
python scripts\backup_databases.py --dry-run            # 只打印计划
```

- 用 SQLite `VACUUM INTO`：服务在写（WAL）时也能拿到**事务一致**的快照，不用停服；
  `cp` 在 WAL 下可能拷到半截状态，所以不用 `cp`。
- 每个快照写完立刻 `PRAGMA integrity_check` + 表数量核对，失败则该库标 `FAIL` 且退出码 1
  （不会出现"备份看起来有、其实打不开"）。
- 轮转：`--keep N` 只保留最近 N 个快照目录。
- **默认不排期**（占盘 = 库大小 × keep）：需要时手工注册一次，例如每天 20:30：

```
schtasks /Create /TN quant-db-backup /SC DAILY /ST 20:30 /TR "D:\ProdApp\Python\Python313\python.exe D:\ProdProject\AI\quantification-harness\scripts\backup_databases.py --keep 2"
```

注意盘位：D: 当前剩余约 48 GB，2 份快照约 15 GB；有条件就 `--dest` 指到别的盘/NAS。

## 11. 其他启动语义

- **meta.db 自举**：quant-sync 启动时按 `contracts/schemas/meta.sql` 幂等建表。
  原实现完全依赖手工执行 `scripts/migrate_meta.py`，全新环境或换 project_root 时会直接
  `no such table: tool_source_routes`。契约缺失时会记 warning 并跳过（不建空库）。
- **重启语义**：quant-engine 启动时 `recover_interrupted()` 把 `queued/running` 标为 `error`（不自动续跑）。



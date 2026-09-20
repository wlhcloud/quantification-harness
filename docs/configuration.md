# 配置面清单（谁说了算）

这个平台有 4 个配置面。它们不是"同一个东西的不同写法"，而是**分层**的：越靠后的越接近运行时。
配置出问题基本都源于"同一个事实在两个面上各写一份，改了其中一个以为生效了"。

| # | 配置面 | 位置 | 谁读 | 什么时候生效 |
|---|---|---|---|---|
| 1 | **密钥 / 凭据** | `.quant.env`（仓库根，**不入库**） | `data/start-910{1,2,3}.bat`（启动前 `set` 进环境）、`scripts/daily_stock_chain.py`（自带解析） | 重启对应进程 |
| 2 | **服务启动默认值** | `services/*/src/*/settings.py`（pydantic 字段默认值）+ `QUANT_*` / `DATA_QUERY_*` 环境变量 | 服务进程自身 | 重启进程 |
| 3 | **运行时可改配置** | `data/sync-config.json` | quant-sync（`PUT /api/v1/sync/config` 写入） | 立即（部分项重启调度线程/下一段同步生效） |
| 4 | **研究参数** | `config/*.yaml`（`stock-ml.yaml` / `etf-quant.yaml` / `factor-models.yaml` / `backtest/t1-default.yaml`） | quant-engine（回测、训练、选股口径）+ data-query（只读选股/诊断） | 下一次 job 运行（`factor-models.yaml` 与 9103 的读取是每请求热加载） |

> `config/factor-models.yaml`：legacy 因子选股已于 2026-09-18 退役，`models` 只剩 `ml_walkforward`，
> 且它的 `weights` 为空（不参与加权回测，仅作模型登记）。`filters`/`factors` 保留，供
> `factor_snapshots` 历史快照做只读诊断。改这个文件会影响：9103 `/selection`、`/selection/models`、
> `/factors/summary`、首页 `market/overview` 的候选面板，以及 9102 的月度回测准入
> （没有带权重的模型时月度回测会被硬阻断）。

前端另有 `apps/quant-web/.env`（`VITE_*`，构建时内联进 bundle），见文末"仍存的一处重复"。

### ⚠️ `.quant.env` 必须 ASCII + CRLF

`data/start-910{1,2,3}.bat` 是用 **cmd 的 `for /f "usebackq eol=# tokens=1,* delims=="`** 解析这个文件的。
如果往里写**多字节字符**（中文注释）或改成 **LF 行尾**，cmd 会把多字节注释行的下一行并进"注释"里，
于是那行的变量被**静默漏读**——服务照常启动，但写接口鉴权失效（`health.authEnabled=false`）。

2026-09-13 就这么踩过一次（`QUANT_API_KEY` 被漏读，看门狗靠 authEnabled 断言当场抓到并重启报警）。
现在由 `services/quant-engine/tests/test_script_layout.py::StartupScriptSafetyTest` 断言
`.bat` 与 `.quant.env` 只含 ASCII + CRLF，并且 `.quant.env` 必须提供 `QUANT_API_KEY`。


## 事实 → 权威来源

| 事实 | 权威来源 | 覆盖关系 | 锁住它的用例 |
|---|---|---|---|
| 管理 API key（写接口鉴权） | `.quant.env` 的 **`QUANT_API_KEY`** | 权威变量压过旧名；旧名 `QUANT_SYNC_API_KEY`/`QUANT_ENGINE_API_KEY` 仅作兼容回退 | `quant-engine/tests/test_settings_api_key.py`、`quant-sync/tests/test_settings_and_sync_config.py`、`test_daily_chain_lock.py::test_api_key_prefers_canonical_variable` |
| 上游 token 加密密钥 | `.quant.env` 的 `QUANT_SYNC_CREDENTIAL_KEY` | 不可随意更换（换了已加密 token 解不开） | 凭据加解密用例 |
| 上游 token 明文 | `data/meta.db`（`enc:v1:` 加密列） | 由 API 写入，不在环境里 | `test_credentials.py` 等 |
| 分钟/日线并发、段天数、请求超时、行情调度开关 | `data/sync-config.json`（面 3） | **深合并**覆盖面 2 的同名键；只覆盖真正改过的子键 | `test_settings_and_sync_config.py::test_load_config_merges_partial_json_over_env_defaults` |
| 服务端口 / 绑定地址 | 面 2（`host`/`port` 默认值）+ `data/start-910*.bat` 的 `--host 127.0.0.1 --port` | 命令行优先（现状两者一致） | `scripts/ensure_services.ps1`（体检）+ `docs/operations.md` §9 |
| 股票分钟线开关、短线回测开关 | 面 2（`QUANT_SYNC_MINUTE_ENABLED` / `QUANT_ENGINE_SHORT_BACKTEST_ENABLED`，默认关） | 关闭后接口返回 410 / 准入 hardBlocked | `test_http_api.py`、`test_etf_quant.py` |
| 交易成本、topN、label、publishTopN 等研究参数 | `config/*.yaml`（面 4） | **改数字必须先确认**（会影响所有回测口径）；yaml 里的键必须被代码读取 | `test_stock_ml_config.py`（配置键体检） |

## 不变量（配置面的"禁止事项"）

1. **管理 key 只有一个权威变量**（`QUANT_API_KEY`）。不要再在别处硬编码；服务端独有的旧名只为兼容旧部署存在。
2. **同一节的部分覆盖不得整块替换**：`sync-config.json` 与面 2 的合并必须是深合并，否则"改一个键、丢三个键"。
3. **yaml 里不得出现代码不读的键**（历史上出现过 `transactionCostBps` 这类"写了没人读"的失效配置）。
4. **`sync-config.json` 里不得出现未定义的键**：`_unknown_config_keys()` 会告警，用例会断言仓库里的那份是干净的。
5. **研究数字只由面 4 决定**：不要在代码里再放一份会"悄悄生效"的默认值。

## 轮换管理 key 的正确姿势

```powershell
# 1) 改权威变量（一个地方）
#    .quant.env:  QUANT_API_KEY=<新值>
# 2) 重启 9101/9102 —— 加载 .quant.env 的是 data\start-910N.bat
pwsh -File scripts\ensure_services.ps1        # 或杀掉进程后靠看门狗自动拉起
#    看门狗每 5 分钟体检；health.authEnabled/authSource 会说明 key 来自哪个变量
curl http://127.0.0.1:9102/api/v1/health      # 期望 authEnabled=true, authSource=QUANT_API_KEY
# 3) 前端（VITE 是构建时内联，必须改 apps/quant-web/.env 并重启 Vite）
# 4) 校验：不带 key 的写请求必须 401
```

## 配置可观测性：每次运行都能回答"用了什么配置、哪个代码版本"

配置面本身（上面 4 层）解决"谁说了算"，但不解决"**上次那个结果是用哪份配置跑出来的**"。
2026-09-20 审计 25 次 walk-forward 运行后补上了这层，机制在
`services/quant-engine/src/quant_engine/stock_ml/config_integrity.py`。

每次 `stock_walkforward` 运行会在 `data/factors.db` 留下四个值：

| 字段 | 含义 | 能回答的问题 |
|---|---|---|
| `config_sha256` | **实际生效配置**的规范化哈希 | 两次运行的配置是否完全相同（相同哈希 == 完全相同） |
| `config_yaml_sha256` | 当次读入的 **YAML 文件**哈希 | 与上一个不同 → YAML 被代码/参数覆盖过，或改了 yaml 没提交 |
| `git_commit` | 部署时的 HEAD 提交号 | 这次跑的是哪个代码版本 |
| `git_dirty` | 部署时工作区是否有未提交改动 | `True` 意味着代码不可复现，结果只作参考 |

同一个哈希只保留一条档案（表 `config_fingerprint`，含**原始 YAML 文本**与完整生效配置），
所以"这个配置被哪些运行用过"是一次索引查询。配置本体同时按内容寻址归档到
`artifacts/config-snapshots/<sha256>/`（`config.json` + `config.yaml` + `meta.json`），
哈希相同不重复写。

### 查询入口

```bash
# 列出运行过的配置指纹（时间倒序）
curl -s 'http://127.0.0.1:9102/api/v1/stock-ml/config/fingerprints?limit=20'

# 按哈希取回完整存档（生效配置 + 原始 YAML + 代码版本）
curl -s 'http://127.0.0.1:9102/api/v1/stock-ml/config/fingerprint?sha256=<64位>'

# 运行列表里每个 run 也带 fingerprint 字段
curl -s 'http://127.0.0.1:9102/api/v1/stock-ml/walkforward/list?limit=10'
```

任务结果里也直接回显 `fingerprint`，不必等落库后再查；dryRun 同样回显。

### 代码版本从哪来（为什么不调 git）

服务进程**不允许 shell out**（架构约定，`test_scripts_never_shell_out` 会红）。
所以版本信息由 `deploy/build-info.sh` 在**部署/启动时**写成 `.build-info.json`，
运行时只读文件。优先级：环境变量 `QUANT_BUILD_COMMIT` / `QUANT_BUILD_DIRTY`
> `.build-info.json` > 未知（`None`）。

`build-info.sh` 由 `install.sh` 与 `screen-up.sh` 自动调用，所以 **`git pull` 后
重跑 `screen-up.sh` 就会刷新版本信息**。

> **未知就是未知**：拿不到版本时这两列是 `None`（不是空串、也不当 clean）。
> 指纹机制上线前的历史运行同样为 `None`，语义是"该次运行早于指纹机制"，
> 不要误读成"没有配置"——那批运行的配置**内容仍完整保存在 `config` 列里**。


## 仍存的一处重复（已知，未收敛）

`apps/quant-web/.env` 的 `VITE_QUANT_API_KEY` 是管理 key 的**第二份副本**。Vite 只按固定文件名
（`.env` / `.env.local` …）加载环境变量，不会读 `.quant.env`，所以要么在 `vite.config.ts` 里自己解析
`.quant.env` 后 `define` 注入（改动会影响前端构建，且需要重启 Vite 验证），要么保留这份副本。
**当前选择保留**：文档把"轮换要改两处"写明，避免出现"改了服务没改前端"的静默 401。

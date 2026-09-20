# Quantification Harness

A 股量化研究与决策平台：Python 独占数据/计算平面，Node/DSH 只保留 Agent 编排，
React 控制台作为唯一的后台管理界面。

核心链路是**股票 walk-forward 机器学习排序选股**（LightGBM `lambdarank` + 3 种子集成），
外加 ETF 量化、技术择时回测等已收敛的研究模块。

> **数据不入库。** `data/` 下是 9 GB 级的 SQLite（`market.db` / `finance.db` / `factors.db` …），
> 由 `quant-sync` 从上游同步、`quant-engine` 派生写入，全部被 `.gitignore` 排除。
> 拿到仓库后需要自备数据，或按 `docs/operations.md` 回填。同理 `artifacts/`（模型与回测产物）不入库。

---

## 1. 架构

```
                    ┌──────────────────────────────┐
   上游 Tushare ───► │ quant-sync      :9101        │  数据平面：拉取 / 归一化 / 落库
                    │ (Python, screen: quant-sync) │  写 market.db / finance.db / meta.db / sync.db
                    └──────────────┬───────────────┘
                                   │ 只读
                    ┌──────────────▼───────────────┐
                    │ quant-engine    :9102        │  计算平面：因子 / 训练 / 回测 / 推理
                    │ (Python, screen: quant-engine)│  写 factors.db / backtest.db / artifacts/
                    └──────────────┬───────────────┘
                                   │ 只读
                    ┌──────────────▼───────────────┐
                    │ data-query      :9103        │  只读查询平面（mode=ro + query_only）
                    │ (Python, screen: data-query) │  无鉴权，只暴露读端点
                    └──────────────┬───────────────┘
                                   │ HTTP
                    ┌──────────────▼───────────────┐
                    │ quant-web       :3082        │  后台控制台（React + Vite）
                    │ (Vite dev, 本机运行)          │  直连 9101/9102/9103
                    └──────────────────────────────┘
```

| 端口 | 服务 | 语言 | 会话/启动 |
|---|---|---|---|
| 9101 | quant-sync | Python 3.12 | `screen: quant-sync` |
| 9102 | quant-engine | Python 3.12 | `screen: quant-engine` |
| 9103 | data-query | Python 3.12 | `screen: data-query` |
| 3082 | quant-web | React + TS | `npm run dev`（本机） |
| 3081 | DSH 宿主（Agent 编排，可选） | Node 22 | `npm run web` |

数据所有权是硬边界：**Python 独占写库，Node 零写入**。三个 Python 服务之间也按
「raw ← sync / derived ← engine / read-only ← data-query」划线，详见 `ARCHITECTURE.md`。

## 2. 目录结构

```
.
├── services/                    # 三个 Python 服务（各自独立可编辑安装）
│   ├── quant-sync/              #   数据平面          :9101
│   ├── quant-engine/            #   计算平面          :9102
│   └── data-query-service/      #   只读查询          :9103
├── apps/quant-web/              # React 控制台        :3082
├── contracts/                   # OpenAPI 契约 + meta.sql DDL（CI 做漂移检查）
├── config/                      # 研究参数（面 4，见 §5）
├── scripts/                     # 同步 / 回填 / 每日链 / 研究分析工具
├── deploy/                      # Linux 服务器部署资产（screen 启停、安装、环境）
├── docs/                        # 架构与运维文档
├── data/                        # SQLite 数据库        （不入库）
├── artifacts/                   # 模型与回测产物        （不入库）
└── .quant.env                   # 密钥                 （不入库，权限 600）
```

## 3. 环境要求与安装

| 组件 | 版本（已在生产实测） |
|---|---|
| Python | 3.12（CI 用 3.13；`requires-python >= 3.11`） |
| Node | 22（CI）/ 18.19（服务器实测可跑前端构建与测试） |
| 训练硬件 | GPU 可选（NVIDIA A100 已验证 CUDA 版 LightGBM） |

### 三个 Python 服务

每个服务都是独立的可编辑安装包：

```bash
cd services/quant-engine          # 或 quant-sync / data-query-service
python -m pip install -e ".[dev]"
```

### 前端

```bash
cd apps/quant-web
npm ci                            # 不要用 npm install，锁文件是 CI 的缓存键
npm run dev                       # http://127.0.0.1:3082
```

前端通过 Vite dev server 代理访问后端，因此浏览器是**同源请求，不涉及 CORS**。
代理目标读 `VITE_API_HOST`（优先级：`.env` > 进程环境变量 > 缺省 `127.0.0.1`）：

```bash
cd apps/quant-web
echo 'VITE_API_HOST=192.168.1.89' >> .env    # 指向远端后端；改完必须重启 Vite
```

### 启动服务

Windows（开发机）：`scripts/run_quant_sync.ps1` / `scripts/start-engine.ps1` / `scripts/run_data_query.ps1`。

Linux（生产，screen 托管，**不装 systemd**）：

```bash
cd /opt/ai_project/quantification-harness
bash deploy/install.sh              # 幂等：建环境 → 可编辑安装 → 自检 → 重启 → 健康检查
bash deploy/screen-up.sh            # 启动全部（已在跑则跳过）；带参数则只起一个：screen-up.sh engine
bash deploy/screen-down.sh          # 停止（先 SIGTERM 再退 screen）
bash deploy/screen-status.sh --logs # 会话 + 端口 + 健康检查 + 日志尾部
```

完整部署说明（目标环境、内存约束、CUDA 版 LightGBM 的编译与取舍、排错表）见
**`deploy/README.md`**。

## 4. 测试

全部是标准库 `unittest`，**不需要 pytest**（CI 与本地跑的是同一条命令，避免"CI 命令没人跑过"）。

```bash
# 单个服务
cd services/quant-engine
PYTHONPATH=src python -m unittest discover -s tests -t tests -p "test_*.py"

# 三个服务一起（Windows）
pwsh -File scripts/run_tests.ps1
```

实测基线：**quant-engine 153 / quant-sync 61 / data-query 15 全部通过**。

```bash
# 前端：类型检查 + 单测（node:test + tsx，无额外依赖）
cd apps/quant-web
npx tsc -b && npm test            # 49 passed

# 契约漂移检查（stdlib，无需安装依赖）
python scripts/check_contracts.py --strict
```

CI（`.github/workflows/ci.yml`）就是这三件事：契约漂移、三个服务的 unittest 矩阵、前端 typecheck + 测试。

## 5. 配置面

配置分 4 层，越靠后越接近运行时。绝大多数配置事故都源于"同一个事实在两处各写一份，
改了其中一个以为生效了"。**权威说明见 `docs/configuration.md`**。

| # | 配置面 | 位置 | 生效时机 |
|---|---|---|---|
| 1 | 密钥 / 凭据 | `.quant.env`（不入库） | 重启进程 |
| 2 | 服务启动默认值 | `services/*/src/*/settings.py` + `QUANT_*` 环境变量 | 重启进程 |
| 3 | 运行时可改配置 | `data/sync-config.json` | 立即 / 下一段同步 |
| 4 | 研究参数 | `config/*.yaml` | 下一次 job |

### 研究参数（面 4）

`config/stock-ml.yaml` 是**股票 ML 链路的权威配置**，改数字会影响所有回测口径。
`services/quant-engine/tests/test_stock_ml_config.py` 断言了其中的关键取值——
**那些用例就是固化配置的锁**，改 yaml 必须同步改用例，否则 CI 会红。

### ⚠️ `deviceType` 的平台差异

`config/stock-ml.yaml` 原有一个**仅 Linux 生效**的分支：`deviceType: cuda` 时会退化成
「pickle 落盘 + spawn 子进程」的隔离路径，同一份训练帧在内存里存在两份（全量历史约 19 GB → 近 40 GB），
在共享机器上会 OOM。

现已通过 `model.cudaInProcess: true` 收敛掉这个分歧：**CUDA 在进程内训练**，
不落盘、不 spawn、不复制，于是 Windows 与 Linux 可以共用同一份配置（`cuda` + `cudaInProcess`）。
切换开关在 `stock_ml/__init__.py` 的 `use_isolated_cuda` 判断里。

> 历史记录中的"服务器必须 `deviceType: cpu`"是 `cudaInProcess` 之前的结论，已过时。

## 6. 股票 ML 链路（核心）

### 运行方式

HTTP（推荐，可从前端提交）：

```bash
curl -s -X POST http://127.0.0.1:9102/api/v1/jobs \
  -H 'content-type: application/json' -H "x-api-key: $QUANT_API_KEY" \
  -d '{"type":"stock_walkforward","parameters":{"walkforward":{"dryRun":true,"maxWindows":2}}}'
curl -s http://127.0.0.1:9102/api/v1/jobs/<job-id>      # 查进度
```

命令行：

```bash
python -c '
from pathlib import Path
import yaml
from quant_engine.stock_ml import run_walkforward
root = Path.cwd()
cfg = yaml.safe_load((root / "config/stock-ml.yaml").read_text(encoding="utf-8"))
run_walkforward(root/"data"/"factors.db", root/"data"/"market.db",
                root/"artifacts/stock-ml", cfg,
                progress=lambda p: print(f"PROGRESS {p:.1f}", flush=True))
'
```

> 直接调用 `run_walkforward` 时取消回调是默认的 `lambda: False`，**没有取消点**。
> 想中途可停就走 HTTP job（`POST /api/v1/jobs/<id>/cancel`）。

### 当前发布基线

已发布运行 `stock-wf-20260919T100135Z`：

| 指标 | 值 |
|---|---|
| Sharpe | 1.149 |
| 年化收益 | 19.78% |
| 累计收益 | 104.69% |
| 最大回撤 | −14.69% |
| 换手 | 26.18 |
| rankIC | 0.0404 |
| positiveWindowRate | 52% |
| meanTrees | 9.7 |
| 特征数 / topN | 31 / 5 |
| 标签 / 集成 | `forward_3` / 3 种子 `[42,123,456]` `median_zscore` |

**这是"发布基线"，不是"研究最优解"**——它的作用是稳定可复现的生产起点。

### 读结果的工具

```bash
python scripts/analyze_stock_wf_run.py        # 单次运行的窗口/持仓/归因分析
python scripts/compare_stock_wf_runs.py       # 两次运行逐窗口对比
python scripts/decompose_position_sizing.py   # 仓位与换手拆解
```

三个脚本都支持 `QUANT_ROOT` 环境变量指定项目根。

### ⚠️ 这些方向已经试过并被证伪，不要再重试

完整研究记录见 §8（**研究记录文档不入库**，在部署机的 `artifacts/stock-ml/` 下）。
结论摘要（含 7 条方法论教训）：

- 放松 early stopping、切换到 `forward_20`
- 特征削减 31 → 17（中性）、新增 `volume_price_div`（中性）
- 强制 `minHoldingDays`（**有害**：Sharpe 1.129 → 0.273）
- 靠延迟卖出来降换手（只影响 2.25% 卖单名义额）
- 指望 GPU 提速（本工作负载只有 1.4–2×）
- **用单次运行做版本决策**（同配置重跑 Sharpe 波动 ±0.06，必须配对实验）

## 7. 同源约束（改指标前必读）

发布一个 walk-forward 版本时，`publish_walkforward` 会做 4 项检查，其中最关键的是
**同源不变量**：指标与信号必须来自同一次运行。

> 该版本没有同源候选股，禁止只切换指标而不切换信号。

也就是说**不能**把 A 运行的 Sharpe 配上 B 运行的持仓。另有 3 项：结果版本过低（`result_version < 2`）、
`publishGate` 未通过、辅助因子（`industry_*` / `money_flow_*`）不新鲜。
`publishGate` 定义在 `config/stock-ml.yaml`，按运行的**冻结配置**求值。

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| `ARCHITECTURE.md` | 架构与数据所有权、端口、v1 已退役清单 |
| `docs/architecture-v2.md` | 完整架构蓝图与迁移记录 |
| `docs/configuration.md` | 4 个配置面、不变量、key 轮换姿势 |
| `docs/operations.md` | 日常运维 |
| `docs/testing.md` | 测试约定 |
| `deploy/README.md` | Linux 服务器部署（screen、内存、CUDA 编译、排错） |
| `docs/technical_timing_v1_baseline.md` | 技术择时基线 |
| `docs/etf-quant-mvp.md` / `docs/instrument-abstraction.md` | ETF 量化与标的抽象 |
| `artifacts/stock-ml/stock-ml-research-notes-*.md` | 股票 ML 研究记录与结论收口（**不在库内，见下**） |
| `services/*/README.md` | 各服务的边界与本地启动 |

> `artifacts/` 不入库，研究记录文档因此不在克隆里。需要时从部署机拷贝，
> 或在有数据的环境重新生成。

## 9. 许可与状态

私有研究项目，未附许可。构建状态见 `.github/workflows/ci.yml`。

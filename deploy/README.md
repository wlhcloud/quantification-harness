# Linux 服务器部署说明（192.168.1.89）

Python 数据/计算平面部署在服务器上训练模型，**前端仍在本机**运行，通过 Vite dev server
的代理指向服务器端口。服务用 **screen** 后台托管（**不装 systemd**）。

## 1. 目标环境（实测）

| 项 | 值 |
|---|---|
| 主机 | `192.168.1.89`（`ssh root@192.168.1.89`，已配置免密 `~/.ssh/id_ed25519_89`） |
| 系统 | Ubuntu 24.04.4 LTS / 内核 6.8.0 |
| CPU / 内存 | 32 核 / 62 GB（**共享机器**，见 §7） |
| GPU | NVIDIA A100-SXM4-80GB（驱动 595.84） |
| conda | `/root/miniconda3`（26.3.2） |
| 部署根目录 | `/opt/ai_project/quantification-harness` |
| conda 环境 | `quant`（Python 3.12.14） |
| 进程托管 | `screen` 4.09（会话名 = 服务名） |

## 2. 目录布局

```
/opt/ai_project/quantification-harness/
├── .git/                # 2026-09-20 起是个正常 git 仓库，origin -> GitHub（§4）
├── services/            # 三个 Python 服务源码（可编辑安装进 quant 环境）
│   ├── quant-sync/          # 数据平面    :9101
│   ├── quant-engine/        # 计算平面    :9102（训练 / 回测 / 因子）
│   └── data-query-service/  # 只读查询    :9103
├── apps/quant-web/      # React 控制台源码（本机跑 dev server）
├── config/              # 研究参数（stock-ml.yaml / etf-quant.yaml / ...）
├── contracts/           # OpenAPI 契约 + meta.sql DDL
├── scripts/             # 研究/回填/每日链脚本
├── artifacts/           # 模型与回测产物（stock-ml / etf）—— 不入库
├── data/                # SQLite 数据库（见下）—— 不入库
├── docs/                # 架构与运维文档
├── logs/                # screen 服务的输出日志 —— 不入库
├── deploy/              # 本次部署资产（本目录）
└── .quant.env           # 密钥（ASCII + CRLF，权限 600）—— 不入库
```

### 数据文件

| 文件 | 用途 | 写入方 |
|---|---|---|
| `data/market.db` (3.6G) | 日线/指数等原始行情 | quant-sync |
| `data/finance.db` (1.2G) | 财报与估值 | quant-sync |
| `data/minute.db` | 分钟线（当前业务上停用） | quant-sync |
| `data/meta.db` | 数据源/字典/同步账本 | quant-sync |
| `data/sync.db` | 同步运行账本 | quant-sync |
| `data/factors.db` (4.5G) | 派生因子 | quant-engine |
| `data/backtest.db` | 回测结果 | quant-engine |
| `data/etf-quant.db` | ETF 量化 | quant-engine |
| `data/quant-engine.db` | job 元数据 | quant-engine |
| `data/sync-config.json` | 运行时可改配置（面 3） | quant-sync API |
| `artifacts/` | 训练出的模型、walk-forward 结果 | quant-engine |

> **没有传过来**：`data/backups/`（6 GB 历史快照）、`data/chrome-headless-quant/`
> （Chrome 调试用户目录）、本机的 `*.log`。
> `data/start-910{1,2,3}.bat` 传了（`test_script_layout.py` 断言它们存在），但它们是
> **Windows 专用的启动器，在 Linux 上不使用**，只作历史参照。

## 3. 服务与端口

| 服务 | 端口 | 绑定 | screen 会话 | 日志文件 |
|---|---|---|---|---|
| quant-sync | 9101 | `0.0.0.0` | `quant-sync` | `logs/quant-sync.log` |
| quant-engine | 9102 | `0.0.0.0` | `quant-engine` | `logs/quant-engine.log` |
| data-query | 9103 | `0.0.0.0` | `data-query` | `logs/data-query.log` |

绑定 `0.0.0.0` 是为了让**本机前端**能连。原 Windows 侧脚本刻意绑 `127.0.0.1`，
如果服务器暴露在不可信网络，请改用防火墙白名单（见 §8）。

### 启停与查看

```bash
cd /opt/ai_project/quantification-harness

bash deploy/screen-up.sh                 # 启动全部（已在运行则跳过）
bash deploy/screen-up.sh engine          # 只启动 quant-engine
bash deploy/screen-down.sh               # 停止全部（先 SIGTERM 再退 screen）
bash deploy/screen-down.sh engine        # 只停 quant-engine
bash deploy/screen-status.sh             # 会话 + 端口 + 健康检查
bash deploy/screen-status.sh --logs      # 附带各服务日志尾部

screen -r quant-engine                   # 进入会话看实时输出（Ctrl-A D 脱离）
tail -f logs/quant-engine.log            # 或者直接看日志文件
curl -s http://127.0.0.1:9102/api/v1/health
```

> **两个 screen 方案已知代价**（相比 systemd）：
> 1. **重启机器后不会自动恢复**，需要重新执行 `bash deploy/screen-up.sh`。
> 2. **进程被 OOM killer 杀掉后不会自动拉起**，screen 会话会一起消失。
>    实际踩到过：训练 job 把 quant-engine 撑爆内存被杀，服务不会自己回来（见 §7）。
>    需要自愈能力时，再补一个 `@reboot` crontab 项 + 一个定时巡检脚本，或改用 systemd。

### 手工前台启动（排错用）

```bash
/opt/ai_project/quantification-harness/deploy/run-service.sh engine
```

## 4. 安装 / 更新

一键脚本幂等，可反复执行；它最后会用 screen 重启服务：

```bash
bash /opt/ai_project/quantification-harness/deploy/install.sh
```

它依次：确保 conda 环境存在 → 可编辑安装三个服务及其依赖 → 导入自检（含
`project_root` 解析与 `QUANT_API_KEY` 是否加载）→ LightGBM 自检 → screen 重启 →
健康检查。

镜像与路径可用环境变量覆盖：

| 变量 | 默认 |
|---|---|
| `QUANT_CONDA_ENV` | `quant` |
| `QUANT_CONDA_PREFIX` | `/root/miniconda3/envs/quant` |
| `QUANT_PIP_INDEX` | `https://pypi.tuna.tsinghua.edu.cn/simple`（置空回退官方源） |
| `QUANT_PROJECT_ROOT` | `deploy/` 的上一级 |

只更新代码（不动环境、不重装依赖）：

```bash
cd /opt/ai_project/quantification-harness
bash deploy/screen-down.sh && bash deploy/screen-up.sh
```

Python 源码改动无需重新安装（可编辑安装）。**改了 `pyproject.toml` 依赖**才需要重跑
`install.sh`。

### 用 git 同步代码（2026-09-20 起）

服务器上的部署目录现在是**一个正常的 git 仓库**，`origin` 指向 GitHub，可以直接
`git pull` 更新代码，不必再手工 rsync/覆盖。

```bash
cd /opt/ai_project/quantification-harness

git pull --ff-only          # 更新代码（工作区应保持干净）
git log --oneline -3        # 看拉到哪个提交
git status --porcelain      # 应为空；非空说明有人在本机改过代码

# 改完重启对应服务（Python 是可编辑安装，但进程需要重启才会加载新代码）
bash deploy/screen-down.sh engine && bash deploy/screen-up.sh engine
bash deploy/screen-status.sh          # 会话 + 端口 + 健康检查
```

| 项 | 值 |
|---|---|
| remote | `git@github.com:wlhcloud/quantification-harness.git`（SSH，不是 HTTPS） |
| 凭据 | `~/.ssh/config` 里 `Host github.com` → `IdentityFile ~/.ssh/github_deploy` |
| 权限 | **只读**：`git fetch` / `git pull` 可用；`git push` 会被拒（见下） |
| `core.fileMode` | `false`（忽略权限位差异，与 Windows 开发机一致） |

**关于推送**：`github_deploy` 是只读凭据，`git push` 会报
`ERROR: Permission to ... denied to deploy key`。生产机只拉取不推送是更安全的默认，
建议保持。确实需要从服务器推送时才去开启写权限：

> GitHub → 仓库 → **Settings → Deploy keys** → 对应 key → 勾选
> **Allow write access**。

**注意两个"改了会出问题"的点**：

- **行尾**：`.gitattributes` 强制 `data/start-910*.bat` 与 `.quant.env` 检出为 CRLF
  （cmd 的 `for /f` 解析密钥文件的要求）。别在服务器上用编辑器保存这些文件——
  保存成 LF 会让 `QUANT_API_KEY` 被静默漏读，服务照常启动但写接口鉴权失效。
- **执行位**：`deploy/*.sh` 在仓库里记的是 `100755`，clone/pull 即带执行位。
  如果哪天又出现 `deploy/run-service.sh: Permission denied`（9102 起不来），
  先确认 `git ls-files -s deploy/ | grep 755` 正常，再 `chmod +x deploy/*.sh` 应急。

> ✅ **2026-09-20 起，仓库与服务器共用同一份 `config/stock-ml.yaml`**：
> `deviceType: cuda` + `cudaInProcess: true`。此前服务器必须手工改成 `cpu` 才能避开
> 「隔离进程」路径的双份内存（见 §7.1）；`cudaInProcess` 落地后这个分歧已消除，
> 重跑 `install.sh` / 同步仓库不再需要事后改配置。

## 5. 已安装的关键包（实测）

| 包 | 版本 |
|---|---|
| lightgbm | 4.7.0（**源码编译的 CUDA 版**，见 §7.5；配 `cudaInProcess: true` 进程内训练） |
| numpy | 2.5.3 |
| pandas | 2.3.3 |
| pyarrow | 20.0.0 |
| scikit-learn | 1.9.1 |
| scipy | 1.18.1 |
| fastapi / uvicorn | 0.141.1 / 0.53.0 |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 |
| cryptography | 49.0.0 |
| pytest / httpx / ruff | 9.1.1 / 0.28.1 / 0.16.8 |

全部满足各 `pyproject.toml` 的版本约束。测试套件在 Python 3.12 下全绿（2026-09-20 复核）：
**quant-engine 153 / quant-sync 61 / data-query 15 = 229 passed，0 failed**
（`OK (skipped=1)`：无 `.quant.env` 时跳过密钥文件检查）。

## 6. 配置面

沿用仓库既有的四层配置（详见 `docs/configuration.md`）：

1. **密钥** — `.quant.env`（项目根，ASCII + CRLF，已 `chmod 600`）
   - `QUANT_API_KEY` — 管理 key 的**唯一权威变量**，9101/9102 写接口鉴权都用它
   - `QUANT_SYNC_CREDENTIAL_KEY` — 上游 token 加密密钥，**换了已加密 token 解不开**
   - `DSH_QUANT_TUSHARE_*` — 上游 token
2. **服务启动默认值** — `services/*/src/*/settings.py` + `QUANT_*` 环境变量
3. **运行时可改配置** — `data/sync-config.json`
4. **研究参数** — `config/*.yaml`

`deploy/env.sh` 通过 `sed 's/\r$//'` 加载 `.quant.env`，因此 Windows 生成的 CRLF 文件
在 Linux 上可直接用，同时**不破坏**仓库侧“必须 CRLF”的不变量
（`test_script_layout.py::StartupScriptSafetyTest`）。

`project_root` 由 `settings.py` 的 `__file__` 推导（`parents[4]`），可编辑安装下正好
指向部署根；`deploy/env.sh` 另外显式导出了
`QUANT_SYNC_PROJECT_ROOT` / `QUANT_ENGINE_PROJECT_ROOT` / `DATA_QUERY_PROJECT_ROOT`
作为双保险。

### 服务器与仓库的配置已收敛（2026-09-20）

| 文件 | 仓库（Windows） | 服务器 | 说明 |
|---|---|---|---|
| `config/stock-ml.yaml` → `model.deviceType` | `cuda` | **`cuda`** | 已一致 |
| `config/stock-ml.yaml` → `model.cudaInProcess` | `true` | **`true`** | 已一致 |

历史上这里有一行差异（服务器 `cpu` / 仓库 `cuda`），原因是 Linux 上 `deviceType: cuda`
会走「pickle 落盘 + spawn 子进程」的隔离路径、内存翻倍。该问题已由
`model.cudaInProcess: true` 从根上解决（见 §7.1），两处配置因此收敛为同一份。

## 7. 训练模型

### 7.1 隔离进程路径与 `cudaInProcess`（已解决，勿再改回）

`stock_ml/__init__.py` 里有一条仅在 Linux 生效的历史分支：

```python
use_isolated_cuda = (sys.platform.startswith("linux") and
                     str(model_cfg.get("deviceType", "cpu")).lower() in {"cuda", "gpu"} and
                     not bool(model_cfg.get("cudaInProcess", False)))
```

`deviceType: cuda` **且** `cudaInProcess: false` 时它会：
1. 把整个训练帧 `pickle.dump` 落盘（全量历史实测约 **2.2 GB**，缩量测试约 960 MB）；
2. `multiprocessing spawn` 一个子进程；
3. 子进程再把这份帧 `pickle.load` 回内存 → **同一份数据在内存里存在两份**；
4. 然后在子进程里训练。

第 3 步是致命的：帧本身约 19 GB，加上子进程的副本接近 40 GB，在共享服务器上直接
触发 OOM（见 §7.3）。而且这个隔离路径**本身很慢**：2.4 GB pickle 往返 + spawn +
帧转换的开销成了瓶颈，掩盖了 GPU 的真实速度。

**2026-09-20 起改用 `model.cudaInProcess: true`**：CUDA 走
`etf_ranker.train_ranker(...)` **进程内训练**——不落盘、不 spawn、不复制，
峰值内存只有一份帧，且真正在 GPU 上算。实测（150 棵 / 41 万行）**4.3 s vs CPU 104 s**。

因此：
- 服务器与仓库共用 `deviceType: cuda` + `cudaInProcess: true`，不再需要每台机器单独改配置；
- §7.5 里"CUDA 比 CPU 慢"的结论**测的是隔离路径**，已被进程内路径推翻，仅作历史记录保留；
- `cudaInProcess` 是要害开关：**如果它被改回 `false`，Linux 上会立刻退回双份内存 + 慢路径**。
  `stock_ml/__init__.py` 的 `engineVersion` 里有 `cudaInProcessSwitch: True` 标记，
  配置回显（`/api/v1/stock-ml/config`）也会回报 `deviceType` 与 `cudaInProcess` 便于核对。

> 单一事实来源提醒：`cudaInProcess` 与 `deviceType` 属于 §6 的「研究参数（面 4）」，
> 由 `config/*.yaml` 决定，不要在代码里再放一份会悄悄生效的默认值。


### 7.2 实测训练结果（2026-09-18）

`stock_walkforward` + `{"walkforward":{"dryRun":true,"maxWindows":2}}`：

| 指标 | 值 |
|---|---|
| 状态 | **complete** |
| runId | `stock-wf-20260918T014810Z` |
| 窗口 | 3（20260320 .. 20260910） |
| 耗时 | **625 s**（本机 Windows 同参数基准 1170 s，快 ~1.9x） |
| 峰值 engine RSS | **19.7 GB** |
| 最低系统可用内存 | 20.3 GB（未触及护栏） |
| effectiveDevice | `cpu`（3 个窗口全部） |
| 集成 | 3 种子 `[42,123,456]` / `median_zscore` |
| 特征数 | 31 |
| 收益 / 超额 | +7.33% / +7.73% |
| Sharpe / 最大回撤 | 0.967 / −6.42% |
| avgRankIc | 0.0177 |
| configWarnings | 无 |

产物：`artifacts/stock-ml/stock-wf-model.txt`（+ `_seed42/123/456.txt`）、
`stock-wf-window-stats.json`、`stock-wf-daily-nav.json`、
`stock-wf-model_ensemble_seeds.json`。

按此推算，完整 24 窗口（`dryRun: false`）约 45–55 分钟，峰值内存与窗口数**无关**
（逐窗口释放，代码里有 `del` + `_release_native_memory()`）。

### 7.3 内存约束（重要）

- **单次训练峰值 ≈ 20 GB**，与窗口数无关；瓶颈是 `factors.db` 的因子帧一次性载入内存。
- 服务器是**共享机器**：基线常驻约 22–25 GB（多个 Tomcat 合计约 16 GB、postgres、
  ollama/llama-server、chsr-platform 等），且随时可能有别人的新任务起来。
- 因此**同时只跑一个训练 job**（`QUANT_ENGINE_MAX_WORKERS` 默认 2，
  两个 walkforward 同时跑就是 40 GB → 必 OOM。共享期建议设为 1）。
- 训练 job 把 engine 撑爆时，OOM killer 杀的是**整个 quant-engine 进程**，不是单个 job；
  服务需手工 `screen-up.sh` 拉起。

#### engine 跑完训练不会自动还内存

LightGBM/Python 释放的页不会归还操作系统。实测：跑完一次 walkforward 后
**engine 空闲持有 3.3 GB**（RSS 3272 MB）。回收方式就是重启（无 job 时零风险）：

```bash
cd /opt/ai_project/quantification-harness
bash deploy/screen-down.sh engine && bash deploy/screen-up.sh engine
# 实测：RSS 3272 MB → 139 MB，系统 available 37.1 GB → 40.2 GB
```

训练前后对比内存、或准备跑大 job 之前，先做这一步。

#### swap（2026-09-18 已扩容）

原 swap 只有 8 GB 且**长期 100% 用满**——意味着任何内存尖峰都直接 OOM，
没有缓冲垫（2026-09-18 那次训练被杀就是如此）。

现已新增 `/swap2.img`（32 GB，`fallocate` + `mkswap` + `swapon`，已写入 `/etc/fstab` 持久化）：

```
NAME       TYPE SIZE USED PRIO
/swap.img  file   8G   8G   -2
/swap2.img file  32G   0B   -3
```

现状：`Swap: 39Gi total, 8.0Gi used, 32Gi free`。
`/etc/fstab` 改动前已备份为 `/etc/fstab.bak-<时间戳>`。

回滚：`swapoff /swap2.img`，删掉 `/etc/fstab` 里那一行，再 `rm /swap2.img`。

> 注意 swap 只是防 OOM 的保险，**不能当性能手段**：真要开始大量换页，训练会变得极慢。
> `vm.swappiness` 保持系统默认 60（未改），因为这台机器上还有别人的 java 服务。


### 7.4 跑训练

HTTP 方式（推荐，可在本机前端提交）：

```bash
cd /opt/ai_project/quantification-harness
set -a; source <(sed 's/\r$//' .quant.env); set +a
curl -s -X POST http://127.0.0.1:9102/api/v1/jobs \
  -H 'content-type: application/json' -H "x-api-key: $QUANT_API_KEY" \
  -d '{"type":"stock_walkforward","parameters":{"walkforward":{"dryRun":true,"maxWindows":2}}}'
curl -s http://127.0.0.1:9102/api/v1/jobs/<job-id>     # 看进度
```

命令行方式（与仓库既有 WSL 脚本同口径）：

```bash
cd /opt/ai_project/quantification-harness
/root/miniconda3/envs/quant/bin/python -c '
from pathlib import Path
import yaml
from quant_engine.stock_ml import run_walkforward
root = Path.cwd()
cfg = yaml.safe_load((root / "config/stock-ml.yaml").read_text(encoding="utf-8"))
print(run_walkforward(root / "data" / "factors.db", root / "data" / "market.db",
                      root / "artifacts/stock-ml", cfg,
                      progress=lambda p: print(f"PROGRESS {p:.1f}", flush=True)))
'
```

> 注意：直接跑 `run_walkforward` 时 `cancelled` 用默认的 `lambda: False`，没有任何取消点。
> 想中途能停，就走 HTTP job（`POST /api/v1/jobs/<id>/cancel`）。

### 7.5 CUDA 版 LightGBM（第二步：已编译成功，但**结论是不切回 cuda**）

#### 已做的事

CUDA 版 LightGBM **已经编译并安装好了**（2026-09-18），`USE_CUDA=ON`、只编 A100 的 `sm_80`：

| 项 | 值 |
|---|---|
| 编译器 | `/usr/bin/gcc-12` + `g++-12`（**必须**，CUDA 12.0 不支持系统的 gcc 13） |
| CUDA | 系统 `nvidia-cuda-toolkit` 12.0，头文件在 `/usr/include`，`libcudart` 在 `/usr/lib/x86_64-linux-gnu` |
| 额外依赖 | `libnccl2` / `libnccl-dev` 2.18.5（`find_package(NCCL REQUIRED)`，4.7.0 无 `USE_NCCL` 开关） |
| 构建工具 | `cmake 4.4.3` + `ninja`（pip 装进 conda 环境，未动系统包） |
| 耗时 | **148 s** |
| 产物 | `/root/lgbm-cuda-build/wheels/lightgbm-4.7.0-py3-none-linux_x86_64.whl`（226 MB） |
| 回滚 | `/root/lgbm-cuda-build/rollback-cpu/` 里的官方 CPU wheel |

复现命令：

```bash
apt-get install -y libnccl2 libnccl-dev
/root/miniconda3/envs/quant/bin/python -m pip install --upgrade cmake ninja
CC=/usr/bin/gcc-12 CXX=/usr/bin/g++-12 CUDAHOSTCXX=/usr/bin/g++-12 \
CMAKE_BUILD_PARALLEL_LEVEL=6 \
/root/miniconda3/envs/quant/bin/python -m pip install \
  --no-binary lightgbm --no-deps --no-cache-dir \
  --config-settings=cmake.define.USE_CUDA=ON \
  --config-settings=cmake.define.CMAKE_CUDA_ARCHITECTURES=80 \
  lightgbm==4.7.0
```

回滚成 CPU 版：`pip install --force-reinstall --no-deps /root/lgbm-cuda-build/rollback-cpu/lightgbm-*.whl`

#### CUDA 确实生效（已验证）

- `lib_lightgbm.so` 299.7 MB，含 **1516 个 CUDA 符号**、`"Using CUDA"` 字符串
- 训练时 GPU 利用率 **max 57% / mean 29.5%**，显存 +614 MB；同一测试的 CPU 阶段 GPU 利用率 **0%**
- LightGBM 自身日志打出只有 CUDA 路径才会有的行：
  `Using sparse features with CUDA is currently not supported.` /
  `Metric ndcg is not implemented in cuda version. Fall back to evaluation on CPU.`

#### 但实测**比 CPU 慢**，所以不切回

同一个 `stock_walkforward`、同一份真实数据、同一参数，只切 `deviceType`：

| deviceType | 峰值内存（引擎） | 峰值内存（spawn 子进程） | 合计 | 磁盘 pickle 峰值 | 耗时 |
|---|---|---|---|---|---|
| `cpu` | 8300 MB | 13 MB | **8313 MB** | 0 | **203 s** |
| `cuda` | 7931 MB | **6251 MB** | **14182 MB** | **960 MB** | **361 s** |

- **CUDA 慢 1.78x**（361s vs 203s）
- **CUDA 内存 1.71x**（14.2G vs 8.3G），每个窗口还多写 ~1 GB pickle 到磁盘
- 合成数据基准也一致：100 万行 0.86x、200 万行 0.94x —— 随规模增大缓慢收敛，但在本工作负载范围内 32 核 CPU 一直更快

放大到全量历史（引擎侧约 19–20 GB）后，CUDA 路径的总需求约 **34–39 GB**，
正是 2026-09-18 那次 OOM 的原因（当时可用内存只剩 ~11 GB）。

**历史结论（2026-09-18）：当时服务器保持 `deviceType: cpu`。**
该结论测的是「隔离进程路径 vs 进程内 CPU」，而隔离路径的开销（pickle 往返 + spawn）
才是真凶 —— 见 §7.1。2026-09-20 起改为 `cuda` + `cudaInProcess: true`（进程内 CUDA），
上述表格仅作为"隔离路径有多贵"的实测证据保留。
CUDA 版装好留着，等数据量大幅增长后再评估；真要切回时，务必先确认共享机器有 ≥40 GB 可用内存。

> `cpuFallback: true` 依旧有效：即使把 `deviceType` 写成 `cuda`，在没有 CUDA 版 LightGBM 的
> 环境下也会回退 CPU —— 但**回退发生在付出 §7.1 双份内存代价之后**，所以别指望它兜底内存。


## 8. 暴露面与安全

- `.quant.env` 含真实 token，已设 `chmod 600`
- 三个服务监听 `0.0.0.0`，9101/9102 有 `QUANT_API_KEY` 鉴权，**9103 只读且无鉴权**
- 如果服务器网段不可信，用 ufw 限制来源：

```bash
ufw allow from <你的本机IP> to any port 9101,9102,9103 proto tcp
ufw enable
```

- **本机与服务器的服务不要同时写同一批库**：数据已迁到服务器，本机的 9101/9102 应停掉，
  否则两边各自调度会产生冲突写入。

## 9. 本机前端连接服务器

前端用 Vite dev server 的代理访问后端（`apps/quant-web/vite.config.ts` 里
`/api/dq|/api/sync|/api/engine|/api/settings` → `127.0.0.1:910x`）。浏览器因此是
**同源请求**，不涉及 CORS。

`vite.config.ts` 的代理 target 已改成读 `VITE_API_HOST`（优先级：`.env` > 进程环境变量
> 缺省 `127.0.0.1`，纯本机开发行为不变）。指向服务器二选一：

```bash
cd apps/quant-web

# 方式一：写进 .env（持久）
echo 'VITE_API_HOST=192.168.1.89' >> .env

# 方式二：临时覆盖（不落盘）
# PowerShell:  $env:VITE_API_HOST='192.168.1.89'; npm run dev
npm run dev          # http://127.0.0.1:3082
```

改完要重启 Vite（proxy 配置在启动时读取）。

## 10. 排错

| 现象 | 排查 |
|---|---|
| `health` 返回 `authEnabled:false` | `.quant.env` 没读到 `QUANT_API_KEY`；`bash deploy/install.sh` 的导入自检会直接点名 |
| 接口 503 / `no such table` | 数据库没传完或传错位置；`ls -l data/*.db` 对一下大小 |
| `unable to open database file` | `project_root` 指错了；看导入自检打印的三个 `project_root` |
| 服务起不来 | `tail -n 60 logs/<会话名>.log`；或 `screen -r <会话名>` |
| 端口被占 | `fuser -k -TERM -n tcp 9102`；或 `bash deploy/screen-down.sh engine` |
| **服务整个消失、日志无异常** | **大概率被 OOM killer 杀了**：`dmesg -T \| grep -i "killed process"`；恢复用 `bash deploy/screen-up.sh` |
| job 报 `worker process restarted before completion` | 父进程在训练中途死了（通常就是 OOM）；同一条报错在本机历史上也反复出现过 |
| 训练内存异常大 / OOM | 检查 `grep -E "deviceType\|cudaInProcess" config/stock-ml.yaml` 必须是 `cuda` + `true`（见 §7.1）；`cudaInProcess: false` 会退回双份内存的隔离路径 |
| 训练实际跑在 CPU 上 | 看运行结果里的 `effectiveDevice`：`deviceType: cuda` 时它应为 `cuda`；若为 `cpu` 说明 CUDA 版 LightGBM 没装好（`cpuFallback` 静默回退了） |
| 界面进度长时间停在 0% | 窗口 1 的因子帧加载阶段不上报进度（约 5 分钟），属正常 |
| screen 会话莫名消失 | 会话内进程退出了；日志在 `logs/`，`screen -r` 可看到退出原因 |
| `sync.db` 里 `rt_idx_k` / `rt_idx_min` 大量 `error` | **既有上游问题，不是部署问题**（见下） |

### 已知：`rt_idx_k` / `rt_idx_min` 的间歇性失败

`quant-sync` 的行情调度在交易时段每分钟拉实时指数。上游 `promax` 数据源会间歇性返回 5xx，
所以 `sync_runs` 里这两类常年有约 40% 的 error。做迁移前后对照（本机冻结库 vs 服务器活库）：

| | 本机（至 2026-09-16） | 服务器（至 2026-09-18） |
|---|---|---|
| `rt_idx_k` 成功 / 失败 | 625 / 394 | 628 / 420 |
| `rt_idx_min` 成功 / 失败 | 577 / 444 | 603 / 447 |
| 报错内容 | `promax rt_idx_k HTTP 502` | `promax rt_idx_k HTTP 503` |

**同一条既有故障，只是 5xx 码不同。** 调度本身是有效的（服务器单日就多成功了 3 次
`rt_idx_k`、26 次 `rt_idx_min`）。真要静默它，在 `deploy/env.sh` 里加
`export QUANT_SYNC_SCHEDULER_ENABLED=0`（或写进 `.quant.env`）后重启 quant-sync，
但这样服务器就不再自动同步行情了。

## 11. 部署过程中的两处代码/配置改动

1. **`services/quant-engine/src/quant_engine/jobs.py`** — 加了
   `from __future__ import annotations`。
   原代码 `def _resource_lock(...) -> threading.Lock | None:` 在 Python < 3.13 会直接
   `TypeError`（3.13 起 `threading.Lock` 才是类型，之前是工厂函数），导致
   `quant_engine.main` 无法导入、服务起不来。仓库声明 `requires-python = ">=3.11"`，
   所以这是个真实缺陷；仓库里另外 61 个文件已经用了这个 future import，属同一约定。
2. **`config/stock-ml.yaml`（曾仅服务器）** — 历史上曾把 `model.deviceType` 由 `cuda`
   改为 `cpu` 以避开隔离进程路径（原因见 §7.1）。**2026-09-20 起此差异已消除**：
   `cudaInProcess: true` 落地后仓库与服务器共用 `cuda`，不再有"仅服务器"的配置改动。

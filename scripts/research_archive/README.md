# scripts/research_archive —— 一次性研究/调试脚本归档

这里放的是**研究过程留下的一次性脚本**：版本实验（v2~v7）、诊断探针、配置一次性修复、
模型评测对比等。它们在 `scripts/` 根目录时没有任何被引用的地方（docs / services / 计划任务 /
其他脚本都不提它们），但**可能仍有考古价值**（某个结论是怎么算出来的），所以**只移动、不删除**。

## 为什么归档而不是删掉

1. `scripts/` 根目录原先混着"编排脚本（计划任务在调）"和"研究脚本（跑过一次就算完）"，
   看目录分不清哪个能动、哪个动不得——这正是编排漂移的温床。
2. 研究结论的复现路径需要留痕：报告里写"某口径"时，能顺着归档脚本找到当时的算法。
3. 归档是**可逆**的：`Move-Item scripts/research_archive/<name>.py scripts/` 即还原。

## 现在 `scripts/` 根目录保留什么（判据：被引用或被生产调用）

| 脚本 | 为什么留下 |
|---|---|
| `daily_stock_chain.py` | 每日链，计划任务 `stock-factor-daily-poll` 调用（内部有单实例锁） |
| `technical_timing_sim.py` | **V1 择时模拟盘**：`daily_stock_chain.py` 直接 `import`（生产路径） |
| `technical_timing_sim_v5.py` | **V5 移动止损模拟盘**：`daily_stock_chain.py` 直接 `import`（生产路径） |
| `backtest_technical_timing.py` | V1 回测基线，`docs/technical_timing_v1_baseline.md` 引用 |
| `backtest_technical_timing_v5.py` / `backtest_technical_timing_v5_long.py` | **V5 回测产物的生成脚本**：产出 `artifacts/stock-ml/technical_timing_v5*_backtest.csv`，被「策略回测」页面（`GET /api/v1/stock/technical-timing/backtest/latest`）与 `technical_timing.py` 的错误提示直接点名 |
| `compute_industry_factors.py` / `compute_money_flow_factors.py` | 两张辅助因子表的手工/巡检入口（引擎 `stock_ml/aux_factors.py` 文档指向它们；日常由 job 产出） |
| `ensure_indexes.py` / `get_tushare_token.py` | `docs/operations.md` §1/§6 引用 |
| `check_contracts.py` / `backup_databases.py` / `run_tests.ps1` | 契约检查、备份、测试入口（CI/文档引用） |
| `migrate_meta.py` / `backfill_fina_indicator.py` / `_ab_rebalance.py` / `_factor_ic.py` / `_red_div_low_vol.py` | 已在 git 里跟踪，本次不动（只处置未跟踪文件） |

## 归档内容分类

### 1) 短线 / 分钟线实验（数据已清零，**不可再跑**）

`_minute_tp_sl_test.py`、`_short_term_test{,2,3,4,5}.py`、`_short_factor_ic.py`、`_gap_decile.py`、
`_gap_ic_check.py`、`_ma_rebound_stat.py`、`_long_window_test.py`、`_tail_stop_demo.py`、`_tp_sl_demo.py`
—— 依赖股票 5 分钟线；`data/minute.db` 已按使用方要求清零，这些脚本现在只能在有分钟数据的环境里跑。

### 2) 技术面择时版本迭代（v2~v4、v6、v7）

`backtest_technical_timing_v{2,3,4,6,7}.py`、`backtest_v1_top50.py`、
`backtest_v6_next_day_sell.py`、`backtest_v7_trailing_stop.py`
—— V5 的方案已固化进 `technical_timing_sim_v5.py`（模拟盘，生产）与
`services/quant-engine/src/quant_engine/technical_timing.py`（回测汇总），V5 的回测脚本在
`scripts/` 根目录保留（它是「策略回测」页面数据的生成入口），其余版本是选型过程。

### 3) ETF 择时/回撤实验

`_etf_bear_test{,2,3}.py`

### 4) 因子与标签的一次性计算

`compute_forward_1.py`、`compute_forward_3.py`（标签已收回服务内）、`compute_hotspot_factors.py`、
`add_hotspot_factors_to_config{,2}.py`

### 5) 模型训练 / 评测 / 对比（一次性）

`train_ensemble_full.py`、`train_hotspot_model.py`、`train_shortterm_model.py`、`multi_train_v1_test.py`、
`eval_ml_model.py`、`professional_eval.py`、`compare_ensemble.py`、`compare_versions.py`、
`compare_wf_runs.py`、`test_ensemble_quick.py`、`test_rt_k_promax{,2}.py`、`read_versions.py`、
`analyze_v1_pnl_attribution.py`、`analyze_v5_pnl_attribution.py`

### 6) 配置一次性修复（危险：会直接改 config/）

`fix_yaml_config.py`、`regenerate_config.py`、`restore_31feat_config.py`
—— 当时的救火脚本，会在服务外直接改写 `config/*.yaml`。**不要再跑**（现在配置由
`services/quant-engine/tests/test_stock_ml_config.py` 这类体检用例守住）。

### 7) 诊断探针（打印型）

`diag_{concentration,ensemble_return,feature_injection,wf_params,windows}.py`、
`check_{factor_date,feature_importance,hotspot_importance,jingyi,jingyi2,minute_tables,model_features,today_bars}.py`、
`inspect_top_runs.py`、`list_wf_records.py`、`cleanup_test_run.py`、`get_top20_today.py`、
`get_top20_realtime.py`、`find_tushare_token.py`

## 注意

- 这些脚本**不在 CI/单测覆盖范围内**，且可能已经跟不上服务内部接口的改动；
  想复用某个口径，最好是"读它的算法，然后在服务里实现"，而不是直接再跑一遍。
- 配套的**配置/产物留在了原处**：`config/stock-ml-shortterm.yaml`（37 特征短线配置，仓库里唯一
  读它的是归档脚本 `train_shortterm_model.py`）与 `artifacts/stock-ml-shortterm/`。
  没跟着搬是为了让归档脚本内的路径保持原样（可追溯优先）。
- 归档目录**不应被生产代码 import**：`services/quant-engine/tests/test_script_layout.py`
  会检查"生产/文档引用的 `scripts/<name>.py` 必须存在"以及"归档目录里的文件都在本 README 中列名"。

## 归档文件清单（机器可校验：测试要求此清单列名所有文件）

- `_etf_bear_test.py`
- `_etf_bear_test2.py`
- `_etf_bear_test3.py`
- `_gap_decile.py`
- `_gap_ic_check.py`
- `_long_window_test.py`
- `_ma_rebound_stat.py`
- `_minute_tp_sl_test.py`
- `_short_factor_ic.py`
- `_short_term_test.py`
- `_short_term_test2.py`
- `_short_term_test3.py`
- `_short_term_test4.py`
- `_short_term_test5.py`
- `_tail_stop_demo.py`
- `_tp_sl_demo.py`
- `add_hotspot_factors_to_config.py`
- `add_hotspot_factors_to_config2.py`
- `analyze_v1_pnl_attribution.py`
- `analyze_v5_pnl_attribution.py`
- `backtest_technical_timing_v2.py`
- `backtest_technical_timing_v3.py`
- `backtest_technical_timing_v4.py`
- `backtest_technical_timing_v6.py`
- `backtest_technical_timing_v7.py`
- `backtest_v1_top50.py`
- `backtest_v6_next_day_sell.py`
- `backtest_v7_trailing_stop.py`
- `check_factor_date.py`
- `check_feature_importance.py`
- `check_hotspot_importance.py`
- `check_jingyi.py`
- `check_jingyi2.py`
- `check_minute_tables.py`
- `check_model_features.py`
- `check_today_bars.py`
- `cleanup_test_run.py`
- `compare_ensemble.py`
- `compare_versions.py`
- `compare_wf_runs.py`
- `compute_forward_1.py`
- `compute_forward_3.py`
- `compute_hotspot_factors.py`
- `diag_concentration.py`
- `diag_ensemble_return.py`
- `diag_feature_injection.py`
- `diag_wf_params.py`
- `diag_windows.py`
- `eval_ml_model.py`
- `find_tushare_token.py`
- `fix_yaml_config.py`
- `get_top20_realtime.py`
- `get_top20_today.py`
- `inspect_top_runs.py`
- `list_wf_records.py`
- `multi_train_v1_test.py`
- `professional_eval.py`
- `read_versions.py`
- `regenerate_config.py`
- `restore_31feat_config.py`
- `test_ensemble_quick.py`
- `test_rt_k_promax.py`
- `test_rt_k_promax2.py`
- `train_ensemble_full.py`
- `train_hotspot_model.py`
- `train_shortterm_model.py`


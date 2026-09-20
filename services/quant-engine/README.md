# DSH Quant Engine

Python计算引擎，统一承载分钟特征、因子挖掘、模型训练、策略回测和每日T+1推理。

## 边界

- 原始 SQLite 数据库由 quant-sync 写入，本服务只读。
- 长任务通过API提交并返回`job_id`，不能占用HTTP请求等待计算结束。
- 派生特征写入`data/features`，模型和报告写入`artifacts`。
- 已注册 `features`、`factor_mining`、`training`、`backtest`、`inference` 五类任务。
- 默认机器学习链路为 `daily-v1`：T 日特征预测 T+1 开盘到收盘收益，严格按交易日期切分训练/验证/测试。

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m uvicorn quant_engine.main:app --host 127.0.0.1 --port 9102
```

环境变量前缀为`QUANT_ENGINE_`，默认项目根目录根据本文件位置自动推导。
非 GET 接口可由 `QUANT_ENGINE_API_KEY` 强制启用 `X-API-Key` 认证。

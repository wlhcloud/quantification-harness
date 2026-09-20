# AI + Quant ETF 智能投研系统设计文档

> 文件名：`AI_QUANT_ETF_SYSTEM_DESIGN.md`  
> 用途：供 Codex / AI Coding Agent 在开发、重构、扩展项目前阅读。  
> 核心思想：**Quant 负责“谁强”，AI 负责“为什么强、逻辑能否持续”。**  
> 本系统定位为研究与决策支持系统，不保证投资收益。

---

## 1. 项目目标

当前项目已有个股股票池与量化推荐能力。后续不要推倒重来，而是把系统升级为同时支持 `STOCK / ETF` 的统一金融标的研究平台，并重点建设 ETF 智能投研能力。

最终系统应具备：

- ETF Universe 管理
- 大盘 Market Regime 判断
- ETF 行业/主题轮动
- 因子计算与因子自动挖掘
- IC / RankIC / ICIR 研究
- LightGBM / XGBoost 横截面 Ranking
- Walk-Forward 训练与回测
- 新闻、政策、宏观、海外市场分析
- LLM + RAG
- 产业链知识图谱
- 新闻事件到行业、ETF 的影响映射
- Quant × AI 双重验证
- 涨跌归因
- 逻辑持续性判断
- 风险管理与组合推荐
- 每日/盘后 ETF 智能投研报告

系统最终回答两个不同的问题：

```text
Quant：现在谁更强？
AI：为什么强？上涨/下跌逻辑是什么？逻辑还能持续多久？
```

---

## 2. 最高优先级设计原则

### 2.1 Quant 与 AI 必须解耦

禁止：

```text
新闻 → LLM → GPT觉得会涨 → BUY
```

正确架构：

```text
行情 ─→ 因子 ─→ ML Ranking ─────────┐
                                   │
大盘 ─→ Market Regime ─────────────┤
                                   ├→ Decision Fusion → ETF候选
新闻/政策/宏观 ─→ RAG/LLM ─────────┤
                                   │
产业链 ─→ Knowledge Graph ─────────┘
```

Quant 负责数值与历史统计，AI 负责语义、事件、产业逻辑和解释。

### 2.2 不以“明天涨跌二分类”为核心目标

不建议：

```text
TomorrowUp = 0 / 1
```

优先：

```text
y_5d  = ETF未来5日收益  - Benchmark未来5日收益
y_10d = ETF未来10日收益 - Benchmark未来10日收益
y_20d = ETF未来20日收益 - Benchmark未来20日收益
```

更推荐横截面 Ranking：

> 在交易日 T，对所有可交易 ETF 排序，预测未来 5~20 个交易日谁的风险收益比更优。

### 2.3 所有模型必须可回测

任何新增因子、AI 分数、组合规则，原则上都要能够回答：

- 历史 IC 如何？
- RankIC 如何？
- 不同年份是否稳定？
- 牛/熊/震荡市场表现如何？
- 最大回撤多少？
- 换手率多少？
- 加入交易成本后是否仍有效？

不能因为“逻辑听起来合理”就进入正式策略。

---

## 3. 从现有个股系统迁移

不要删除 Stock 能力。

建议抽象：

```java
enum InstrumentType {
    STOCK,
    ETF,
    INDEX
}
```

统一实体：

```text
Instrument
├── instrumentId
├── symbol
├── name
├── instrumentType
├── exchange
├── category
├── industry
├── theme
├── benchmark
├── listingDate
└── status
```

原：

```text
stock_pool
```

逐步抽象为：

```text
instrument_pool
```

但迁移必须渐进式进行，避免一次性大规模破坏现有功能。

---

## 4. ETF Universe

第一版建议 30~100 只高质量 ETF，不追求数量。

分类：

```text
宽基
行业
主题
科技
医药
消费
金融
周期
红利
黄金/商品
跨境
债券
```

过滤：

```text
上市天数
基金规模
5/20日平均成交额
成交量
跟踪误差
折溢价异常
交易状态
```

同类 ETF 必须去重。

可根据：

```text
跟踪指数
主题
行业
60/120日收益相关性
```

若：

```text
corr > 0.90
```

可视为高度重复，优先保留流动性更好或 Quant Score 更高者。

---

## 5. Market Regime Engine

ETF 轮动前必须先判断市场环境。

建议输出：

```text
BULL_TREND
BULL_RANGE
NEUTRAL
BEAR_RANGE
BEAR_TREND
```

以及：

```text
RISK_ON
NEUTRAL
RISK_OFF
```

输入包括：

### 指数

```text
上证指数
沪深300
中证500
中证1000
创业板
科创50
```

### 市场宽度

```text
上涨家数
下跌家数
涨停数量
跌停数量
创新高数量
创新低数量
```

### 成交

```text
全市场成交额
5日平均成交额
20日平均成交额
成交额趋势
```

### 风格

```text
大盘 vs 小盘
成长 vs 价值
科技 vs 防御
高Beta vs 低Beta
```

Market Regime 必须成为组合仓位和模型解释的重要上下文，而不是简单展示字段。

---

## 6. ETF 因子体系

### 6.1 Momentum

```text
ret_3
ret_5
ret_10
ret_20
ret_60
ret_120
```

可构建复合动量。

### 6.2 Relative Strength

ETF 特别重要：

```text
ETF收益 - 沪深300收益
ETF收益 - 中证全指收益
ETF收益 - 同类ETF平均收益
```

周期：

```text
rs_5
rs_10
rs_20
rs_60
```

### 6.3 Trend

```text
close / ma5 - 1
close / ma10 - 1
close / ma20 - 1
close / ma60 - 1
```

均线多头排列也可以编码为趋势结构特征。

### 6.4 Trend Quality

不要只判断“涨了多少”，还要判断“涨得稳不稳”。

```text
positive_day_ratio_20
linear_regression_r2_20
linear_regression_slope_20
ret_20 / volatility_20
ret_60 / volatility_60
```

### 6.5 Volume / Amount

```text
volume_ma5 / volume_ma20
amount_ma5 / amount_ma20
amount_today / amount_ma20
turnover_change
breakout_volume_ratio
```

### 6.6 Risk

```text
volatility_10
volatility_20
volatility_60
mdd_20
mdd_60
mdd_120
downside_volatility
atr_14
```

风险因子通常为负向。

### 6.7 Liquidity

```text
avg_amount_5
avg_amount_20
avg_volume_20
turnover
```

流动性首先用于保证可交易性。

### 6.8 Fund Flow

数据允许时加入：

```text
ETF份额变化
ETF资金净流入
融资相关数据
其他可靠资金流数据
```

必须严格检查数据发布时间，禁止未来数据泄漏。

### 6.9 Sector Strength

```text
行业涨跌幅
行业相对强度
行业成交额变化
行业上涨比例
行业龙头强度
行业宽度
```

---

## 7. 因子自动挖掘系统

不要永远手工写死因子。

建设：

```text
Factor Generation
      ↓
Factor Calculation
      ↓
Factor Evaluation
      ↓
Factor Deduplication
      ↓
Factor Registry
```

候选表达式：

```python
ts_mean(close, 5) / ts_mean(close, 20) - 1
ts_rank(volume, 20)
correlation(ret, volume, 10)
ret_20 / volatility_20
close / ts_max(close, 60) - 1
```

后续可设计 Factor DSL。

---

## 8. 因子评价

每个因子至少计算：

```text
IC
RankIC
ICIR
正IC比例
分层收益
Top-Bottom Spread
换手率
年度稳定性
Regime稳定性
```

IC：

```text
corr(factor_t, future_return)
```

RankIC：

```text
Spearman(factor_rank, future_return_rank)
```

ICIR：

```text
mean(IC) / std(IC)
```

不能只看全历史平均 IC。

必须观察：

```text
年度
季度
牛市
熊市
震荡市
Risk-On
Risk-Off
```

---

## 9. 因子去相关

例如：

```text
momentum_20
roc_20
ret_20
```

可能高度重复。

建立 Factor Correlation Matrix。

例如：

```text
abs(corr) > 0.85
```

进入去冗余候选。

优先保留：

```text
IC高
ICIR高
稳定性好
解释性好
交易成本低
```

的因子。

---

## 10. Feature Store

建议统一存储：

```text
trade_date
instrument_id
factor_name
factor_value
factor_version
calculated_at
```

或者为训练建立日频宽表。

所有因子必须有：

```text
version
formula
parameters
created_at
enabled
```

确保未来可以复现实验。

---

## 11. 模型训练

第一阶段模型优先级：

```text
Baseline线性模型
↓
LightGBM
↓
LightGBM Ranker
↓
XGBoost Ranker
```

暂时不要把 LSTM / Transformer 当主模型。

ETF 横截面较小，复杂深度模型容易过拟合。

推荐训练目标：

```text
未来10日超额收益Rank
```

并同时研究：

```text
5D
10D
20D
```

不同 horizon。

---

## 12. Walk-Forward

严禁：

```python
train_test_split(shuffle=True)
```

必须按时间：

```text
2019-2022 TRAIN
2023 VALID
2024 TEST
```

然后滚动：

```text
2020-2023 TRAIN
2024 VALID
2025 TEST
```

最终生产模型也应支持定期滚动重训。

---

## 13. 防止未来数据泄漏

在 T 日做决策时，只允许使用 T 日当时真实可获取的信息。

重点检查：

```text
未来价格
未来指数成分
未来ETF池
未来资金数据
未来新闻
修订后的宏观数据
发布时间晚于决策时点的数据
```

同时处理：

```text
Survivorship Bias
```

历史回测不能只使用今天仍存在的 ETF。

---

## 14. Sector Rotation Engine

输出类似：

```text
科技      ↑↑
医药      ↑
有色      ↑
金融      →
消费      ↓
```

综合：

```text
行业动量
行业RS
行业宽度
资金
成交
Market Regime
宏观
新闻事件
```

Quant 行业轮动分数和 AI 行业逻辑分数必须分开保存。

---

## 15. 新闻与事件系统

新闻分类：

```text
宏观
政策
行业
公司
海外
商品
利率
汇率
地缘事件
```

LLM 负责结构化事件抽取，例如：

```json
{
  "eventType": "POLICY",
  "entities": [],
  "industries": [],
  "themes": [],
  "direction": "POSITIVE",
  "impactScore": 78,
  "duration": "MEDIUM",
  "confidence": 0.84,
  "summary": "..."
}
```

新闻本身不能直接触发 BUY/SELL。

必须检查：

```text
价格确认？
成交确认？
资金确认？
行业扩散？
Quant Rank 是否同步上升？
```

---

## 16. RAG

RAG 数据源可包括：

```text
政策文件
行业研究资料
历史新闻
公司公告
产业资料
宏观资料
历史事件复盘
```

流程：

```text
Query
↓
Retriever
↓
Relevant Context
↓
LLM
↓
Structured Research Result
```

AI 生成结论时尽量保留来源、时间和证据，避免无依据编故事。

---

## 17. Knowledge Graph

建议复用现有知识图谱能力。

节点：

```text
Industry
Theme
Company
ETF
Commodity
Policy
Technology
Product
Country
Index
```

边：

```text
BELONGS_TO
UPSTREAM_OF
DOWNSTREAM_OF
BENEFITS
HURTS
TRACKS
RELATED_TO
SUPPLIES
DEPENDS_ON
```

例如：

```text
AI
↓
数据中心
↓
服务器
↓
PCB / 光模块 / 液冷 / 电源
↓
对应行业
↓
对应ETF
```

新闻事件可通过知识图谱传播到潜在受影响 ETF。

---

## 18. AI Research Agent

输入：

```text
ETF基础信息
Quant Score
Quant Rank
因子贡献
Market Regime
Sector Rotation
相关新闻
政策
宏观
海外市场
RAG上下文
Knowledge Graph路径
历史相似事件
```

输出必须结构化：

```json
{
  "logicSummary": "",
  "mainDrivers": [],
  "negativeDrivers": [],
  "marketConfirmation": "",
  "sustainabilityScore": 0,
  "crowdingRisk": 0,
  "eventRisk": 0,
  "confidence": 0,
  "conclusion": ""
}
```

LLM 不输出“必涨”“稳赚”等确定性语言。

---

## 19. 涨跌归因系统

系统每天应回答：

> 为什么这个 ETF 今天涨了/跌了？

归因来源：

```text
大盘Beta
行业Beta
海外映射
商品价格
政策
新闻事件
资金
技术突破
市场风格
```

结果示例：

```text
半导体ETF 今日 +3.2%

主要驱动：
1. 行业整体强势
2. 海外半导体映射偏强
3. 成交额明显放大
4. 20日相对强度创新高
5. 最新产业事件形成正面催化

判断：
量价与产业逻辑形成共振。
```

必须区分：

```text
事实
模型统计
AI推断
```

---

## 20. 逻辑持续性判断

AI 不只是解释今天为什么涨，还要判断：

```text
这个驱动是一次性事件？
还是中期产业趋势？
```

建议：

```text
Sustainability Score: 0~100
```

输入：

```text
事件类型
政策级别
产业链影响范围
基本面持续时间
Quant趋势
资金持续性
拥挤度
历史相似事件
```

---

## 21. Quant × AI 双重验证

建立二维决策矩阵：

| Quant | AI Logic | 解释 |
|---|---|---|
| 强 | 强 | 重点候选 |
| 强 | 弱 | 可能为资金/技术行情 |
| 弱 | 强 | 逻辑存在但价格尚未确认，观察 |
| 弱 | 弱 | 通常排除 |

注意：

AI 不应该简单覆盖 Quant。

推荐分别保存：

```text
quant_score
ai_logic_score
risk_score
final_research_score
```

这样可以回测：

> AI 层到底有没有提高 Quant 原模型表现？

---

## 22. 综合评分

第一版可以使用可解释权重：

```text
FinalScore =
    QuantScore * W1
  + SectorScore * W2
  + MarketCompatibility * W3
  + AILogicScore * W4
  - RiskPenalty * W5
```

但不要永久写死权重。

后续要通过历史实验验证。

特别注意：

```text
AI Logic Score
```

加入前后必须做 A/B Backtest。

---

## 23. 风险管理

推荐系统不能只输出收益预期。

至少考虑：

```text
单ETF最大仓位
单行业最大仓位
相关性约束
最大组合波动
最大回撤阈值
止损/趋势失效规则
流动性约束
换手成本
交易费用
滑点
```

Market Regime 可影响总仓位。

例如：

```text
RISK_ON  → 正常风险预算
NEUTRAL  → 中等风险预算
RISK_OFF → 降低风险暴露
```

具体阈值必须通过回测决定，不能拍脑袋写死。

---

## 24. 组合层

不要最终只输出：

```text
Top1 ETF
```

应支持：

```text
Top3
Top5
```

并进行：

```text
相关性去重
行业约束
风险预算
权重分配
```

避免 Top3 实际全部属于同一科技因子暴露。

---

## 25. 每日推荐结果

建议结构：

```json
{
  "tradeDate": "YYYY-MM-DD",
  "marketRegime": "RISK_ON",
  "recommendations": [
    {
      "symbol": "",
      "quantRank": 1,
      "quantScore": 86,
      "sectorScore": 82,
      "aiLogicScore": 78,
      "riskScore": 35,
      "sustainabilityScore": 80,
      "reasons": [],
      "risks": []
    }
  ]
}
```

---

## 26. 每日投研报告

系统每天收盘后生成：

```text
《ETF 智能投研日报》

1. 今日市场概览
2. Market Regime
3. 大盘趋势
4. 市场成交与宽度
5. 风格判断
6. 最强行业 TOP10
7. ETF Quant Ranking TOP10
8. 今日排名变化最大的 ETF
9. 今日主要行业事件
10. 为什么涨 / 为什么跌
11. Quant 与 AI 共振候选
12. 逻辑持续性分析
13. 风险与拥挤度
14. 明日重点观察 ETF
15. 模型历史表现与当前置信度
```

---

## 27. 推荐后必须跟踪

不要只记录“当天推荐”。

建立：

```text
recommendation_snapshot
```

后续跟踪：

```text
1D
3D
5D
10D
20D
```

收益。

用于分析：

```text
Quant预测准确率
AI逻辑有效性
不同Market Regime表现
不同ETF类别表现
```

形成真正闭环：

```text
预测
↓
交易日过去
↓
结果
↓
评价
↓
模型改进
```

---

## 28. 模型可解释性

LightGBM 可以使用：

```text
Feature Importance
SHAP
```

推荐页面展示：

```text
为什么模型把半导体ETF排第1？
```

例如：

```text
20日相对强度      +18
趋势质量          +13
成交额变化        +10
行业强度           +9
波动率             -4
短期拥挤度         -6
```

然后 AI 将这些数字翻译成自然语言。

---

## 29. AI 不得伪造量化解释

AI 只能解释 Quant Engine 已提供的数据。

禁止 AI 自己编造：

```text
资金净流入
Rank
IC
收益率
成交额
模型胜率
```

所有数字必须来自真实计算结果。

---

## 30. 推荐的技术边界

结合当前技术栈：

### Java

适合：

```text
业务系统
REST API
用户配置
ETF池管理
策略配置
任务调度
权限
推荐结果
报告管理
前端接口
```

### Python

适合：

```text
行情处理
因子计算
因子挖掘
机器学习
LightGBM
XGBoost
回测
统计分析
SHAP
RAG/LLM研究任务
```

推荐：

```text
Java Business Service
        ↕
Python Quant/AI Service
```

可通过：

```text
HTTP / REST
```

第一版连接。

不要为了“微服务”而过度拆分。

---

## 31. Python 模块建议

```text
quant/
├── data/
├── universe/
├── factors/
│   ├── momentum/
│   ├── trend/
│   ├── volume/
│   ├── risk/
│   ├── relative_strength/
│   └── sector/
├── factor_research/
│   ├── ic.py
│   ├── rank_ic.py
│   ├── factor_backtest.py
│   └── correlation.py
├── features/
├── models/
│   ├── baseline/
│   ├── lightgbm/
│   └── ranking/
├── regime/
├── rotation/
├── backtest/
├── portfolio/
├── risk/
├── ai/
│   ├── rag/
│   ├── event/
│   ├── research/
│   └── attribution/
└── jobs/
```

---

## 32. 数据库建议

核心表可以包括：

```text
instrument
instrument_pool
etf_metadata
market_daily
index_daily
factor_definition
factor_value
factor_evaluation
model_definition
model_training_run
model_prediction
market_regime
sector_daily_score
news
news_event
news_instrument_mapping
ai_research_result
recommendation
recommendation_snapshot
backtest_run
backtest_trade
portfolio_snapshot
research_report
```

实际表结构应先检查现有项目，能复用则复用。

---

## 33. 训练任务

建议流程：

```text
prepare_universe
↓
load_market_data
↓
calculate_factors
↓
validate_factors
↓
build_dataset
↓
walk_forward_train
↓
predict
↓
backtest
↓
evaluate
↓
register_model
```

所有 Training Run 保存：

```text
代码版本
数据范围
ETF池版本
因子版本
参数
随机种子
模型文件
评价指标
```

保证可复现。

---

## 34. 每日生产任务

盘后：

```text
1. 更新行情
2. 更新指数
3. 更新 ETF 元数据
4. 更新新闻
5. 计算 Market Regime
6. 计算所有 ETF 因子
7. 运行 Ranking Model
8. 计算 Sector Rotation
9. 事件抽取
10. RAG
11. Knowledge Graph 影响传播
12. AI Research
13. Quant × AI Fusion
14. Risk Filter
15. 生成候选组合
16. 保存 Recommendation
17. 生成日报
```

次日继续追踪历史推荐表现。

---

## 35. 回测必须考虑现实交易

至少考虑：

```text
交易费用
滑点
ETF流动性
调仓频率
信号生成时间
信号执行时间
停牌/不可交易状态
```

尤其注意：

> 使用收盘数据生成信号时，不能假设自己能够以同一个已经知道的收盘价无限成交。

---

## 36. AI Semantic Factor

未来可以研究把 AI 输出变成可回测因子：

```text
policy_score
news_sentiment
industry_logic_score
sustainability_score
event_impact_score
crowding_risk
```

但是必须：

```text
保存当时的原始AI输出
保存模型版本
保存Prompt版本
保存数据时间
```

然后进行历史统计。

不能只看几个成功案例。

---

## 37. 历史相似行情

后续可以建立：

```text
Historical Analogy Engine
```

输入：

```text
当前Market Regime
ETF趋势
波动率
成交
行业强度
事件类型
```

寻找历史相似窗口。

AI 可以解释相似点和不同点，但不能把历史路径机械当成未来路径。

---

## 38. 推荐系统的最终页面思想

ETF Detail：

```text
ETF名称
当前Quant Rank
Quant Score
趋势
RS
资金
行业强度
风险
Market Regime适配度
AI逻辑
主要驱动
负面因素
持续性
历史相似行情
模型解释
```

Dashboard：

```text
Market Regime
↓
行业热力图
↓
ETF Ranking
↓
Quant × AI 共振
↓
风险
↓
推荐组合
```

---

## 39. 第一阶段 Roadmap

目标：

> 先证明 ETF Quant 有效。

实现：

```text
ETF池
行情
20~40个基础因子
IC / RankIC
因子去相关
LightGBM Ranker
Walk-Forward
Top3/Top5回测
Market Regime
推荐结果
```

此阶段 AI 不参与最终评分。

---

## 40. 第二阶段 Roadmap

目标：

> 加入 AI 投研解释。

实现：

```text
新闻采集
事件抽取
行业映射
RAG
AI Research Agent
涨跌归因
逻辑持续性
每日投研报告
```

仍然保持：

```text
Quant推荐
AI解释
```

---

## 41. 第三阶段 Roadmap

目标：

> 验证 AI 是否真的提供 Alpha。

把：

```text
AI Logic Score
News Score
Policy Score
Sustainability Score
```

作为候选 Semantic Factors。

进行：

```text
IC
RankIC
Walk-Forward
A/B Backtest
```

只有证明有效，才允许进入正式综合评分。

---

## 42. 第四阶段 Roadmap

目标：

> ETF → 行业 → 个股二阶段体系。

流程：

```text
Market Regime
↓
ETF模型选择最强行业
↓
Sector Selection
↓
在强行业内部运行Stock Model
↓
选择行业龙头/强势个股
```

最终系统同时拥有：

```text
ETF资产配置能力
+
个股Alpha能力
```

---

## 43. Codex 开发规则

Codex 在修改项目之前必须遵守：

### RULE 1

先阅读本文档，再阅读现有代码。

### RULE 2

不要因为建设 ETF 功能就删除现有 STOCK 功能。

### RULE 3

优先抽象公共能力：

```text
Stock → Instrument
```

### RULE 4

Quant 与 LLM 必须解耦。

### RULE 5

LLM 不允许直接成为 BUY / SELL 模型。

### RULE 6

任何模型训练必须按时间切分。

### RULE 7

任何新因子必须检查未来数据泄漏。

### RULE 8

不要仅以回测总收益评价策略。

至少评价：

```text
Annual Return
Sharpe
Max Drawdown
Calmar
Turnover
Win Rate
Excess Return
IC
RankIC
```

### RULE 9

所有模型、因子、Prompt 都要版本化。

### RULE 10

不要未经验证一次性增加大量复杂技术。

第一阶段优先：

```text
简单
可解释
可回测
可复现
```

---

## 44. Codex 禁止事项

禁止：

```text
LLM直接预测明天涨跌
LLM直接根据新闻下单
随机train_test_split
使用未来数据
只展示训练集效果
忽略交易成本
忽略ETF重复性
把所有ETF视为完全独立资产
为了AI而AI
为了微服务而微服务
未经回测修改正式策略权重
AI伪造Quant数据
```

---

## 45. 核心哲学

系统不是：

```text
AI猜股票
```

也不是：

```text
堆几十个技术指标
```

而应该是：

```text
市场环境
    ↓
行业轮动
    ↓
ETF Quant Ranking
    ↓
新闻 / 政策 / 宏观
    ↓
产业链知识
    ↓
AI理解与归因
    ↓
Quant × AI验证
    ↓
风险控制
    ↓
组合
    ↓
持续跟踪
    ↓
重新训练与改进
```

最重要的三个问题：

### 1. 谁强？

由 Quant 回答。

### 2. 为什么强？

由 AI + RAG + Knowledge Graph 回答。

### 3. 这个强势还能不能持续？

由：

```text
Quant趋势
+
Market Regime
+
产业逻辑
+
事件持续性
+
资金确认
+
风险
```

共同判断。

---

## 46. 给 Codex 的执行指令

当 Codex 接手当前项目时：

1. 首先分析现有项目目录、数据库、股票池、行情、因子、训练、回测和推荐代码。
2. 输出“现有能力 → 本设计目标”的 Gap Analysis。
3. 不要立即大规模改代码。
4. 先给出迁移方案。
5. 明确哪些模块可以复用。
6. 明确哪些 Stock 专属逻辑需要抽象。
7. 明确新增 ETF 所需数据。
8. 优先完成 ETF Quant MVP。
9. Quant MVP 回测可靠后，再加入 AI Research。
10. AI Research 上线后，先作为解释层运行。
11. 积累历史 AI 输出后，再研究 Semantic Factor。
12. 所有重要架构变化必须保证 STOCK / ETF 后续可以共存。

---

# 最终系统定位

> **AI + Quant ETF Research & Decision Support System**

它不是一个承诺收益的“AI炒股神器”。

它应该是一套：

**有数据、有模型、有回测、有逻辑、有证据、有风险控制、有持续学习闭环的智能投研系统。**

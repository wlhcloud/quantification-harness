import { Card, Descriptions, Space, Tag } from "antd";

import type { StockMlConfigDto } from "../api";

/**
 * 「训练配置」页签：模型参数 / walkforward 配置 / 回测与股票池配置。
 * 纯展示（数据来自 GET /api/v1/stock-ml/config），没有任何交互。
 */
export function ConfigTab({ config }: { config: StockMlConfigDto | null }) {
  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {/* 模型参数 */}
      <Card size="small" title="LightGBM 模型参数"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ fontSize: 12 }}>
          {config?.model.randomStates && config.model.randomStates.length > 1 ? (
            <Descriptions.Item label="多种子集成 ensemble" span={1}>
              <Tag color="purple" style={{ fontWeight: 700 }}>{config.model.randomStates.length} 种子 · {config.model.ensembleMethod === 'median_zscore' ? 'median_zscore（中位数）' : 'mean_zscore（均值）'}</Tag>
              <span style={{ fontSize: 11, color: "#8a94a6" }}>{config.model.randomStates.join(", ")}</span>
            </Descriptions.Item>
          ) : (
            <Descriptions.Item label="随机种子 randomState"><Tag color="red" style={{ fontWeight: 700 }}>{config?.model.randomState ?? "—"}</Tag></Descriptions.Item>
          )}
          <Descriptions.Item label="主种子 randomState"><Tag color={config?.model.randomStates && config.model.randomStates.length > 1 ? "default" : "red"}>{config?.model.randomState ?? "—"}</Tag></Descriptions.Item>
          <Descriptions.Item label="nEstimators（树数量）">{config?.model.nEstimators ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="learningRate（学习率）">{config?.model.learningRate ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="numLeaves（叶子数）">{config?.model.numLeaves ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="minDataInLeaf（最小叶样本）">{config?.model.minDataInLeaf ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="subsample（行采样）">{config?.model.subsample ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="colsampleBytree（列采样）">{config?.model.colsampleBytree ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="earlyStopping（早停轮数）">{config?.model.earlyStopping ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="labelGrades（标签分箱）">{config?.model.labelGrades ?? "—"}</Descriptions.Item>
        </Descriptions>
        {config?.model.randomStates && config.model.randomStates.length > 1 && (
          <div style={{ marginTop: 10, padding: "8px 12px", background: "#f6f0ff", border: "1px solid #e3d4ff", borderRadius: 8, fontSize: 12, color: "#6b4fb8", lineHeight: 1.6 }}>
            多种子集成：每个 walkforward 窗口用 {config.model.randomStates.length} 个随机种子（{config.model.randomStates.join("、")}）各训练一个 LightGBM 模型，
            各模型预测分数先做横截面 z-score 标准化再{config.model.ensembleMethod === 'median_zscore' ? '取中位数（median_zscore，抗异常值更稳健）' : '等权平均（mean_zscore）'}，降低单一种子随机性导致的结果不稳定（同配置换种子收益大幅跳变问题）。
          </div>
        )}
      </Card>

      {/* Walkforward 配置 */}
      <Card size="small" title="Walkforward 滚动训练配置"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ fontSize: 12 }}>
          <Descriptions.Item label="label（预测目标）"><Tag color="blue">{config?.label ?? "—"}</Tag></Descriptions.Item>
          <Descriptions.Item label="minTrainDays（最小训练天数）">{config?.walkforward.minTrainDays ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="stepDays（滚动步长）">{config?.walkforward.stepDays ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="testDays（测试窗口）">{config?.walkforward.testDays ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="validationDays（验证窗口）">{config?.model.validationDays ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="特征数量">{config?.featureCount ?? "—"}</Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 回测配置 */}
      <Card size="small" title="回测与股票池配置"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ fontSize: 12 }}>
          <Descriptions.Item label="topN（持仓数量）"><Tag color="green">{config?.backtest.topN ?? "—"}</Tag></Descriptions.Item>
          <Descriptions.Item label="rebalanceDays（调仓周期）">{config?.backtest.rebalanceDays ?? "—"} 天</Descriptions.Item>
          <Descriptions.Item label="transactionCostBps（佣金）">{config?.backtest.transactionCostBps ?? "—"} bps</Descriptions.Item>
          <Descriptions.Item label="slippageBps（滑点）">{config?.backtest.slippageBps ?? "—"} bps</Descriptions.Item>
          <Descriptions.Item label="minMarketCap（最小市值）">{(config?.universe.minMarketCap ?? 0) / 10000} 亿</Descriptions.Item>
          <Descriptions.Item label="maxMarketCap（最大市值）">{(config?.universe.maxMarketCap ?? 0) / 10000} 亿</Descriptions.Item>
          <Descriptions.Item label="excludeST（剔除ST）">{config?.universe.excludeST ? "是" : "否"}</Descriptions.Item>
          <Descriptions.Item label="excludeSuspended（剔除停牌）">{config?.universe.excludeSuspended ? "是" : "否"}</Descriptions.Item>
          <Descriptions.Item label="minListDays（最小上市天数）">{config?.universe.minListDays ?? "—"} 天</Descriptions.Item>
        </Descriptions>
      </Card>
    </Space>
  );
}

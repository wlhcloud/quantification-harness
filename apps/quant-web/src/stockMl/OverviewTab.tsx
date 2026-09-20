import { Card, Empty, Space, Table, Tag } from "antd";
import { FundOutlined } from "@ant-design/icons";

import type { StockMlCandidate, StockMlHolding, StockMlMetrics } from "../api";
import { dateText, fmtNum, fmtPct } from "./format";
import { stockName } from "./labels";
import { StockCodeLink } from "./StockCodeLink";

export interface OverviewTabProps {
  metrics: StockMlMetrics | undefined;
  /** 最新信号的交易日（来自 walkforward holdings） */
  signalDate?: string | null;
  holdings: StockMlHolding[];
  candidates: StockMlCandidate[];
  candidateDate: string | null;
  loading: boolean;
}

/** 「回测与信号」页签：Walkforward 指标 + 最新持仓 + 候选池 */
export function OverviewTab({ metrics, signalDate, holdings, candidates, candidateDate, loading }: OverviewTabProps) {
  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {/* Walkforward 指标 */}
      <Card size="small" title="Walkforward 回测指标（滚动窗口）"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {[
            { t: "总收益", v: fmtPct(metrics?.totalReturn), c: (metrics?.totalReturn ?? 0) > 0 ? "#0baa81" : "#e5524f" },
            { t: "超额收益", v: fmtPct(metrics?.excessReturn), c: (metrics?.excessReturn ?? 0) > 0 ? "#0baa81" : "#e5524f" },
            { t: "年化收益", v: fmtPct(metrics?.annualReturn), c: "#1a232c" },
            { t: "Sharpe", v: fmtNum(metrics?.sharpe), c: (metrics?.sharpe ?? 0) > 1 ? "#0baa81" : "#e5a33b" },
            { t: "最大回撤", v: fmtPct(metrics?.maxDrawdown), c: "#e5524f" },
            { t: "avgRankIC", v: fmtNum(metrics?.avgRankIc), c: (metrics?.avgRankIc ?? 0) > 0.03 ? "#0baa81" : "#e5a33b" },
            { t: "窗口数", v: String(metrics?.windows ?? "—"), c: "#1a232c" },
          ].map((x) => (
            <div key={x.t} style={{ flex: "1 1 110px", minWidth: 100, background: "#f7f9fb", borderRadius: 8, padding: "10px 12px" }}>
              <div style={{ fontSize: 11, color: "#5f6b76" }}>{x.t}</div>
              <div style={{ fontSize: 18, fontWeight: 700, color: x.c, marginTop: 2 }}>{x.v}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* 最新持仓 */}
      <Card size="small" title={<Space><FundOutlined /> 最新选股信号（{signalDate ? `信号日 ${dateText(signalDate)}` : "—"}）</Space>}
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "8px 0 0" }}>
        <Table
          size="small" rowKey="code" dataSource={holdings} loading={loading} pagination={false}
          locale={{ emptyText: <Empty description="暂无持仓（bearRegime 时空仓）" /> }}
          columns={[
            { title: "排名", dataIndex: "rank", width: 60, render: (_: unknown, __: unknown, idx: number) => <span style={{ fontWeight: 600, color: idx < 3 ? "#0baa81" : "#1a232c" }}>{idx + 1}</span> },
            { title: "代码", dataIndex: "code", width: 110, render: (v: string) => <StockCodeLink code={v} /> },
            { title: "名称", dataIndex: "code", width: 100, render: (v: string) => <span style={{ fontWeight: 500, color: "#1a232c" }}>{stockName(v) || "—"}</span> },
            { title: "模型得分", dataIndex: "score", width: 120, align: "right" as const, render: (v: number) => <span style={{ fontWeight: 700, color: "#1a232c" }}>{fmtNum(v, 4)}</span> },
            { title: "权重", dataIndex: "weight", width: 100, align: "right" as const, render: (v: number) => fmtPct(v) },
          ]}
        />
      </Card>

      {/* 选股候选 */}
      <Card size="small" title={<Space><FundOutlined /> 选股候选池（{candidateDate ? `信号日 ${dateText(candidateDate)} · ${candidates.length} 只` : "—"}）</Space>}
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "8px 0 0" }}>
        <Table
          size="small" rowKey={(r) => `${r.code}_${r.rank}`} dataSource={candidates} loading={loading} pagination={{ pageSize: 20, showSizeChanger: false }}
          locale={{ emptyText: <Empty description="暂无候选（需先运行模型训练）" /> }}
          columns={[
            { title: "排名", dataIndex: "rank", width: 60, render: (v: number) => <span style={{ fontWeight: 600, color: v <= 3 ? "#0baa81" : "#1a232c" }}>{v}</span> },
            { title: "代码", dataIndex: "code", width: 110, render: (v: string) => <StockCodeLink code={v} /> },
            { title: "名称", dataIndex: "code", width: 100, render: (v: string) => <span style={{ fontWeight: 500, color: "#1a232c" }}>{stockName(v) || "—"}</span> },
            { title: "综合得分", dataIndex: "score", width: 110, align: "right" as const, sorter: (a: StockMlCandidate, b: StockMlCandidate) => a.score - b.score,
              render: (v: number) => <span style={{ color: v >= 70 ? "#e5524f" : v >= 60 ? "#fa8c16" : "#595959", fontWeight: 700 }}>{v.toFixed(2)}</span> },
            { title: "入选理由", dataIndex: "reasons", render: (reasons: StockMlCandidate["reasons"]) => (
              <Space size={4} wrap>
                {(reasons ?? []).slice(0, 3).map((r, i) => (
                  <Tag key={i} color="blue" style={{ marginInlineEnd: 0 }}>{r.label} {(r.score * 100).toFixed(0)}分</Tag>
                ))}
              </Space>
            ) },
          ]}
        />
      </Card>
    </Space>
  );
}

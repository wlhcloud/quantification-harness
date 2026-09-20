import { useCallback, useEffect, useState } from "react";
import { App, Button, Card, Empty, Progress, Space, Table, Tag, Typography } from "antd";
import { ReloadOutlined, StopOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { etfApi, engineApi, type EtfJobType, type EtfResearchDto } from "./api";
import { fmtNum, fmtPct } from "./format";
import { useJobPolling } from "./useJobPolling";

const { Text, Paragraph } = Typography;

/** 本页展示口径：IC 类指标保留 4 位小数；占比类百分比不带正号、保留 1 位。 */
const fmtIc = (v: number | null | undefined) => fmtNum(v, 4);
const fmtShare = (v: number | null | undefined) => fmtPct(v, 1, false);

function IcBar({ value }: { value: number | null }) {
  if (value == null) return <span style={{ color: "#8a94a0" }}>—</span>;
  const pct = Math.max(-100, Math.min(100, value * 100));
  const pos = pct >= 0;
  const color = pos ? "#0baa81" : "#e5524f";
  const w = Math.max(2, Math.abs(pct));
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 90 }}>
      <div style={{ flex: 1, height: 8, borderRadius: 4, background: "#eef2f6", position: "relative", overflow: "hidden" }}>
        <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "#c9d4e2" }} />
        <div style={{ position: "absolute", top: 0, bottom: 0, [pos ? "left" : "right"]: "50%", width: `${Math.min(w, 50)}%`, background: color, borderRadius: 4 }} />
      </div>
      <span style={{ fontSize: 11, color: pos ? "#0baa81" : "#e5524f", minWidth: 46 }}>{fmtIc(value)}</span>
    </div>
  );
}

const FACTOR_LABELS: Record<string, string> = {
  mom20: "20日动量", mom60: "60日动量", mom120: "120日动量", ma_alignment: "均线排列",
  vol_ratio: "量比", amount_trend: "量能趋势", mom_accel: "动量加速度", skew20: "20日偏度",
  drawdown20: "20日回撤", drawdown60: "60日回撤", volatility20: "20日波动", rs20: "20日相对强弱",
  rs60: "60日相对强弱", rs60_vol_adj: "波动调整RS", trend_quality: "趋势质量",
  log_amount20: "对数成交额", amount_share: "成交额占比", ind_rs: "行业相对强弱",
};

export default function FactorsPage() {
  const { message } = App.useApp();
  const [data, setData] = useState<EtfResearchDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState<EtfJobType | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await etfApi.researchLatest();
      setData(r.status === "none" ? null : r); // 后端无数据时返回 {status:"none"}
    } catch (e) {
      message.error("获取因子 IC 失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const poll = useJobPolling({
    fetchJob: etfApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 4000,
    onComplete: async () => { setRunning(null); message.success("计算完成，已刷新"); await load(); },
    onError: (m) => { setRunning(null); message.error("运行失败：" + m); },
    onCancelled: () => { setRunning(null); message.info("任务已取消"); },
    onPollError: (m) => { setRunning(null); message.error("轮询任务失败：" + m); },
    onTimeout: () => { setRunning(null); message.error("任务轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const run = async (type: EtfJobType) => {
    if (running) return;
    setRunning(type);
    try {
      const job = await etfApi.startJob(type);
      message.info("因子研究已启动，运行中…");
      poll.watch(job);
    } catch (e) {
      setRunning(null);
      message.error("启动失败：" + String((e as Error).message ?? e));
    }
  };

  const rows = (data?.results ?? []).map((x) => ({
    ...x,
    key: x.factor,
    label: FACTOR_LABELS[x.factor] ?? x.factor,
  }));
  const positive = rows.filter((x) => (x.ic ?? 0) > 0).length;

  return (
    <PageContainer
      title="ETF 因子研究 · 单因子 IC 扫描"
      subTitle={data ? `窗口 ${data.window_start ?? "—"} → ${data.window_end ?? "—"} · ${data.days ?? 0} 个交易日 · ${rows.length} 个因子 · label ${data.label ?? "—"}` : "加载中…"}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          <Button icon={<ThunderboltOutlined />} onClick={() => run("etf_research")} loading={running === "etf_research"} disabled={!!running && running !== "etf_research"}>重跑 IC 扫描</Button>
          {running ? <Button danger icon={<StopOutlined />} loading={poll.cancelling} onClick={() => void poll.cancel()}>取消任务</Button> : null}
        </Space>
      }
    >

      <Card size="small" style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "8px 0 0" }}>
        <Table
          size="small"
          loading={loading}
          dataSource={rows}
          rowKey="key"
          pagination={false}
          locale={{ emptyText: <Empty description="暂无因子 IC 数据（点击右上角重跑 IC 扫描）" /> }}
          columns={[
            { title: "因子", dataIndex: "label", width: 150, render: (v, r) => <span><b>{v}</b> <Text type="secondary" style={{ fontSize: 11 }}>{r.factor}</Text></span> },
            { title: "IC", dataIndex: "ic", width: 170, sorter: (a, b) => (a.ic ?? -9) - (b.ic ?? -9), defaultSortOrder: "descend", render: (v) => <IcBar value={v} /> },
            { title: "RankIC", dataIndex: "rankIc", width: 90, render: (v) => <span style={{ color: (v ?? 0) > 0 ? "#0baa81" : (v ?? 0) < 0 ? "#e5524f" : "#8a94a0" }}>{fmtIc(v)}</span> },
            { title: "ICIR", dataIndex: "icir", width: 80, render: (v) => fmtIc(v) },
            { title: "IC 为正占比", dataIndex: "icPositive", width: 110, render: (v) => <span>{fmtShare(v)} <Tag color={(v ?? 0) >= 0.5 ? "green" : "default"} style={{ fontSize: 10, lineHeight: "16px" }}>{(v ?? 0) >= 0.5 ? "稳定" : "震荡"}</Tag></span> },
            { title: "有效天数", dataIndex: "days", width: 90, render: (v) => <span>{v ?? "—"} <Text type="secondary" style={{ fontSize: 11 }}>/ {data?.days ?? "—"}</Text></span> },
            { title: "IC 波动", dataIndex: "icStd", width: 80, render: (v) => fmtIc(v) },
          ]}
          summary={() => rows.length ? (
            <Table.Summary.Row>
              <Table.Summary.Cell index={0}><b>合计</b></Table.Summary.Cell>
              <Table.Summary.Cell index={1} colSpan={3}><Text type="secondary" style={{ fontSize: 12 }}>{rows.length} 个因子 · 正 IC {positive} 个 · 负 IC {rows.length - positive} 个</Text></Table.Summary.Cell>
              <Table.Summary.Cell index={5}><Progress percent={rows.length ? Math.round(positive / rows.length * 100) : 0} size="small" format={(p) => `${p}%`} /></Table.Summary.Cell>
              <Table.Summary.Cell index={6} />
            </Table.Summary.Row>
          ) : null}
        />
      </Card>

      <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
        口径：每因子对全池（top200 ETF）按日截面计算 forward_20 收益的 Spearman RankIC，聚合为窗口内均值（rankIc）、ICIR（均值/标准差）、IC 为正占比与有效天数。模型使用 11 因子组合（featuresOverride），此处为单因子诊断，用于监测因子有效性衰减；个别因子（如 ind_rs）覆盖率受限时样本天数会少于总窗口。
      </Paragraph>
    </PageContainer>
  );
}

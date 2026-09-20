import { useCallback, useEffect, useRef, useState } from "react";
import { App, Button, Card, Col, Empty, Row, Space, Table, Tag, Typography } from "antd";
import {
  ReloadOutlined,
  PlayCircleOutlined,
  StopOutlined,
  ThunderboltOutlined,
  SafetyCertificateOutlined,
  LineChartOutlined,
} from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { etfApi, engineApi, etfSimApi, type EtfWalkforwardDto, type EtfJobType, type SimStatusDto, type SimDailyDto } from "./api";
import { fmtNum, fmtPct } from "./format";
import { useJobPolling } from "./useJobPolling";

const { Text, Paragraph } = Typography;

/** 本页展示口径：百分比保留 1 位小数（净值曲线页信息密度高，2 位太挤）。 */
const fmtPct1 = (v: number | undefined | null) => fmtPct(v, 1);

function NavCurve({ daily }: { daily: EtfWalkforwardDto["daily"] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 260 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0].contentRect.width;
      if (w > 0) setSize({ w, h: 260 });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  if (!daily || daily.length < 2)
    return <Empty description="暂无净值曲线（先运行滚动评估）" style={{ padding: 40 }} />;
  const step = Math.max(1, Math.floor(daily.length / 220));
  const pts = daily.filter((_, i) => i % step === 0 || i === daily.length - 1);
  const base = daily[0];
  const navNorm = pts.map((d) => d.nav / base.nav);
  const benchNorm = pts.map((d) => d.benchmark / base.benchmark);
  const all = [...navNorm, ...benchNorm];
  const min = Math.min(...all), max = Math.max(...all);
  const pad = { l: 46, r: 12, t: 14, b: 26 };
  const w = Math.max(size.w - pad.l - pad.r, 120);
  const h = size.h - pad.t - pad.b;
  const X = (i: number) => pad.l + (i / (pts.length - 1)) * w;
  const Y = (v: number) => pad.t + (1 - (v - min) / (max - min || 1)) * h;
  const line = (arr: number[]) => arr.map((v, i) => `${i === 0 ? "M" : "L"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((g) => {
    const v = min + (max - min) * g;
    return (
      <g key={g}>
        <line x1={pad.l} x2={pad.l + w} y1={Y(v)} y2={Y(v)} stroke="rgba(0,0,0,0.06)" strokeWidth={1} />
        <text x={pad.l - 6} y={Y(v) + 3} fontSize={10} fill="#8a94a0" textAnchor="end">
          {v.toFixed(2)}
        </text>
      </g>
    );
  });
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={size.h} viewBox={`0 0 ${size.w} ${size.h}`} style={{ display: "block" }}>
        {gridLines}
        <line x1={pad.l} x2={pad.l + w} y1={pad.t + h} y2={pad.t + h} stroke="rgba(0,0,0,0.12)" />
        <path d={line(navNorm)} fill="none" stroke="#0baa81" strokeWidth={2} />
        <path d={line(benchNorm)} fill="none" stroke="#9aa7b5" strokeWidth={1.5} strokeDasharray="5 4" />
        <text x={pad.l + w - 4} y={pad.t + 12} fontSize={11} fill="#0baa81" textAnchor="end">策略 NAV</text>
        <text x={pad.l + w - 4} y={pad.t + 26} fontSize={11} fill="#9aa7b5" textAnchor="end">基准 沪深300</text>
        <text x={pad.l} y={pad.t + h + 16} fontSize={10} fill="#8a94a0">{pts[0].tradeDate}</text>
        <text x={pad.l + w} y={pad.t + h + 16} fontSize={10} fill="#8a94a0" textAnchor="end">{pts[pts.length - 1].tradeDate}</text>
      </svg>
    </div>
  );
}

const GREEN = "#0baa81", RED = "#e5524f", MUTED = "#5f6b76", LINE = "#e5eaee";

function YearBars({ daily }: { daily: EtfWalkforwardDto["daily"] }) {
  if (!daily || daily.length < 2) return <Empty description="暂无年度数据" style={{ padding: 40 }} />;
  const byYear = new Map<string, { nav0: number; nav1: number; b0: number; b1: number }>();
  for (const d of daily) {
    const y = String(d.tradeDate).slice(0, 4);
    const g = byYear.get(y);
    if (!g) byYear.set(y, { nav0: d.nav, nav1: d.nav, b0: d.benchmark, b1: d.benchmark });
    else { g.nav1 = d.nav; g.b1 = d.benchmark; }
  }
  const years = [...byYear.keys()].sort();
  const rows = years.map((y) => {
    const g = byYear.get(y)!;
    return { y, nav: g.nav1 / g.nav0 - 1, bench: g.b1 / g.b0 - 1 };
  });
  const W = 820, H = 230, pad = { l: 48, r: 12, t: 18, b: 28 };
  const all = rows.flatMap((r) => [r.nav, r.bench]);
  const min = Math.min(0, ...all), max = Math.max(0, ...all);
  const iw = (W - pad.l - pad.r) / rows.length;
  const Y = (v: number) => pad.t + (1 - (v - min) / (max - min || 1)) * (H - pad.t - pad.b);
  const zeroY = Y(0);
  const bw = iw * 0.26;
  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((g) => {
        const v = min + (max - min) * g;
        return (
          <g key={g}>
            <line x1={pad.l} x2={W - pad.r} y1={Y(v)} y2={Y(v)} stroke="rgba(0,0,0,0.06)" />
            <text x={pad.l - 6} y={Y(v) + 3} fontSize={10} fill="#8a94a0" textAnchor="end">{(v * 100).toFixed(0)}%</text>
          </g>
        );
      })}
      <line x1={pad.l} x2={W - pad.r} y1={zeroY} y2={zeroY} stroke="rgba(0,0,0,0.18)" />
      {rows.map((r, i) => {
        const cx = pad.l + iw * i + iw / 2;
        const hh = (H - pad.t - pad.b) / (max - min || 1);
        const draw = (v: number, color: string) => {
          const top = v >= 0 ? Y(v) : zeroY;
          const h = Math.abs(v) * hh;
          return <rect x={cx - bw} y={top} width={bw} height={Math.max(h, 1)} fill={color} rx={2} />;
        };
        return (
          <g key={r.y}>
            {draw(r.nav, r.nav >= 0 ? GREEN : RED)}
            {draw(r.bench, "#9aa7b5")}
            <text x={cx} y={zeroY + 14} fontSize={11} fill="#5f6b76" textAnchor="middle">{r.y}</text>
            <text x={cx + bw / 2} y={r.nav >= 0 ? Y(r.nav) - 4 : Y(r.nav) + 12} fontSize={10} fill={r.nav >= 0 ? GREEN : RED} textAnchor="middle">
              {(r.nav * 100).toFixed(0)}%
            </text>
          </g>
        );
      })}
      <text x={W - pad.r} y={pad.t + 10} fontSize={11} fill={GREEN} textAnchor="end">■ 策略</text>
      <text x={W - pad.r} y={pad.t + 24} fontSize={11} fill="#9aa7b5" textAnchor="end">■ 沪深300</text>
    </svg>
  );
}

function StatCard({ title, value, color, sub }: { title: string; value: string; color?: string; sub?: string }) {
  return (
    <div style={{ flex: "1 1 150px", minWidth: 140, background: "#fff", border: `1px solid ${LINE}`, borderRadius: 10, padding: "12px 14px" }}>
      <div style={{ fontSize: 12, color: MUTED }}>{title}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color ?? "#1a232c", marginTop: 4 }}>{value}</div>
      {sub ? <div style={{ fontSize: 11, color: MUTED, marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
}

export default function EtfQuantPage() {
  const { message } = App.useApp();
  const [wf, setWf] = useState<EtfWalkforwardDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState<EtfJobType | null>(null);
  const [simStatus, setSimStatus] = useState<SimStatusDto | null>(null);
  const [simItems, setSimItems] = useState<SimDailyDto[]>([]);

  const loadSim = useCallback(async () => {
    try {
      const st = await etfSimApi.status();
      setSimStatus(st);
      const hist = await etfSimApi.history(undefined, 6);
      setSimItems(hist.items ?? []);
    } catch {
      /* 模拟盘不可用时不阻塞页面 */
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await etfApi.walkforwardLatest();
      setWf(data);
    } catch (e) {
      message.error("获取 ETF 滚动评估失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    load();
    loadSim();
    return () => { poll.stop(); };
  }, [load, loadSim]);

  const poll = useJobPolling({
    fetchJob: etfApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 4000,
    onComplete: async () => { setRunning(null); message.success("运行完成，数据已刷新"); await load(); },
    onError: (m) => { setRunning(null); message.error("运行失败：" + m); },
    onCancelled: () => { setRunning(null); message.info("任务已取消"); },
    onPollError: () => { setRunning(null); },
    onTimeout: () => { setRunning(null); message.error("任务轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const run = async (type: EtfJobType) => {
    if (running) return;
    setRunning(type);
    try {
      const job = await etfApi.startJob(type);
      message.info(`${type === "etf_factors" ? "因子快照重建" : "滚动重训评估"}已启动，运行中…`);
      poll.watch(job);
    } catch (e) {
      setRunning(null);
      message.error("启动失败：" + String((e as Error).message ?? e));
    }
  };

  const m = wf?.metrics;
  const h = wf?.holdings;
  const bear = h?.bearRegime ?? false;
  const holdingsRows = (h?.holdings ?? []).map((code, i) => ({ key: i, code, weight: wf && wf.config && (wf.config as { topN?: number }).topN ? 1 / Number((wf.config as { topN?: number }).topN) : null }));

  return (
    <PageContainer
      title="ETF 智能投研 · 滚动评估"
      subTitle={
        <>
          LGBMRanker 11 因子 + 相对 label + Top5 + score_lin 加权 + ma20 空仓门控 · 样本外滚动 3.7 年 · {wf ? `${wf.windows} 窗口 ${wf.start_date} → ${wf.end_date}` : "—"}
        </>
      }
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => { load(); loadSim(); }} loading={loading}>刷新</Button>
          <Button icon={<ThunderboltOutlined />} onClick={() => run("etf_factors")} loading={running === "etf_factors"} disabled={!!running && running !== "etf_factors"}>重建因子快照</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => run("etf_walkforward")} loading={running === "etf_walkforward"} disabled={!!running && running !== "etf_walkforward"}>滚动重训评估</Button>
          {running ? <Button danger icon={<StopOutlined />} loading={poll.cancelling} onClick={() => void poll.cancel()}>取消任务</Button> : null}
        </Space>
      }
    >
      <Row gutter={[10, 10]}>
        <Col xs={12} sm={8} md={4}><StatCard title="样本外总收益" value={fmtPct1(m?.totalReturn)} color={GREEN} sub={`基准 ${fmtPct1(m?.benchmarkReturn)}`} /></Col>
        <Col xs={12} sm={8} md={4}><StatCard title="超额收益" value={fmtPct1(m?.excessReturn)} color={GREEN} /></Col>
        <Col xs={12} sm={8} md={4}><StatCard title="年化收益" value={fmtPct1(m?.annualReturn)} color={GREEN} /></Col>
        <Col xs={12} sm={8} md={4}><StatCard title="Sharpe" value={fmtNum(m?.sharpe)} color={GREEN} /></Col>
        <Col xs={12} sm={8} md={4}><StatCard title="最大回撤" value={fmtPct1(m?.maxDrawdown)} color={RED} /></Col>
        <Col xs={12} sm={8} md={4}><StatCard title="avgRankIC" value={fmtNum(m?.avgRankIc)} sub={`${m?.windows ?? "—"} 窗口`} /></Col>
      </Row>

      <Card size="small" style={{ border: `1px solid ${LINE}`, borderRadius: 10, marginTop: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <SafetyCertificateOutlined style={{ fontSize: 34, color: bear ? "#d9a13b" : GREEN }} />
          <div>
            <div style={{ fontSize: 15, fontWeight: 700 }}>
              {bear ? "空仓信号（熊市防御）" : "持仓信号"}
              <Tag color={bear ? "gold" : "green"} style={{ marginLeft: 10 }}>{bear ? "bearRegime" : "active"}</Tag>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              因子日 {h?.tradeDate ?? "—"} · 运行 {wf?.run_id?.slice(0, 24) ?? "—"} · label {wf?.label ?? "—"}
            </Text>
          </div>
        </div>
        {bear ? (
          <div style={{ marginTop: 12, padding: "10px 14px", background: "#fdf6e7", border: "1px solid #f0dfb8", borderRadius: 8, fontSize: 13 }}>
            当前处于 MA20 空仓区间（benchmark 低于 20 日均线），系统持有 0 只 ETF，仓位全部转入现金等价物。这是 3.7 年回测中主要的回撤控制来源。
          </div>
        ) : holdingsRows.length ? (
          <Table
            style={{ marginTop: 12 }}
            size="small"
            pagination={false}
            dataSource={holdingsRows}
            rowKey="key"
            columns={[
              { title: "ETF 代码", dataIndex: "code" },
              { title: "权重", dataIndex: "weight", render: (v) => v ? `${(v * 100).toFixed(1)}%` : "—" },
            ]}
          />
        ) : null}
      </Card>

      <Card
        size="small"
        style={{ border: `1px solid ${LINE}`, borderRadius: 10, marginTop: 14 }}
        title={<Space><SafetyCertificateOutlined /> 实盘模拟验证（按真实行情逐日推进）</Space>}
        extra={<Text type="secondary" style={{ fontSize: 11 }}>T+1 开盘成交 · 含佣金/印花税/滑点 · 每交易日推进</Text>}
      >
        {simStatus?.run ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Row gutter={[10, 10]}>
              <Col xs={12} sm={8} md={4}><StatCard title="模拟净值" value={fmtNum(simStatus.latest?.nav, 4)} color={GREEN} sub={`起始 ${fmtNum(simStatus.run.initial_capital)} 元`} /></Col>
              <Col xs={12} sm={8} md={4}><StatCard title="累计收益" value={fmtPct1(simStatus.totalReturn)} color={(simStatus.totalReturn ?? 0) >= 0 ? GREEN : RED} /></Col>
              <Col xs={12} sm={8} md={4}><StatCard title="持仓数" value={String(simStatus.latest?.positionCount ?? 0)} sub={simStatus.latest?.bearRegime ? "空仓防御中" : "持仓运行中"} /></Col>
              <Col xs={12} sm={8} md={4}><StatCard title="启动日" value={simStatus.run.start_date ?? "—"} /></Col>
              <Col xs={12} sm={8} md={4}><StatCard title="最新信号日" value={simStatus.run.last_signal_date ?? "—"} /></Col>
              <Col xs={12} sm={8} md={4}><StatCard title="运行状态" value={simStatus.run.status ?? "—"} color={simStatus.run.status === "active" ? GREEN : MUTED} /></Col>
            </Row>
            <div style={{ padding: "10px 14px", background: simStatus.latest?.bearRegime ? "#fdf6e7" : "#eef9f4", border: `1px solid ${simStatus.latest?.bearRegime ? "#f0dfb8" : "#cdeade"}`, borderRadius: 8, fontSize: 13 }}>
              {simStatus.latest?.bearRegime
                ? "当前处于 MA20 空仓区间：策略在真实市场下选择不持有（沪深300 低于 20 日均线）。空仓防御正是 2022 熊市能保住本金的原因——净值暂时持平，等信号转多后自动建仓、曲线开始积累。"
                : `当前持仓信号：${(simStatus.positions ?? []).map((p) => p.ts_code).join("、") || "等待调仓"}。模拟盘按真实行情逐日推进，这就是你正在积累的实盘验证成绩。`}
            </div>
            {simItems.length ? (
              <div>
                <div style={{ fontSize: 12, color: MUTED, marginBottom: 6 }}>最近推进记录：</div>
                {[...simItems].reverse().slice(-4).map((it, i) => (
                  <div key={i} style={{ fontSize: 12, color: "#3a444d", padding: "3px 0", borderBottom: `1px dashed ${LINE}` }}>
                    <b>{it.tradeDate}</b> · NAV {fmtNum(it.nav, 4)} · {it.positionCount} 只{it.bearRegime ? "（空仓防御）" : ""}{it.note ? ` · ${it.note}` : ""}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <Empty description="暂无运行中的模拟盘（可在 ETF 模拟盘页启动）" style={{ padding: 20 }} />
        )}
      </Card>

      <Card
        size="small"
        style={{ border: `1px solid ${LINE}`, borderRadius: 10, marginTop: 14 }}
        title={<Space><LineChartOutlined /> 样本外净值曲线（滚动拼接）</Space>}
        extra={<Text type="secondary" style={{ fontSize: 11 }}>策略 NAV vs 沪深300 基准 · 起点归一化 1.0</Text>}
      >
        <NavCurve daily={wf?.daily ?? []} />
      </Card>

      <Card
        size="small"
        style={{ border: `1px solid ${LINE}`, borderRadius: 10, marginTop: 14 }}
        title={<Space><LineChartOutlined /> 历年收益（样本外滚动，策略 vs 沪深300）</Space>}
        extra={<Text type="secondary" style={{ fontSize: 11 }}>逐年累计，2022 年完整覆盖熊市</Text>}
      >
        <YearBars daily={wf?.daily ?? []} />
      </Card>

      <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 14, marginBottom: 0 }}>
        数据口径：正式 etf_walkforward 任务（etf-quant.db），25 窗口滚动（训练 200 天 → 验证 40 天 → 前推 40 天），含换手成本。早期窗口（2021-2024）池内 ETF 供给较少，选股 alpha 集中于 2025 后；全程超额 +120%，Sharpe 2.01，空仓语义已在正式链路验证。模拟盘从 2026-07-20 启动，按真实行情逐日推进。
      </Paragraph>
    </PageContainer>
  );
}

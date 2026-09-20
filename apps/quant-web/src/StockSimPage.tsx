import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, Button, Card, Empty, InputNumber, Popconfirm, Radio, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import { AccountBookOutlined, PlayCircleOutlined, ReloadOutlined, RocketOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { stockSimApi, stockTechnicalApi, dataApi, type StockSimDailyDto, type StockSimRunListItemDto, type StockSimStatusDto, type StockTechnicalDto } from "./api";
import { flatPositionHint, isFrozenRun, pickDefaultRun, runLabel, runStateSuffix } from "./stockSimRun";
import { fmtMoney, fmtNum, fmtPct, quoteUrl } from "./format";

const { Text, Paragraph } = Typography;

/** 后端模型注册表（config/factor-models.yaml 的 models 段）的展示名回退。
 *  页面优先用 9103 /selection/models 返回的 label，这里只做离线兜底。
 *  legacy 因子模型已于 2026-09-18 退役，配置里只剩 ml_walkforward；
 *  更早的假模型 ml_technical_timing / ml_technical_timing_v5 曾经被前端硬编码提供过，
 *  选中后会建出"净值恒 1.0"的死账本——现已移除，且后端也会拒绝未登记模型。 */
const MODEL_LABELS: Record<string, string> = {
  ml_walkforward: "ML多因子选股",
};
const DEFAULT_MODEL = "ml_walkforward";

const SERIES_COLORS = ["#1677ff", "#fa8c16", "#13c2c2", "#722ed1"];

function NavCurve({ series }: { series: { name: string; color: string; items: StockSimDailyDto[] }[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((es) => { if (es[0].contentRect.width > 0) setW(es[0].contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const plots = series
    .map((s) => ({ ...s, pts: s.items.filter((x) => x.tradeDate !== "INIT") }))
    .filter((s) => s.pts.length >= 2);
  if (plots.length === 0) return <Empty description="暂无净值序列（首次 T+1 调仓后生成）" style={{ padding: 30 }} />;
  const H = 260, pad = { l: 52, r: 14, t: 18, b: 28 };
  const allNav = plots.flatMap((s) => s.pts.map((x) => x.nav));
  const min = Math.min(...allNav, 1), max = Math.max(...allNav, 1), span = (max - min) || 1;
  const pw = Math.max(w - pad.l - pad.r, 120), ph = H - pad.t - pad.b;
  const allDates = [...new Set(plots.flatMap((s) => s.pts.map((x) => x.tradeDate)))].sort();
  const X = (i: number, n: number) => pad.l + (i / (n - 1)) * pw;
  const Y = (v: number) => pad.t + (1 - (v - min) / span) * ph;
  const base = Y(1);
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={H} viewBox={`0 0 ${Math.max(w, 160)} ${H}`} style={{ display: "block" }}>
        {[0, 0.5, 1].map((g) => { const v = min + span * g; return (
          <g key={g}><line x1={pad.l} x2={pad.l + pw} y1={Y(v)} y2={Y(v)} stroke="rgba(0,0,0,0.06)" />
          <text x={pad.l - 6} y={Y(v) + 3} fontSize={10} fill="#8a94a0" textAnchor="end">{fmtNum(v, 3)}</text></g>); })}
        <line x1={pad.l} x2={pad.l + pw} y1={base} y2={base} stroke="#d9dee3" strokeDasharray="3 3" />
        {plots.map((s, si) => {
          const line = s.pts.map((x, i) => `${i ? "L" : "M"}${X(i, s.pts.length).toFixed(1)},${Y(x.nav).toFixed(1)}`).join(" ");
          return <path key={si} d={line} fill="none" stroke={s.color} strokeWidth={2} />;
        })}
        <text x={pad.l} y={pad.t + ph + 18} fontSize={10} fill="#8a94a0">{allDates[0]}</text>
        <text x={pad.l + pw} y={pad.t + ph + 18} fontSize={10} fill="#8a94a0" textAnchor="end">{allDates[allDates.length - 1]}</text>
      </svg>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 6 }}>
        {plots.map((s, i) => (
          <span key={i} style={{ fontSize: 12, color: "#5f6b76" }}>
            <span style={{ display: "inline-block", width: 10, height: 2, background: s.color, verticalAlign: "middle", marginRight: 5 }} />
            {s.name} · 末值 {fmtNum(s.pts[s.pts.length - 1].nav, 4)}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function StockSimPage() {
  const { message } = App.useApp();
  const [model, setModel] = useState(DEFAULT_MODEL);
  // 模型清单来自后端真实注册表（9103 /selection/models），不再前端硬编码，
  // 避免再次出现"页面能选、后端不认"的模型。
  const [modelOptions, setModelOptions] = useState<Array<{ id: string; label: string }>>([
    { id: DEFAULT_MODEL, label: MODEL_LABELS[DEFAULT_MODEL] },
  ]);
  const [topN, setTopN] = useState(5);
  const [regime, setRegime] = useState(false);
  const [runs, setRuns] = useState<StockSimRunListItemDto[]>([]);
  const [currentRun, setCurrentRun] = useState<string | null>(null);
  const [compareRun, setCompareRun] = useState<string | null>(null);
  const [data, setData] = useState<StockSimStatusDto | null>(null);
  const [history, setHistory] = useState<StockSimDailyDto[]>([]);
  const [compareHistory, setCompareHistory] = useState<StockSimDailyDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [advancing, setAdvancing] = useState(false);

  // 技术指标数据
  const [technicalMap, setTechnicalMap] = useState<Map<string, StockTechnicalDto>>(new Map());
  const [technicalLoading, setTechnicalLoading] = useState(false);

  const load = useCallback(async (runId?: string | null) => {
    try {
      const rr = await stockSimApi.runs();
      setRuns(rr.items);
      const target = runId ?? pickDefaultRun(rr.items)?.runId ?? null;
      setCurrentRun(target);
      const st = await stockSimApi.status(target ?? undefined);
      setData(st);
      const h = await stockSimApi.history(target ?? undefined);
      setHistory(h.items);
      if (!runId && st.run) {
        setModel(st.run.model_id);
        setTopN(st.run.top_n);
        setRegime(!!st.run.regime_filter);
      }
      // 加载持仓股票的技术指标
      if (st.positions && st.positions.length > 0) {
        setTechnicalLoading(true);
        try {
          const codes = st.positions.map((p) => p.code);
          const techR = await stockTechnicalApi.batch(codes);
          const map = new Map<string, StockTechnicalDto>();
          techR.items.forEach((t) => map.set(t.code, t));
          setTechnicalMap(map);
        } catch (techErr) {
          console.error("加载技术指标失败：", techErr);
          setTechnicalMap(new Map());
        } finally {
          setTechnicalLoading(false);
        }
      } else {
        setTechnicalMap(new Map());
      }
    } catch (e) {
      message.error("读取股票模拟盘失败：" + String((e as Error).message ?? e));
    } finally { setLoading(false); }
  }, [message]);

  useEffect(() => { void load(); }, [load]);

  // 拉取后端模型注册表；失败时保留内置回退项，不阻塞页面。
  useEffect(() => {
    dataApi.selectionModels()
      .then((r) => {
        const items = (r.items ?? []).map((m) => ({ id: m.id, label: m.label || MODEL_LABELS[m.id] || m.id }));
        if (items.length) setModelOptions(items);
      })
      .catch(() => undefined);
  }, []);

  const modelLabel = useCallback(
    (id: string | null | undefined) => {
      if (!id) return "—";
      return modelOptions.find((m) => m.id === id)?.label ?? MODEL_LABELS[id] ?? id;
    },
    [modelOptions],
  );

  const switchModel = (m: string) => {
    setModel(m);
  };

  const selectRun = async (rid: string) => {
    setLoading(true);
    setCompareRun(null);
    await load(rid);
  };

  const start = async () => {
    setStarting(true);
    try {
      const r = await stockSimApi.start(model, topN, 1_000_000, regime);
      message.success(`模拟盘已建立（${r.runId}）：信号 ${r.signalDate}，目标 ${r.targetCount} 只，${r.regimeFilter ? "已开启熊市择时" : "未择时"}，等待 T+1 建仓`);
      await load(r.runId);
    } catch (e) { message.error("建立失败：" + String((e as Error).message ?? e)); }
    finally { setStarting(false); }
  };

  const advance = async () => {
    const rid = data?.run?.run_id ?? currentRun;
    if (!rid) { message.warning("请先建立模拟盘"); return; }
    setAdvancing(true);
    try {
      const r = await stockSimApi.advance(rid);
      if (!r.ok) { message.info(r.message ?? "暂无可推进的信号"); return; }
      message.success(`T+1 ${r.tradeDate} 调仓完成：${r.bearRegime ? "熊市空仓清仓 " + (r.trades?.sold.length ?? 0) + " 只" : `卖 ${r.trades?.sold.length ?? 0} / 买 ${r.trades?.bought.length ?? 0}`}，持仓 ${r.positions?.length ?? 0} 只，净值 ${fmtNum(r.nav, 4)}`);
      await load(rid);
    } catch (e) { message.error("推进失败：" + String((e as Error).message ?? e)); }
    finally { setAdvancing(false); }
  };

  const run = data?.run;
  const latest = data?.latest;

  const rows = (data?.positions ?? []).map((p, i) => {
    const mv = p.shares * p.lastPrice, cost = p.shares * p.avgCost;
    const highPrice = p.highPrice ?? p.avgCost;
    const stopPrice = Math.max(p.avgCost * 0.95, highPrice * 0.92);
    const tech = technicalMap.get(p.code);
    const stopDistance = p.lastPrice > 0 ? (p.lastPrice - stopPrice) / p.lastPrice : 0;
    return {
      key: i, ...p, highPrice, stopPrice, mv,
      profit: mv - cost,
      profitPct: cost > 0 ? (mv - cost) / cost : 0,
      technical: tech,
      stopDistance,
    };
  });

  const currentInfo = runs.find((r) => r.runId === currentRun);
  const series = [{ name: `${modelLabel(run?.model_id)}${run?.regime_filter ? "·择时" : ""}`, color: SERIES_COLORS[0], items: history }];
  if (compareRun && compareHistory.length >= 2) {
    const cr = runs.find((r) => r.runId === compareRun);
    series.push({ name: `${modelLabel(cr?.modelId)}${cr?.regimeFilter ? "·择时" : ""}`, color: SERIES_COLORS[1], items: compareHistory });
  }
  const runOptions = runs.map((r) => ({
    value: r.runId,
    label: `${runLabel(r)} · Top${r.topN}${r.regimeFilter ? " · 择时" : ""}${runStateSuffix(r)}`,
  }));

  // 旧口径的账本后端拒绝推进（advance_sim），前端提前禁用并说明，而不是让用户点一下吃报错。
  const frozen = isFrozenRun(data?.run ?? currentInfo);

  // V5策略信号渲染
  const renderV5Signal = (tech?: StockTechnicalDto) => {
    if (technicalLoading) return <Tag icon={<span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "#d9d9d9", animation: "pulse 1s infinite" }} />}>加载中</Tag>;
    if (!tech || tech.error) return <Tag style={{ color: "#8c8c8c" }}>—</Tag>;
    if (tech.v5SellSignal || tech.deathCross) return <Tag color="red">死叉卖出</Tag>;
    if (tech.v5BuySignal) return <Tag color="green">金叉买入</Tag>;
    if (tech.trendUp && tech.ma5AboveMa10) return <Tag color="blue">趋势持有</Tag>;
    if (!tech.trendUp) return <Tag color="orange">跌破MA20</Tag>;
    return <Tag>等待信号</Tag>;
  };

  return (
    <PageContainer
      title="股票 T+1 模拟盘 · 真实选股信号跟踪"
      subTitle="T+1 开盘成交 · 佣金万3（双边）· 印花税千0.5（仅卖出）· 100 股整手 · 最小换手等权"
      extra={
        <Space wrap>
          {/* 只剩一个模型时不必摆一组单选项，用 Tag 表明"新建的账会用哪个模型"即可 */}
          {modelOptions.length > 1 ? (
            <Radio.Group value={model} onChange={(e) => switchModel(e.target.value)} optionType="button" size="small"
              options={modelOptions.map((m) => ({ value: m.id, label: m.label }))} />
          ) : (
            <Tag color="purple">{modelOptions[0]?.label ?? MODEL_LABELS[DEFAULT_MODEL]}</Tag>
          )}
          <span style={{ fontSize: 12, color: "#5f6b76" }}>TopN</span>
          <InputNumber value={topN} min={1} max={300} step={5} style={{ width: 90 }} onChange={(v) => setTopN(Number(v) || 5)} />
          <span style={{ fontSize: 12, color: "#5f6b76" }}>熊市择时</span>
          <Switch size="small" checked={regime} onChange={setRegime} title="沪深300 收于 MA20 下方时信号日清仓空仓" />
          <Popconfirm title="建立新的股票模拟盘？（现有 run 保留，可切换/对比）" onConfirm={start}>
            <Button type="primary" icon={<RocketOutlined />} loading={starting}>开始模拟</Button>
          </Popconfirm>
          <Tooltip title={frozen ? "该 run 使用旧 T+1 口径，后端已停止推进；请点「开始模拟」新建一本 same_day_close 的账" : undefined}>
            {/* 禁用态按钮不冒泡事件，Tooltip 需要包一层 span 才能触发 */}
            <span>
              <Button icon={<PlayCircleOutlined />} loading={advancing} disabled={!run || frozen} onClick={advance}>推进一期</Button>
            </span>
          </Tooltip>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => load(currentRun)}>刷新</Button>
        </Space>
      }
    >
      {runs.length > 0 && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: "#5f6b76" }}>当前 run</span>
          <Select size="small" style={{ width: 320 }} value={currentRun ?? undefined} options={runOptions} onChange={selectRun} />
          <span style={{ fontSize: 12, color: "#5f6b76" }}>对比 run</span>
          <Select size="small" style={{ width: 320 }} allowClear placeholder="选择后同框对比净值" value={compareRun ?? undefined}
            options={runOptions.filter((r) => r.value !== currentRun)}
            onChange={async (v) => {
              setCompareRun(v ?? null);
              if (v) { const h = await stockSimApi.history(v); setCompareHistory(h.items); } else setCompareHistory([]);
            }} />
        </div>
      )}

      {frozen ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 14 }}
          message="当前 run 已冻结：旧 T+1 口径，后端不再推进"
          description="该账本（execution_mode≠same_day_close）保留历史净值可供查看与对比，但「推进一期」已被禁用。要继续逐日跟踪请点右上角「开始模拟」新建一本 same_day_close 的账。"
        />
      ) : null}

      {/* 已冻结时上面那条 Alert 已经解释过原因，这里不再重复 */}
      {!frozen && flatPositionHint(currentInfo, (data?.latest as { note?: string } | undefined)?.note) && (
        <Alert type="info" showIcon style={{ marginTop: 14 }} message="为什么看到 0 持仓 / 净值 1.0000？"
          description={flatPositionHint(currentInfo, (data?.latest as { note?: string } | undefined)?.note) ?? undefined} />
      )}

      {!run ? (
        <Card style={{ border: "1px solid #e5eaee", borderRadius: 10, marginTop: 14 }}>
          <Empty description="尚未建立股票模拟盘。选择模型与 TopN 后点「开始模拟」；之后每日链自动更新信号，日线到位后点「推进一期」按 T+1 开盘调仓。（V5 技术面择时的回测数字见「策略回测 → V5策略回测」页签）" style={{ padding: 36 }} />
        </Card>
      ) : (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 14 }}>
            {[
              { t: "初始资金", v: fmtMoney(run.initial_capital) },
              { t: "当前净值", v: fmtNum(latest?.nav ?? run.last_nav, 4), c: "#1677ff" },
              { t: "累计收益", v: fmtPct(data?.totalReturn), c: (data?.totalReturn ?? 0) >= 0 ? "#0baa81" : "#e5524f" },
              { t: "持仓数量", v: `${rows.length} 只` },
              { t: "现金", v: fmtMoney(latest?.cash) },
              { t: "持仓市值", v: fmtMoney(latest?.marketValue) },
              { t: "上次信号", v: run.last_signal_date ?? "未调仓" },
            ].map((x) => (
              <div key={x.t} style={{ flex: "1 1 130px", minWidth: 120, background: "#fff", border: "1px solid #e5eaee", borderRadius: 10, padding: "12px 14px" }}>
                <div style={{ fontSize: 12, color: "#5f6b76" }}>{x.t}</div>
                <div style={{ fontSize: 19, fontWeight: 700, color: x.c ?? "#1a232c", marginTop: 4 }}>{x.v}</div>
              </div>
            ))}
          </div>

          <Card size="small" title={<Space><AccountBookOutlined />当前持仓</Space>}
            extra={latest?.note ? <Tag>{latest.note}</Tag> : null}
            style={{ border: "1px solid #e5eaee", borderRadius: 10, marginTop: 14 }} bodyStyle={{ padding: "8px 0 0" }}>
            <Table size="small" rowKey="key" dataSource={rows} pagination={false}
              locale={{ emptyText: <Empty description="尚未建仓（等待首个 T+1 交易日开盘，或处于熊市空仓）" /> }}
              scroll={{ x: 1200 }}
              columns={[
                { title: "代码", dataIndex: "code", width: 110, fixed: "left" as const, render: (v) => <a href={quoteUrl(v)} target="_blank" rel="noreferrer" style={{ color: "#1677ff", fontFamily: "monospace" }}>{v}</a> },
                { title: "份额", dataIndex: "shares", width: 80, align: "right" as const, render: (v) => Number(v).toLocaleString() },
                { title: "成本价", dataIndex: "avgCost", width: 80, align: "right" as const, render: (v) => fmtNum(v, 3) },
                { title: "最新价", dataIndex: "lastPrice", width: 80, align: "right" as const, render: (v) => fmtNum(v, 3) },
                // 移动止损是引擎级规则（start_sim 对每个 run 都写入 initial_stop_loss/trailing_drawdown），
                // 不再按"V5 模型"条件显示。
                { title: "最高价", dataIndex: "highPrice", width: 80, align: "right" as const, render: (v: number) => fmtNum(v, 3) },
                { title: "止损价", dataIndex: "stopPrice", width: 80, align: "right" as const, render: (v: number) => <span style={{ color: "#e5524f", fontWeight: 600 }}>{fmtNum(v, 3)}</span> },
                { title: "距止损", dataIndex: "stopDistance", width: 80, align: "right" as const, render: (v: number) => <span style={{ color: v < 0.02 ? "#e5524f" : v < 0.05 ? "#fa8c16" : "#0baa81", fontWeight: 600 }}>{fmtPct(v)}</span> },
                { title: "MA5", width: 70, align: "right" as const, render: (_: unknown, r: typeof rows[number]) => r.technical ? <span style={{ color: r.technical.ma5AboveMa10 ? "#0baa81" : "#e5524f", fontWeight: 600 }}>{fmtNum(r.technical.ma5, 2)}</span> : "—" },
                { title: "MA10", width: 70, align: "right" as const, render: (_: unknown, r: typeof rows[number]) => r.technical ? fmtNum(r.technical.ma10, 2) : "—" },
                { title: "MA20", width: 70, align: "right" as const, render: (_: unknown, r: typeof rows[number]) => r.technical ? <span style={{ color: r.technical.closeAboveMa20 ? "#0baa81" : "#e5524f", fontWeight: 600 }}>{fmtNum(r.technical.ma20, 2)}</span> : "—" },
                { title: "策略信号", width: 100, align: "center" as const, render: (_: unknown, r: typeof rows[number]) => renderV5Signal(r.technical) },
                { title: "市值", dataIndex: "mv", width: 100, align: "right" as const, render: (v) => fmtMoney(v) },
                { title: "浮动盈亏", dataIndex: "profit", width: 140, align: "right" as const, fixed: "right" as const, render: (_: unknown, r: typeof rows[number]) => <span style={{ color: r.profitPct >= 0 ? "#0baa81" : "#e5524f" }}>{fmtMoney(r.profit)}（{fmtPct(r.profitPct)}）</span> },
              ]} />
          </Card>

          <Card size="small" title={<Space><AccountBookOutlined />模拟盘净值</Space>}
            extra={<Text type="secondary" style={{ fontSize: 11 }}>{currentInfo ? `${modelLabel(currentInfo.modelId)} · Top${currentInfo.topN}${currentInfo.regimeFilter ? " · 择时" : ""} · ${currentInfo.runId.slice(-12)}` : run.run_id.slice(-12)}</Text>}
            style={{ border: "1px solid #e5eaee", borderRadius: 10, marginTop: 14 }}>
            <NavCurve series={series} />
          </Card>

          <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 14, marginBottom: 0 }}>
            执行口径：信号来自所选选股模型（每日链自动重建）；信号日之后首个共同交易日开盘价成交（T+1，杜绝未来函数）；买入佣金万3、卖出佣金万3 + 印花税千0.5；100 股整手向下取整；先卖退出标的、再按可用现金等权买入新进标的（保留共有持仓，最小换手），T+1 收盘估值。持仓期间跟踪最高价，跌破 max(买入价×0.95, 最高价×0.92) 触发移动止损。
          </Paragraph>
        </>
      )}
    </PageContainer>
  );
}

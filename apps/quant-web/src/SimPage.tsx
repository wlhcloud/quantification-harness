import { useCallback, useEffect, useRef, useState } from "react";
import { App, Button, Card, Empty, InputNumber, Popconfirm, Space, Table, Tag, Typography } from "antd";
import { AccountBookOutlined, PlayCircleOutlined, ReloadOutlined, RocketOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { etfSimApi, type SimDailyDto, type SimStatusDto } from "./api";
import { fmtMoney, fmtNum, fmtPct } from "./format";

const { Text, Paragraph } = Typography;

function NavCurve({ items }: { items: SimDailyDto[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0].contentRect.width;
      if (width > 0) setW(width);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  if (items.length < 2) return <Empty description="暂无净值序列（开始模拟并推进后生成）" style={{ padding: 36 }} />;
  const H = 240, pad = { l: 52, r: 14, t: 16, b: 26 };
  const navs = items.map((x) => x.nav);
  const min = Math.min(...navs), max = Math.max(...navs), span = (max - min) || 1;
  const plotW = Math.max(w - pad.l - pad.r, 120), plotH = H - pad.t - pad.b;
  const X = (i: number) => pad.l + (i / (items.length - 1)) * plotW;
  const Y = (v: number) => pad.t + (1 - (v - min) / span) * plotH;
  const line = navs.map((v, i) => `${i === 0 ? "M" : "L"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${X(items.length - 1).toFixed(1)},${pad.t + plotH} L${X(0).toFixed(1)},${pad.t + plotH} Z`;
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={H} viewBox={`0 0 ${Math.max(w, 160)} ${H}`} style={{ display: "block" }}>
        {[0, 0.5, 1].map((g) => {
          const v = min + span * g;
          return (
            <g key={g}>
              <line x1={pad.l} x2={pad.l + plotW} y1={Y(v)} y2={Y(v)} stroke="rgba(0,0,0,0.06)" />
              <text x={pad.l - 6} y={Y(v) + 3} fontSize={10} fill="#8a94a0" textAnchor="end">{fmtNum(v, 3)}</text>
            </g>
          );
        })}
        <path d={area} fill="rgba(11,170,129,0.10)" />
        <path d={line} fill="none" stroke="#0baa81" strokeWidth={2} />
        <text x={pad.l} y={pad.t + plotH + 16} fontSize={10} fill="#8a94a0">{items[0].tradeDate}</text>
        <text x={pad.l + plotW} y={pad.t + plotH + 16} fontSize={10} fill="#8a94a0" textAnchor="end">{items[items.length - 1].tradeDate}</text>
      </svg>
    </div>
  );
}

export default function SimPage() {
  const { message } = App.useApp();
  const [data, setData] = useState<SimStatusDto | null>(null);
  const [history, setHistory] = useState<SimDailyDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [capital, setCapital] = useState(1_000_000);

  const load = useCallback(async () => {
    try {
      const [st, h] = await Promise.all([etfSimApi.status(), etfSimApi.history()]);
      setData(st);
      setHistory(h.items);
    } catch (e) {
      message.error("读取模拟盘失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const start = async () => {
    setStarting(true);
    try {
      const st = await etfSimApi.start(capital);
      message.success(`模拟盘已建立（run ${st.runId}${st.signalDate ? ` · 信号日 ${st.signalDate}` : ""}），初始资金 ${capital.toLocaleString()} 元`);
      await load();
    } catch (e) {
      message.error("建立模拟盘失败：" + String((e as Error).message ?? e));
    } finally {
      setStarting(false);
    }
  };

  const advance = async () => {
    const runId = data?.run?.run_id;
    if (!runId) { message.warning("请先建立模拟盘"); return; }
    setAdvancing(true);
    try {
      const r = await etfSimApi.advance(runId);
      // ok=false 有语义之分：waiting 表示"信号未更新"（后端不返回 error），
      // 原先一律报「推进失败」是误报。
      if (r.ok === false) {
        if (r.waiting) message.info(r.message ?? "信号未更新，等待下一期");
        else message.error(r.error ?? r.message ?? "推进失败");
        return;
      }
      message.success(`已按最新信号推进：${r.bearRegime ? "空仓防御（清仓转现金）" : `买入 ${r.positions.length} 只`} · 净值 ${fmtNum(r.nav, 4)}`);
      await load();
    } catch (e) {
      message.error("推进失败：" + String((e as Error).message ?? e));
    } finally {
      setAdvancing(false);
    }
  };

  const run = data?.run;
  const latest = data?.latest;
  const positions = data?.positions ?? [];
  const rows = positions.map((p, i) => {
    const mv = p.shares * p.last_price;
    const cost = p.shares * p.avg_cost;
    return { key: i, ...p, mv, profit: mv - cost, profitPct: cost > 0 ? (mv - cost) / cost : 0 };
  });

  return (
    <PageContainer
      title="ETF 模拟盘 · 真实信号跟踪"
      subTitle="T+1 开盘价成交 · 佣金万2 · ETF 免印花税 · 空仓防御时全现金"
      extra={
        <Space wrap>
          <InputNumber
            value={capital} min={10000} step={100000} style={{ width: 160 }}
            formatter={(v) => `${Number(v ?? 0).toLocaleString()}`} parser={(s) => Number((s ?? "").replace(/,/g, ""))}
            onChange={(v) => setCapital(Number(v) || 1_000_000)}
            disabled={!!run} addonBefore="初始资金"
          />
          <Popconfirm title="建立新的模拟盘？" description={run ? "已有模拟盘，新建会并存，不影响旧账本。" : "以当前 walkforward 信号为起点开始跟踪。"} onConfirm={start}>
            <Button type="primary" icon={<RocketOutlined />} loading={starting} disabled={!!run && !data?.run}>开始模拟</Button>
          </Popconfirm>
          <Button icon={<PlayCircleOutlined />} loading={advancing} disabled={!run} onClick={advance}>推进一天（按最新信号调仓）</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        </Space>
      }
    >

      {!run ? (
        <Card style={{ border: "1px solid #e5eaee", borderRadius: 10 }}>
          <Empty description={<span>尚未建立模拟盘。点击「开始模拟」以最新 walkforward 信号（<Text type="secondary">当前：空仓防御</Text>）为起点，建立 etf_sim 台账。</span>} style={{ padding: 40 }} />
        </Card>
      ) : (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {[
              { t: "初始资金", v: fmtMoney(run.initial_capital) },
              { t: "当前净值", v: fmtNum(latest?.nav ?? run.last_nav, 4), c: "#0baa81" },
              { t: "累计收益", v: fmtPct(data?.totalReturn), c: (data?.totalReturn ?? 0) >= 0 ? "#0baa81" : "#e5524f" },
              { t: "持仓数量", v: `${positions.length} 只` },
              { t: "现金", v: fmtMoney(latest?.cash ?? run.initial_capital) },
              { t: "持仓市值", v: fmtMoney(latest?.marketValue ?? 0) },
            ].map((x) => (
              <div key={x.t} style={{ flex: "1 1 150px", minWidth: 140, background: "#fff", border: "1px solid #e5eaee", borderRadius: 10, padding: "12px 14px" }}>
                <div style={{ fontSize: 12, color: "#5f6b76" }}>{x.t}</div>
                <div style={{ fontSize: 20, fontWeight: 700, color: x.c ?? "#1a232c", marginTop: 4 }}>{x.v}</div>
              </div>
            ))}
          </div>

          <Card size="small" title={<Space><AccountBookOutlined /> 当前持仓</Space>} extra={<span><Tag color={latest?.bearRegime ? "gold" : "green"}>{latest?.bearRegime ? "空仓防御" : "持仓中"}</Tag> 信号日 {latest?.note ?? "—"}</span>}
            style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "8px 0 0" }}>
            <Table
              size="small" rowKey="key" dataSource={rows} pagination={false}
              locale={{ emptyText: <Empty description="当前无持仓（空仓防御，资金全现金）" /> }}
              columns={[
                { title: "ETF 代码", dataIndex: "ts_code" },
                { title: "份额", dataIndex: "shares", render: (v) => Number(v).toLocaleString() },
                { title: "成本价", dataIndex: "avg_cost", render: (v) => fmtNum(v, 4) },
                { title: "最新价", dataIndex: "last_price", render: (v) => fmtNum(v, 4) },
                { title: "市值", dataIndex: "mv", render: (v) => fmtMoney(v) },
                { title: "浮动盈亏", dataIndex: "profit", render: (v, r) => <span style={{ color: (r.profitPct ?? 0) >= 0 ? "#0baa81" : "#e5524f" }}>{fmtMoney(v)}（{fmtPct(r.profitPct)}）</span> },
              ]}
            />
          </Card>

          <Card size="small" title={<Space><AccountBookOutlined /> 模拟盘净值</Space>} extra={<Text type="secondary" style={{ fontSize: 11 }}>run {run.run_id.slice(-12)} · 起点 {run.start_date}</Text>}
            style={{ border: "1px solid #e5eaee", borderRadius: 10 }}>
            <NavCurve items={history} />
          </Card>

          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            执行口径：信号日（walkforward 因子日）之后首个交易日的<b>开盘价</b>成交（T+1，验证 gap/滑点）；佣金 0.02%（万2），ETF 免印花税与过户费；目标持仓按 Top5 等权；bearRegime 时空仓转现金。每日链 18:30 更新后点「推进一天」即同步最新信号；连续运行 2-3 个月误差可控后，即具备接真实账户的前置条件。
          </Paragraph>
        </>
      )}
    </PageContainer>
  );
}

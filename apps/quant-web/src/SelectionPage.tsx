import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, App, Badge, Button, Card, Empty, Radio, Select, Space, Statistic, Table, Tag, Typography } from "antd";
import { ReloadOutlined, ThunderboltOutlined, FundOutlined, AccountBookOutlined, RocketOutlined, CheckCircleOutlined, MinusCircleOutlined, ArrowUpOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { dataApi, stockSimApi, stockTechnicalApi, type SelectionItemDto, type SelectionModelDto, type StockSimStatusDto, type StockSimRunListItemDto, type StockSimPositionDto, type StockTechnicalDto } from "./api";
import { flatPositionHint, isFrozenRun, pickDefaultRun, runLabel, runStateSuffix } from "./stockSimRun";
import { fmtMoney, fmtNum, fmtPct, quoteUrl } from "./format";

const { Text, Paragraph } = Typography;

/** 股票选股现只有 ML 一个模型（legacy 因子选股已于 2026-09-18 退役，见 config/factor-models.yaml）。
 *  ML 的 score 是原始排序分（可达数百），所以不再套用 legacy 的 0-100 分位阈值着色。 */
const ML_MODEL = "ml_walkforward";
const ML_SCORE_COLOR = "#722ed1";


type FilterType = "all" | "top10" | "held" | "v5_buy";

interface EnrichedCandidate {
  rank: number;
  code: string;
  name: string | null;
  industry: string | null;
  score: number;
  reasons: Array<{ factor: string; label: string; score: number; contribution: number }>;
  isHeld: boolean;
  position?: StockSimPositionDto;
  positionPnl?: number;
  positionPnlPct?: number;
  v5Signal: "buy" | "watch" | "unknown";
  v5SignalText: string;
  technical?: StockTechnicalDto;
}

export default function SelectionPage({ onNavigate }: { onNavigate: (path: string) => void }) {
  const { message } = App.useApp();
  // 候选来自 9103 /selection（读平面），而不是引擎的 /stock-ml/candidates：
  // 前者能带出证券名称与行业，且模型清单由后端注册表驱动。
  const [modelId, setModelId] = useState(ML_MODEL);
  const [modelOptions, setModelOptions] = useState<SelectionModelDto[]>([]);
  const [mlData, setMlData] = useState<{ items: SelectionItemDto[]; count: number; tradeDate?: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterType>("all");

  // 模拟盘数据
  const [simRuns, setSimRuns] = useState<StockSimRunListItemDto[]>([]);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [simStatus, setSimStatus] = useState<StockSimStatusDto | null>(null);
  const [simLoading, setSimLoading] = useState(true);

  // 技术指标数据
  const [technicalMap, setTechnicalMap] = useState<Map<string, StockTechnicalDto>>(new Map());
  const [technicalLoading, setTechnicalLoading] = useState(false);

  /** 只按 modelId 拉取；切换模型由下面的 effect 统一触发，避免"改 state + 手动再调一次"的重复请求。 */
  const loadMlCandidates = useCallback(async (model: string) => {
    setLoading(true);
    try {
      const r = await dataApi.selection(model, 50);
      setMlData({ items: r.items, count: r.count, tradeDate: r.items[0]?.tradeDate });
      // 加载技术指标
      if (r.items && r.items.length > 0) {
        setTechnicalLoading(true);
        try {
          const codes = r.items.map((item) => item.code);
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
      }
    } catch (e) {
      message.error("获取选股候选失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, [modelId]);

  // 模型清单来自后端注册表（含动态追加的 ml_walkforward）。
  useEffect(() => {
    dataApi.selectionModels()
      .then((r) => setModelOptions(r.items))
      .catch(() => setModelOptions([]));
  }, []);

  const loadSimData = useCallback(async (runId?: string | null) => {
    setSimLoading(true);
    try {
      const rr = await stockSimApi.runs();
      setSimRuns(rr.items);
      const target = runId ?? pickDefaultRun(rr.items)?.runId ?? null;
      setCurrentRunId(target);
      if (target) {
        const st = await stockSimApi.status(target);
        setSimStatus(st);
      } else {
        setSimStatus(null);
      }
    } catch (e) {
      console.error("读取模拟盘失败：", e);
      setSimStatus(null);
    } finally {
      setSimLoading(false);
    }
  }, []);

  useEffect(() => { void loadMlCandidates(modelId); }, [loadMlCandidates, modelId]);
  useEffect(() => { void loadSimData(); }, [loadSimData]);
  useEffect(() => { void loadSimData(); }, [loadSimData]);

  // 持仓映射
  const positionMap = useMemo(() => {
    const m = new Map<string, StockSimPositionDto>();
    simStatus?.positions?.forEach((p) => m.set(p.code, p));
    return m;
  }, [simStatus]);

  /** 当前选中的 run（原先在下拉/提示/联动面板里各 find 一次）。 */
  const selectedRun = useMemo(
    () => simRuns.find((r) => r.runId === currentRunId) ?? null,
    [simRuns, currentRunId],
  );

  // 富化候选股数据
  const enrichedItems: EnrichedCandidate[] = useMemo(() => {
    return (mlData?.items ?? []).map((r, i) => {
      const pos = positionMap.get(r.code);
      const tech = technicalMap.get(r.code);
      const isHeld = !!pos;
      let positionPnl: number | undefined;
      let positionPnlPct: number | undefined;
      if (pos && pos.avgCost > 0 && pos.lastPrice > 0) {
        positionPnl = (pos.lastPrice - pos.avgCost) * pos.shares;
        positionPnlPct = (pos.lastPrice - pos.avgCost) / pos.avgCost;
      }
      // V5策略信号判断
      let v5Signal: "buy" | "watch" | "unknown" = "unknown";
      let v5SignalText = "技术面待接入";
      if (tech && !tech.error) {
        if (tech.v5BuySignal) {
          v5Signal = "buy";
          v5SignalText = "金叉+趋势向上";
        } else if (tech.v5SellSignal) {
          v5Signal = "watch";
          v5SignalText = "死叉-建议卖出";
        } else if (tech.trendUp && tech.ma5AboveMa10) {
          v5Signal = "watch";
          v5SignalText = "趋势向上持有";
        } else if (!tech.trendUp) {
          v5Signal = "watch";
          v5SignalText = "跌破MA20观望";
        } else {
          v5Signal = "watch";
          v5SignalText = "等待金叉信号";
        }
      }
      return {
        rank: r.rank ?? i + 1,
        code: r.code,
        name: r.name ?? null,
        industry: r.industry ?? null,
        score: r.score,
        reasons: (r.reasons ?? []) as EnrichedCandidate["reasons"],
        isHeld,
        position: pos,
        positionPnl,
        positionPnlPct,
        v5Signal,
        v5SignalText,
        technical: tech,
      };
    });
  }, [mlData, positionMap, technicalMap]);

  // 筛选后的列表
  const filteredItems = useMemo(() => {
    switch (filter) {
      case "top10":
        return enrichedItems.slice(0, 10);
      case "held":
        return enrichedItems.filter((i) => i.isHeld);
      case "v5_buy":
        return enrichedItems.filter((i) => i.v5Signal === "buy");
      default:
        return enrichedItems;
    }
  }, [enrichedItems, filter]);

  // 统计数据
  const stats = useMemo(() => {
    const held = enrichedItems.filter((i) => i.isHeld).length;
    const v5Buy = enrichedItems.filter((i) => i.v5Signal === "buy").length;
    return { total: enrichedItems.length, held, v5Buy };
  }, [enrichedItems]);

  const tradeDate = mlData?.tradeDate;
  const count = mlData?.count ?? 0;

  const columns = [
    { title: "排名", dataIndex: "rank", width: 60, fixed: "left" as const, render: (v: number) => <span style={{ fontWeight: 600, color: v <= 3 ? "#0baa81" : v <= 5 ? "#1677ff" : "#1a232c" }}>{v}</span> },
    { title: "代码", dataIndex: "code", width: 100, render: (v: string) => <a href={quoteUrl(v)} target="_blank" rel="noreferrer" style={{ color: "#1677ff", fontFamily: "monospace" }}>{v}</a> },
    { title: "名称", dataIndex: "name", width: 110, render: (v: string | null, r: EnrichedCandidate) => v ? <span>{v}<span style={{ color: "#8a94a0", fontSize: 11, marginLeft: 6 }}>{r.industry ?? ""}</span></span> : <span style={{ color: "#bfbfbf" }}>—</span> },
    { title: "ML得分", dataIndex: "score", width: 100, align: "right" as const, sorter: (a: EnrichedCandidate, b: EnrichedCandidate) => a.score - b.score,
      render: (v: number) => <span style={{ color: ML_SCORE_COLOR, fontWeight: 700 }}>{fmtNum(v, 2)}</span> },
    { title: "入选理由", dataIndex: "reasons", render: (reasons: EnrichedCandidate["reasons"]) => (
        <Space size={4} wrap>
          {(reasons ?? []).slice(0, 3).map((r, i) => (
            <Tag key={i} color={r.score >= 85 ? "red" : r.score >= 70 ? "orange" : "blue"} style={{ fontSize: 11, marginInlineEnd: 0 }}>
              {r.label} <span style={{ color: "#8a94a0" }}>+{r.contribution.toFixed(1)}</span>
            </Tag>
          ))}
        </Space>
      ) },
    { title: "模拟盘状态", dataIndex: "isHeld", width: 130, render: (_: boolean, r: EnrichedCandidate) => (
        r.isHeld ? (
          <Space size={4}>
            <Badge status="success" />
            <span style={{ fontSize: 12, color: "#389e0d" }}>已持仓</span>
          </Space>
        ) : (
          <Space size={4}>
            <Badge status="default" />
            <span style={{ fontSize: 12, color: "#8c8c8c" }}>未持仓</span>
          </Space>
        )
      ) },
    { title: "持仓成本", dataIndex: ["position", "avgCost"], width: 90, align: "right" as const, render: (v: number | undefined) => v ? fmtNum(v) : "—" },
    { title: "现价", dataIndex: ["position", "lastPrice"], width: 90, align: "right" as const, render: (v: number | undefined) => v ? fmtNum(v) : "—" },
    { title: "盈亏", dataIndex: "positionPnl", width: 100, align: "right" as const, render: (v: number | undefined, r: EnrichedCandidate) => {
        if (v == null) return "—";
        const color = v >= 0 ? "#cf1322" : "#3f8600";
        return (
          <span style={{ color, fontWeight: 600 }}>
            {v >= 0 ? "+" : ""}{fmtNum(v)}
            <span style={{ fontSize: 10, marginLeft: 2 }}>({fmtPct(r.positionPnlPct)})</span>
          </span>
        );
      } },
    { title: "V5策略信号", dataIndex: "v5Signal", width: 160, fixed: "right" as const, render: (v: string, r: EnrichedCandidate) => {
        if (technicalLoading) return <Tag icon={<MinusCircleOutlined />}>加载中...</Tag>;
        if (r.technical?.error) return <Tag color="red">{r.technical.error}</Tag>;
        if (v === "buy") return <Tag color="green" icon={<CheckCircleOutlined />}>满足买入</Tag>;
        if (v === "watch") {
          const color = r.v5SignalText.includes("死叉") ? "red" : r.v5SignalText.includes("跌破") ? "orange" : "blue";
          return <Tag color={color}>{r.v5SignalText}</Tag>;
        }
        return <Tag icon={<MinusCircleOutlined />} style={{ color: "#8c8c8c" }}>{r.v5SignalText}</Tag>;
      } },
    { title: "MA5/MA10/MA20", width: 180, fixed: "right" as const, render: (_: unknown, r: EnrichedCandidate) => {
        if (!r.technical || r.technical.error) return "—";
        const t = r.technical;
        return (
          <div style={{ fontSize: 11, lineHeight: 1.5 }}>
            <div>MA5: <span style={{ color: t.ma5AboveMa10 ? "#3f8600" : "#cf1322", fontWeight: 600 }}>{fmtNum(t.ma5)}</span></div>
            <div>MA10: <span style={{ fontWeight: 600 }}>{fmtNum(t.ma10)}</span></div>
            <div>MA20: <span style={{ color: t.closeAboveMa20 ? "#3f8600" : "#cf1322", fontWeight: 600 }}>{fmtNum(t.ma20)}</span></div>
          </div>
        );
      } },
  ];

  return (
    <PageContainer
      title={
        <>
          <ThunderboltOutlined style={{ color: "#722ed1", marginRight: 8 }} />
          选股结果中心
        </>
      }
      subTitle={tradeDate ? `信号日 ${tradeDate} · ${count} 只候选 · 模型 ${modelOptions.find((m) => m.id === modelId)?.label ?? modelId}` : "加载中…"}
      extra={
        <Space wrap>
          {/* legacy 因子选股退役后只剩 ml_walkforward 一个模型，单选项下拉是噪音，直接隐藏；
              多模型时（例如将来恢复其它模型）自动出现。 */}
          {modelOptions.length > 1 ? (
            <Select
              size="small"
              style={{ width: 200 }}
              value={modelId}
              options={modelOptions.map((m) => ({ value: m.id, label: m.label }))}
              onChange={(v) => setModelId(v)}
            />
          ) : null}
          <Button icon={<AccountBookOutlined />} onClick={() => onNavigate("/stock-sim")}>股票模拟盘</Button>
          <Button icon={<FundOutlined />} onClick={() => onNavigate("/stock-ml")}>模型训练</Button>
          <Button type="primary" icon={<ReloadOutlined />} onClick={() => { void loadMlCandidates(modelId); void loadSimData(); }} loading={loading || simLoading}>刷新</Button>
        </Space>
      }
    >
      {/* 顶部概览 */}

      {/* 模拟盘 run 选择 + T+1 提示（原先固定用"最新 run"，常落在刚创建还没成交的择时盘上） */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>模拟盘 run</Text>
        <Select size="small" style={{ width: 420 }} value={currentRunId ?? undefined} loading={simLoading}
          options={simRuns.map((r) => ({ value: r.runId, label: `${runLabel(r)} · Top${r.topN}${runStateSuffix(r)}` }))}
          onChange={(v) => { void loadSimData(v); }} />
        <Text type="secondary" style={{ fontSize: 12 }}>（默认选可推进的 ML 多因子盘；归档的历史盘不显示，已冻结的旧口径盘会标注）</Text>
      </div>

      {selectedRun && isFrozenRun(selectedRun) ? (
        <Alert
          type="warning"
          showIcon
          message="所选模拟盘已冻结（旧 T+1 口径）"
          description="该账本后端已停止推进，因此下方「当前持仓」是它的历史末态，不会再变化；它仍可用于核对候选的持仓状态。要继续逐日推进请在「股票模拟盘」页新建一本 same_day_close 的账。"
        />
      ) : null}

      {/* 已冻结时上面那条 Alert 已解释原因，这里不重复 */}
      {!isFrozenRun(selectedRun) && flatPositionHint(selectedRun, (simStatus?.latest as { note?: string } | undefined)?.note) && (
        <Alert type="info" showIcon message="模拟盘为什么是 0 持仓 / 净值 1.0000？"
          description={flatPositionHint(selectedRun, (simStatus?.latest as { note?: string } | undefined)?.note) ?? undefined} />
      )}

      {/* 统计卡片 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
        <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "12px 16px" }}>
          <Statistic title="ML候选总数" value={stats.total} prefix={<ThunderboltOutlined style={{ color: "#722ed1" }} />} valueStyle={{ fontSize: 24 }} />
        </Card>
        <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "12px 16px" }}>
          <Statistic title="已在模拟盘持仓" value={stats.held} prefix={<AccountBookOutlined style={{ color: "#389e0d" }} />} valueStyle={{ fontSize: 24, color: "#389e0d" }} />
        </Card>
        <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "12px 16px" }}>
          <Statistic title="满足V5买入条件" value={stats.v5Buy} prefix={<RocketOutlined style={{ color: "#cf1322" }} />} valueStyle={{ fontSize: 24, color: "#cf1322" }} />
        </Card>
        <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "12px 16px" }}>
          <Statistic title="模拟盘净值" value={simStatus?.latest?.nav ?? 1} precision={4} prefix={<ArrowUpOutlined style={{ color: "#1677ff" }} />} valueStyle={{ fontSize: 24 }}
            suffix={<span style={{ fontSize: 12, color: simStatus?.totalReturn != null && simStatus.totalReturn >= 0 ? "#cf1322" : "#3f8600" }}>{fmtPct(simStatus?.totalReturn)}</span>} />
        </Card>
      </div>

      {/* 筛选标签 */}
      <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "10px 16px" }}>
        <Space size={8} wrap>
          <Text type="secondary" style={{ fontSize: 12 }}>筛选：</Text>
          <Radio.Group value={filter} onChange={(e) => setFilter(e.target.value)} optionType="button" buttonStyle="solid">
            <Radio.Button value="all">全部候选 ({stats.total})</Radio.Button>
            <Radio.Button value="top10">Top10</Radio.Button>
            <Radio.Button value="held">已持仓 ({stats.held})</Radio.Button>
            <Radio.Button value="v5_buy">V5可买入 ({stats.v5Buy})</Radio.Button>
          </Radio.Group>
        </Space>
      </Card>

      {/* 候选股表格 */}
      <Card size="small" title={<Space><ThunderboltOutlined style={{ color: "#722ed1" }} /> ML选股候选（按模型预测得分排序）</Space>}
        style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "8px 0 0" }}>
        <Table
          size="small" rowKey={(r) => `${r.code}_${r.rank}`} dataSource={filteredItems} loading={loading}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 1400 }}
          locale={{ emptyText: <Empty description="暂无ML候选（需先在「股票ML模型」页运行训练）" /> }}
          columns={columns}
        />
      </Card>

      {/* 模拟盘联动区 */}
      <Card size="small" title={<Space><AccountBookOutlined style={{ color: "#389e0d" }} /> 模拟盘联动 · 当前持仓</Space>}
        extra={<Button type="link" size="small" onClick={() => onNavigate("/stock-sim")}>去模拟盘操作 →</Button>}
        style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "12px 16px" }}>
        {simLoading ? (
          <Empty description="加载模拟盘数据中..." style={{ padding: 20 }} />
        ) : !simStatus?.run ? (
          <Empty description="尚未建立股票模拟盘，去「股票模拟盘」页面创建" style={{ padding: 20 }} />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {/* 模拟盘概览 */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, padding: "8px 0", borderBottom: "1px solid #f0f0f0" }}>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>模型</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1a232c" }}>{simStatus.run.model_id}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>净值</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1a232c" }}>{fmtNum(simStatus.latest?.nav, 4)}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>总收益</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: simStatus.totalReturn != null && simStatus.totalReturn >= 0 ? "#cf1322" : "#3f8600" }}>{fmtPct(simStatus.totalReturn)}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>持仓数量</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1a232c" }}>{simStatus.positions?.length ?? 0} 只</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>现金</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1a232c" }}>{fmtMoney(simStatus.latest?.cash)}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: "#8c8c8c" }}>市值</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "#1a232c" }}>{fmtMoney(simStatus.latest?.marketValue)}</div>
              </div>
            </div>

            {/* 持仓列表 */}
            {simStatus.positions && simStatus.positions.length > 0 ? (
              <Table
                size="small" rowKey="code" dataSource={simStatus.positions} pagination={false}
                columns={[
                  { title: "代码", dataIndex: "code", width: 100, render: (v: string) => <a href={quoteUrl(v)} target="_blank" rel="noreferrer" style={{ color: "#1677ff", fontFamily: "monospace" }}>{v}</a> },
                  { title: "持仓股数", dataIndex: "shares", width: 100, align: "right" as const, render: (v: number) => v.toLocaleString() },
                  { title: "成本价", dataIndex: "avgCost", width: 100, align: "right" as const, render: (v: number) => fmtNum(v) },
                  { title: "现价", dataIndex: "lastPrice", width: 100, align: "right" as const, render: (v: number) => fmtNum(v) },
                  { title: "盈亏", width: 120, align: "right" as const, render: (_: unknown, r: StockSimPositionDto) => {
                      if (r.avgCost > 0 && r.lastPrice > 0) {
                        const pnl = (r.lastPrice - r.avgCost) * r.shares;
                        const pnlPct = (r.lastPrice - r.avgCost) / r.avgCost;
                        const color = pnl >= 0 ? "#cf1322" : "#3f8600";
                        return <span style={{ color, fontWeight: 600 }}>{pnl >= 0 ? "+" : ""}{fmtNum(pnl)} ({fmtPct(pnlPct)})</span>;
                      }
                      return "—";
                    } },
                  { title: "在ML候选中", width: 100, render: (_: unknown, r: StockSimPositionDto) => {
                      const inCandidates = enrichedItems.some((i) => i.code === r.code);
                      return inCandidates ? <Tag color="green">是</Tag> : <Tag color="orange">否（非候选）</Tag>;
                    } },
                ]}
              />
            ) : (
              <Empty description="当前空仓（等待T+1调仓信号）" style={{ padding: 20 }} />
            )}
          </div>
        )}
      </Card>

      {/* 说明 */}
      <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
        <b>口径说明：</b>ML选股基于LGBMRanker排序模型（10种子中位数集成，31特征，forward_3 label，walkforward滚动训练），模型预测得分越高代表未来3日相对收益预期越好；信号日为最近交易日，次日（T+1）开盘按排名等权建仓。
        <br />
        <b>V5策略：</b>金叉（MA5上穿MA10）+ 收盘价{'>'}MA20 为买入条件；死叉或移动止损（max(买入价×0.95, 持仓最高价×0.92)）为卖出条件。技术指标已实时接入，V5策略信号列显示每只股票的买卖建议。
        <br />
        <b>模拟盘联动：</b>显示当前股票模拟盘持仓状态，可对比ML选股结果与实际持仓的差异。去「股票模拟盘」页面可进行调仓操作。
      </Paragraph>
    </PageContainer>
  );
}

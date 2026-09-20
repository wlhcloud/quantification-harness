import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Descriptions, Divider, Empty, Input, InputNumber, Progress, Radio, Select, Space, Statistic, Table, Tag, Typography } from "antd";
const { Title, Paragraph } = Typography;
import { PlayCircleOutlined, ReloadOutlined, ThunderboltOutlined, RocketOutlined } from "@ant-design/icons";
import { PageContainer, ProCard } from "@ant-design/pro-components";
import { stockMlApi, type StockMlBacktestDto, type StockMlWalkforwardListItem, type V5BacktestDto } from "./api";
import { fmtNum, fmtPct } from "./format";

type Mode = "ml" | "v5";

export default function BacktestPage() {
  const { message } = App.useApp();
  const [mode, setMode] = useState<Mode>("ml");

  // ML模型回测状态
  const [mlVersions, setMlVersions] = useState<StockMlWalkforwardListItem[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<StockMlWalkforwardListItem | null>(null);
  const [mlLoading, setMlLoading] = useState(false);
  const [mlResult, setMlResult] = useState<StockMlBacktestDto | null>(null);
  const [mlStartDate, setMlStartDate] = useState("2024-01-01");
  const [mlEndDate, setMlEndDate] = useState("2026-09-01");
  const [mlTopN, setMlTopN] = useState(5);

  // V5 技术面择时回测（读 artifacts 产物，可复算）
  const [v5Variant, setV5Variant] = useState<"long" | "short">("long");
  const [v5Data, setV5Data] = useState<V5BacktestDto | null>(null);
  const [v5Loading, setV5Loading] = useState(false);

  const loadV5 = useCallback(async (variant: "long" | "short" = v5Variant) => {
    setV5Loading(true);
    try {
      setV5Data(await stockMlApi.technicalTimingBacktest(variant));
    } catch (e) {
      message.error("读取 V5 回测产物失败：" + String((e as Error).message ?? e));
      setV5Data(null);
    } finally {
      setV5Loading(false);
    }
  }, [v5Variant]);

  useEffect(() => { void loadV5(v5Variant); }, [v5Variant, loadV5]);

  // 加载ML训练版本
  useEffect(() => {
    stockMlApi.walkforwardList(20).then((r) => {
      setMlVersions(r.items);
      if (r.items.length > 0) {
        const published = r.items.find((i) => i.isPublished);
        setSelectedVersion(published ?? r.items[0]);
      }
    }).catch(() => undefined);
  }, []);

  // 运行ML模型回测
  const runMlBacktest = async () => {
    if (!selectedVersion || mlLoading) return;
    setMlLoading(true);
    try {
      const result = await stockMlApi.backtest({
        runId: selectedVersion.runId,
        startDate: mlStartDate,
        endDate: mlEndDate,
        topN: mlTopN,
      });
      setMlResult(result);
      message.success("ML模型回测完成");
    } catch (e) {
      message.error("ML模型回测失败：" + String((e as Error).message ?? e));
    } finally {
      setMlLoading(false);
    }
  };

  // 绘制净值曲线
  const renderNavChart = (data: Array<{ tradeDate: string; nav: number }>) => {
    if (!data || data.length < 2) return <Empty description="暂无净值数据" style={{ padding: 20 }} />;
    const W = 800, H = 280, pad = { l: 50, r: 20, t: 20, b: 30 };
    const navs = data.map((d) => d.nav);
    const min = Math.min(...navs, 1), max = Math.max(...navs, 1);
    const span = (max - min) || 1;
    const X = (i: number) => pad.l + (i / (data.length - 1)) * (W - pad.l - pad.r);
    const Y = (v: number) => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);
    const line = data.map((d, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(d.nav).toFixed(1)}`).join(" ");
    const baseY = Y(1);
    const dates = data.map((d) => d.tradeDate);
    return (
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((g) => {
          const v = min + span * g;
          return <g key={g}><line x1={pad.l} x2={W - pad.r} y1={Y(v)} y2={Y(v)} stroke="rgba(0,0,0,0.06)" /><text x={pad.l - 6} y={Y(v) + 3} fontSize={10} fill="#8a94a0" textAnchor="end">{v.toFixed(3)}</text></g>;
        })}
        <line x1={pad.l} x2={W - pad.r} y1={baseY} y2={baseY} stroke="#d9dee3" strokeDasharray="3 3" />
        <path d={line} fill="none" stroke="#1677ff" strokeWidth={2} />
        <text x={pad.l} y={H - 8} fontSize={10} fill="#8a94a0">{dates[0]}</text>
        <text x={W - pad.r} y={H - 8} fontSize={10} fill="#8a94a0" textAnchor="end">{dates[dates.length - 1]}</text>
      </svg>
    );
  };

  return (
    <PageContainer title="策略回测中心" subTitle="ML模型回测 · V5策略回测，统一对比">
      {/* 模式切换 */}
      <div style={{ marginBottom: 16, display: "flex", gap: 8 }}>
        <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)} optionType="button" buttonStyle="solid" size="large">
          <Radio.Button value="ml"><ThunderboltOutlined /> ML模型回测</Radio.Button>
          <Radio.Button value="v5"><RocketOutlined /> V5策略回测</Radio.Button>
        </Radio.Group>
      </div>

      {/* ML模型回测模式 */}
      {mode === "ml" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <ProCard split="vertical">
            {/* 左侧：回测配置 */}
            <ProCard colSpan="38%" title="回测配置">
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>选择训练版本</Typography.Text>
                  <Select
                    style={{ width: "100%", marginTop: 4 }}
                    value={selectedVersion?.runId}
                    onChange={(val) => setSelectedVersion(mlVersions.find((v) => v.runId === val) ?? null)}
                    options={mlVersions.map((v) => ({
                      value: v.runId,
                      label: (
                        <Space>
                          <span>{v.runId.slice(-12)}</span>
                          {v.isPublished && <Tag color="green" style={{ fontSize: 10 }}>已发布</Tag>}
                          <Tag color="blue" style={{ fontSize: 10 }}>{v.model.nSeeds > 1 ? `${v.model.nSeeds}种子` : `单种子${v.model.randomState}`}</Tag>
                          <span style={{ color: v.metrics.totalReturn >= 0 ? "#cf1322" : "#3f8600", fontSize: 12 }}>{fmtPct(v.metrics.totalReturn)}</span>
                        </Space>
                      ),
                    }))}
                  />
                </div>

                {selectedVersion && (
                  <Descriptions size="small" column={1} bordered>
                    <Descriptions.Item label="特征数量">{selectedVersion.featureCount} 个</Descriptions.Item>
                    <Descriptions.Item label="种子数量">{selectedVersion.model.nSeeds} 个 {selectedVersion.model.ensembleMethod === "median_zscore" ? "(中位数集成)" : selectedVersion.model.ensembleMethod === "mean_zscore" ? "(均值集成)" : ""}</Descriptions.Item>
                    <Descriptions.Item label="walkforward窗口">{selectedVersion.walkforward.minTrain}天训练 / {selectedVersion.walkforward.stepDays}天步进 / {selectedVersion.walkforward.testDays}天测试</Descriptions.Item>
                    <Descriptions.Item label="回测区间">{selectedVersion.startDate} ~ {selectedVersion.endDate}</Descriptions.Item>
                    <Descriptions.Item label="walkforward总收益"><span style={{ color: selectedVersion.metrics.totalReturn >= 0 ? "#cf1322" : "#3f8600", fontWeight: 600 }}>{fmtPct(selectedVersion.metrics.totalReturn)}</span></Descriptions.Item>
                    <Descriptions.Item label="Sharpe">{fmtNum(selectedVersion.metrics.sharpe, 3)}</Descriptions.Item>
                    <Descriptions.Item label="最大回撤"><span style={{ color: "#cf1322" }}>{fmtPct(selectedVersion.metrics.maxDrawdown)}</span></Descriptions.Item>
                    <Descriptions.Item label="avgRankIC">{fmtNum(selectedVersion.metrics.avgRankIc, 4)}</Descriptions.Item>
                  </Descriptions>
                )}

                <Divider style={{ margin: "8px 0" }} />

                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>回测开始日期</Typography.Text>
                  <Input style={{ width: "100%", marginTop: 4 }} value={mlStartDate} onChange={(e) => setMlStartDate(e.target.value)} placeholder="YYYY-MM-DD" />
                </div>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>回测结束日期</Typography.Text>
                  <Input style={{ width: "100%", marginTop: 4 }} value={mlEndDate} onChange={(e) => setMlEndDate(e.target.value)} placeholder="YYYY-MM-DD" />
                </div>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>持仓数量（TopN）</Typography.Text>
                  <InputNumber min={1} max={50} style={{ width: "100%", marginTop: 4 }} value={mlTopN} onChange={(v) => setMlTopN(v ?? 5)} />
                </div>

                <Button type="primary" size="large" icon={<PlayCircleOutlined />} loading={mlLoading} onClick={runMlBacktest} disabled={!selectedVersion}>
                  启动ML模型回测
                </Button>

                <Alert type="info" showIcon message="ML模型回测说明" description="使用选定的训练版本模型，在指定区间内按TopN等权持仓，每日调仓。包含佣金和滑点成本。" style={{ fontSize: 11 }} />
              </div>
            </ProCard>

            {/* 右侧：回测结果 */}
            <ProCard title={mlResult ? `回测结果 · ${mlResult.metrics.startDate} ~ ${mlResult.metrics.endDate}` : "回测结果"}>
              {mlLoading ? (
                <div style={{ padding: 40, textAlign: "center" }}>
                  <Progress type="circle" percent={50} status="active" />
                  <div style={{ marginTop: 16, color: "#8c8c8c" }}>正在运行ML模型回测...</div>
                </div>
              ) : mlResult ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                  {/* 指标卡片 */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="总收益" value={mlResult.metrics.totalReturn * 100} precision={2} suffix="%"
                        valueStyle={{ color: mlResult.metrics.totalReturn >= 0 ? "#cf1322" : "#3f8600", fontSize: 20, fontWeight: 600 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="基准收益" value={mlResult.metrics.benchmarkReturn * 100} precision={2} suffix="%" valueStyle={{ fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="超额收益" value={mlResult.metrics.excessReturn * 100} precision={2} suffix="%"
                        valueStyle={{ color: mlResult.metrics.excessReturn >= 0 ? "#cf1322" : "#3f8600", fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="年化收益" value={mlResult.metrics.annualReturn * 100} precision={2} suffix="%" valueStyle={{ fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="Sharpe" value={mlResult.metrics.sharpe} precision={3} valueStyle={{ fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="最大回撤" value={mlResult.metrics.maxDrawdown * 100} precision={2} suffix="%" valueStyle={{ color: "#cf1322", fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="交易天数" value={mlResult.metrics.tradingDays} valueStyle={{ fontSize: 16 }} />
                    </Card>
                    <Card size="small" style={{ borderRadius: 8 }} bodyStyle={{ padding: "10px 12px" }}>
                      <Statistic title="持仓数量" value={mlTopN} suffix="只" valueStyle={{ fontSize: 16 }} />
                    </Card>
                  </div>

                  {/* 净值曲线 */}
                  <Card size="small" title="净值曲线（策略 vs 基准）" style={{ borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
                    {renderNavChart(mlResult.daily)}
                    <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 12, color: "#5f6b76" }}>
                      <span><span style={{ display: "inline-block", width: 12, height: 2, background: "#1677ff", verticalAlign: "middle", marginRight: 5 }} />策略累计净值</span>
                      <span><span style={{ display: "inline-block", width: 12, height: 2, background: "#d9dee3", verticalAlign: "middle", marginRight: 5 }} />基准（净值=1）</span>
                    </div>
                  </Card>

                  {/* 最新持仓 */}
                  {mlResult.holdings && mlResult.holdings.length > 0 && (
                    <Card size="small" title={`最新持仓（Top ${mlResult.holdings.length}）`} style={{ borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
                      <Table dataSource={mlResult.holdings} rowKey="code" size="small" pagination={false} tableLayout="fixed"
                        columns={[
                          { title: "排名", dataIndex: "rank", width: 60, render: (v: number) => <span style={{ fontWeight: 600, color: v <= 3 ? "#0baa81" : "#1a232c" }}>{v}</span> },
                          { title: "代码", dataIndex: "code", width: 120, render: (v: string) => <a href={`https://quote.eastmoney.com/${v.startsWith("6") ? "sh" : "sz"}${v.slice(0, 6)}.html`} target="_blank" rel="noreferrer" style={{ color: "#1677ff", fontFamily: "monospace" }}>{v}</a> },
                          { title: "模型得分", dataIndex: "score", width: 100, align: "right", render: (v: number) => <span style={{ fontWeight: 600, color: v >= 80 ? "#e5524f" : v >= 70 ? "#fa8c16" : "#595959" }}>{fmtNum(v, 2)}</span> },
                        ]} />
                    </Card>
                  )}
                </div>
              ) : (
                <Empty description="选择训练版本并配置参数后，点击「启动ML模型回测」" style={{ padding: 40 }} />
              )}
            </ProCard>
          </ProCard>
        </div>
      )}

      {/* V5策略回测模式：读 artifacts 下的回测 CSV 现场计算（原先是一段写死数字的占位文案） */}
      {mode === "v5" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Card size="small" style={{ borderRadius: 10, border: "1px solid #e5eaee" }} bodyStyle={{ padding: "10px 16px" }}>
            <Space wrap>
              <span style={{ fontSize: 12, color: "#5f6b76" }}>样本</span>
              <Radio.Group size="small" value={v5Variant} onChange={(e) => setV5Variant(e.target.value)} optionType="button">
                <Radio.Button value="long">长样本（2024-01 起）</Radio.Button>
                <Radio.Button value="short">短样本（2025-06 起）</Radio.Button>
              </Radio.Group>
              <Tag color="green">买入：MA5上穿MA10金叉 + 收盘价{'>'}MA20</Tag>
              <Tag color="red">卖出：死叉 OR 移动止损（max(买入价×0.95, 最高价×0.92)）</Tag>
              <Tag color="blue">持仓：最多5只等权</Tag>
              <Button size="small" icon={<ReloadOutlined />} loading={v5Loading} onClick={() => { void loadV5(v5Variant); }}>刷新</Button>
            </Space>
          </Card>

          {v5Data?.status === "ok" ? (
            <>
              <ProCard split="vertical">
                <ProCard colSpan="62%" title={`净值曲线（${v5Data.startDate} ~ ${v5Data.endDate}，${v5Data.tradingDays} 个交易日）`}>
                  {renderNavChart((v5Data.curve ?? []).map((p) => ({ tradeDate: p.date, nav: p.value / (v5Data.initialValue || 1) })))}
                </ProCard>
                <ProCard title="区间指标">
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                    <Statistic title="策略总收益" value={fmtPct(v5Data.totalReturn)} valueStyle={{ fontSize: 20, color: (v5Data.totalReturn ?? 0) >= 0 ? "#cf1322" : "#3f8600" }} />
                    <Statistic title={`年化（${v5Data.annualization}）`} value={fmtPct(v5Data.annualReturn)} valueStyle={{ fontSize: 20 }} />
                    <Statistic title="Sharpe" value={fmtNum(v5Data.sharpe, 3)} valueStyle={{ fontSize: 20 }} />
                    <Statistic title="最大回撤" value={fmtPct(v5Data.maxDrawdown)} valueStyle={{ fontSize: 20, color: "#cf1322" }} />
                    <Statistic title="期末资产" value={fmtNum(v5Data.finalValue, 0)} valueStyle={{ fontSize: 16 }} />
                    <Statistic title="初始资产" value={fmtNum(v5Data.initialValue, 0)} valueStyle={{ fontSize: 16 }} />
                  </div>
                  <Divider style={{ margin: "10px 0" }} />
                  <Typography.Paragraph type="secondary" style={{ fontSize: 11, margin: 0 }}>
                    数据源 <code>{v5Data.sourceFile}</code>（{(v5Data.generatedAt ?? "").slice(0, 10)} 生成），由页面现场计算，可复算。
                    口径：{v5Data.note}
                  </Typography.Paragraph>
                </ProCard>
              </ProCard>
              <Alert type="warning" showIcon message="这不是模拟盘台账"
                description="上表来自脚本产出的回测文件（scripts/backtest_technical_timing_v5_long.py），按满仓理想化成交估算；真实跟踪请看「股票模拟盘」页。该策略当前未接入每日链。" />
            </>
          ) : (
            <Card style={{ borderRadius: 10 }} bodyStyle={{ padding: 40 }}>
              <div style={{ textAlign: "center" }}>
                <RocketOutlined style={{ fontSize: 40, color: "#fa8c16" }} />
                <Title level={5} style={{ marginTop: 12 }}>
                  {v5Loading ? "读取回测产物…" : "暂无该样本的回测产物"}
                </Title>
                <Paragraph type="secondary" style={{ fontSize: 12 }}>
                  {v5Data?.status === "none"
                    ? <>期望文件：<code>{v5Data.expectedFile}</code>。运行对应脚本后刷新即可。</>
                    : "运行 scripts/backtest_technical_timing_v5_long.py（或 …_v5.py）生成 CSV 后重试。"}
                </Paragraph>
              </div>
            </Card>
          )}
        </div>
      )}
    </PageContainer>
  );
}

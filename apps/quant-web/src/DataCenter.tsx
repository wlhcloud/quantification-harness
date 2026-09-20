import { useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Card, Col, Empty, Progress, Row, Statistic, Tabs, Tag, Tooltip } from "antd";
import { ArrowRightOutlined, DatabaseOutlined, ExclamationCircleFilled, ReloadOutlined, SafetyCertificateOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { PageContainer, ProCard, ProTable, type ProColumns } from "@ant-design/pro-components";
import {
  dataApi, settingsApi,
  type CatalogDatasetDto, type DatasetDto, type DataSourceDto,
  type GovernanceDto, type GovernanceIssueDto, type QualityDto, type ToolRouteDto,
} from "./api";
import { fmtInt, fmtMinute, fmtPercentTrim } from "./format";
import { useServiceHealth } from "./useServiceHealth";

/** 本页展示口径：计数千分位、完整度百分比裁剪尾随 0、时间到分钟。 */
const fmt = (n: number | null | undefined) => fmtInt(n);
const fmtPct = (n: number | null | undefined) => fmtPercentTrim(n, 2);
const fmtTime = (v?: string | null) => fmtMinute(v);

const statusColor: Record<string, string> = {
  complete: "success", incomplete: "warning", source_missing: "error", non_trading: "default", not_applicable: "processing", ready: "success", syncing: "processing", stale: "warning", degraded: "warning", missing: "error",
};
const statusText: Record<string, string> = {
  complete: "完整", incomplete: "不完整", source_missing: "缺源", non_trading: "非交易日", not_applicable: "不适用",
  ready: "就绪", syncing: "同步中", stale: "过期", degraded: "有缺口", missing: "缺失",
};

const th = { padding: "6px 10px", borderBottom: "1px solid #f0f0f0", textAlign: "left", fontWeight: 600 } as const;
const td = { padding: "6px 10px", borderBottom: "1px solid #f0f0f0" } as const;

type RepairRequest = { freq: "1m" | "5m"; startDate: string; endDate: string; reason: string };

export default function DataCenter({ onOpenRepair }: { onOpenRepair?: (request: RepairRequest) => void }) {
  // 分钟线同步在服务端被停用（9101 health.minuteSyncEnabled === false）时，
  // 修复入口必须置灰：否则点下去只会落到一个必然 410 的表单。
  const health = useServiceHealth();
  const minuteEnabled = health.sync.minuteSyncEnabled !== false;
  const [datasets, setDatasets] = useState<DatasetDto[]>([]);
  const [catalog, setCatalog] = useState<CatalogDatasetDto[]>([]);
  const [sources, setSources] = useState<DataSourceDto[]>([]);
  const [routes, setRoutes] = useState<ToolRouteDto[]>([]);
  const [dict, setDict] = useState<{ datasets: unknown[]; fields: unknown[]; mappings: unknown[]; types: unknown[]; items: unknown[] } | null>(null);
  const [freq, setFreq] = useState<"1m" | "5m">("5m");
  const [quality, setQuality] = useState<QualityDto | null>(null);
  const [governance, setGovernance] = useState<GovernanceDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadErrors, setLoadErrors] = useState<string[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    (async () => {
      setLoading(true);
      const jobs = [
        ["数据资产", dataApi.datasets().then(setDatasets)],
        ["数据集目录", dataApi.metaDatasets().then((v) => setCatalog(v.items))],
        ["数据源", settingsApi.sources().then((v) => setSources(v.items))],
        ["工具路由", settingsApi.toolRoutes().then((v) => setRoutes(v.items))],
        ["数据字典", settingsApi.dictionary().then(setDict)],
      ] as const;
      const results = await Promise.allSettled(jobs.map(([, job]) => job));
      setLoadErrors(results.flatMap((result, index) => result.status === "rejected" ? [jobs[index][0]] : []));
      setLoading(false);
    })();
  }, [refreshKey]);

  useEffect(() => {
    dataApi.quality(freq).then(setQuality).catch(() => setQuality(null));
    dataApi.governance(freq).then(setGovernance).catch(() => setGovernance(null));
  }, [freq, refreshKey]);

  const totalRecords = useMemo(() => datasets.reduce((s, d) => s + (d.records ?? 0), 0), [datasets]);
  const datasetsMeetingSla = useMemo(() => datasets.filter(d => d.meets_sla).length, [datasets]);
  const datasetsNeedingAttention = useMemo(() => datasets.filter(d => !d.meets_sla), [datasets]);
  const onlineSources = sources.filter(s => s.enabled).length;
  const lastUpdate = useMemo(() => datasets.map(d => d.updated_at).filter(Boolean).sort().reverse()[0], [datasets]);

  const datasetColumns: ProColumns<DatasetDto>[] = [
    { title: "数据集", dataIndex: "name", render: (_, r) => <div className="tool-name"><b>{r.name}</b><span>{r.id}</span></div> },
    { title: "类别", dataIndex: "category", width: 80, render: (v) => <Tag>{String(v)}</Tag> },
    { title: "频率", dataIndex: "frequency", width: 90 },
    { title: "起始", dataIndex: "start_date", width: 100, render: (v) => String(v ?? "—") },
    { title: "截止", dataIndex: "end_date", width: 100, render: (v) => String(v ?? "—") },
    { title: "标的数", dataIndex: "symbols", width: 100, render: (_, r) => fmt(r.symbols) },
    { title: "记录数", dataIndex: "records", width: 120, render: (_, r) => fmt(r.records) },
    { title: "完整度 / 目标", dataIndex: "completeness", width: 180, render: (_, r) => <Progress percent={r.completeness ?? 0} size="small" format={() => `${fmtPct(r.completeness)} / ${fmtPct(r.completeness_target)}`} /> },
    { title: "标的覆盖 / 目标", dataIndex: "coverage", width: 180, render: (_, r) => <Progress percent={r.coverage ?? 0} size="small" status={r.coverage >= r.coverage_target ? "success" : "exception"} format={() => `${fmtPct(r.coverage)} / ${fmtPct(r.coverage_target)}`} /> },
    { title: "时效（交易日）", dataIndex: "trading_day_lag", width: 140, render: (_, r) => r.trading_day_lag === null ? "无基准" : `${r.trading_day_lag} / ${r.max_trading_day_lag}` },
    { title: "更新时间", dataIndex: "updated_at", width: 150, render: (_, r) => fmtTime(r.updated_at) },
    { title: "状态", dataIndex: "status", width: 90, render: (_, r) => <Badge status={(statusColor[r.status] ?? "default") as "success"} text={statusText[r.status] ?? String(r.status)} /> },
  ];

  const catalogColumns: ProColumns<CatalogDatasetDto>[] = [
    { title: "数据集目录", dataIndex: "id", render: (_, r) => <div className="tool-name"><b>{r.id}</b><span>{r.provider}</span></div> },
    { title: "频率", dataIndex: "frequency", width: 110 },
    { title: "最新时间", dataIndex: "latestAt", width: 160, render: (_, r) => fmtTime(r.latestAt) },
    { title: "行数", dataIndex: "rowCount", width: 110, render: (_, r) => fmt(r.rowCount) },
    { title: "状态", dataIndex: "status", width: 90, render: (_, r) => <Badge status={(statusColor[r.status] ?? "default") as "success"} text={statusText[r.status] ?? String(r.status)} /> },
  ];

  const sourceColumns: ProColumns<DataSourceDto>[] = [
    { title: "数据源", dataIndex: "label", render: (_, r) => <div className="tool-name"><b>{r.label}</b><span>{r.id}</span></div> },
    { title: "协议", dataIndex: "protocol", width: 110, render: (v) => <Tag color="blue">{String(v)}</Tag> },
    { title: "接口地址", dataIndex: "baseUrl", ellipsis: true },
    { title: "优先级", dataIndex: "priority", width: 80 },
    { title: "超时", dataIndex: "timeoutMs", width: 90, render: (_, r) => r.timeoutMs + " ms" },
    { title: "状态", dataIndex: "enabled", width: 80, render: (_, r) => <Badge status={r.enabled ? "success" : "default"} text={r.enabled ? "启用" : "停用"} /> },
  ];

  const routeColumns: ProColumns<ToolRouteDto>[] = [
    { title: "工具", dataIndex: "label", render: (_, r) => <div className="tool-name"><b>{r.label}</b><span>{r.toolId}</span></div> },
    { title: "主数据源", dataIndex: "primarySourceId", width: 130, render: (v) => <Tag color="green">{String(v)}</Tag> },
    { title: "降级数据源", dataIndex: "fallbackSourceId", width: 130, render: (v) => v ? <Tag color="orange">{String(v)}</Tag> : "—" },
  ];

  const issueColumns: ProColumns<GovernanceIssueDto>[] = [
    { title: "状态", dataIndex: "status", width: 110, render: (_, r) => <Badge status={(statusColor[r.status] ?? "default") as "success"} text={statusText[r.status] ?? r.status} /> },
    { title: "交易日", dataIndex: "trade_date", width: 110, render: (v) => String(v || "—") },
    { title: "受影响标的", dataIndex: "affected_symbols", width: 110, render: (_, r) => fmt(r.affected_symbols) },
    { title: "缺失桶数", dataIndex: "missing_buckets", width: 110, render: (_, r) => fmt(r.missing_buckets) },
    { title: "样例代码", dataIndex: "sample_code", width: 110 },
  ];

  const govDates = governance?.dates ?? [];
  const govCodes = governance?.codes ?? [];
  const govCells = governance?.cells ?? [];
  const cellMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of govCells) m.set(c.code + "|" + c.trade_date, c.status);
    return m;
  }, [govCells]);
  const repairRequest = (dataset: DatasetDto): RepairRequest | null => {
    const freqValue = dataset.id === "minute-1m" ? "1m" : dataset.id === "minute-5m" ? "5m" : null;
    if (!freqValue || !dataset.start_date || !dataset.end_date) return null;
    const issueDate = freqValue === "5m" ? governance?.issues.find(issue => issue.status === "source_missing" || issue.status === "incomplete")?.trade_date : undefined;
    const reason = dataset.coverage < dataset.coverage_target ? `${dataset.name}标的覆盖不足` : `${dataset.name}存在数据缺口`;
    return { freq: freqValue, startDate: issueDate || dataset.start_date, endDate: issueDate || dataset.end_date, reason };
  };

  const stat = (label: string, value: string | number | undefined, color?: string) => (
    <Card size="small"><Statistic title={label} value={value} styles={{ content: { color } }} /></Card>
  );

  return (
    <PageContainer className="data-center-page" title="数据中心" subTitle="数据资产、可用性与治理状态" extra={<Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRefreshKey(v => v + 1)}>刷新数据</Button>}>
      <section className="data-command-bar">
        <div className="data-command-title"><span className="data-command-icon"><SafetyCertificateOutlined /></span><div><b>数据可用性总览</b><small>以日线最新交易日为时效基准 · 每次刷新均重新核验 SLA</small></div></div>
        <div className="data-command-metric"><span>满足 SLA</span><strong>{datasetsMeetingSla}<em> / {datasets.length}</em></strong></div>
        <div className="data-command-metric"><span>数据质量风险</span><strong className={datasetsNeedingAttention.length ? "risk" : ""}>{datasetsNeedingAttention.length}</strong></div>
        <div className="data-command-metric"><span>最近数据更新</span><strong className="date">{fmtTime(lastUpdate)}</strong></div>
      </section>

      <Row className="data-kpi-row" gutter={[14, 14]}>
        <Col flex="1"><ProCard><Statistic title="数据集资产" value={datasets.length} prefix={<DatabaseOutlined />} /></ProCard></Col>
        <Col flex="1"><ProCard><Statistic title="总记录数" value={totalRecords} /></ProCard></Col>
        <Col flex="1"><ProCard><Statistic title="数据源在线" value={onlineSources + " / " + sources.length} prefix={<ThunderboltOutlined />} /></ProCard></Col>
      </Row>

      {datasetsNeedingAttention.length > 0 && <section className="data-attention-panel">
        <div className="data-attention-head"><span><ExclamationCircleFilled /> 数据质量风险</span><small>{datasetsNeedingAttention.length} 个数据集未达到可用性 SLA；这不等同于同步任务仍在运行{minuteEnabled ? "" : " · 分钟线同步已在服务端停用，对应修复入口不可用"}</small></div>
        <div className="data-attention-list">{datasetsNeedingAttention.map(dataset => <div className="data-attention-item" key={dataset.id}>
          <div><b>{dataset.name}</b><span>{dataset.coverage < dataset.coverage_target ? `标的覆盖 ${fmtPct(dataset.coverage)}，目标 ${fmtPct(dataset.coverage_target)}` : dataset.trading_day_lag && dataset.trading_day_lag > dataset.max_trading_day_lag ? `滞后 ${dataset.trading_day_lag} 个交易日，允许 ${dataset.max_trading_day_lag} 个交易日` : "同步任务虽已结束，但治理台账仍记录数据不完整；请查看具体代码与日期"}</span></div>
          <Tooltip title={minuteEnabled ? undefined : "服务端已停用股票分钟线同步（QUANT_SYNC_MINUTE_ENABLED=0），无法提交修复任务"}>
            <Button type="link" size="small" disabled={!repairRequest(dataset) || !minuteEnabled} onClick={() => { const request = repairRequest(dataset); if (request && minuteEnabled) onOpenRepair?.(request); }}>启动修复 <ArrowRightOutlined /></Button>
          </Tooltip>
        </div>)}</div>
      </section>}

      {loadErrors.length > 0 && (
        <Alert
          style={{ marginTop: 16 }}
          type="warning"
          showIcon
          message="部分数据暂时不可用"
          description={`${loadErrors.join("、")}加载失败，其他模块仍可正常查看。请稍后刷新。`}
        />
      )}

      <ProCard className="data-workspace" style={{ marginTop: 16 }}>
        <Tabs
          items={[
            {
              key: "assets", label: "数据资产",
              children: (
                <>
                  <Alert className="sla-note" type="info" showIcon style={{ marginBottom: 12 }} message="可用性 SLA" description="就绪表示同时满足完整度、全市场标的覆盖率、交易日时效及无已知缺口；交易日时效以日线行情最新交易日为基准。" />
                  <ProTable<DatasetDto> rowKey="id" search={false} options={false} pagination={false} loading={loading} dataSource={datasets} columns={datasetColumns} headerTitle="数据集资产（实际值 / SLA 目标）" />
                  <ProTable<CatalogDatasetDto> rowKey="id" search={false} options={false} pagination={false} style={{ marginTop: 16 }} dataSource={catalog} columns={catalogColumns} headerTitle={"数据集目录（Python 元数据注册表，共 " + catalog.length + " 项）"} />
                </>
              ),
            },
            {
              key: "quality", label: "数据质量",
              children: quality ? (
                <>
                  <Tabs size="small" activeKey={freq} onChange={(k) => setFreq(k as "1m" | "5m")} items={[{ key: "1m", label: "1 分钟" }, { key: "5m", label: "5 分钟" }]} />
                  <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
                    <Col flex="1">{stat("完整标的日", quality.complete_days, "#3f8600")}</Col>
                    <Col flex="1">{stat("不完整标的日", quality.incomplete_days, "#d46b08")}</Col>
                    <Col flex="1">{stat("缺源标的日", quality.source_missing_days, "#cf1322")}</Col>
                    <Col flex="1">{stat("非交易标的日", quality.non_trading_days)}</Col>
                    <Col flex="1">{stat("不适用标的日", quality.not_applicable_days)}</Col>
                    <Col flex="1">{stat("重复桶", quality.duplicate_buckets)}</Col>
                  </Row>
                  <ProCard title="各数据源记录分布" style={{ marginTop: 12 }}>
                    {quality.sources.map(s => (
                      <div key={s.source} style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
                        <Tag color="blue">{s.source}</Tag>
                        <div style={{ flex: 1, margin: "0 12px" }}><Progress percent={quality.sources.length ? Math.round(s.records / Math.max(...quality.sources.map(x => x.records)) * 100) : 0} showInfo={false} strokeColor="#1677ff" size="small" /></div>
                        <span>{fmt(s.records)} 条</span>
                      </div>
                    ))}
                  </ProCard>
                </>
              ) : <Empty description="质量数据不可用" />,
            },
            {
              key: "governance", label: "数据治理",
              children: governance && govDates.length ? (
                <>
                  <ProCard title={"分钟治理矩阵（期望每标的/日 " + governance.expected_buckets + " 桶）"}>
                    <table style={{ borderCollapse: "collapse", width: "100%" }}>
                      <thead><tr><th style={th}>代码</th>{govDates.map(d => <th key={d} style={th}>{d.slice(4)}</th>)}</tr></thead>
                      <tbody>
                        {govCodes.map(code => (
                          <tr key={code}>
                            <td style={td}><b>{code}</b></td>
                            {govDates.map(d => {
                              const st = cellMap.get(code + "|" + d) ?? "not_applicable";
                              return <td key={d} style={td}><Badge status={(statusColor[st] ?? "default") as "success"} text={statusText[st] ?? st} /></td>;
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </ProCard>
                  <ProTable<GovernanceIssueDto> rowKey={(r) => r.status + r.trade_date} search={false} options={false} pagination={false} style={{ marginTop: 16 }} dataSource={governance.issues} columns={issueColumns} headerTitle="治理问题清单" />
                </>
              ) : <Empty description="治理数据不可用" />,
            },
            {
              key: "sources", label: "数据源与字典",
              children: (
                <>
                  <ProTable<DataSourceDto> rowKey="id" search={false} options={false} pagination={false} dataSource={sources} columns={sourceColumns} headerTitle={"数据源连接（" + onlineSources + " 在线）"} />
                  <ProTable<ToolRouteDto> rowKey="toolId" search={false} options={false} pagination={false} style={{ marginTop: 16 }} dataSource={routes} columns={routeColumns} headerTitle="工具 → 数据源路由" />
                  <ProCard title="数据字典" style={{ marginTop: 16 }}>
                    <Row gutter={12}>
                      <Col flex="1">{stat("数据集", dict?.datasets.length, "#1677ff")}</Col>
                      <Col flex="1">{stat("字段", dict?.fields.length, "#1677ff")}</Col>
                      <Col flex="1">{stat("映射", dict?.mappings.length, "#1677ff")}</Col>
                      <Col flex="1">{stat("字典类型", dict?.types.length)}</Col>
                      <Col flex="1">{stat("字典项", dict?.items.length)}</Col>
                    </Row>
                  </ProCard>
                </>
              ),
            },
          ]}
        />
      </ProCard>
    </PageContainer>
  );
}

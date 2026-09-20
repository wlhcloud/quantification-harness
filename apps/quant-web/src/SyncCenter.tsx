import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  App,
  Badge,
  Button,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Form,
  InputNumber,
  Modal,
  Progress,
  Select,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  DatabaseOutlined,
  ReloadOutlined,
  RetweetOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import { PageContainer, ProCard, ProTable, type ProColumns } from "@ant-design/pro-components";
import dayjs, { type Dayjs } from "dayjs";
import { coreSyncApi, dataApi, syncApi, type GenericSyncStatusDto, type MinuteSyncDetailsDto, type MinuteSyncItemDto, type MinuteSyncStatusDto, type SecurityDto, type SyncConfigDto, type SyncLogDto, type SyncRunDto, type SyncToolDto, type SyncToolParams } from "./api";
import { fmtDateTime, fmtParamsText } from "./format";
import EngineJobsCard from "./engine/EngineJobsCard";
import EtfSyncCard from "./sync/EtfSyncCard";
import { useServiceHealth } from "./useServiceHealth";

type LaunchValues = { range: [Dayjs, Dayjs]; freq: "1m" | "5m"; code?: string; concurrency?: number; segmentDays?: number; requestTimeoutMs?: number };
type ToolLaunchValues = { range: [Dayjs, Dayjs]; code?: string; day: Dayjs; freq: string };
export type SyncRepairPreset = { id: string; freq: "1m" | "5m"; startDate: string; endDate: string; reason: string };
const EMPTY: MinuteSyncStatusDto = { runId: null, startDate: null, endDate: null, freq: null, status: "idle", total: 0, completed: 0, failed: 0, skipped: 0, startedAt: null, finishedAt: null, error: null, current: null };
const EMPTY_HISTORY: GenericSyncStatusDto = { status: "idle", totalDays: 0, completedDays: 0, currentDate: null, startedAt: null, finishedAt: null, error: null };
const statusMeta: Record<string, { status: "success" | "processing" | "error" | "default" | "warning"; text: string }> = {
  idle: { status: "default", text: "空闲" }, running: { status: "processing", text: "运行中" },
  complete: { status: "success", text: "已完成" }, error: { status: "error", text: "异常" },
  starting: { status: "processing", text: "提交中" },
  complete_with_gaps: { status: "warning", text: "完成但有缺口" },
};
const formatTime = (value?: string | null) => fmtDateTime(value);
const qualityPercent = (passed: number, total: number) => total ? (passed >= total ? 100 : Math.floor(passed / total * 1000) / 10) : 0;
const toText = (value: unknown): string => { if (value === null || value === undefined) return ""; if (typeof value === "string") return value; if (typeof value === "number" || typeof value === "boolean") return String(value); try { return JSON.stringify(value); } catch { return String(value); } };
const fmtParams = (value: unknown): string => fmtParamsText(value);
const kindMeta: Record<string, { text: string; color: string }> = {
  "market-snapshot": { text: "主档", color: "cyan" }, "market-calendar": { text: "日历", color: "geekblue" },
  "market-range": { text: "行情", color: "blue" }, finance: { text: "财务", color: "purple" }, minute: { text: "分钟", color: "green" },
};

function SecuritySelect({ value, onChange }: { value?: string; onChange?: (value?: string) => void }) {
  const [items, setItems] = useState<SecurityDto[]>([]), [loading, setLoading] = useState(false);
  const timer = useRef<number | undefined>(undefined), controller = useRef<AbortController | null>(null);
  const search = useCallback((query: string) => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => { controller.current?.abort(); controller.current = new AbortController(); setLoading(true); dataApi.securities(query.trim(), controller.current.signal).then(setItems).catch((error) => { if (!(error instanceof DOMException && error.name === "AbortError")) setItems([]); }).finally(() => setLoading(false)); }, 250); }, []);
  useEffect(() => { search(""); return () => { window.clearTimeout(timer.current); controller.current?.abort(); }; }, [search]);
  return <Select showSearch allowClear value={value} onChange={onChange} onSearch={search} filterOption={false} loading={loading} placeholder="输入股票代码或中文名称；留空同步全市场" notFoundContent={loading ? "搜索中…" : "没有匹配股票"}
    options={items.map((item) => ({ value: item.code, label: <span className="security-option"><b>{item.code}</b><span>{item.name ?? "未命名"}</span><small>{item.industry ?? item.market ?? ""}</small></span> }))} />;
}

export default function SyncCenter({ repairPreset }: { repairPreset?: SyncRepairPreset | null }) {
  const { message, modal } = App.useApp();
  // 9101 /health 的 minuteSyncEnabled：后端置 false 时 /sync/minute* 一律 410，
  // 前端据此禁用入口而不是等用户点提交才报错。
  // undefined 视为"未知"（老版本服务没有该字段），保持可用，让后端错误信息透出。
  const health = useServiceHealth();
  const minuteEnabled = health.sync.minuteSyncEnabled !== false;
  const [status, setStatus] = useState<MinuteSyncStatusDto>(EMPTY);
  const [marketHistory, setMarketHistory] = useState<GenericSyncStatusDto>(EMPTY_HISTORY);
  const [details, setDetails] = useState<MinuteSyncDetailsDto>({ run: null, counts: {}, items: [], dayLedger: { completed: 0, zeroRows: 0 } });
  const [serviceOk, setServiceOk] = useState(false);
  const [tools, setTools] = useState<SyncToolDto[]>([]);
  const [records, setRecords] = useState<SyncRunDto[]>([]);
  const [activeRun, setActiveRun] = useState<SyncRunDto | null>(null);
  const [logs, setLogs] = useState<SyncLogDto[]>([]);
  const [logLoading, setLogLoading] = useState(false);
  const [syncCfg, setSyncCfg] = useState<SyncConfigDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [minuteOpen, setMinuteOpen] = useState(false);
  const [activeTool, setActiveTool] = useState<SyncToolDto | null>(null);
  const [minuteForm] = Form.useForm<LaunchValues>();
  const [toolForm] = Form.useForm<ToolLaunchValues>();

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    const historyRequest = coreSyncApi.marketStatus().then(setMarketHistory).catch(() => undefined);
    try {
      const [health, nextStatus, toolsRes, runs] = await Promise.all([syncApi.health(), syncApi.status(), syncApi.tools(), syncApi.records()]);
      setServiceOk(health.status === "ok"); setStatus(nextStatus); setTools(toolsRes.items); setRecords(runs.items);
      const nextDetails = await syncApi.details(nextStatus.runId ?? undefined);
      setDetails(nextDetails);
    } catch (error) {
      setServiceOk(false);
      if (!silent) message.error(error instanceof Error ? error.message : String(error));
    } finally { await historyRequest; if (!silent) setLoading(false); }
  }, [message]);

  const loadMinuteDetails = useCallback(async (runId?: string) => {
    try { setDetails(await syncApi.details(runId)); } catch { /* ignore */ }
  }, []);
  /**
   * 明细抽屉的节流。
   *
   * 后端 `minute.sync_all` 是**每个代码**都调 `_save_run_progress()` 并广播一帧 WebSocket
   * （services/quant-sync/src/quant_sync/minute.py:403），原先前端每帧都发一次
   * `GET /sync/minute/all/details?limit=200`——全市场 5000 只就是约 5000 次请求。
   * 这里把刷新上限压到每 2 秒一次；任务终结时再强制刷一次，保证最终状态不丢。
   */
  const DETAIL_THROTTLE_MS = 2000;
  const detailFetchedAtRef = useRef(0);
  const refreshMinuteDetails = useCallback((runId: string, force = false) => {
    const now = Date.now();
    if (!force && now - detailFetchedAtRef.current < DETAIL_THROTTLE_MS) return;
    detailFetchedAtRef.current = now;
    void loadMinuteDetails(runId);
  }, [loadMinuteDetails]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { syncApi.config().then(setSyncCfg).catch(() => { }); }, []);
  const statusRef = useRef(status); statusRef.current = status;
  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    let ws: WebSocket | undefined, closed = false, retry = 0, connectTimer: number | undefined;
    const connect = () => {
      ws = new WebSocket(proto + "//" + window.location.host + "/api/sync/ws");
      ws.onopen = () => { retry = 0; };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(String(e.data));
          if (msg.type === "minute" && msg.state) {
            const prev = statusRef.current.status;
            setStatus(msg.state);
            setTools(prevTools => prevTools.map(t => t.toolId === "stk_mins" ? { ...t, status: msg.state.status ?? t.status, total: msg.state.total ?? t.total, done: msg.state.completed ?? t.done, current: msg.state.current, startDate: msg.state.startDate, endDate: msg.state.endDate, freq: msg.state.freq, startedAt: msg.state.startedAt, finishedAt: msg.state.finishedAt, error: msg.state.error } : t));
            if (prev === "running" && (msg.state.status === "complete" || msg.state.status === "error")) {
              // 终结：强制刷新明细 + 记录列表，避免节流把最后一次状态吃掉
              if (msg.state.runId) refreshMinuteDetails(msg.state.runId, true);
              void load(true);
            } else if (msg.state.runId) {
              refreshMinuteDetails(msg.state.runId);
            }
          } else if (msg.type === "tool" && msg.tool) {
            setTools(prevTools => prevTools.map(t => t.toolId === msg.tool ? { ...t, ...msg.state } : t));
          } else if (msg.type === "daily" && msg.state) {
            setMarketHistory(msg.state);
          }
        } catch { /* ignore malformed frame */ }
      };
      ws.onclose = () => { if (!closed) { retry = Math.min(retry + 1, 6); connectTimer = window.setTimeout(connect, retry * 1000); } };
      ws.onerror = () => ws?.close();
    };
    connectTimer = window.setTimeout(connect, 0);
    // StrictMode 下 dev 会 mount→unmount→mount：cleanup 已 closed+close()，
    // 第二帧会新建一条连接，不会泄漏（生产构建只跑一次）。
    return () => { closed = true; window.clearTimeout(connectTimer); ws?.close(); };
  }, [load, refreshMinuteDetails]);

  const openMinute = (freq: "1m" | "5m", preset?: SyncRepairPreset | null) => {
    const m = syncCfg?.minuteRun;
    const toDay = (value: string) => dayjs(`${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`);
    minuteForm.setFieldsValue({
      freq,
      range: preset ? [toDay(preset.startDate), toDay(preset.endDate)] : undefined,
      concurrency: m?.concurrency ?? 10,
      segmentDays: (freq === "1m" ? (m?.oneMinuteSegmentDays ?? 30) : (m?.fiveMinuteSegmentDays ?? 120)),
      requestTimeoutMs: syncCfg?.requestTimeoutMs ?? 30000,
    });
    setMinuteOpen(true);
  };
  useEffect(() => {
    if (!repairPreset) return;
    // 服务端停用分钟同步时不要自动弹窗：否则用户从数据中心点"启动修复"会直接落到一个必 410 的表单
    if (!minuteEnabled) {
      modal.warning({
        title: "分钟线修复不可用",
        content: "服务端已停用股票分钟线同步（QUANT_SYNC_MINUTE_ENABLED=0），无法提交修复任务。请在服务端开启后重启 9101。",
      });
      return;
    }
    openMinute(repairPreset.freq, repairPreset);
  }, [repairPreset?.id, minuteEnabled]);
  const launchMinute = async () => {
    const values = await minuteForm.validateFields();
    setSubmitting(true);
    try {
      const input = { startDate: values.range[0].format("YYYYMMDD"), endDate: values.range[1].format("YYYYMMDD"), freq: values.freq, concurrency: values.concurrency, segmentDays: values.segmentDays, requestTimeoutMs: values.requestTimeoutMs };
      if (values.code?.trim()) {
        const response = await syncApi.startSingle({ ...input, code: values.code.trim().toUpperCase() });
        const result = response.result;
        if (result.skippedSegments > 0) message.info(`本地账本已覆盖，跳过 ${result.skippedSegments} 段（来源：${result.source}）`);
        else message.success(`接收 ${result.received.toLocaleString()} 条，写入 ${result.accepted.toLocaleString()} 条（来源：${result.source}）`);
      }
      else { setStatus(await syncApi.start(input)); message.success("分钟同步任务已提交"); }
      setMinuteOpen(false); void load(true);
    } catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
    finally { setSubmitting(false); }
  };
  const retryMinute = async () => {
    setSubmitting(true);
    try { setStatus(await syncApi.retry(status.runId ?? undefined)); message.success("失败项已重新提交"); void load(true); }
    catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
    finally { setSubmitting(false); }
  };
  const openTool = (tool: SyncToolDto) => {
    toolForm.setFieldsValue({
      range: [dayjs().subtract(7, "day"), dayjs()],
      code: undefined,
      day: dayjs(),
      // 两个指数分钟工具的 freq 大小写口径不同：rt_idx_min 要大写 1MIN，idx_mins 要小写 1min
      freq: tool.toolId === "rt_idx_min" ? "1MIN" : "1min",
    });
    setActiveTool(tool);
  };
  const launchTool = async () => {
    if (!activeTool) return;
    setSubmitting(true);
    try {
      const kind = activeTool.kind;
      let params: SyncToolParams = {};
      if (kind === "market-range" || kind === "market-calendar") {
        const v = await toolForm.validateFields();
        params = { start_date: v.range[0].format("YYYYMMDD"), end_date: v.range[1].format("YYYYMMDD") };
      } else if (kind === "finance") {
        const v = await toolForm.validateFields();
        if (v.code?.trim()) params = { code: v.code.trim().toUpperCase() };
      } else if (activeTool.toolId === "rt_idx_min") {
        // 后端 _sync_realtime_index 读 freq（大写口径），原先前端签名不接受该参数，
        // 于是这个入口永远只能拿默认的 1MIN。
        const v = await toolForm.validateFields();
        params = { freq: v.freq };
      } else if (activeTool.toolId === "idx_mins") {
        // 后端 sync_tool 把 start_date/end_date 映射成 params["trade_date"]
        // （main.py:215-216），_sync_index_history 再按 trade_date + freq（小写口径）补齐单日。
        const v = await toolForm.validateFields();
        params = { start_date: v.day.format("YYYYMMDD"), freq: v.freq };
      }
      await syncApi.startTool(activeTool.toolId, params);
      message.success("同步任务已提交");
      setTools(prev => prev.map(t => t.toolId === activeTool.toolId ? { ...t, status: "starting" } : t));
      setActiveTool(null);
    } catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
    finally { setSubmitting(false); }
  };
  const trigger = (tool: SyncToolDto) => {
    if (tool.status === "running" || tool.status === "starting") return;
    if (tool.toolId === "stk_mins" && !minuteEnabled) {
      modal.warning({
        title: "股票分钟线同步已停用",
        content: "服务端已置 QUANT_SYNC_MINUTE_ENABLED=0：本地 minute_bars 已清零，重新同步会产生约 21GB / 1.3 亿行数据。确需恢复请在服务端开启后重启 9101。",
      });
      return;
    }
    if (tool.toolId === "stk_mins") openMinute("5m");
    else openTool(tool);
  };
  const confirmHistoryRetry = () => {
    const latest = records.find(run => run.kind === "daily.history");
    const params = latest?.parameters ?? {};
    const startDate = String(params.start_date ?? dayjs().subtract(5, "year").format("YYYYMMDD"));
    const endDate = String(params.end_date ?? dayjs().format("YYYYMMDD"));
    const days = Number(params.days ?? marketHistory.totalDays ?? 1236);
    const gaps = marketHistory.failedDays ?? marketHistory.gapDates?.length ?? 0;
    modal.confirm({
      title: "确认补拉历史质量缺口",
      content: <Space direction="vertical" size={4}>
        <Typography.Text>范围：{startDate} — {endDate}，目标 {days} 个交易日</Typography.Text>
        <Typography.Text>账本缺口：{gaps} 天；预计最多约 {Math.max(1, gaps) * 7 + 1} 次上游调用</Typography.Text>
        <Typography.Text type="secondary">执行器会先查本地覆盖率，只请求未通过日期，并逐日校验 PIT 状态与复权因子。</Typography.Text>
      </Space>,
      okText: "仅补拉缺口",
      cancelText: "取消",
      onOk: async () => {
        setSubmitting(true);
        try { setMarketHistory(await coreSyncApi.startMarketHistory(startDate, endDate, days)); message.success("历史缺口补拉已提交"); }
        catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
        finally { setSubmitting(false); }
      },
    });
  };

  const openLogs = async (run: SyncRunDto) => {
    setActiveRun(run); setLogs([]); setLogLoading(true);
    try { setLogs((await syncApi.logs(run.id)).items); }
    catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
    finally { setLogLoading(false); }
  };

  const runningCount = tools.filter(t => t.status === "running" || t.status === "starting").length + (["running", "starting"].includes(marketHistory.status) ? 1 : 0);
  const errorCount = tools.filter(t => t.status === "error").length + (["error", "complete_with_gaps"].includes(marketHistory.status) ? 1 : 0);
  const historyTotal = marketHistory.totalDays ?? 0;
  const historyDone = marketHistory.completedDays ?? 0;
  const historyFailed = marketHistory.failedDays ?? 0;
  const historyPercent = qualityPercent(historyDone, historyTotal);
  const columns: ProColumns<SyncToolDto>[] = [
    { title: "上游工具", dataIndex: "label", render: (_, row) => <div className="tool-name"><b>{row.label}</b><span>{row.table}（{row.toolId}）</span></div> },
    { title: "类型", dataIndex: "kind", width: 80, render: (_, row) => { const km = kindMeta[row.kind]; return <Tag color={km.color}>{km.text}</Tag>; } },
    { title: "状态", dataIndex: "status", width: 100, render: (_, row) => { const cur = statusMeta[row.status] ?? statusMeta.idle; return <Badge status={cur.status} text={!serviceOk && row.status === "idle" ? "服务离线" : cur.text} />; } },
    { title: "进度", width: 130, render: (_, row) => (row.total ? <Progress percent={Math.min(100, Math.round((row.done ?? 0) / row.total * 100))} size="small" /> : "—") },
    { title: "当前项", dataIndex: "current", ellipsis: true, render: (_, row) => { const c = (row as { current?: unknown })?.current; const t = toText(c); return t ? <Tooltip title={t}><span>{t}</span></Tooltip> : "—"; } },
    { title: "最近运行", width: 165, render: (_, row) => formatTime(row.finishedAt ?? row.startedAt) },
    { title: "操作", valueType: "option", width: 110, render: (_, row) => <Button type="link" icon={<PlayCircleOutlined />} loading={row.status === "running" || row.status === "starting"} onClick={() => trigger(row)}>{row.status === "running" ? "执行中" : "同步"}</Button> },
  ];
  const minuteColumns: ProColumns<MinuteSyncItemDto>[] = [
    { title: "股票代码", dataIndex: "code", copyable: true },
    { title: "状态", dataIndex: "status", render: (value) => <Tag color={value === "error" ? "error" : value === "running" ? "processing" : "default"}>{String(value)}</Tag> },
    { title: "尝试次数", dataIndex: "attempts", width: 100 },
    { title: "错误信息", dataIndex: "error", ellipsis: true, render: (value) => <Tooltip title={String(value ?? "")}><span>{value ? String(value) : "—"}</span></Tooltip> },
    { title: "更新时间", dataIndex: "updatedAt", width: 180, render: (value) => formatTime(String(value ?? "")) },
  ];
  return <PageContainer className="sync-center-page" title="同步中心" subTitle="任务发起、实时执行与完整审计"
    extra={<Button icon={<ReloadOutlined />} loading={loading} onClick={() => { void load(); }}>刷新</Button>}>
    <div className="sync-service-bar">
      <div><DatabaseOutlined /><span><b>同步执行器</b><small>本地数据编排与质量校验</small><Badge status={serviceOk ? "success" : "error"} text={serviceOk ? "服务在线" : "服务离线"} /></span></div>
      <Statistic title="上游工具" value={tools.length} />
      <Statistic title="运行中" value={runningCount} />
      <Statistic title="异常" value={errorCount} />
      <Statistic title="同步记录" value={records.length} />
    </div>

    {!minuteEnabled && (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="股票分钟线同步已在服务端停用"
        description="QUANT_SYNC_MINUTE_ENABLED=0 时 /sync/minute* 会返回 410（本地 minute_bars 已清零，重新同步约 21GB）。分钟线同步入口与数据中心的分钟修复按钮均已禁用；如需恢复请在服务端开启后重启 9101。"
      />
    )}

    <ProCard
      className="sync-run-card"
      title="历史研究数据回填"
      extra={<Space><Badge status={(statusMeta[marketHistory.status] ?? statusMeta.idle).status} text={(statusMeta[marketHistory.status] ?? statusMeta.idle).text} /><Button size="small" icon={<RetweetOutlined />} loading={submitting} disabled={["running", "starting"].includes(marketHistory.status)} onClick={confirmHistoryRetry}>补拉缺口</Button></Space>}
    >
      <div className="sync-run-summary">
        <div className="sync-progress">
          <Progress type="circle" percent={historyPercent} size={76} status={marketHistory.status === "error" ? "exception" : marketHistory.status === "complete_with_gaps" ? "active" : undefined} />
          <div>
            <b>{historyDone.toLocaleString()} / {historyTotal.toLocaleString()} 个交易日通过质量校验</b>
            <span>当前日期：{marketHistory.currentDate ?? "—"}</span>
          </div>
        </div>
        <Space size={36} wrap>
          <Statistic title="开始时间" value={formatTime(marketHistory.startedAt)} />
          <Statistic title="完成时间" value={formatTime(marketHistory.finishedAt)} />
          <Statistic title="未通过交易日" value={historyFailed} valueStyle={historyFailed ? { color: "#d46b08" } : undefined} />
          <Statistic title="目标数据" value="历史可交易状态 / 复权因子" />
        </Space>
      </div>
      {marketHistory.error ? <Typography.Text type="danger">{marketHistory.error}</Typography.Text> : null}
      {marketHistory.gapDates?.length ? <div style={{ marginTop: 12 }}><Typography.Text type="secondary">缺口日期：</Typography.Text><Space size={[4, 4]} wrap>{marketHistory.gapDates.slice(0, 20).map(date => <Tag color="warning" key={date}>{date}</Tag>)}</Space></div> : null}
    </ProCard>

    <ProTable<SyncToolDto> className="sync-task-catalog" rowKey="toolId" search={false} options={false} pagination={false}
      dataSource={tools} loading={loading} columns={columns} headerTitle="上游数据工具（一个工具 = 一个独立同步入口）" />

    <ProTable<SyncRunDto> className="sync-records" rowKey="id" search={false} options={false} dataSource={records} scroll={{ x: 1450 }}
      headerTitle={"同步记录（" + records.length + "）"} pagination={{ pageSize: 10, showSizeChanger: true }}
      columns={[
        { title: "任务 / Run ID", dataIndex: "kind", width: 190, render: (_, row) => <div className="tool-name"><Tag color={String(row.kind).startsWith("minute") ? "blue" : "default"}>{row.kind}</Tag><Typography.Text copyable={{ text: row.id }} type="secondary">{row.id.slice(-12)}</Typography.Text></div> },
        { title: "同步参数", dataIndex: "parameters", width: 260, ellipsis: true, render: (_, row) => <Typography.Text code>{fmtParams(row.parameters)}</Typography.Text> },
        { title: "状态", dataIndex: "status", width: 100, render: (value) => { const cur = statusMeta[String(value)] ?? statusMeta.idle; return <Badge status={cur.status} text={cur.text} />; } },
        { title: "质量通过率", width: 150, render: (_, row) => (row.total ? <Progress percent={qualityPercent(row.completed, row.total)} size="small" status={row.failed ? "exception" : undefined} /> : "—") },
        { title: "通过 / 失败 / 跳过", width: 145, render: (_, row) => `${row.completed} / ${row.failed} / ${row.skipped}` },
        { title: "接收 / 写入", width: 125, render: (_, row) => `${(row.received ?? 0).toLocaleString()} / ${(row.written ?? 0).toLocaleString()}` },
        { title: "来源", dataIndex: "source", width: 120, ellipsis: true, render: (_, row) => row.source ? <Tag color="geekblue">{row.source}</Tag> : "—" },
        { title: "质量", dataIndex: "qualityStatus", width: 105, render: (_, row) => row.qualityStatus ? <Tag color={row.qualityStatus === "passed" ? "success" : row.qualityStatus === "cached" ? "blue" : "warning"}>{row.qualityStatus}</Tag> : "—" },
        { title: "开始时间", dataIndex: "startedAt", width: 170, render: (value) => formatTime(String(value)) },
        { title: "结束时间", dataIndex: "finishedAt", width: 170, render: (value) => formatTime(value ? String(value) : null) },
        { title: "错误", dataIndex: "error", ellipsis: true, render: (_, row) => { const text = toText(row.error); return text ? <Tooltip title={text}><Typography.Text type="danger">{text}</Typography.Text></Tooltip> : "—"; } },
        { title: "日志", valueType: "option", fixed: "right", width: 95, render: (_, row) => <Button type="link" onClick={() => void openLogs(row)}>查看日志</Button> },
      ]} />

    {status.runId && status.failed > 0 ? <Button danger icon={<RetweetOutlined />} loading={submitting} disabled={status.status === "running"} onClick={() => void retryMinute()} style={{ marginTop: 8 }}>重试分钟失败项 {status.failed}</Button> : null}

    {status.runId ? (
      <ProTable<MinuteSyncItemDto>
        className="sync-minute-details"
        rowKey="code"
        search={false}
        options={false}
        dataSource={details.items}
        columns={minuteColumns}
        pagination={{ pageSize: 10, showSizeChanger: true }}
        style={{ marginTop: 16 }}
        locale={{ emptyText: <Empty description="本次运行暂无可展示的标的明细" /> }}
        headerTitle={`分钟同步明细（${details.items.length} 只${status.failed ? ` · ${status.failed} 失败` : ""}）`}
        toolBarRender={() => [
          <Typography.Text key="ledger" type="secondary" style={{ fontSize: 12 }}>
            账本：完成 {details.dayLedger.completed} 日 / 零行 {details.dayLedger.zeroRows} 日
          </Typography.Text>,
        ]}
      />
    ) : null}

    <div style={{ marginTop: 16 }}>
      <EtfSyncCard />
    </div>

    <div style={{ marginTop: 16 }}>
      <EngineJobsCard />
    </div>

    <Drawer title={activeRun ? `运行日志 · ${activeRun.kind}` : "运行日志"} open={activeRun !== null} onClose={() => setActiveRun(null)} destroyOnHidden width="min(1180px, 94vw)">
      {activeRun ? <Descriptions size="small" bordered column={2} style={{ marginBottom: 16 }} items={[
        { key: "id", label: "Run ID", children: <Typography.Text copyable>{activeRun.id}</Typography.Text> },
        { key: "status", label: "运行状态", children: (statusMeta[activeRun.status] ?? statusMeta.idle).text },
        { key: "params", label: "任务参数", span: 2, children: <Typography.Text code>{fmtParams(activeRun.parameters)}</Typography.Text> },
        { key: "result", label: "通过/失败/跳过", children: `${activeRun.completed}/${activeRun.failed}/${activeRun.skipped}` },
        { key: "io", label: "接收/写入", children: `${activeRun.received ?? 0}/${activeRun.written ?? 0}` },
      ]} /> : null}
      <ProTable<SyncLogDto> rowKey="id" search={false} options={false} loading={logLoading} dataSource={logs}
        headerTitle={`请求与质量日志（${logs.length}）`} pagination={{ pageSize: 20, showSizeChanger: true }} scroll={{ x: 1350 }} columns={[
          { title: "时间", dataIndex: "timestamp", width: 170, render: (value) => formatTime(String(value ?? "")) },
          { title: "级别", dataIndex: "level", width: 80, render: (_, row) => <Tag color={row.level === "error" ? "error" : row.level === "warning" ? "warning" : "blue"}>{row.level}</Tag> },
          { title: "股票", dataIndex: "code", width: 115, copyable: true, render: (_, row) => row.code ?? "—" },
          { title: "交易日", dataIndex: "tradeDate", width: 100, render: (_, row) => row.tradeDate ?? "—" },
          { title: "上游请求", dataIndex: "requestApi", width: 125, render: (_, row) => <Typography.Text code>{row.requestApi ?? "—"}</Typography.Text> },
          { title: "请求参数", dataIndex: "requestParams", width: 260, ellipsis: true, render: (_, row) => <Tooltip title={fmtParams(row.requestParams)}><Typography.Text code>{fmtParams(row.requestParams)}</Typography.Text></Tooltip> },
          { title: "尝试", dataIndex: "attempt", width: 65 },
          { title: "状态", dataIndex: "status", width: 90, render: (_, row) => <Tag color={["error", "gap"].includes(row.status) ? "error" : "success"}>{row.status}</Tag> },
          { title: "接收/写入", width: 105, render: (_, row) => `${row.received}/${row.written}` },
          { title: "来源", dataIndex: "source", width: 110, render: (_, row) => row.source ?? "—" },
          { title: "问题详情", dataIndex: "error", width: 280, render: (_, row) => row.error ? <Typography.Text type="danger" copyable>{row.error}</Typography.Text> : "—" },
        ]} />
    </Drawer>

    <Modal title={repairPreset ? `修复任务：${repairPreset.reason}` : "新建分钟线同步（stk_mins）"} open={minuteOpen} confirmLoading={submitting} onOk={() => void launchMinute()} onCancel={() => setMinuteOpen(false)} destroyOnHidden forceRender width={560}>
      <Form form={minuteForm} layout="vertical" initialValues={{ range: [dayjs().subtract(7, "day"), dayjs()], freq: "5m", code: "" }}>
        {repairPreset ? <Typography.Paragraph type="secondary">已根据数据中心风险项带入修复范围。确认后才会向上游发起请求。</Typography.Paragraph> : null}
        <Form.Item name="range" label="交易日期范围" rules={[{ required: true, message: "请选择日期范围" }]}><DatePicker.RangePicker style={{ width: "100%" }} /></Form.Item>
        <Form.Item name="freq" label="分钟频率" rules={[{ required: true }]}><Select options={[{ label: "5 分钟", value: "5m" }, { label: "1 分钟", value: "1m" }]} /></Form.Item>
        <Form.Item label="高级参数（留空使用服务端默认值）" style={{ marginBottom: 0 }} />
        <Space size="large" style={{ marginBottom: 16 }}>
          <Form.Item name="concurrency" label="并发线程数" tooltip="全市场同步并行拉取线程，1-25"><InputNumber min={1} max={25} placeholder="默认10" /></Form.Item>
          <Form.Item name="segmentDays" label="每段交易日跨度" tooltip="分钟接口单次请求覆盖的交易日数，1m≤45 / 5m≤180"><InputNumber min={1} max={180} placeholder="默认按频率" /></Form.Item>
          <Form.Item name="requestTimeoutMs" label="请求超时(ms)"><InputNumber min={1000} max={120000} placeholder="默认30000" /></Form.Item>
        </Space>
        <Form.Item name="code" label="股票（可选）" extra="支持代码和中文名称模糊搜索；留空同步全市场"><SecuritySelect /></Form.Item>
      </Form>
    </Modal>

    <Modal title={"同步工具：" + (activeTool?.label ?? "")} open={activeTool !== null} confirmLoading={submitting} onOk={() => void launchTool()} onCancel={() => setActiveTool(null)} destroyOnHidden forceRender width={520}>
      {activeTool?.toolId === "rt_idx_k" ? (
        <Typography.Paragraph>将通过 Promax 拉取上证、深证、创业板、科创 50 和沪深 300 实时日线快照，写入 <Typography.Text code>index_snapshots</Typography.Text>。任务日志会记录请求、数据源、返回数量与接收时间。</Typography.Paragraph>
      ) : activeTool?.toolId === "rt_idx_min" ? (
        <Form form={toolForm} layout="vertical">
          <Typography.Paragraph>将通过 Promax 拉取五大指数最新分钟行情，写入 <Typography.Text code>index_minute_bars</Typography.Text>，用于首页分时走势。</Typography.Paragraph>
          <Form.Item name="freq" label="分钟频率" rules={[{ required: true }]} extra="上游口径为大写（1MIN/5MIN/…）">
            <Select options={[{ label: "1 分钟", value: "1MIN" }, { label: "5 分钟", value: "5MIN" }, { label: "15 分钟", value: "15MIN" }, { label: "30 分钟", value: "30MIN" }, { label: "60 分钟", value: "60MIN" }]} />
          </Form.Item>
        </Form>
      ) : activeTool?.toolId === "idx_mins" ? (
        <Form form={toolForm} layout="vertical">
          <Typography.Paragraph>将从 Promax 补齐指定交易日、指定频率的五大指数分钟线（<Typography.Text code>09:30–15:00</Typography.Text>），写入 <Typography.Text code>index_minute_bars</Typography.Text>。</Typography.Paragraph>
          <Space size="large">
            <Form.Item name="day" label="交易日" rules={[{ required: true, message: "请选择交易日" }]}><DatePicker /></Form.Item>
            <Form.Item name="freq" label="分钟频率" rules={[{ required: true }]} extra="上游口径为小写（1min/5min/…）">
              <Select style={{ width: 140 }} options={[{ label: "1 分钟", value: "1min" }, { label: "5 分钟", value: "5min" }, { label: "15 分钟", value: "15min" }, { label: "30 分钟", value: "30min" }, { label: "60 分钟", value: "60min" }]} />
            </Form.Item>
          </Space>
        </Form>
      ) : activeTool?.kind === "market-snapshot" ? (
        <Typography.Paragraph>将拉取并更新 <Typography.Text code>security_master</Typography.Text>（全市场上市证券主档，覆盖增删改）。</Typography.Paragraph>
      ) : activeTool?.kind === "finance" ? (
        <Form form={toolForm} layout="vertical" initialValues={{ code: "" }}>
          <Form.Item name="code" label="股票" rules={[{ required: true, message: "该报表上游仅支持按单只股票查询，请选择股票" }]} extra="财务数据上游仅支持按股票查询（全市场按季度批量暂不被上游支持）"><SecuritySelect /></Form.Item>
        </Form>
      ) : (
        <Form form={toolForm} layout="vertical" initialValues={{ range: [dayjs().subtract(7, "day"), dayjs()] }}>
          <Form.Item name="range" label="交易日期范围" rules={[{ required: true, message: "请选择日期范围" }]} extra={activeTool?.kind === "market-calendar" ? "拉取区间内的交易日历" : "拉取区间内每个交易日的该工具数据"}><DatePicker.RangePicker style={{ width: "100%" }} /></Form.Item>
        </Form>
      )}
    </Modal>
  </PageContainer>;
}

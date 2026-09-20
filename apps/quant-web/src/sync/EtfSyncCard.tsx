import { useCallback, useEffect, useState } from "react";
import { App, Alert, Badge, Button, Card, DatePicker, Descriptions, Form, InputNumber, Modal, Space, Statistic, Tag, Typography } from "antd";
import { CloudSyncOutlined, ReloadOutlined, RetweetOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";

import { etfSyncApi, type EtfBarStateDto, type EtfSyncStatusDto, type SyncStatus } from "../api";
import { fmtDateTime, fmtInt } from "../format";

/**
 * ETF 数据同步卡（9101 `/sync/etf/*`，共 8 个端点）。
 *
 * 原先这套后端能力在前端**完全不可达**：同步中心的工具目录只列 `tool_sync` 里的股票工具
 * （`CATALOG` 无 ETF 项），Dashboard 与 ETF 量化页也没有入口，ETF 池/日线/指数日线一旦落后
 * 就只能靠手工 curl。这里补齐：池同步、单日日线、单日指数日线、历史区间回填与状态展示。
 */

const STATUS_META: Record<SyncStatus, { status: "default" | "processing" | "success" | "error" | "warning"; text: string }> = {
  idle: { status: "default", text: "空闲" },
  starting: { status: "processing", text: "提交中" },
  running: { status: "processing", text: "运行中" },
  complete: { status: "success", text: "已完成" },
  complete_with_gaps: { status: "warning", text: "完成但有缺口" },
  error: { status: "error", text: "异常" },
};
const meta = (status: SyncStatus) => STATUS_META[status] ?? STATUS_META.idle;
const working = (status: SyncStatus) => status === "running" || status === "starting";

function StateBlock({ title, state, extra }: { title: string; state: EtfBarStateDto; extra?: React.ReactNode }) {
  const m = meta(state.status);
  const percent = state.totalDays ? Math.min(100, Math.round((state.completedDays / state.totalDays) * 100)) : 0;
  return (
    <div style={{ flex: "1 1 260px", minWidth: 240, border: "1px solid #e5eaee", borderRadius: 10, padding: "12px 14px" }}>
      <Space size={6} style={{ marginBottom: 8 }}>
        <b style={{ fontSize: 13 }}>{title}</b>
        <Badge status={m.status} text={m.text} />
      </Space>
      <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginBottom: 8 }}>
        <Statistic title="已写入" value={state.written} valueStyle={{ fontSize: 16 }} />
        <Statistic title="目标交易日" value={state.totalDays} valueStyle={{ fontSize: 16 }} />
        <Statistic title="已完成" value={state.completedDays} valueStyle={{ fontSize: 16 }} />
        {state.failedDays ? <Statistic title="未通过" value={state.failedDays} valueStyle={{ fontSize: 16, color: "#d46b08" }} /> : null}
      </div>
      {working(state.status) && state.totalDays ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          进度 {percent}%{state.currentDate ? ` · 当前 ${state.currentDate}` : ""}
        </Typography.Text>
      ) : null}
      <div style={{ fontSize: 12, color: "#8a94a0", marginTop: 4 }}>
        开始 {fmtDateTime(state.startedAt)} · 结束 {fmtDateTime(state.finishedAt)}
      </div>
      {state.error ? <Typography.Text type="danger" style={{ fontSize: 12 }}>{state.error}</Typography.Text> : null}
      {extra}
    </div>
  );
}

export default function EtfSyncCard() {
  const { message } = App.useApp();
  const [status, setStatus] = useState<EtfSyncStatusDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [form] = Form.useForm<{ range: [Dayjs, Dayjs]; days: number; concurrency: number }>();

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await etfSyncApi.status());
    } catch (error) {
      if (!silent) message.error("读取 ETF 同步状态失败：" + String((error as Error).message ?? error));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [message]);

  useEffect(() => { void load(); }, [load]);

  // daily / index 是 202 后台任务，运行期间轮询状态
  const running = !!status && (working(status.daily.status) || working(status.index.status));
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void load(true), 6000);
    return () => window.clearInterval(timer);
  }, [running, load]);

  /** 统一的"执行一次同步动作"包装：错误直接透出后端文案（例如缺少凭据/ETF 池为空）。 */
  const run = async (key: string, action: () => Promise<unknown>, success: string) => {
    setBusy(key);
    try {
      await action();
      message.success(success);
      await load(true);
    } catch (error) {
      message.error(String((error as Error).message ?? error));
    } finally {
      setBusy(null);
    }
  };

  const today = dayjs().format("YYYYMMDD");
  const submitHistory = async () => {
    const values = await form.validateFields();
    const start = values.range[0].format("YYYYMMDD");
    const end = values.range[1].format("YYYYMMDD");
    setBusy("history");
    try {
      // 两个回填入口共用 etf 对象但各持一把锁，可以并行提交
      await Promise.all([
        etfSyncApi.history(start, end, values.days, { concurrency: values.concurrency }),
        etfSyncApi.indexHistory(start, end, values.days, { concurrency: values.concurrency }),
      ]);
      message.success("ETF 与指数日线历史回填已提交（后台执行）");
      setHistoryOpen(false);
      await load(true);
    } catch (error) {
      message.error(String((error as Error).message ?? error));
    } finally {
      setBusy(null);
    }
  };

  const counts = status?.counts;

  return (
    <Card
      className="etf-sync-card"
      size="small"
      title={<Space><CloudSyncOutlined /> ETF 数据同步</Space>}
      extra={
        <Space>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button>
          <Button size="small" type="primary" icon={<RetweetOutlined />} disabled={running} onClick={() => {
            form.setFieldsValue({ range: [dayjs().subtract(1, "year"), dayjs()], days: 260, concurrency: 4 });
            setHistoryOpen(true);
          }}>历史回填</Button>
        </Space>
      }
    >
      {status?.daily.error || status?.index.error ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="ETF 同步最近一次执行失败"
          description={status.index.error ?? status.daily.error}
        />
      ) : null}

      <Space size={[24, 8]} wrap style={{ marginBottom: 12 }}>
        <Statistic title="ETF 池（已登记）" value={counts?.universe ?? 0} formatter={(v) => fmtInt(Number(v))} valueStyle={{ fontSize: 18 }} />
        <Statistic title="可交易 ETF" value={counts?.eligible ?? 0} valueStyle={{ fontSize: 18, color: "#0baa81" }} />
        <Statistic title="ETF 日线行数" value={counts?.etfBars ?? 0} formatter={(v) => fmtInt(Number(v))} valueStyle={{ fontSize: 18 }} />
        <Statistic title="指数日线行数" value={counts?.indexBars ?? 0} formatter={(v) => fmtInt(Number(v))} valueStyle={{ fontSize: 18 }} />
      </Space>

      <Descriptions size="small" column={{ xs: 1, sm: 2, md: 4 }} style={{ marginBottom: 12 }}>
        <Descriptions.Item label="ETF 池状态">
          <Space size={6}>
            <Badge status={meta(status?.universe.status ?? "idle").status} text={meta(status?.universe.status ?? "idle").text} />
            <Tag color="blue">{status?.universe.count ?? 0} 只</Tag>
            {status?.universe.eligible ? <Tag color="green">可交易 {status.universe.eligible}</Tag> : null}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="池更新时间">{fmtDateTime(status?.universe.latestAt)}</Descriptions.Item>
        <Descriptions.Item label="状态来源">{status?.universe.runId ?? "—"}</Descriptions.Item>
      </Descriptions>

      <Space size={[8, 8]} wrap style={{ marginBottom: 12 }}>
        <Button size="small" loading={busy === "universe"} disabled={running}
          onClick={() => void run("universe", () => etfSyncApi.syncUniverse(), "ETF 池已同步")}>
          同步 ETF 池
        </Button>
        <Button size="small" loading={busy === "daily"} disabled={running}
          onClick={() => void run("daily", () => etfSyncApi.syncDaily(today), `ETF 日线已同步（${today}）`)}>
          同步当日 ETF 日线
        </Button>
        <Button size="small" loading={busy === "index"} disabled={running}
          onClick={() => void run("index", () => etfSyncApi.syncIndexDaily(today), `指数日线已同步（${today}）`)}>
          同步当日指数日线
        </Button>
      </Space>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <StateBlock title="ETF 日线回填" state={status?.daily ?? EMPTY_STATE} />
        <StateBlock title="指数日线回填" state={status?.index ?? EMPTY_STATE} />
      </div>

      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
        口径：ETF 池来自 <Typography.Text code>fund_basic</Typography.Text>，日线与指数日线分别落
        <Typography.Text code>etf_daily_bars</Typography.Text> / <Typography.Text code>index_daily_bars</Typography.Text>；
        「可交易 ETF」按上市状态与存续期过滤，是 walkforward 与模拟盘的选池来源。历史回填是后台任务，
        会先查本地覆盖再只补缺口。
      </Typography.Paragraph>

      <Modal
        title="ETF / 指数日线历史回填"
        open={historyOpen}
        confirmLoading={busy === "history"}
        onOk={() => void submitHistory()}
        onCancel={() => setHistoryOpen(false)}
        destroyOnHidden
        width={520}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="range" label="交易日期范围" rules={[{ required: true, message: "请选择日期范围" }]}>
            <DatePicker.RangePicker style={{ width: "100%" }} />
          </Form.Item>
          <Space size="large">
            <Form.Item name="days" label="目标交易日数" tooltip="用于估算缺口规模，服务端据此判定完成度">
              <InputNumber min={1} max={5000} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发线程数" tooltip="1-25">
              <InputNumber min={1} max={25} />
            </Form.Item>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            将同时提交 ETF 日线与指数日线两个回填任务；已完成的日期会被跳过。
          </Typography.Text>
        </Form>
      </Modal>
    </Card>
  );
}

const EMPTY_STATE: EtfBarStateDto = {
  status: "idle", totalDays: 0, completedDays: 0, failedDays: 0, gapDates: [],
  currentDate: null, written: 0, startedAt: null, finishedAt: null, error: null, runId: null,
};

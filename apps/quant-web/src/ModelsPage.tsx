import { useCallback, useEffect, useState } from "react";
import { App, Button, Card, Descriptions, Empty, Space, Table, Tag, Typography } from "antd";
import { ReloadOutlined, StopOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";
import { etfApi, engineApi, type EtfTrainDto } from "./api";
import { fmtDate, fmtNum } from "./format";
import { useJobPolling } from "./useJobPolling";

const { Text, Paragraph } = Typography;

/** 本页展示口径：IC 类指标保留 4 位小数。 */
const fmtIc = (v: number | null | undefined) => fmtNum(v, 4);

export default function ModelsPage() {
  const { message } = App.useApp();
  const [data, setData] = useState<EtfTrainDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);

  const poll = useJobPolling({
    fetchJob: etfApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 4000,
    onComplete: async () => { setRunning(false); message.success("训练完成，已刷新"); await load(); },
    onError: (m) => { setRunning(false); message.error("训练失败：" + m); },
    onCancelled: () => { setRunning(false); message.info("训练任务已取消"); },
    onPollError: () => { setRunning(false); },
    onTimeout: () => { setRunning(false); message.error("训练轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await etfApi.trainLatest());
    } catch (e) {
      message.error("获取训练记录失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
    return () => { poll.stop(); };
  }, [load]);

  const run = async () => {
    if (running) return;
    setRunning(true);
    try {
      const job = await etfApi.startJob("etf_train");
      message.info("模型训练已启动，运行中…");
      poll.watch(job);
    } catch (e) {
      setRunning(false);
      message.error("启动失败：" + String((e as Error).message ?? e));
    }
  };

  const p = data?.params ?? {};
  const history = data?.history ?? [];

  return (
    <PageContainer
      title="ETF 模型训练 · LGBMRanker"
      subTitle={data ? `label ${data.label} · 最近训练 ${fmtDate(data.generatedAt)} · 共 ${history.length} 次` : "加载中…"}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} onClick={run} loading={running} disabled={running}>重新训练模型</Button>
          {running ? <Button danger icon={<StopOutlined />} loading={poll.cancelling} onClick={() => void poll.cancel()}>取消训练</Button> : null}
        </Space>
      }
    >
      {data?.status === "none" ? (
        <Card style={{ border: "1px solid #e5eaee", borderRadius: 10 }}>
          <Empty description="暂无训练记录（点击右上角「重新训练模型」开始）" style={{ padding: 40 }} />
        </Card>
      ) : (
        <>
          <Card size="small" title="最近一次训练" style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
              {[
                { t: "验证集 RankIC", v: fmtIc(data?.rankIc), c: (data?.rankIc ?? 0) > 0.03 ? "#0baa81" : "#e5a33b" },
                { t: "训练期 IC 均值", v: fmtIc(data?.icMean), c: "#0baa81" },
                { t: "ICIR", v: fmtIc(data?.icir) },
                { t: "模型", v: String(p.type ?? "lgbm_ranker") },
                { t: "nEstimators", v: String(p.nEstimators ?? "—") },
                { t: "learningRate", v: String(p.learningRate ?? "—") },
                { t: "numLeaves", v: String(p.numLeaves ?? "—") },
              ].map((x) => (
                <div key={x.t} style={{ flex: "1 1 120px", minWidth: 110, background: "#f7f9fb", borderRadius: 8, padding: "10px 12px" }}>
                  <div style={{ fontSize: 11, color: "#5f6b76" }}>{x.t}</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: x.c ?? "#1a232c", marginTop: 2 }}>{x.v}</div>
                </div>
              ))}
            </div>
            <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ fontSize: 12 }}>
              <Descriptions.Item label="训练区间">{fmtDate(data?.trainStart)} → {fmtDate(data?.trainEnd)}</Descriptions.Item>
              <Descriptions.Item label="验证区间">{fmtDate(data?.validStart)} → {fmtDate(data?.validEnd)}</Descriptions.Item>
              <Descriptions.Item label="测试区间">{fmtDate(data?.testStart)} → {fmtDate(data?.testEnd)}</Descriptions.Item>
              <Descriptions.Item label="相对 label">{String(p.labelRelative ?? "—")}</Descriptions.Item>
              <Descriptions.Item label="模型文件" span={2}>{data?.modelPath ?? "—"}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Card size="small" title="训练历史" extra={<Text type="secondary" style={{ fontSize: 11 }}>按时间倒序</Text>}
            style={{ border: "1px solid #e5eaee", borderRadius: 10, marginTop: 14 }} bodyStyle={{ padding: "8px 0 0" }}>
            <Table
              size="small" rowKey="runId" dataSource={history} loading={loading} pagination={false}
              locale={{ emptyText: <Empty description="暂无记录" /> }}
              columns={[
                { title: "时间", dataIndex: "generatedAt", width: 150, render: (v) => { const d = new Date(v); return `${d.toLocaleDateString("zh-CN")} ${d.toLocaleTimeString("zh-CN", { hour12: false })}`; } },
                { title: "label", dataIndex: "label", width: 100 },
                { title: "训练区间", width: 180, render: (_, r) => `${fmtDate(r.trainStart)} → ${fmtDate(r.trainEnd)}` },
                { title: "RankIC", dataIndex: "rankIc", width: 90, sorter: (a, b) => a.rankIc - b.rankIc, render: (v) => <span style={{ color: v > 0.03 ? "#0baa81" : v > 0 ? "#1a232c" : "#e5524f" }}>{fmtIc(v)}</span> },
                { title: "IC 均值", dataIndex: "icMean", width: 90, render: (v) => fmtIc(v) },
                { title: "ICIR", dataIndex: "icir", width: 80, render: (v) => fmtIc(v) },
                { title: "状态", dataIndex: "status", width: 90, render: (v) => <Tag color={v === "complete" ? "green" : v === "error" ? "red" : "default"}>{v}</Tag> },
              ]}
            />
          </Card>

          <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 14, marginBottom: 0 }}>
            口径：LGBMRanker 排序模型，11 因子（featuresOverride），相对 forward_20 label，训练 200 天 → 验证 40 天 → 测试 40 天；RankIC 为验证集上相对收益的 Spearman 相关。单次训练仅供监测模型有效性，正式持仓信号以「ETF 量化」页滚动评估（walkforward 25 窗口）为准。
          </Paragraph>
        </>
      )}
    </PageContainer>
  );
}

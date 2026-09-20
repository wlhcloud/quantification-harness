import { useCallback, useEffect, useState } from "react";
import { App, Button, Popconfirm, Progress, Space, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined, StopOutlined } from "@ant-design/icons";
import { ProTable, type ProColumns } from "@ant-design/pro-components";

import { engineApi, type EngineJobDto, type EngineJobStatus } from "../api";
import { fmtDateTime, fmtParamsText } from "../format";

/**
 * 计算引擎任务中心（9102 `GET /api/v1/jobs` + `POST /api/v1/jobs/{id}/cancel`）。
 *
 * 这两个端点一直存在，但前端零调用：用户提交训练/回测/选股后既看不到任务列表，
 * 也无法取消——轮询 90 分钟超时后只能干等。这里把列表、进度与取消补齐。
 */

/** job 类型展示名；未登记的保留原始 id，便于识别新增类型。 */
const JOB_TYPE_LABELS: Record<string, string> = {
  features: "特征构建",
  factor_mining: "因子挖掘",
  training: "模型训练",
  backtest: "策略回测",
  inference: "推理",
  etf_factors: "ETF 因子快照",
  etf_research: "ETF 因子研究",
  etf_train: "ETF 模型训练",
  etf_backtest: "ETF 回测",
  etf_walkforward: "ETF 滚动评估",
  stock_ml_factors: "股票 ML 因子",
  stock_walkforward: "股票 ML 滚动训练",
  stock_ml_backtest: "股票 ML 回测",
  stock_ml_predict: "股票 ML 选股预测",
  stock_industry_factors: "行业因子",
  stock_money_flow_factors: "资金流因子",
};

const STATUS_META: Record<EngineJobStatus, { color: string; text: string }> = {
  queued: { color: "default", text: "排队中" },
  running: { color: "processing", text: "运行中" },
  complete: { color: "success", text: "已完成" },
  error: { color: "error", text: "异常" },
  cancelled: { color: "warning", text: "已取消" },
};

const isActive = (job: EngineJobDto) => job.status === "queued" || job.status === "running";

export default function EngineJobsCard({ limit = 50 }: { limit?: number }) {
  const { message } = App.useApp();
  const [jobs, setJobs] = useState<EngineJobDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setJobs(await engineApi.jobs(limit));
    } catch (error) {
      if (!silent) message.error("读取计算任务失败：" + String((error as Error).message ?? error));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [limit, message]);

  useEffect(() => { void load(); }, [load]);

  // 有活跃任务时自动刷新；全部终结后停止轮询，避免无意义的后台请求
  const activeCount = jobs.filter(isActive).length;
  useEffect(() => {
    if (!activeCount) return;
    const timer = window.setInterval(() => void load(true), 5000);
    return () => window.clearInterval(timer);
  }, [activeCount, load]);

  const cancel = async (job: EngineJobDto) => {
    setCancelling(job.id);
    try {
      const updated = await engineApi.cancelJob(job.id);
      if (updated.status === "cancelled") message.success("任务已取消");
      else message.info("已请求取消；处理器会在下一个检查点退出（长任务可能需要一会儿）");
      await load(true);
    } catch (error) {
      message.error("取消失败：" + String((error as Error).message ?? error));
    } finally {
      setCancelling(null);
    }
  };

  const columns: ProColumns<EngineJobDto>[] = [
    {
      title: "任务",
      dataIndex: "type",
      width: 190,
      render: (_, row) => (
        <div className="tool-name">
          <b>{JOB_TYPE_LABELS[row.type] ?? row.type}</b>
          <span>{row.type}</span>
        </div>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (_, row) => {
        const meta = STATUS_META[row.status] ?? { color: "default", text: row.status };
        return (
          <Space size={4}>
            <Tag color={meta.color}>{meta.text}</Tag>
            {row.cancel_requested && row.status !== "cancelled" ? <Tag color="warning">取消中</Tag> : null}
          </Space>
        );
      },
    },
    {
      title: "进度",
      dataIndex: "progress",
      width: 150,
      render: (_, row) =>
        isActive(row) ? (
          <Progress percent={Math.min(100, Math.round(row.progress ?? 0))} size="small" />
        ) : row.status === "complete" ? (
          <Progress percent={100} size="small" />
        ) : (
          "—"
        ),
    },
    { title: "创建时间", dataIndex: "created_at", width: 165, render: (_, row) => fmtDateTime(row.created_at) },
    { title: "结束时间", dataIndex: "finished_at", width: 165, render: (_, row) => fmtDateTime(row.finished_at) },
    { title: "耗时", width: 90, render: (_, row) => elapsed(row) },
    {
      title: "参数",
      dataIndex: "parameters",
      ellipsis: true,
      render: (_, row) => <Typography.Text code>{fmtParamsText(row.parameters)}</Typography.Text>,
    },
    {
      title: "错误 / 结果",
      width: 220,
      ellipsis: true,
      render: (_, row) => {
        if (row.error) {
          return (
            <Tooltip title={row.error}>
              <Typography.Text type="danger">{row.error}</Typography.Text>
            </Tooltip>
          );
        }
        if (row.status === "complete" && row.result) {
          const text = fmtParamsText(row.result);
          return <Tooltip title={text}><Typography.Text type="secondary">{text}</Typography.Text></Tooltip>;
        }
        return "—";
      },
    },
    {
      title: "操作",
      valueType: "option",
      fixed: "right",
      width: 90,
      render: (_, row) =>
        isActive(row) ? (
          <Popconfirm
            title="取消该任务？"
            description="取消是协作式的：处理器会在下一个检查点退出，已写入的部分结果不会回滚。"
            okText="取消任务"
            cancelText="再想想"
            onConfirm={() => void cancel(row)}
          >
            <Button type="link" danger icon={<StopOutlined />} loading={cancelling === row.id}>取消</Button>
          </Popconfirm>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <ProTable<EngineJobDto>
      className="engine-jobs"
      rowKey="id"
      search={false}
      options={false}
      loading={loading}
      dataSource={jobs}
      columns={columns}
      scroll={{ x: 1450 }}
      pagination={{ pageSize: 10, showSizeChanger: true }}
      headerTitle={`计算引擎任务（${jobs.length}${activeCount ? ` · ${activeCount} 个进行中` : ""}）`}
      toolBarRender={() => [
        <Button key="refresh" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>刷新</Button>,
      ]}
    />
  );
}

/** 运行时长：活跃任务算到"现在"，终结任务算 finished_at - started_at。 */
function elapsed(job: EngineJobDto): string {
  if (!job.started_at) return "—";
  const start = new Date(job.started_at).getTime();
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

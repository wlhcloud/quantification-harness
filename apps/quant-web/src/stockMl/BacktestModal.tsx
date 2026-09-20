import { useState } from "react";
import { App, Button, Card, DatePicker, Empty, InputNumber, Modal, Space, Statistic, Table, Tag } from "antd";
import { PlayCircleOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";

import type { StockMlBacktestDto, StockMlWalkforwardListItem } from "../api";
import { dateText, fileBaseName } from "./format";
import { NavChart } from "./nav-chart";

export interface BacktestModalProps {
  open: boolean;
  version: StockMlWalkforwardListItem | null;
  onClose: () => void;
  onRun: (params: { runId: string; startDate: string; endDate: string; topN: number }) => void;
  loading: boolean;
  /** job 进度（0-100），null 表示尚未上报 */
  progress: number | null;
  result: StockMlBacktestDto | null;
}

/**
 * 指定模型回测弹窗。
 * 参数状态（起止日期/TopN）就放在弹窗里——原先挂在页面组件上，只有这个弹窗用得到。
 */
export function BacktestModal({ open, version, onClose, onRun, loading, progress, result }: BacktestModalProps) {
  const { message } = App.useApp();
  const [startDate, setStartDate] = useState<Dayjs | null>(dayjs("2024-01-01"));
  const [endDate, setEndDate] = useState<Dayjs | null>(dayjs("2024-12-31"));
  const [topN, setTopN] = useState<number>(5);

  const handleRun = () => {
    if (!version) return;
    if (!startDate || !endDate) {
      message.warning("请选择回测起止日期");
      return;
    }
    onRun({
      runId: version.runId,
      startDate: startDate.format("YYYY-MM-DD"),
      endDate: endDate.format("YYYY-MM-DD"),
      topN,
    });
  };

  return (
    <Modal
      title={version ? `模型回测 · ${version.runId}` : "模型回测"}
      open={open}
      onCancel={onClose}
      width={900}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        <Button key="run" type="primary" icon={<PlayCircleOutlined />} loading={loading} onClick={handleRun}>
          执行回测{loading && progress != null ? `（${Math.round(progress)}%）` : ""}
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: "100%" }} size={16}>
        {/* 回测参数配置 */}
        <Card size="small" title="回测参数" style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
          <Space size={20} wrap>
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>开始日期</div>
              <DatePicker value={startDate} onChange={(v) => setStartDate(v)} style={{ width: 160 }} />
            </div>
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>结束日期</div>
              <DatePicker value={endDate} onChange={(v) => setEndDate(v)} style={{ width: 160 }} />
            </div>
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>持仓数（Top N）</div>
              <InputNumber min={1} max={50} value={topN} onChange={(v) => setTopN(v ?? 5)} style={{ width: 100 }} />
            </div>
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>模型版本</div>
              <Tag color="purple" style={{ fontSize: 11 }}>
                {(version?.model.nSeeds ?? 1) > 1
                  ? `${version?.model.nSeeds}种子·${version?.model.ensembleMethod === "median_zscore" ? "中位数" : "均值"}`
                  : `单种子${version?.model.randomState}`}
                {" · "}{version?.featureCount}特征
              </Tag>
            </div>
          </Space>
        </Card>

        {/* 回测结果 */}
        {result && (
          <>
            <Card size="small" title="回测指标" style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
              <Space size={32} wrap>
                <Statistic title="总收益" value={result.metrics.totalReturn * 100} precision={2} suffix="%"
                  valueStyle={{ color: result.metrics.totalReturn > 0 ? "#cf1322" : "#3f8600", fontSize: 22, fontWeight: 600 }} />
                <Statistic title="基准收益" value={result.metrics.benchmarkReturn * 100} precision={2} suffix="%" valueStyle={{ fontSize: 18 }} />
                <Statistic title="超额收益" value={result.metrics.excessReturn * 100} precision={2} suffix="%"
                  valueStyle={{ color: result.metrics.excessReturn > 0 ? "#cf1322" : "#3f8600", fontSize: 18 }} />
                <Statistic title="年化收益" value={result.metrics.annualReturn * 100} precision={2} suffix="%" valueStyle={{ fontSize: 18 }} />
                <Statistic title="Sharpe" value={result.metrics.sharpe} precision={3} valueStyle={{ fontSize: 18 }} />
                <Statistic title="最大回撤" value={result.metrics.maxDrawdown * 100} precision={2} suffix="%" valueStyle={{ color: "#cf1322", fontSize: 18 }} />
                <Statistic title="交易天数" value={result.metrics.tradingDays} valueStyle={{ fontSize: 18 }} />
              </Space>
              <div style={{ marginTop: 8, fontSize: 11, color: "#8a94a6" }}>
                回测区间：{dateText(result.metrics.startDate)} ~ {dateText(result.metrics.endDate)}
                {" · "}模型文件：{fileBaseName(result.metrics.modelFile)}
              </div>
            </Card>

            {/* 净值曲线 */}
            <Card size="small" title="净值曲线" style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: 0 }}>
              <div style={{ width: "100%", height: 280, padding: "8px 12px" }}>
                <NavChart daily={result.daily} />
              </div>
            </Card>

            {/* 最新持仓 */}
            {result.holdings && result.holdings.length > 0 && (
              <Card size="small" title={`最新持仓（Top ${result.holdings.length}）`} style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
                <Table dataSource={result.holdings} rowKey="code" size="small" pagination={false} tableLayout="fixed">
                  <Table.Column title="排名" dataIndex="rank" key="rank" width={60} />
                  <Table.Column title="股票代码" dataIndex="code" key="code" width={120} render={(v: string) => <span style={{ fontFamily: "monospace" }}>{v}</span>} />
                  <Table.Column title="模型分数" dataIndex="score" key="score" render={(v: number) => Number(v).toFixed(4)} />
                </Table>
              </Card>
            )}
          </>
        )}

        {!result && !loading && (
          <Empty description="配置参数后点击「执行回测」" style={{ padding: 40 }} />
        )}
      </Space>
    </Modal>
  );
}

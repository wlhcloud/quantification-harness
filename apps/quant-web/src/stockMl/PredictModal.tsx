import { useState } from "react";
import { Button, Card, Empty, InputNumber, Modal, Space, Statistic, Table, Tag } from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";

import type { StockMlPredictDto, StockMlWalkforwardListItem } from "../api";
import { dateText, fileBaseName, fmtPct } from "./format";

export interface PredictModalProps {
  open: boolean;
  version: StockMlWalkforwardListItem | null;
  onClose: () => void;
  onRun: (params: { runId: string; topN: number }) => void;
  loading: boolean;
  /** job 进度（0-100），null 表示尚未上报 */
  progress: number | null;
  result: StockMlPredictDto | null;
}

/**
 * 指定模型预测选股弹窗。
 * 两点刻意保留：① 预览不改信号，但要先说清楚（Modal.confirm 二次确认）；
 * ② 参数（TopN）状态就放在弹窗里（原先挂在页面上，只有这里用得到）。
 */
export function PredictModal({ open, version, onClose, onRun, loading, progress, result }: PredictModalProps) {
  const [topN, setTopN] = useState<number>(10);

  // 预览动作不改信号，但影响面容易被误解（选股页显示的信号由 walkforward 写入），先说明再执行
  const confirmRun = () => {
    if (!version) return;
    Modal.confirm({
      title: "用该模型预览选股结果？",
      content: "这是只读预览：不会写回 selection_candidates。"
        + "T+1 选股页与模拟盘使用的信号由「重训 walkforward」写入，两者是独立的。",
      okText: "开始预览",
      cancelText: "取消",
      onOk: () => onRun({ runId: version.runId, topN }),
    });
  };

  return (
    <Modal
      title={version ? `模型预测选股 · ${version.runId}` : "模型预测选股"}
      open={open}
      onCancel={onClose}
      width={800}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        <Button key="run" type="primary" icon={<ThunderboltOutlined />} loading={loading} onClick={confirmRun}>
          执行预测{loading && progress != null ? `（${Math.round(progress)}%）` : ""}
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: "100%" }} size={16}>
        {/* 预测参数配置 */}
        <Card size="small" title="预测参数" style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
          <Space size={20} wrap>
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>选股数量（Top N）</div>
              <InputNumber min={1} max={50} value={topN} onChange={(v) => setTopN(v ?? 10)} style={{ width: 100 }} />
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
            <div>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>回测总收益</div>
              <span style={{ fontSize: 16, fontWeight: 600, color: version && version.metrics.totalReturn > 0 ? "#cf1322" : "#3f8600" }}>
                {version ? fmtPct(version.metrics.totalReturn) : "—"}
              </span>
            </div>
          </Space>
        </Card>

        {/* 预测结果 */}
        {result && (
          <>
            <Card size="small" title="预测结果" style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: "12px 16px" }}>
              <Space size={32} wrap>
                <Statistic title="预测交易日" value={dateText(result.tradeDate)} valueStyle={{ fontSize: 16 }} />
                <Statistic title="候选池" value={result.candidateCount} valueStyle={{ fontSize: 16 }} />
                <Statistic title="特征数" value={result.featureCount} valueStyle={{ fontSize: 16 }} />
                <Statistic title="选股数" value={result.count} valueStyle={{ fontSize: 16 }} />
              </Space>
              <div style={{ marginTop: 8, fontSize: 11, color: "#8a94a6" }}>
                模型文件：{fileBaseName(result.modelFile)}
              </div>
            </Card>

            {/* 选股列表 */}
            <Card size="small" title={`推荐股票（Top ${result.count}）`} style={{ border: "1px solid #e5eaee", borderRadius: 8 }} bodyStyle={{ padding: 0 }}>
              <Table dataSource={result.items} rowKey="rank" size="small" pagination={false} tableLayout="fixed">
                <Table.Column title="排名" dataIndex="rank" key="rank" width={60} />
                <Table.Column title="股票代码" dataIndex="code" key="code" width={120} render={(v: string) => <span style={{ fontFamily: "monospace", fontWeight: 600 }}>{v}</span>} />
                <Table.Column title="模型分数" dataIndex="score" key="score" render={(v: number) => Number(v).toFixed(4)} />
                <Table.Column title="20日动量" dataIndex="momentum20" key="momentum20" render={(v: number | null) => v == null ? "—" : `${(v * 100).toFixed(2)}%`} />
                <Table.Column title="行业排名" dataIndex="industryRank20" key="industryRank20" render={(v: number | null) => v == null ? "—" : `${(v * 100).toFixed(1)}%`} />
                <Table.Column title="市值(亿)" dataIndex="mktCap" key="mktCap" render={(v: number | null) => v == null ? "—" : (v / 1e8).toFixed(1)} />
              </Table>
            </Card>

            <div style={{ fontSize: 11, color: "#8a94a6", padding: "0 8px" }}>
              说明：以上为模型基于最新交易日因子数据的排序预测，仅供参考，不构成投资建议。实际买入需结合大盘环境、板块热度和个股基本面综合判断。
            </div>
          </>
        )}

        {!result && !loading && (
          <Empty description="配置参数后点击「执行预测」" style={{ padding: 40 }} />
        )}
      </Space>
    </Modal>
  );
}

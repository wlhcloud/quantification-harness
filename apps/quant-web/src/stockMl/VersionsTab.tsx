import { useState } from "react";
import { App, Button, Card, Input, Popconfirm, Space, Table, Tag } from "antd";
import { CheckCircleOutlined, DeleteOutlined, PlayCircleOutlined, ThunderboltOutlined } from "@ant-design/icons";

import { stockMlApi, type StockMlWalkforwardListItem } from "../api";
import { dateText, fmtNum, fmtPct } from "./format";

export interface VersionsTabProps {
  versions: StockMlWalkforwardListItem[];
  /** 发布/删除后刷新整页数据（版本列表 + 最新版本指标） */
  onReload: () => Promise<void>;
  /** 备注保存后本地 patch，避免整页 loading */
  onPatchNotes: (runId: string, notes: string) => void;
  onSelectVersion: (version: StockMlWalkforwardListItem) => void;
  onBacktest: (version: StockMlWalkforwardListItem) => void;
  onPredict: (version: StockMlWalkforwardListItem) => void;
}

/**
 * 「训练版本」页签：历史训练记录表 + 发布/删除/备注/回测/预测入口。
 *
 * 拆分说明：编辑备注、发布中、删除中这三个状态只有本页签用得到（原先挂在 1000 行的页面组件上），
 * 现在连同它们的处理器一起搬进来；对外只暴露"数据 + 四个回调"。
 */
export function VersionsTab({
  versions, onReload, onPatchNotes, onSelectVersion, onBacktest, onPredict,
}: VersionsTabProps) {
  const { message } = App.useApp();
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [editingNotesId, setEditingNotesId] = useState<string | null>(null);
  const [editingNotesText, setEditingNotesText] = useState("");
  const [savingNotesId, setSavingNotesId] = useState<string | null>(null);

  // 发布指定版本（设为当前正式使用的模型）
  const handlePublish = async (runId: string, label: string) => {
    if (publishingId) return;
    setPublishingId(runId);
    try {
      const result = await stockMlApi.walkforwardPublish(runId);
      if (result.ok) {
        message.success(`版本已发布：${label}`);
        await onReload();  // 刷新列表和最新版本
      } else {
        message.error("发布失败：" + (result.error ?? "未知错误"));
      }
    } catch (e) {
      message.error("发布失败：" + String((e as Error).message ?? e));
    } finally {
      setPublishingId(null);
    }
  };

  // 删除指定版本（已发布的版本不允许删除）
  const handleDelete = async (runId: string, label: string) => {
    if (deletingId) return;
    if (!window.confirm(`确定要删除版本「${label}」吗？此操作不可恢复。`)) return;
    setDeletingId(runId);
    try {
      const result = await stockMlApi.walkforwardDelete(runId);
      if (result.ok) {
        message.success(`版本已删除：${label}`);
        await onReload();
      } else {
        message.error("删除失败：" + (result.error ?? "未知错误"));
      }
    } catch (e) {
      message.error("删除失败：" + String((e as Error).message ?? e));
    } finally {
      setDeletingId(null);
    }
  };

  const handleEditNotes = (runId: string, currentNotes: string) => {
    setEditingNotesId(runId);
    setEditingNotesText(currentNotes || "");
  };

  const handleSaveNotes = async (runId: string) => {
    if (savingNotesId) return;
    setSavingNotesId(runId);
    try {
      const result = await stockMlApi.walkforwardUpdateNotes(runId, editingNotesText);
      if (result.ok) {
        message.success("备注已保存");
        onPatchNotes(runId, editingNotesText);
        setEditingNotesId(null);
      } else {
        message.error("保存失败：" + (result.error ?? "未知错误"));
      }
    } catch (e) {
      message.error("保存失败：" + String((e as Error).message ?? e));
    } finally {
      setSavingNotesId(null);
    }
  };

  const handleCancelNotes = () => {
    setEditingNotesId(null);
    setEditingNotesText("");
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Card size="small" title="历史训练记录（按时间倒序，可对比不同参数版本）"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "12px 16px" }}>
        <Table dataSource={versions} rowKey="runId" size="small" pagination={{ pageSize: 10, showSizeChanger: true }}
          tableLayout="fixed"
          onRow={(record) => ({
            onClick: () => onSelectVersion(record),
            style: { cursor: "pointer" },
          })}>
          <Table.Column title="训练时间" dataIndex="generatedAt" key="generatedAt" width={95}
            render={(v: string) => dateText(v)} />
          <Table.Column title="模型配置" key="model" width={210}
            render={(_: unknown, r: StockMlWalkforwardListItem) => (
              <div style={{ fontSize: 11, lineHeight: 1.7 }}>
                <Tag color={r.model.nSeeds > 1 ? "purple" : "default"} style={{ fontSize: 10, marginBottom: 2 }}>
                  {r.model.nSeeds > 1 ? `${r.model.nSeeds}种子·${r.model.ensembleMethod === "median_zscore" ? "中位数" : "均值"}` : `单种子${r.model.randomState}`}
                </Tag>
                <div style={{ color: "#8a94a6" }}>{r.featureCount ?? "—"}特征 · {r.label} · n={r.model.nEstimators} lr={r.model.learningRate}</div>
              </div>
            )} />
          <Table.Column title="总收益" dataIndex={["metrics", "totalReturn"]} key="totalReturn" width={90}
            render={(v: number) => <span style={{ color: v > 0 ? "#cf1322" : "#3f8600", fontWeight: 600, whiteSpace: "nowrap" }}>{fmtPct(v)}</span>}
            sorter={(a: StockMlWalkforwardListItem, b: StockMlWalkforwardListItem) => (a.metrics?.totalReturn ?? 0) - (b.metrics?.totalReturn ?? 0)} />
          <Table.Column title="超额" dataIndex={["metrics", "excessReturn"]} key="excessReturn" width={75}
            render={(v: number) => <span style={{ whiteSpace: "nowrap" }}>{fmtPct(v)}</span>} />
          <Table.Column title="Sharpe" dataIndex={["metrics", "sharpe"]} key="sharpe" width={85}
            render={(v: number) => fmtNum(v, 3)}
            sorter={(a: StockMlWalkforwardListItem, b: StockMlWalkforwardListItem) => (a.metrics?.sharpe ?? 0) - (b.metrics?.sharpe ?? 0)} />
          <Table.Column title="最大回撤" dataIndex={["metrics", "maxDrawdown"]} key="maxDrawdown" width={90}
            render={(v: number) => <span style={{ color: "#cf1322", whiteSpace: "nowrap" }}>{fmtPct(v)}</span>}
            sorter={(a: StockMlWalkforwardListItem, b: StockMlWalkforwardListItem) => (a.metrics?.maxDrawdown ?? 0) - (b.metrics?.maxDrawdown ?? 0)} />
          <Table.Column title="avgRankIC" dataIndex={["metrics", "avgRankIc"]} key="avgRankIc" width={100}
            render={(v: number) => fmtNum(v, 4)}
            sorter={(a: StockMlWalkforwardListItem, b: StockMlWalkforwardListItem) => (a.metrics?.avgRankIc ?? 0) - (b.metrics?.avgRankIc ?? 0)} />
          <Table.Column title="窗口" dataIndex={["metrics", "windows"]} key="windows" width={50}
            render={(v: number) => v ?? "—"} />
          <Table.Column title="状态" key="status" width={75}
            render={(_: unknown, r: StockMlWalkforwardListItem) => (
              r.isPublished
                ? <Tag color="green" style={{ fontSize: 10, margin: 0 }}>已发布</Tag>
                : <Tag style={{ fontSize: 10, margin: 0 }}>历史</Tag>
            )} />
          <Table.Column title="备注/介绍" key="notes" width={220}
            render={(_: unknown, r: StockMlWalkforwardListItem) => (
              editingNotesId === r.runId ? (
                <Space size={4} direction="vertical" style={{ width: "100%" }} onClick={(e) => e.stopPropagation()}>
                  <Input.TextArea
                    value={editingNotesText}
                    onChange={(e) => setEditingNotesText(e.target.value)}
                    placeholder="输入模型介绍/备注..."
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    style={{ fontSize: 11, width: 200 }}
                  />
                  <Space size={4}>
                    <Button type="primary" size="small" loading={savingNotesId === r.runId}
                      onClick={() => handleSaveNotes(r.runId)}>
                      保存
                    </Button>
                    <Button size="small" onClick={handleCancelNotes}>取消</Button>
                  </Space>
                </Space>
              ) : (
                <div
                  onClick={(e) => { e.stopPropagation(); handleEditNotes(r.runId, r.notes); }}
                  style={{
                    fontSize: 11, color: r.notes ? "#333" : "#bfbfbf",
                    cursor: "pointer", lineHeight: 1.5,
                    whiteSpace: "pre-wrap", wordBreak: "break-all",
                    maxHeight: 48, overflow: "hidden",
                  }}
                  title={r.notes || "点击添加备注"}
                >
                  {r.notes || <span style={{ fontStyle: "italic" }}>点击添加备注...</span>}
                </div>
              )
            )} />
          <Table.Column title="回测期" key="period" width={170}
            render={(_: unknown, r: StockMlWalkforwardListItem) => (
              <span style={{ fontSize: 11, color: "#8a94a6", whiteSpace: "nowrap" }}>{dateText(r.startDate)} ~ {dateText(r.endDate)}</span>
            )} />
          <Table.Column title="runId" dataIndex="runId" key="runId" ellipsis
            render={(v: string) => <span style={{ fontSize: 10, color: "#8a94a6", fontFamily: "monospace" }}>{v}</span>} />
          <Table.Column title="操作" key="action" width={200} fixed="right"
            render={(_: unknown, r: StockMlWalkforwardListItem) => (
              <Space size={2}>
                {!r.isPublished && (
                  <Button type="link" size="small" icon={<CheckCircleOutlined />}
                    loading={publishingId === r.runId}
                    onClick={(e) => { e.stopPropagation(); handlePublish(r.runId, r.label); }}>
                    发布
                  </Button>
                )}
                <Button type="link" size="small" icon={<PlayCircleOutlined />}
                  onClick={(e) => { e.stopPropagation(); onBacktest(r); }}>
                  回测
                </Button>
                <Button type="link" size="small" icon={<ThunderboltOutlined />}
                  onClick={(e) => { e.stopPropagation(); onPredict(r); }}>
                  预测
                </Button>
                {!r.isPublished && (
                  <Popconfirm title="确定删除此版本？" description="删除后不可恢复"
                    onConfirm={(e) => { e?.stopPropagation(); handleDelete(r.runId, r.label); }}
                    okText="删除" cancelText="取消" okButtonProps={{ danger: true }}>
                    <Button type="link" size="small" danger icon={<DeleteOutlined />}
                      loading={deletingId === r.runId}
                      onClick={(e) => e.stopPropagation()}>
                      删除
                    </Button>
                  </Popconfirm>
                )}
              </Space>
            )} />
        </Table>
      </Card>
      <Card size="small" title="版本对比说明"
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "12px 16px", fontSize: 12, color: "#6b7280", lineHeight: 1.8 }}>
        <div>• <b>种子数</b>：单种子（seed=42）vs 多种子集成（5种子均值 / 10种子中位数）</div>
        <div>• <b>集成方法</b>：mean_zscore（等权平均，对异常值敏感）vs median_zscore（中位数，抗异常值更稳健）</div>
        <div>• <b>特征数</b>：31特征（价量+估值+财务+行业轮动）vs 其他特征组合</div>
        <div>• <b>判断标准</b>：优先看 avgRankIC（排序能力）和 Sharpe（风险调整收益），其次看总收益；总收益高但 IC 低通常是离散选股的幸运抽样</div>
        <div>• 点击列标题可排序，方便对比不同版本的指标</div>
      </Card>
    </Space>
  );
}

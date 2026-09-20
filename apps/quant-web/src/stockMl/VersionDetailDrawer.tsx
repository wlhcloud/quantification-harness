import { Descriptions, Divider, Drawer, Tag } from "antd";

import type { StockMlWalkforwardListItem } from "../api";
import { dateText, fmtNum, fmtPct } from "./format";
import { featureLabel } from "./labels";

/** 训练版本详情抽屉（点击版本表某一行时打开） */
export function VersionDetailDrawer({ version, onClose }: {
  version: StockMlWalkforwardListItem | null;
  onClose: () => void;
}) {
  return (
    <Drawer title="训练版本详情" placement="right" width={520} open={!!version}
      onClose={onClose}
      styles={{ body: { padding: "16px 20px" } }}>
      {version && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Descriptions size="small" column={1} bordered
            labelStyle={{ width: 110, fontSize: 12, background: "#fafafa" }}
            contentStyle={{ fontSize: 12 }}>
            <Descriptions.Item label="runId"><span style={{ fontFamily: "monospace", fontSize: 11 }}>{version.runId}</span></Descriptions.Item>
            <Descriptions.Item label="训练时间">{dateText(version.generatedAt)}</Descriptions.Item>
            <Descriptions.Item label="label">{version.label}</Descriptions.Item>
            <Descriptions.Item label="回测期">{dateText(version.startDate)} ~ {dateText(version.endDate)}</Descriptions.Item>
            <Descriptions.Item label="窗口数">{version.windows ?? version.metrics?.windows ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="状态">{version.status ?? "complete"}</Descriptions.Item>
          </Descriptions>

          <div>
            <Divider style={{ fontSize: 13, fontWeight: 600, margin: "4px 0" }}>回测指标</Divider>
            <Descriptions size="small" column={2} bordered
              labelStyle={{ fontSize: 11, background: "#fafafa" }}
              contentStyle={{ fontSize: 12 }}>
              <Descriptions.Item label="总收益"><span style={{ color: version.metrics?.totalReturn > 0 ? "#cf1322" : "#3f8600", fontWeight: 600 }}>{fmtPct(version.metrics?.totalReturn)}</span></Descriptions.Item>
              <Descriptions.Item label="基准收益">{fmtPct(version.metrics?.benchmarkReturn)}</Descriptions.Item>
              <Descriptions.Item label="超额收益"><span style={{ color: version.metrics?.excessReturn > 0 ? "#cf1322" : "#3f8600", fontWeight: 600 }}>{fmtPct(version.metrics?.excessReturn)}</span></Descriptions.Item>
              <Descriptions.Item label="年化收益">{fmtPct(version.metrics?.annualReturn)}</Descriptions.Item>
              <Descriptions.Item label="Sharpe">{fmtNum(version.metrics?.sharpe, 3)}</Descriptions.Item>
              <Descriptions.Item label="最大回撤"><span style={{ color: "#cf1322" }}>{fmtPct(version.metrics?.maxDrawdown)}</span></Descriptions.Item>
              <Descriptions.Item label="avgRankIC" span={2}>{fmtNum(version.metrics?.avgRankIc, 4)}</Descriptions.Item>
            </Descriptions>
          </div>

          <div>
            <Divider style={{ fontSize: 13, fontWeight: 600, margin: "4px 0" }}>模型配置</Divider>
            <Descriptions size="small" column={1} bordered
              labelStyle={{ width: 110, fontSize: 12, background: "#fafafa" }}
              contentStyle={{ fontSize: 12 }}>
              <Descriptions.Item label="种子数">{version.model.nSeeds}</Descriptions.Item>
              <Descriptions.Item label="种子列表">
                {version.model.seeds && version.model.seeds.length > 0
                  ? version.model.seeds.join(", ")
                  : `seed=${version.model.randomState}`}
              </Descriptions.Item>
              <Descriptions.Item label="集成方法">
                <Tag color={version.model.ensembleMethod === "median_zscore" ? "purple" : version.model.ensembleMethod === "mean_zscore" ? "blue" : "default"} style={{ fontSize: 11 }}>
                  {version.model.ensembleMethod === "median_zscore" ? "中位数集成 (median_zscore)" :
                   version.model.ensembleMethod === "mean_zscore" ? "均值集成 (mean_zscore)" :
                   `单种子 (${version.model.ensembleMethod})`}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="nEstimators">{version.model.nEstimators ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="learningRate">{version.model.learningRate ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="numLeaves">{version.model.numLeaves ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="subsample">{version.model.subsample ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="colsampleBytree">{version.model.colsampleBytree ?? "—"}</Descriptions.Item>
            </Descriptions>
          </div>

          <div>
            <Divider style={{ fontSize: 13, fontWeight: 600, margin: "4px 0" }}>Walkforward 配置</Divider>
            <Descriptions size="small" column={2} bordered
              labelStyle={{ fontSize: 11, background: "#fafafa" }}
              contentStyle={{ fontSize: 12 }}>
              <Descriptions.Item label="minTrain">{version.walkforward?.minTrain ?? "—"} 天</Descriptions.Item>
              <Descriptions.Item label="stepDays">{version.walkforward?.stepDays ?? "—"} 天</Descriptions.Item>
              <Descriptions.Item label="testDays">{version.walkforward?.testDays ?? "—"} 天</Descriptions.Item>
              <Descriptions.Item label="topN">{version.backtest?.topN ?? "—"} 只</Descriptions.Item>
            </Descriptions>
          </div>

          {version.features && version.features.length > 0 && (
            <div>
              <Divider style={{ fontSize: 13, fontWeight: 600, margin: "4px 0" }}>
                特征列表（{version.featureCount ?? version.features.length}个）
              </Divider>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {version.features.map((f: string, i: number) => (
                  <Tag key={i} style={{ fontSize: 11, margin: 0 }}>{featureLabel(f)}</Tag>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

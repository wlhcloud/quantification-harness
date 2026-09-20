import { Card, Empty, Space, Tag } from "antd";

import type { StockMlConfigDto, StockMlFeatureImportanceDto } from "../api";
import { featureLabel, FEATURE_CATEGORIES } from "./labels";

export interface FeaturesTabProps {
  config: StockMlConfigDto | null;
  featureImp: StockMlFeatureImportanceDto | null;
}

/** 「特征与因子」页签：Gain 重要性条形图 + 按分类的特征清单 */
export function FeaturesTab({ config, featureImp }: FeaturesTabProps) {
  const topFeatures = featureImp?.items?.slice(0, 15) ?? [];
  const maxGain = topFeatures.length > 0 ? topFeatures[0].gain : 1;

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {/* 特征重要性图表 */}
      <Card size="small" title={
        <Space wrap>
          <span>特征重要性排名（Gain 口径，前15）</span>
          {featureImp?.ensemble ? (
            <Tag color="purple" style={{ marginLeft: 4 }}>{featureImp.ensembleSize} 种子{config?.model.ensembleMethod === 'median_zscore' ? '中位数集成' : '均值集成'}{featureImp.seeds && featureImp.seeds.length > 0 ? ` · ${featureImp.seeds.join("/")}` : ""}</Tag>
          ) : (
            <Tag color="default" style={{ marginLeft: 4 }}>单模型</Tag>
          )}
        </Space>
      }
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        {featureImp?.error ? (
          <Empty description={featureImp.error} />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {topFeatures.map((f, i) => (
              <div key={f.feature} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ width: 24, textAlign: "right", fontSize: 12, color: i < 3 ? "#0baa81" : "#5f6b76", fontWeight: 600 }}>{i + 1}</span>
                <span style={{ width: 110, fontSize: 12, color: "#1a232c" }} title={f.feature}>{featureLabel(f.feature)}</span>
                <div style={{ flex: 1, background: "#f0f2f5", borderRadius: 4, height: 18, overflow: "hidden" }}>
                  <div style={{
                    width: `${(f.gain / maxGain) * 100}%`,
                    height: "100%",
                    background: i < 3 ? "linear-gradient(90deg, #0baa81, #52c41a)" : i < 8 ? "linear-gradient(90deg, #1677ff, #69b1ff)" : "linear-gradient(90deg, #8c8c8c, #bfbfbf)",
                    borderRadius: 4,
                  }} />
                </div>
                <span style={{ width: 60, textAlign: "right", fontSize: 12, color: "#1a232c", fontWeight: 600 }}>{f.gainPct.toFixed(2)}%</span>
                <span style={{ width: 50, textAlign: "right", fontSize: 11, color: "#8a94a0" }}>split {f.split}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 特征列表（按分类） */}
      <Card size="small" title={`全部特征（${config?.featureCount ?? 0} 个）`}
        style={{ border: "1px solid #e5eaee", borderRadius: 10 }} bodyStyle={{ padding: "16px 20px" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {Object.entries(FEATURE_CATEGORIES).map(([cat, feats]) => {
            const catFeats = feats.filter((f) => config?.features?.includes(f));
            if (catFeats.length === 0) return null;
            return (
              <div key={cat}>
                <div style={{ fontSize: 12, fontWeight: 600, color: "#1a232c", marginBottom: 6 }}>{cat}（{catFeats.length}）</div>
                <Space size={[6, 6]} wrap>
                  {catFeats.map((f) => {
                    const imp = featureImp?.items?.find((x) => x.feature === f);
                    return (
                      <Tag key={f} color={imp && imp.gainPct >= 3 ? "green" : imp && imp.gainPct >= 1 ? "blue" : "default"}
                        style={{ fontSize: 11, padding: "2px 8px", marginInlineEnd: 0 }}
                        title={`${f}：gain ${imp?.gainPct?.toFixed(2) ?? 0}%，split ${imp?.split ?? 0}`}>
                        {featureLabel(f)}
                        {imp && <span style={{ color: "#8a94a0", marginLeft: 4 }}>{imp.gainPct.toFixed(1)}%</span>}
                      </Tag>
                    );
                  })}
                </Space>
              </div>
            );
          })}
        </div>
      </Card>
    </Space>
  );
}

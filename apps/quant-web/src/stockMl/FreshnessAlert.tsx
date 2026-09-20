import { useCallback, useEffect, useState } from "react";
import { Alert, Space, Tag, Typography } from "antd";

import { stockMlApi, type StockMlDataFreshnessDto } from "../api";
import { fmtDate } from "../format";

/**
 * 辅助因子表新鲜度提示（9102 `GET /api/v1/stock-ml/data-freshness`）。
 *
 * 背景：行业/资金流因子表落后于最新因子日时，walkforward 结果里会出现告警，
 * 但前端此前拿不到这个端点，用户无从知道是哪张表过期。
 * 本组件只在"确有落后"时渲染，正常时不占用页面空间。
 */
export default function FreshnessAlert() {
  const [data, setData] = useState<StockMlDataFreshnessDto | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await stockMlApi.dataFreshness());
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const tables = Object.entries(data?.tables ?? {});
  const warnings = data?.warnings ?? [];
  const lagging = tables.filter(([, latest]) => latest !== data?.signalDate);

  if (error || (!warnings.length && !lagging.length)) return null;

  return (
    <Alert
      type={warnings.length ? "warning" : "info"}
      showIcon
      message={warnings.length ? "辅助因子表新鲜度告警" : "辅助因子表落后于最新因子日"}
      description={
        <Space direction="vertical" size={4}>
          <Typography.Text style={{ fontSize: 12 }}>
            最新因子日 <Tag color="blue">{fmtDate(data?.signalDate)}</Tag>
            {tables.map(([table, latest]) => (
              <span key={table} style={{ marginLeft: 8 }}>
                {table} <Tag color={latest === data?.signalDate ? "success" : "warning"}>{fmtDate(latest)}</Tag>
              </span>
            ))}
          </Typography.Text>
          {warnings.map((warning) => (
            <Typography.Text key={warning} type="warning" style={{ fontSize: 12 }}>{warning}</Typography.Text>
          ))}
          {error ? <Typography.Text type="danger" style={{ fontSize: 12 }}>读取失败：{error}</Typography.Text> : null}
        </Space>
      }
    />
  );
}

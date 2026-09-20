import { dateText } from "./format";
import { NAV_CHART_LAYOUT, navChartGeometry } from "./navChartGeometry";

/**
 * 回测净值曲线（内联 SVG，零图表依赖）。
 * 几何计算在 ./navChart（纯函数、有单测），这里只负责画。
 */
export function NavChart({ daily }: { daily: Array<{ tradeDate: string; nav: number }> | null | undefined }) {
  const data = daily ?? [];
  const geometry = navChartGeometry(data.map((d) => d.nav));
  if (!geometry) return null;
  const { width: w, height: h, padLeft: padL, padRight: padR } = NAV_CHART_LAYOUT;

  return (
    <svg width="100%" height="100%" viewBox={`0 0 ${w} ${h + 20}`} preserveAspectRatio="none">
      {/* 网格线 + y 轴刻度 */}
      {geometry.yTicks.map((tick, i) => (
        <g key={i}>
          <line x1={padL} y1={tick.y} x2={w - padR} y2={tick.y} stroke="#e5eaee" strokeWidth={1} />
          <text x={padL - 5} y={tick.y + 4} textAnchor="end" fontSize={10} fill="#8a94a6">
            {tick.value.toFixed(2)}
          </text>
        </g>
      ))}
      {/* 基准线（净值=1） */}
      {geometry.baseY != null && (
        <line x1={padL} y1={geometry.baseY} x2={w - padR} y2={geometry.baseY}
          stroke="#8a94a6" strokeWidth={1} strokeDasharray="4,4" />
      )}
      {/* 净值曲线 */}
      <polyline points={geometry.points} fill="none" stroke="#cf1322" strokeWidth={2} />
      {/* 面积填充 */}
      <polygon points={geometry.areaPoints} fill="rgba(207,19,34,0.08)" />
      {/* x 轴日期刻度 */}
      {geometry.xTicks.map((x, i) => (
        <text key={i} x={x} y={h - 8} textAnchor="middle" fontSize={10} fill="#8a94a6">
          {dateText(data[geometry.xTickIndices[i]]?.tradeDate)}
        </text>
      ))}
    </svg>
  );
}

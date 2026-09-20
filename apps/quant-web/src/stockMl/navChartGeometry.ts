/**
 * 回测净值曲线的几何计算（纯函数，可单测）。
 *
 * 原先这段计算写死在 StockMlPage.tsx 的 JSX 里（一个 IIFE 里算 padL/padT/polyline/刻度），
 * 既没法测、也没法复用。这里把"数值 → SVG 坐标"抽出来，渲染留在 NavChart.tsx。
 */

export interface NavChartLayout {
  width: number;
  height: number;
  padLeft: number;
  padRight: number;
  padTop: number;
  padBottom: number;
}

export const NAV_CHART_LAYOUT: NavChartLayout = {
  width: 800, height: 240, padLeft: 50, padRight: 20, padTop: 10, padBottom: 30,
};

export interface NavChartGeometry {
  /** <polyline points> 的值 */
  points: string;
  /** 面积填充用的多边形 points */
  areaPoints: string;
  /** 净值=1 基准线的 y 坐标；超出绘图区时为 null（调用方不渲染那条虚线） */
  baseY: number | null;
  minNav: number;
  maxNav: number;
  /** y 轴网格线（含刻度文字值） */
  yTicks: Array<{ y: number; value: number }>;
  /** x 轴刻度对应的数据下标（配合 data[idx].tradeDate 显示日期） */
  xTickIndices: number[];
  /** x 轴刻度对应的坐标 */
  xTicks: number[];
}

/**
 * 净值序列 → SVG 几何。少于 2 个点（画不出线）或存在非有限值时返回 null，
 * 调用方渲染空白（与原实现 `data.length < 2 → null` 一致）。
 */
export function navChartGeometry(
  navs: number[],
  layout: NavChartLayout = NAV_CHART_LAYOUT,
): NavChartGeometry | null {
  if (!Array.isArray(navs) || navs.length < 2) return null;
  if (!navs.every((v) => typeof v === "number" && Number.isFinite(v))) return null;

  const { width: w, height: h, padLeft: padL, padRight: padR, padTop: padT, padBottom: padB } = layout;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  const minNav = Math.min(...navs);
  const maxNav = Math.max(...navs);
  const range = maxNav - minNav || 1;

  const yOf = (nav: number) => padT + plotH - ((nav - minNav) / range) * plotH;
  const xOf = (i: number) => padL + (i / (navs.length - 1)) * plotW;

  const points = navs.map((nav, i) => `${xOf(i)},${yOf(nav)}`).join(" ");
  const baseY = yOf(1);
  return {
    points,
    areaPoints: `${padL},${padT + plotH} ${points} ${w - padR},${padT + plotH}`,
    baseY: baseY >= padT && baseY <= padT + plotH ? baseY : null,
    minNav,
    maxNav,
    yTicks: [0, 0.25, 0.5, 0.75, 1].map((p) => ({
      y: padT + plotH * p,
      value: maxNav - range * p,
    })),
    xTickIndices: [0, 0.25, 0.5, 0.75, 1].map((p) => Math.floor(p * (navs.length - 1))),
    xTicks: [0, 0.25, 0.5, 0.75, 1].map((p) => padL + p * plotW),
  };
}

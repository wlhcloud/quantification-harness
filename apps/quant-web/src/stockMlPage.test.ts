/**
 * StockMlPage 拆分后抽出的纯函数单测（node:test + tsx，零新依赖）。
 *
 * 拆分前这些逻辑埋在 1000 行组件的 JSX 里：格式化、代码→链接、股票名映射，
 * 以及回测净值曲线的 SVG 几何（原先是一个内联 IIFE），既没法测也没法复用。
 * 页面组件本身仍无组件级测试（无 jsdom），所以这里覆盖的是"能确定行为的那部分"。
 *
 * 运行：cd apps/quant-web && npm test
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { dateText, fileBaseName, fmtNum, fmtPct, quoteUrl } from "./stockMl/format";
import { featureLabel, FEATURE_CATEGORIES, FEATURE_LABELS, stockName } from "./stockMl/labels";
import { navChartGeometry, NAV_CHART_LAYOUT, type NavChartGeometry } from "./stockMl/navChartGeometry";

/** 断言几何可算（专门避开 TS 的 null 收窄噪音） */
function mustGeo(navs: number[]): NavChartGeometry {
  const geo = navChartGeometry(navs);
  if (!geo) throw new Error("navChartGeometry 不该返回 null");
  return geo;
}

test("fmtPct：空值/NaN 显示破折号，否则保留两位百分比", () => {
  assert.equal(fmtPct(null), "—");
  assert.equal(fmtPct(undefined), "—");
  assert.equal(fmtPct(Number.NaN), "—");
  assert.equal(fmtPct(0.12345), "12.35%");
  assert.equal(fmtPct(-0.1), "-10.00%");
});

test("fmtNum：默认 4 位小数，可指定精度，空值破折号", () => {
  assert.equal(fmtNum(1.234567), "1.2346");
  assert.equal(fmtNum(1.234567, 3), "1.235");
  assert.equal(fmtNum(null), "—");
  assert.equal(fmtNum(Number.NaN, 2), "—");
});

test("dateText：兼容 20260911 与 ISO 两种来源", () => {
  assert.equal(dateText("20260911"), "2026-09-11");
  assert.equal(dateText("2026-09-11T10:00:00"), "2026-09-11");
  assert.equal(dateText(null), "—");
  assert.equal(dateText(""), "—");
  assert.equal(dateText("2026"), "2026");
});

test("fileBaseName：只取 Windows 绝对路径的文件名", () => {
  assert.equal(fileBaseName("D:\\x\\artifacts\\model.txt"), "model.txt");
  assert.equal(fileBaseName(null), "—");
});

test("quoteUrl：6 开头走沪市，其余走深市，只取前 6 位代码", () => {
  assert.equal(quoteUrl("600519.SH"), "https://quote.eastmoney.com/sh600519.html");
  assert.equal(quoteUrl("000001.SZ"), "https://quote.eastmoney.com/sz000001.html");
  assert.equal(quoteUrl("300750.SZ"), "https://quote.eastmoney.com/sz300750.html");
});

test("featureLabel / stockName：有映射用中文，没有则原样回退", () => {
  assert.equal(featureLabel("momentum20"), "20日动量");
  assert.equal(featureLabel("unknown_feature"), "unknown_feature");
  assert.equal(stockName("600519.SH"), "贵州茅台");
  assert.equal(stockName("999999.SZ"), "");
});

test("特征分类里的每个特征都要有中文名（防加因子忘了加标签）", () => {
  const categorized = Object.values(FEATURE_CATEGORIES).flat();
  for (const feature of categorized) {
    assert.ok(feature in FEATURE_LABELS, `特征 ${feature} 缺中文名`);
  }
  assert.ok(categorized.length >= 30, "特征分类表不该被误删");
});

test("navChartGeometry：少于 2 个点或含非法值时返回 null（画不了线）", () => {
  assert.equal(navChartGeometry([]), null);
  assert.equal(navChartGeometry([1.02]), null);
  assert.equal(navChartGeometry([1.0, Number.NaN]), null);
});

test("navChartGeometry：点数与折线点一一对应，首尾贴住绘图区边界", () => {
  const navs = [0.8, 0.9, 1.1, 1.2];
  const geo = mustGeo(navs);
  assert.equal(geo.points.split(" ").length, navs.length);
  const pts = geo.points.split(" ").map((p) => p.split(",").map(Number));
  const { padLeft, padRight, padTop, padBottom, width, height } = NAV_CHART_LAYOUT;
  const plotBottom = padTop + (height - padTop - padBottom);
  assert.deepEqual(pts[0], [padLeft, plotBottom]);   // 首点是最低净值 → 绘图区底部
  assert.equal(pts[3][0], width - padRight);         // 末点贴右边界
  assert.equal(pts[3][1], padTop);                   // 末点是最高净值 → 绘图区顶部
});

test("navChartGeometry：净值=1 基准线在区间外时不给坐标（不画虚线）", () => {
  assert.equal(navChartGeometry([1.5, 1.8, 2.0])?.baseY, null);
  const baseY = mustGeo([0.8, 1.0, 1.2]).baseY;
  assert.ok(baseY != null && baseY >= NAV_CHART_LAYOUT.padTop);
});

test("navChartGeometry：y 轴刻度自上而下递减，x 轴刻度取 5 个等分下标", () => {
  const geo = mustGeo([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  assert.deepEqual(geo.yTicks.map((t) => t.value), [11, 8.5, 6, 3.5, 1]);
  assert.deepEqual(geo.xTickIndices, [0, 2, 5, 7, 10]);
  assert.equal(geo.xTicks.length, 5);
});

test("navChartGeometry：净值全相等时不除零（range 兜底为 1）", () => {
  const geo = mustGeo([1.0, 1.0, 1.0]);
  assert.equal(geo.minNav, geo.maxNav);
  assert.ok(geo.points.split(" ").every((p) => Number.isFinite(Number(p.split(",")[1]))));
});

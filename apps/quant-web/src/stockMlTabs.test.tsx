/**
 * 组件渲染冒烟测试：把拆分后的页面/页签在 node 里渲染一遍，确认不炸。
 *
 * 为什么能做：`api.ts` 的 `import.meta.env` 已做空值兜底，所以在 node:test 里也能 import
 * 组件模块；用 `react-dom/server` 的 `renderToStaticMarkup` 做纯渲染断言。
 * 覆盖的是"渲染期不崩 + 关键文案在"，不是交互（交互仍需 jsdom/浏览器；本仓库刻意零新依赖）。
 *
 * 注意：Modal / Drawer 走 portal，服务端渲染不支持，所以只渲染四个页签与页面壳。
 *
 * 运行：cd apps/quant-web && npm test
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { StockMlConfigDto, StockMlWalkforwardListItem } from "./api";
import StockMlPage from "./StockMlPage";
import { OverviewTab } from "./stockMl/OverviewTab";
import { FeaturesTab } from "./stockMl/FeaturesTab";
import { ConfigTab } from "./stockMl/ConfigTab";
import { VersionsTab } from "./stockMl/VersionsTab";

const CONFIG: StockMlConfigDto = {
  label: "forward_3",
  features: ["momentum20", "volumeRatio", "peTtm"],
  featureCount: 31,
  model: {
    nEstimators: 300, learningRate: 0.05, numLeaves: 31, minDataInLeaf: 50, subsample: 0.8,
    colsampleBytree: 0.8, randomState: 42, randomStates: [42, 7, 2024], ensembleMethod: "median_zscore",
    earlyStopping: 50, validationDays: 20, testDays: 20, labelGrades: 5,
  },
  walkforward: { minTrainDays: 250, stepDays: 20, testDays: 20, publishTopN: 20 },
  backtest: { topN: 5, rebalanceDays: 5, transactionCostBps: 15, slippageBps: 5 },
  universe: { minMarketCap: 500000, maxMarketCap: 50000000, excludeST: true, excludeSuspended: true, minListDays: 120 },
};

const VERSION: StockMlWalkforwardListItem = {
  runId: "stock-ml-wf-20260911-abcdef123456", generatedAt: "20260911", label: "forward_3",
  startDate: "20240101", endDate: "20260911", windows: 20, status: "complete", isPublished: true,
  notes: "10种子中位数集成",
  model: { nSeeds: 10, seeds: [1, 2, 3], ensembleMethod: "median_zscore", nEstimators: 300, learningRate: 0.05, numLeaves: 31, subsample: 0.8, colsampleBytree: 0.8, randomState: 42 },
  walkforward: { minTrain: 250, stepDays: 20, testDays: 20 },
  backtest: { topN: 5, rebalanceDays: 5 },
  featureCount: 31, features: ["momentum20", "volumeRatio"],
  metrics: { windows: 20, totalReturn: 1.2, benchmarkReturn: 0.4, excessReturn: 0.8, annualReturn: 0.25, sharpe: 1.5, maxDrawdown: -0.2, avgRankIc: 0.05 },
};

test("页面壳：无训练记录时渲染空态提示（而不是崩）", () => {
  const html = renderToStaticMarkup(React.createElement(StockMlPage));
  assert.match(html, /股票 ML 模型/);
  assert.match(html, /重新训练模型/);
  assert.match(html, /暂无训练记录/);
});

test("回测与信号页签：指标卡与两张表的关键列都在", () => {
  const html = renderToStaticMarkup(React.createElement(OverviewTab, {
    metrics: VERSION.metrics,
    signalDate: "20260911",
    holdings: [{ code: "600519.SH", score: 0.9123, weight: 0.2 }],
    candidates: [{
      tradeDate: "20260911", rank: 1, code: "600519.SH", score: 82.5,
      reasons: [{ factor: "momentum20", label: "20日动量", score: 0.9, contribution: 0.3 }], computedAt: "20260911",
    }],
    candidateDate: "20260911",
    loading: false,
  }));
  assert.match(html, /Walkforward 回测指标/);
  assert.match(html, /signalDate|信号日/);
  assert.match(html, /贵州茅台/);           // 股票名映射接上了
  assert.match(html, /600519\.SH/);
  assert.match(html, /20日动量/);           // 入选理由的标签
  assert.match(html, /120\.00%/);           // 总收益 1.2 → 120.00%
});

test("特征与因子页签：重要性条与分类标签都渲染", () => {
  const html = renderToStaticMarkup(React.createElement(FeaturesTab, {
    config: CONFIG,
    featureImp: {
      featureCount: 31, modelFile: "D:\\x\\model.txt", ensemble: true, ensembleSize: 10, seeds: [1, 2],
      items: [
        { feature: "momentum20", gain: 100, gainPct: 12.5, split: 30, splitPct: 0.1 },
        { feature: "volumeRatio", gain: 50, gainPct: 6.25, split: 20, splitPct: 0.07 },
      ],
    },
  }));
  assert.match(html, /特征重要性排名/);
  assert.match(html, /10 种子中位数集成/);
  assert.match(html, /20日动量/);
  assert.match(html, /全部特征（31 个）/);
});

test("训练配置页签：种子说明与回测参数都在", () => {
  const html = renderToStaticMarkup(React.createElement(ConfigTab, { config: CONFIG }));
  assert.match(html, /LightGBM 模型参数/);
  assert.match(html, /median_zscore（中位数）/);
  assert.match(html, /Walkforward 滚动训练配置/);
  assert.match(html, /回测与股票池配置/);
});

test("训练版本页签：版本行、状态标签与操作按钮都在（发布过的行不显示删除）", () => {
  const html = renderToStaticMarkup(React.createElement(VersionsTab, {
    versions: [VERSION, { ...VERSION, runId: "run-2", isPublished: false, notes: "" }],
    onReload: async () => {},
    onPatchNotes: () => {},
    onSelectVersion: () => {},
    onBacktest: () => {},
    onPredict: () => {},
  }));
  assert.match(html, /历史训练记录/);
  assert.match(html, /已发布/);
  assert.match(html, /10种子·中位数/);
  assert.match(html, /点击添加备注/);
  assert.match(html, /版本对比说明/);
});

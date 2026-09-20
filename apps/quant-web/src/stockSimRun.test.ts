/** 模拟盘 run 选择逻辑单测（node:test + tsx，零新依赖）。运行：cd apps/quant-web && npm test */
import assert from "node:assert/strict";
import { test } from "node:test";

import { activeRuns, awaitingT1, flatPositionHint, isFrozenRun, pickDefaultRun, runLabel, runStateSuffix, type SimRunLike } from "./stockSimRun";

function run(over: Partial<SimRunLike>): SimRunLike {
  return { runId: "stock-sim-1", modelId: "balanced", status: "active", lastNav: 1, ...over };
}

const LEGACY = run({ runId: "stock-sim-legacy", modelId: "balanced", lastSignalDate: "20260904" });
const ML = run({ runId: "stock-sim-ml", modelId: "ml_walkforward", lastSignalDate: "20260909", lastNav: 1.02 });
const V5 = run({ runId: "tech-sim-v5-new", modelId: "ml_technical_timing_v5", lastSignalDate: "20260911", navSeries: [] });
const ARCHIVED = run({ runId: "stock-sim-old", modelId: "ml_walkforward", status: "archived" });
/** 真实库里 3 个可见 run 全部是旧口径（execution_mode=legacy_t1_open） */
const FROZEN = run({ runId: "stock-sim-frozen", modelId: "ml_walkforward", executionMode: "legacy_t1_open", legacy: true });
const LIVE = run({ runId: "stock-sim-live", modelId: "ml_walkforward", executionMode: "same_day_close", legacy: false });

test("默认优先选 ml_walkforward，而不是最新的择时盘", () => {
  const picked = pickDefaultRun([LEGACY, ML, V5]);
  assert.equal(picked?.runId, "stock-sim-ml");
});

test("没有 ml_walkforward 时回退到最新 run", () => {
  assert.equal(pickDefaultRun([LEGACY, V5])?.runId, "tech-sim-v5-new");
});

test("归档 run 不参与默认选择，也不在候选列表里", () => {
  assert.deepEqual(activeRuns([LEGACY, ARCHIVED, V5]).map((r) => r.runId), ["stock-sim-legacy", "tech-sim-v5-new"]);
  assert.equal(pickDefaultRun([ARCHIVED])?.runId, undefined);
  assert.equal(pickDefaultRun([ARCHIVED]), null);
});

test("空列表返回 null", () => {
  assert.equal(pickDefaultRun([]), null);
});

test("多个 ML 盘时取最新的那个", () => {
  const older = run({ runId: "stock-sim-ml-old", modelId: "ml_walkforward" });
  const newer = run({ runId: "stock-sim-ml-new", modelId: "ml_walkforward" });
  assert.equal(pickDefaultRun([older, newer])?.runId, "stock-sim-ml-new");
});

test("isFrozenRun：只认 same_day_close 为可推进", () => {
  assert.equal(isFrozenRun(FROZEN), true, "legacy 标记 / legacy_t1_open 都算冻结");
  assert.equal(isFrozenRun(LIVE), false);
  assert.equal(isFrozenRun(run({ executionMode: "legacy_t1_open" })), true, "没有 legacy 布尔时按 executionMode 判");
  // /stock/sim/status 返回的是数据库原始行（snake_case）
  assert.equal(isFrozenRun({ execution_mode: "legacy_t1_open" }), true);
  assert.equal(isFrozenRun({ execution_mode: "same_day_close" }), false);
  assert.equal(isFrozenRun(run({})), false, "两个字段都没有时不做判断（旧后端兼容）");
  assert.equal(isFrozenRun(null), false);
  assert.equal(isFrozenRun(undefined), false);
});

test("runStateSuffix：已冻结优先于待 T+1", () => {
  assert.equal(runStateSuffix(FROZEN), " · 已冻结（旧T+1口径）");
  // 冻结盘即使同时满足 awaitingT1，也只显示冻结
  assert.equal(runStateSuffix(run({ executionMode: "legacy_t1_open", lastSignalDate: "20260911", navSeries: [] })),
    " · 已冻结（旧T+1口径）");
  assert.equal(runStateSuffix(V5), " · 待T+1");
  assert.equal(runStateSuffix(ML), "");
  assert.equal(runStateSuffix(null), "");
});

test("默认 run 跳过已冻结的 ML 盘，优先能真正推进的那本", () => {
  // 冻结的更新 → 仍应选可推进的旧账，否则默认落在"点不动"的线上
  assert.equal(pickDefaultRun([LIVE, FROZEN])?.runId, "stock-sim-live");
  // 只有冻结盘可选时仍退回它（有历史净值可看），而不是返回 null
  assert.equal(pickDefaultRun([FROZEN])?.runId, "stock-sim-frozen");
  // 没有 ML 盘时优先可推进的任意 run
  const frozenOther = run({ runId: "tech-frozen", modelId: "ml_technical_timing_v5", executionMode: "legacy_t1_open" });
  const liveOther = run({ runId: "tech-live", modelId: "trend_momentum", executionMode: "same_day_close" });
  assert.equal(pickDefaultRun([liveOther, frozenOther])?.runId, "tech-live");
});

test("flatPositionHint 对冻结盘给出可执行的原因", () => {
  const hint = flatPositionHint(FROZEN, null) ?? "";
  assert.match(hint, /旧 T\+1 口径/);
  assert.match(hint, /已停止推进/);
  assert.match(hint, /开始模拟/);
});

test("awaitingT1 识别“已出信号但还没到 T+1”的空盘", () => {
  assert.equal(awaitingT1(V5), true);
  assert.equal(awaitingT1(ML), false, "已有成交期的不算等待");
  assert.equal(awaitingT1(run({ lastSignalDate: null, navSeries: [] })), false, "还没信号的也不算");
  assert.equal(awaitingT1(null), false);
});

test("runLabel 带模型、时间戳（runId 末 12 位）与收益", () => {
  const id = "stock-sim-20260908T022828";
  assert.equal(runLabel(run({ runId: id, modelId: "balanced", lastNav: 0.9722 })),
    `balanced · ${id.slice(-12)} · -2.78%`);
  assert.equal(runLabel(run({ lastNav: null })).includes("—"), true);
});

test("flatPositionHint 区分“熊市空仓”与“等 T+1”", () => {
  const bearNote = "信号 20260910 · 沪深300 MA20 熊市空仓 · 清仓 0 只 · T+1 20260911";
  assert.match(flatPositionHint(ML, bearNote) ?? "", /熊市空仓/);
  assert.match(flatPositionHint(V5, "信号 20260911 · 持仓 5 只 · T+1 开盘成交") ?? "", /等待下一交易日 T\+1/);
  assert.equal(flatPositionHint(ML, "信号 20260910 · 持仓 5 只 · T+1 开盘成交"), null,
    "有持仓、有成交期时不应提示空仓原因");
  assert.equal(flatPositionHint(null, null), null);
});

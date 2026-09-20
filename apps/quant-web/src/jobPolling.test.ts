/**
 * 轮询决策单测（node:test + tsx，零新依赖）。
 *
 * 运行：cd apps/quant-web && npm test
 *   （脚本 = node --import tsx --test src/*.test.ts；tsx 在仓库根 node_modules）
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { decideFromJob, isPollTimedOut, pollErrorMessage } from "./jobPolling";

test("complete 是终态", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "complete" }), { action: "complete" });
});

test("error 透出引擎给出的错误信息", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "error", error: "no daily bars" }),
    { action: "error", message: "no daily bars" });
});

test("error 无信息时回退为未知错误", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "error", error: null }),
    { action: "error", message: "未知错误" });
});

test("cancelled 也是终态（原实现会一直轮询下去）", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "cancelled", error: null }),
    { action: "error", message: "任务已取消" });
  assert.deepEqual(decideFromJob({ id: "j1", status: "cancelled", error: "用户取消" }),
    { action: "error", message: "用户取消" });
});

test("queued/running 继续轮询", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "queued" }), { action: "continue" });
  assert.deepEqual(decideFromJob({ id: "j1", status: "running", progress: 42 }), { action: "continue" });
});

test("未知状态按继续处理（不误判为终态）", () => {
  assert.deepEqual(decideFromJob({ id: "j1", status: "weird" }), { action: "continue" });
});

test("总超时判定：到达 deadline 不算超时，超过才算", () => {
  assert.equal(isPollTimedOut(1000, 1000), false);
  assert.equal(isPollTimedOut(1001, 1000), true);
  assert.equal(isPollTimedOut(0, 1000), false);
});

test("错误文案归一化能处理非 Error 抛出值", () => {
  assert.equal(pollErrorMessage(new Error("boom")), "boom");
  assert.equal(pollErrorMessage("plain"), "plain");
  assert.equal(pollErrorMessage({ message: "obj" }), "obj");
});

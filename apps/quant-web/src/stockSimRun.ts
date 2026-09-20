/**
 * 股票模拟盘 run 的选择逻辑（抽成纯函数，便于单测与两个页面复用）。
 *
 * 背景：`/stock/sim/runs` 里既有你实际在跟踪的 ML 盘（ml_walkforward），也有每天新建的
 * 技术面择时盘（ml_technical_timing[_v5]）与历史实验盘。原先两个页面都取"最后一条"（最新），
 * 于是打开页面默认看到的是**刚创建、还没到 T+1、0 持仓、净值 1.0** 的择时盘，看起来像坏了。
 */

export interface SimRunLike {
  runId: string;
  modelId: string;
  status: string;
  createdAt?: string;
  topN?: number;
  lastSignalDate?: string | null;
  lastNav?: number | null;
  navSeries?: Array<{ tradeDate: string; nav: number }>;
  /** `/stock/sim/runs` 的 camelCase 口径与 legacy 标记 */
  executionMode?: string;
  legacy?: boolean;
  /** `/stock/sim/status` 返回的是原始行，字段是 snake_case */
  execution_mode?: string;
}

/** 用户实际跟踪的模拟盘模型（T+1 选股页与模拟盘页默认展示它）。 */
export const PREFERRED_SIM_MODEL = "ml_walkforward";

/** 只有这个成交口径的 run 还能被推进（见 services/.../stock/sim.py 的 advance_sim）。 */
export const ADVANCEABLE_EXECUTION_MODE = "same_day_close";

/**
 * 判断冻结只依赖成交口径标记，因此放宽到"任意带这些字段的对象"：
 * `/stock/sim/runs` 返回 camelCase 的 `executionMode`+`legacy`，
 * `/stock/sim/status` 直接返回数据库行的 snake_case `execution_mode`。
 */
export interface RunExecutionLike {
  legacy?: boolean;
  executionMode?: string;
  execution_mode?: string;
}

/**
 * 是否"已冻结的旧口径 run"。
 *
 * 后端 `advance_sim` 对 `execution_mode != 'same_day_close'` 的 run 直接拒绝推进
 * （"该模拟盘使用旧T+1口径，已停止推进；请新建 same_day_close 模拟盘"），
 * 而 `/stock/sim/runs` 早就返回了 `executionMode` + `legacy`。前端原先没有声明这两个字段，
 * 于是用户只能点「推进一期」吃一个错误 toast 才知道这本账是死的。
 */
export function isFrozenRun(run: RunExecutionLike | null | undefined): boolean {
  if (!run) return false;
  if (run.legacy === true) return true;
  const mode = run.executionMode ?? run.execution_mode;
  return mode != null && mode !== ADVANCEABLE_EXECUTION_MODE;
}

/** 下拉/列表里跟在 run 名后的状态后缀：已冻结优先于待 T+1。 */
export function runStateSuffix(run: SimRunLike | null | undefined): string {
  if (!run) return "";
  if (isFrozenRun(run)) return " · 已冻结（旧T+1口径）";
  if (awaitingT1(run)) return " · 待T+1";
  return "";
}

export function activeRuns<T extends SimRunLike>(runs: T[]): T[] {
  return runs.filter((run) => run.status !== "archived");
}

/** 从后往前找第一个满足条件的 run（列表按 created_at 升序）。 */
function lastMatch<T>(runs: T[], predicate: (run: T) => boolean): T | null {
  for (let i = runs.length - 1; i >= 0; i -= 1) {
    if (predicate(runs[i])) return runs[i];
  }
  return null;
}

/**
 * 默认选中的 run，按优先级：
 *   1) 最新的**可推进**的 ml_walkforward 盘；
 *   2) 最新的 ml_walkforward 盘（即使已冻结）；
 *   3) 最新的可推进 run；
 *   4) 最新的非归档 run；
 *   5) 全为空时返回 null。
 *
 * 第 1 步是关键：把"能真正往下推的盘"排在前面，避免默认落到一本已冻结的旧账上。
 */
export function pickDefaultRun<T extends SimRunLike>(runs: T[]): T | null {
  const active = activeRuns(runs);
  if (!active.length) return null;
  const preferred = active.filter((run) => run.modelId === PREFERRED_SIM_MODEL);
  const advanceable = (run: T) => !isFrozenRun(run);
  return lastMatch(preferred, advanceable)
    ?? (preferred.length ? preferred[preferred.length - 1] : null)
    ?? lastMatch(active, advanceable)
    ?? active[active.length - 1];
}

/** 是否"已建仓待 T+1 成交"：有信号但还没有任何成交期（INIT 阶段、0 持仓）。 */
export function awaitingT1(run: SimRunLike | null | undefined): boolean {
  if (!run) return false;
  const hasPeriods = (run.navSeries?.length ?? 0) > 0;
  return !hasPeriods && !!run.lastSignalDate && (run.lastNav ?? 1) === 1;
}

/**
 * 空仓原因提示：把"0 持仓 + 净值 1.0000"的正常原因讲清楚，免得被当成数据缺失 ——
 * ①择时为熊市、策略持币；②还没到 T+1；③该 run 是已冻结的旧口径账本（不再推进）。
 */
export function flatPositionHint(run: SimRunLike | null | undefined, latestNote?: string | null): string | null {
  const note = latestNote ?? "";
  if (note.includes("熊市空仓") || note.includes("空仓防御")) {
    return "当前处于熊市空仓（基准跌破 MA20）：策略按规则持币，因此 0 持仓、净值维持 1.0000 —— 这是策略状态，不是数据缺失。";
  }
  if (isFrozenRun(run)) {
    return "该 run 使用旧 T+1 口径（execution_mode≠same_day_close），已停止推进，因此不会再产生新的成交期。需要在「股票模拟盘」页点「开始模拟」新建一本 same_day_close 的账才能继续逐日推进。";
  }
  if (awaitingT1(run)) {
    return `已出信号（${run?.lastSignalDate}），等待下一交易日 T+1 开盘成交：因此 0 持仓、净值暂为 1.0000。`;
  }
  return null;
}

export function runLabel(run: SimRunLike): string {
  const stamp = run.runId.length > 12 ? run.runId.slice(-12) : run.runId;
  const nav = run.lastNav == null ? "—" : `${((run.lastNav - 1) * 100).toFixed(2)}%`;
  return `${run.modelId} · ${stamp} · ${nav}`;
}

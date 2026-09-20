/**
 * 轮询决策的纯逻辑（与 React 解耦，便于直接用 node:test 单测）。
 *
 * 背景：四个页面各自写了一份 setInterval 轮询，重复且都缺总超时；
 * 统一到 useJobPolling 后，把"什么时候停、停成什么状态"抽成纯函数锁进测试。
 */

export interface PollableJob {
  id: string;
  status: string;
  progress?: number;
  error?: string | null;
  /** 终态时引擎返回的结果（job 化端点把原本的响应体放在这里）。 */
  result?: Record<string, unknown> | null;
}

export type PollDecision =
  | { action: "continue" }
  | { action: "complete" }
  | { action: "error"; message: string };

/** 是否已超过轮询总超时（等于 deadline 不算超时）。 */
export function isPollTimedOut(now: number, deadline: number): boolean {
  return now > deadline;
}

/** 由 job 状态决定下一步：终态停止，其余继续轮询。 */
export function decideFromJob(job: PollableJob): PollDecision {
  if (job.status === "complete") return { action: "complete" };
  if (job.status === "cancelled") return { action: "error", message: String(job.error ?? "任务已取消") };
  if (job.status === "error") return { action: "error", message: String(job.error ?? "未知错误") };
  return { action: "continue" };
}

/** 归一化任意抛出值到可展示的错误文案。 */
export function pollErrorMessage(error: unknown): string {
  return String((error as Error | undefined)?.message ?? error);
}

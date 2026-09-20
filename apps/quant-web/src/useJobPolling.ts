import { useCallback, useEffect, useRef, useState } from "react";

import { decideFromJob, isPollTimedOut, pollErrorMessage, type PollableJob } from "./jobPolling";

export type { PollableJob } from "./jobPolling";

/**
 * 计算引擎 job 轮询（原先 FactorsPage / ModelsPage / EtfQuantPage / StockMlPage 各写一份，
 * 都是 setInterval 4-5s + 只在 complete/error 时停 → **没有总超时**，
 * 一旦 job 卡在 running（历史上有过），页面会永远轮询下去；cancelled 也被当成未终结）。
 *
 * 本 hook 统一：轮询间隔、终态处理、查询失败处理，并新增总超时（默认 90 分钟）。
 * 决策逻辑抽在 ./jobPolling（纯函数，有 node:test 单测）；页面自己保留 running 状态，
 * 通过回调更新即可（有的是 boolean，有的是 job type）。
 *
 * 另新增 `cancel()`：后端早就提供 POST /jobs/{id}/cancel，但前端一直只能干等——
 * 这里把"请求取消"和"取消生效"接进来（取消是协作式的：后端在处理器下次轮询时退出）。
 */
export interface JobPollingOptions {
  fetchJob: (id: string) => Promise<PollableJob>;
  /** 请求取消当前 job（POST /jobs/{id}/cancel）。不传则不提供取消能力。 */
  cancelJob?: (id: string) => Promise<PollableJob>;
  /** 轮询间隔，默认 4000ms */
  intervalMs?: number;
  /** 总超时，默认 90 分钟；超时后停止轮询并回调 onTimeout（不再无限轮询） */
  timeoutMs?: number;
  /** job 进入 complete；回传终态 job（含 result） */
  onComplete?: (job: PollableJob) => void | Promise<void>;
  /** job 进入 error / cancelled；参数为错误信息 */
  onError?: (message: string) => void;
  /** 用户主动取消且已生效（与 onError 区分，避免把"我取消了"报成失败） */
  onCancelled?: () => void;
  /** 查询 job 本身失败（网络/服务错误） */
  onPollError?: (message: string) => void;
  /** 超过总超时仍未到终态 */
  onTimeout?: () => void;
}

export function useJobPolling(options: JobPollingOptions) {
  const { intervalMs = 4000, timeoutMs = 90 * 60 * 1000 } = options;
  // 回调取 ref 里的最新值，避免 setInterval 闭包捕获过期状态
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const timerRef = useRef<number | null>(null);
  const deadlineRef = useRef(0);
  const currentIdRef = useRef<string | null>(null);
  /** 是否由用户主动发起了取消：用于把 cancelled 归到 onCancelled 而不是 onError。 */
  const cancelRequestedRef = useRef(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const stop = useCallback(() => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
  }, []);

  const watch = useCallback((job: PollableJob) => {
    stop();
    currentIdRef.current = job.id;
    cancelRequestedRef.current = false;
    setProgress(typeof job.progress === "number" ? job.progress : null);
    deadlineRef.current = Date.now() + timeoutMs;
    timerRef.current = window.setInterval(async () => {
      const current = optionsRef.current;
      if (isPollTimedOut(Date.now(), deadlineRef.current)) {
        stop();
        current.onTimeout?.();
        return;
      }
      try {
        const state = await current.fetchJob(job.id);
        if (typeof state.progress === "number") setProgress(state.progress);
        const decision = decideFromJob(state);
        if (decision.action === "complete") {
          stop();
          await current.onComplete?.(state);
        } else if (decision.action === "error") {
          stop();
          if (state.status === "cancelled" && cancelRequestedRef.current) current.onCancelled?.();
          else current.onError?.(decision.message);
        }
      } catch (error) {
        stop();
        current.onPollError?.(pollErrorMessage(error));
      }
    }, intervalMs);
  }, [intervalMs, stop, timeoutMs]);

  /**
   * 请求取消当前正在轮询的 job。
   * 返回 true 表示取消已生效（后端立刻置 cancelled，例如 job 还在队列里）；
   * 返回 false 表示已发出请求但处理器尚未退出（协作式取消），轮询会继续直到看到终态。
   */
  const cancel = useCallback(async (): Promise<boolean> => {
    const id = currentIdRef.current;
    const { cancelJob, onCancelled } = optionsRef.current;
    if (!id || !cancelJob) return false;
    cancelRequestedRef.current = true;
    setCancelling(true);
    try {
      const job = await cancelJob(id);
      if (job.status === "cancelled") {
        stop();
        onCancelled?.();
        return true;
      }
      return false;
    } finally {
      setCancelling(false);
    }
  }, [stop]);

  // 组件卸载时停止轮询（原实现只在部分页面做了这件事）
  useEffect(() => () => stop(), [stop]);

  return { watch, stop, cancel, cancelling, progress };
}

import { useState } from "react";
import { App } from "antd";

import { engineApi, stockMlApi, type StockMlBacktestDto, type StockMlPredictDto } from "../api";
import { useJobPolling } from "../useJobPolling";

/**
 * 股票 ML 页面三个长任务（训练 / 回测 / 选股预测）的 job 轮询与状态。
 *
 * 背景：这三个动作原先都是**同步长请求**（点击后全程转圈，15 分钟超时后前端还以为是失败），
 * 已改为 job 化；轮询统一走 useJobPolling（带总超时 90 分钟、终态判定、卸载时停止）。
 * 拆分时保持行为不变：回测完成只更新结果、预测完成后额外刷新数据（预测会写候选池）。
 *
 * 另外接入了两件事：
 * - `message` 改用 App.useApp()（静态 message 拿不到 ConfigProvider 的主题上下文）；
 * - `cancel*` 走引擎的 POST /jobs/{id}/cancel（此前只能干等或刷新页面）。
 */
export function useStockMlJobs({ onDataChanged }: { onDataChanged: () => Promise<void> }) {
  const { message } = App.useApp();
  const [running, setRunning] = useState(false);
  const [backtestLoading, setBacktestLoading] = useState(false);
  const [backtestResult, setBacktestResult] = useState<StockMlBacktestDto | null>(null);
  const [predictLoading, setPredictLoading] = useState(false);
  const [predictResult, setPredictResult] = useState<StockMlPredictDto | null>(null);

  const trainPoll = useJobPolling({
    fetchJob: stockMlApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 5000,
    onComplete: async () => { setRunning(false); message.success("训练完成，已刷新"); await onDataChanged(); },
    onError: (m) => { setRunning(false); message.error("训练失败：" + m); },
    onCancelled: () => { setRunning(false); message.info("训练任务已取消"); },
    onPollError: () => { setRunning(false); },
    onTimeout: () => { setRunning(false); message.error("训练轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const backtestPoll = useJobPolling({
    fetchJob: stockMlApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 3000,
    onComplete: (job) => {
      setBacktestLoading(false);
      setBacktestResult((job.result ?? null) as StockMlBacktestDto | null);
      message.success("回测完成");
    },
    onError: (m) => { setBacktestLoading(false); message.error("回测失败：" + m); },
    onCancelled: () => { setBacktestLoading(false); message.info("回测任务已取消"); },
    onPollError: (m) => { setBacktestLoading(false); message.error("回测轮询失败：" + m); },
    onTimeout: () => { setBacktestLoading(false); message.error("回测轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const predictPoll = useJobPolling({
    fetchJob: stockMlApi.job,
    cancelJob: engineApi.cancelJob,
    intervalMs: 3000,
    onComplete: async (job) => {
      setPredictLoading(false);
      setPredictResult((job.result ?? null) as StockMlPredictDto | null);
      message.success("预测完成");
      await onDataChanged();
    },
    onError: (m) => { setPredictLoading(false); message.error("预测失败：" + m); },
    onCancelled: () => { setPredictLoading(false); message.info("预测任务已取消"); },
    onPollError: (m) => { setPredictLoading(false); message.error("预测轮询失败：" + m); },
    onTimeout: () => { setPredictLoading(false); message.error("预测轮询超过 90 分钟，请到任务列表确认实际状态"); },
  });

  const runTrain = async () => {
    if (running) return;
    setRunning(true);
    try {
      const job = await stockMlApi.startJob("stock_ml_walkforward");
      message.info("股票ML模型训练已启动，运行中…（约10-15分钟）");
      trainPoll.watch(job);
    } catch (e) {
      setRunning(false);
      message.error("启动失败：" + String((e as Error).message ?? e));
    }
  };

  /** 执行回测：参数由回测弹窗传入（弹窗里选择起止日期/持仓数） */
  const runBacktest = async (params: { runId: string; startDate: string; endDate: string; topN: number }) => {
    if (backtestLoading) return;
    setBacktestLoading(true);
    try {
      const job = await stockMlApi.startJobWithParams("stock_ml_backtest", params);
      message.info(`回测任务已提交（${job.id.slice(-8)}），运行中…`);
      backtestPoll.watch(job);
    } catch (e) {
      setBacktestLoading(false);
      message.error("回测启动失败：" + String((e as Error).message ?? e));
    }
  };

  /** 执行指定模型预测：只做预览，不写 selection_candidates（弹窗内已二次确认） */
  const runPredict = async (params: { runId: string; topN: number }) => {
    if (predictLoading) return;
    setPredictLoading(true);
    try {
      const job = await stockMlApi.startJobWithParams("stock_ml_predict", params);
      message.info(`选股预测任务已提交（${job.id.slice(-8)}），运行中…`);
      predictPoll.watch(job);
    } catch (e) {
      setPredictLoading(false);
      message.error("预测启动失败：" + String((e as Error).message ?? e));
    }
  };

  return {
    running, runTrain, cancelTrain: trainPoll.cancel, trainCancelling: trainPoll.cancelling,
    backtestLoading, backtestResult, runBacktest, backtestProgress: backtestPoll.progress,
    cancelBacktest: backtestPoll.cancel, backtestCancelling: backtestPoll.cancelling,
    predictLoading, predictResult, runPredict, predictProgress: predictPoll.progress,
    cancelPredict: predictPoll.cancel, predictCancelling: predictPoll.cancelling,
    clearBacktestResult: () => setBacktestResult(null),
    clearPredictResult: () => setPredictResult(null),
  };
}

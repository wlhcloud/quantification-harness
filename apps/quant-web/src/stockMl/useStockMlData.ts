import { useCallback, useEffect, useState } from "react";
import { message } from "antd";

import {
  stockMlApi,
  type StockMlCandidate,
  type StockMlConfigDto,
  type StockMlFeatureImportanceDto,
  type StockMlWalkforwardDto,
  type StockMlWalkforwardListItem,
} from "../api";

/**
 * 股票 ML 页面的只读数据源：一次并行拉 5 个接口，外加"备注改完本地即时生效"的小工具。
 *
 * 从前这些 state 和 load 都写在 1000 行的 StockMlPage.tsx 里，与 job 轮询、Tabs JSX 混在一起；
 * 拆开后页面壳子只消费这个 hook 的返回值。
 */
export function useStockMlData() {
  const [wf, setWf] = useState<StockMlWalkforwardDto | null>(null);
  const [candidates, setCandidates] = useState<StockMlCandidate[]>([]);
  const [candidateDate, setCandidateDate] = useState<string | null>(null);
  const [config, setConfig] = useState<StockMlConfigDto | null>(null);
  const [featureImp, setFeatureImp] = useState<StockMlFeatureImportanceDto | null>(null);
  const [versions, setVersions] = useState<StockMlWalkforwardListItem[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [wfData, candData, cfgData, impData, verData] = await Promise.all([
        stockMlApi.walkforwardLatest(),
        stockMlApi.candidates(50),
        stockMlApi.config(),
        stockMlApi.featureImportance(50),
        stockMlApi.walkforwardList(50),
      ]);
      setWf(wfData);
      setCandidates(candData.items ?? []);
      setCandidateDate(candData.tradeDate ?? null);
      setConfig(cfgData);
      setFeatureImp(impData);
      setVersions(verData.items ?? []);
    } catch (e) {
      message.error("加载失败：" + String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 备注保存后不回表（原实现就是本地 patch，避免整页 loading 闪一下） */
  const patchVersionNotes = useCallback((runId: string, notes: string) => {
    setVersions((prev) => prev.map((v) => (v.runId === runId ? { ...v, notes } : v)));
  }, []);

  return {
    wf, candidates, candidateDate, config, featureImp, versions, loading,
    load, patchVersionNotes,
  };
}

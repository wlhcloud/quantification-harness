import { useState } from "react";
import { Button, Card, Empty, Space, Tabs, Typography } from "antd";
import { ExperimentOutlined, FundOutlined, ReloadOutlined, SettingOutlined, StopOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { PageContainer } from "@ant-design/pro-components";

import type { StockMlWalkforwardListItem } from "./api";
import { dateText } from "./stockMl/format";
import { useStockMlData } from "./stockMl/useStockMlData";
import { useStockMlJobs } from "./stockMl/useStockMlJobs";
import { OverviewTab } from "./stockMl/OverviewTab";
import { FeaturesTab } from "./stockMl/FeaturesTab";
import { ConfigTab } from "./stockMl/ConfigTab";
import { VersionsTab } from "./stockMl/VersionsTab";
import { VersionDetailDrawer } from "./stockMl/VersionDetailDrawer";
import { BacktestModal } from "./stockMl/BacktestModal";
import { PredictModal } from "./stockMl/PredictModal";
import FreshnessAlert from "./stockMl/FreshnessAlert";

const { Paragraph } = Typography;

/**
 * 股票 ML 模型页（LGBMRanker）。
 *
 * 2026-09-13 拆分：原文件 1073 行（状态 + 3 个 job 轮询 + 4 个页签 JSX + 抽屉 + 2 个弹窗 + 90 行
 * 股票名映射）全部塞在一个组件里。现在按"数据 / 长任务 / 页签 / 覆盖层 / 纯函数"切开：
 *   - ./stockMl/format.ts、labels.ts、navChart.ts   纯函数（有 node:test 单测）
 *   - ./stockMl/useStockMlData.ts                    只读数据（5 个接口 + 备注本地 patch）
 *   - ./stockMl/useStockMlJobs.ts                    训练/回测/预测三个 job 的轮询与状态
 *   - ./stockMl/{Overview,Features,Config,Versions}Tab.tsx   四个页签
 *   - ./stockMl/{VersionDetailDrawer,BacktestModal,PredictModal}.tsx  抽屉与弹窗
 * 本文件只负责：页面骨架、UI 开关状态（选中的版本、弹窗开关）与页脚口径说明。
 * 行为保持与原实现一致（文案、配色、表格列、job 参数、二次确认都未改）。
 */
export default function StockMlPage() {
  const { wf, candidates, candidateDate, config, featureImp, versions, loading, load, patchVersionNotes } = useStockMlData();
  const {
    running, runTrain, cancelTrain, trainCancelling,
    backtestLoading, backtestResult, runBacktest, backtestProgress, clearBacktestResult,
    predictLoading, predictResult, runPredict, predictProgress, clearPredictResult,
  } = useStockMlJobs({ onDataChanged: load });

  // 覆盖层（抽屉/弹窗）的开关状态：纯 UI，与数据无关
  const [selectedVersion, setSelectedVersion] = useState<StockMlWalkforwardListItem | null>(null);
  const [backtestVersion, setBacktestVersion] = useState<StockMlWalkforwardListItem | null>(null);
  const [showBacktestModal, setShowBacktestModal] = useState(false);
  const [predictVersion, setPredictVersion] = useState<StockMlWalkforwardListItem | null>(null);
  const [showPredictModal, setShowPredictModal] = useState(false);

  const metrics = wf?.run?.metrics;
  const holdings = wf?.run?.holdings?.holdings ?? [];

  // 打开回测参数配置（清掉上一次的结果，避免看着旧数字改参数）
  const openBacktest = (version: StockMlWalkforwardListItem) => {
    setBacktestVersion(version);
    clearBacktestResult();
    setShowBacktestModal(true);
  };
  const closeBacktest = () => {
    setShowBacktestModal(false);
    clearBacktestResult();
  };

  // 打开指定模型预测
  const openPredict = (version: StockMlWalkforwardListItem) => {
    setPredictVersion(version);
    clearPredictResult();
    setShowPredictModal(true);
  };
  const closePredict = () => {
    setShowPredictModal(false);
    clearPredictResult();
  };

  const tabItems = [
    {
      key: "overview",
      label: (<span><FundOutlined /> 回测与信号</span>),
      children: (
        <OverviewTab
          metrics={metrics}
          signalDate={wf?.run?.holdings?.tradeDate}
          holdings={holdings}
          candidates={candidates}
          candidateDate={candidateDate}
          loading={loading}
        />
      ),
    },
    {
      key: "features",
      label: (<span><ExperimentOutlined /> 特征与因子</span>),
      children: <FeaturesTab config={config} featureImp={featureImp} />,
    },
    {
      key: "config",
      label: (<span><SettingOutlined /> 训练配置</span>),
      children: <ConfigTab config={config} />,
    },
    {
      key: "versions",
      label: (<span><FundOutlined /> 训练版本</span>),
      children: (
        <VersionsTab
          versions={versions}
          onReload={load}
          onPatchNotes={patchVersionNotes}
          onSelectVersion={setSelectedVersion}
          onBacktest={openBacktest}
          onPredict={openPredict}
        />
      ),
    },
  ];

  return (
    <PageContainer
      title="股票 ML 模型 · LGBMRanker"
      subTitle={
        <>
          {wf?.run ? `runId ${wf.run.runId} · label ${wf.run.label} · 生成于 ${dateText(wf.run.generatedAt)}` : "加载中…"}
          {config && ` · ${config.featureCount} 特征 · ${config.model.randomStates && config.model.randomStates.length > 1 ? `${config.model.randomStates.length}种子集成[${config.model.randomStates.join(",")}]` : `seed=${config.model.randomState}`}`}
        </>
      }
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          <Button type="primary" icon={<ThunderboltOutlined />} onClick={runTrain} loading={running} disabled={running}>重新训练模型</Button>
          {running ? <Button danger icon={<StopOutlined />} loading={trainCancelling} onClick={() => void cancelTrain()}>取消训练</Button> : null}
        </Space>
      }
    >
      <FreshnessAlert />

      {!wf?.run ? (
        <Card style={{ border: "1px solid #e5eaee", borderRadius: 10 }}>
          <Empty description="暂无训练记录（点击右上角「重新训练模型」开始）" style={{ padding: 40 }} />
        </Card>
      ) : (
        <Tabs items={tabItems} defaultActiveKey="overview" />
      )}

      <VersionDetailDrawer version={selectedVersion} onClose={() => setSelectedVersion(null)} />

      <BacktestModal
        open={showBacktestModal}
        version={backtestVersion}
        onClose={closeBacktest}
        onRun={runBacktest}
        loading={backtestLoading}
        progress={backtestProgress}
        result={backtestResult}
      />

      <PredictModal
        open={showPredictModal}
        version={predictVersion}
        onClose={closePredict}
        onRun={runPredict}
        loading={predictLoading}
        progress={predictProgress}
        result={predictResult}
      />

      <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
        口径：LGBMRanker 排序模型，{config?.featureCount ?? 31} 因子（featuresOverride），{config?.label ?? "forward_3"} label，walkforward 滚动训练；选股候选写入 selection_candidates 表（model_id=ml_walkforward，候选池 {config?.walkforward?.publishTopN ?? 20} 只 / 实际持仓 Top{config?.backtest?.topN ?? 5}），股票模拟盘可直接消费。{config?.model.randomStates && config.model.randomStates.length > 1
          ? `当前为 ${config.model.randomStates.length} 种子${config.model.ensembleMethod === 'median_zscore' ? '中位数集成（median_zscore）' : '均值集成（mean_zscore）'}（${config.model.randomStates.join("、")}），各窗口多种子训练后分数 z-score 标准化后${config.model.ensembleMethod === 'median_zscore' ? '取中位数聚合' : '等权平均'}，降低单种子随机性；主种子 seed=${config?.model.randomState ?? 42}。`
          : `随机种子 seed=${config?.model.randomState ?? 42} 固定，保证结果可复现。`}
      </Paragraph>
    </PageContainer>
  );
}

// Vite 在构建/开发时注入 import.meta.env；这样取值是为了同一份代码在
// 非 Vite 环境（node:test 里做组件渲染冒烟测试）也能 import，不会因 import.meta.env 为
// undefined 直接抛 TypeError。行为不变：Vite 下 env 一定有值。
const ENV: Record<string, string | undefined> = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
const DQ_BASE=ENV.VITE_DATA_QUERY_URL??'/api/dq'
const SYNC_BASE=ENV.VITE_SYNC_URL??'/api/sync'
const SETTINGS_BASE=ENV.VITE_SETTINGS_URL??'/api/settings'
const ENGINE_BASE=ENV.VITE_ENGINE_URL??'/api/engine'
const API_KEY=ENV.VITE_QUANT_API_KEY??''
const authHeaders=():Record<string,string>=>API_KEY?{'x-api-key':API_KEY}:{}

export interface DatasetDto { id:string;name:string;category:string;frequency:string;start_date:string|null;end_date:string|null;symbols:number;records:number;completeness:number;coverage:number;completeness_target:number;coverage_target:number;trading_day_lag:number|null;max_trading_day_lag:number;meets_sla:boolean;updated_at:string|null;status:string }
export interface QualityDto { frequency:string;complete_days:number;incomplete_days:number;source_missing_days:number;not_applicable_days:number;non_trading_days:number;sources:Array<{source:string;records:number}>;duplicate_buckets:number;anomalous_ohlc:number|null }
export interface MinuteBarDto { code:string;trade_time:string;freq:string;open:number;high:number;low:number;close:number;volume:number;amount:number;source:string }
export interface SecurityDto {code:string;name:string|null;industry:string|null;market:string|null;list_date:string|null}
export interface GovernanceCellDto { code:string;trade_date:string;status:'complete'|'incomplete'|'source_missing'|'not_applicable'|'non_trading';received:number }
export interface GovernanceIssueDto { status:GovernanceCellDto['status'];trade_date:string;affected_symbols:number;missing_buckets:number;sample_code:string;updated_at:string }
export interface GovernanceDto { frequency:string;expected_buckets:number;dates:string[];codes:string[];cells:GovernanceCellDto[];issues:GovernanceIssueDto[] }
export interface DataSourceDto { id:string;label:string;protocol:'x-api-key'|'official';baseUrl:string;token:string;hasToken?:boolean;enabled:boolean;priority:number;timeoutMs:number;notes:string|null;updatedAt:string }
export interface ToolRouteDto { toolId:string;label:string;primarySourceId:string;fallbackSourceId:string|null;updatedAt:string }
export interface DictionaryDatasetDto {id:string;name:string;category:string;frequency:string;description:string|null;updatedAt:string}
export interface DictionaryFieldDto {datasetId:string;fieldName:string;displayName:string;dataType:string;unit:string|null;nullable:boolean;isPrimaryKey:boolean;description:string|null;updatedAt:string}
export interface DictionaryMappingDto {datasetId:string;sourceId:string;sourceField:string;standardField:string;transformExpression:string|null;enabled:boolean;updatedAt:string}
export interface DictionaryTypeDto {code:string;name:string;description:string|null;isSystem:boolean;enabled:boolean;updatedAt:string}
export interface DictionaryItemDto {dictionaryCode:string;itemCode:string;itemName:string;sortOrder:number;isSystem:boolean;enabled:boolean;description:string|null;updatedAt:string}
/** 后端实际会返回的状态集合：`starting` 出现在 202 提交后（main.py:521），
 *  `complete_with_gaps` 出现在单日同步质量判定未通过时（main.py:302）。 */
export type SyncStatus = 'idle'|'starting'|'running'|'complete'|'complete_with_gaps'|'error';
export interface MinuteSyncStatusDto {runId:string|null;startDate:string|null;endDate:string|null;freq:string|null;status:SyncStatus;total:number;completed:number;failed:number;skipped:number;startedAt:string|null;finishedAt:string|null;error:string|null;current:string|null}
/** GET /sync/health：minuteSyncEnabled=false 时 /sync/minute* 一律 410，前端据此禁用入口。 */
export interface SyncHealthDto {status:string;databasePath:string;failedSegments:number;currentRunId?:string|null;currentRunStatus?:string|null;unresolvedQualityCells?:number;historicalSegmentErrors?:number;authEnabled?:boolean;authSource?:string|null;minuteSyncEnabled?:boolean}
export interface MinuteSyncItemDto {code:string;status:string;attempts:number;error:string|null;updatedAt:string}
export interface MinuteSyncDetailsDto {run:MinuteSyncStatusDto|null;counts:Record<string,number>;items:MinuteSyncItemDto[];dayLedger:{completed:number;zeroRows:number}}
export interface GenericSyncStatusDto {status:'idle'|'starting'|'running'|'complete'|'complete_with_gaps'|'error';runId?:string|null;totalDays?:number;completedDays?:number;failedDays?:number;gapDates?:string[];totalSteps?:number;completedSteps?:number;currentDate?:string|null;current?:string|null;startedAt?:string|null;finishedAt?:string|null;error?:string|null}
export interface SyncRunDto {id:string;kind:string;toolId?:string|null;status:string;parameters:Record<string,unknown>;total:number;completed:number;failed:number;skipped:number;received?:number;written?:number;source?:string|null;qualityStatus?:string|null;qualityDetails?:Record<string,unknown>|null;currentItem:string|null;startedAt:string;finishedAt:string|null;error:string|null;triggerSource:string;notificationStatus:string|null}
export interface SyncLogDto {id:string;timestamp:string;level:'info'|'warning'|'error';code:string|null;tradeDate:string|null;requestApi:string|null;requestParams:Record<string,unknown>;status:string;attempt:number;received:number;written:number;source:string|null;error:string|null;details:Record<string,unknown>|null}
export interface SyncConfigDto {minute:{concurrency:number;oneMinuteSegmentDays:number;fiveMinuteSegmentDays:number};requestTimeoutMs:number;sources:Array<{id:string;timeoutMs:number}>;minuteRun:{concurrency:number;oneMinuteSegmentDays:number;fiveMinuteSegmentDays:number}}
export interface MarketSchedulerDto {status:string;marketState:'trading'|'closed'|'non-trading-day'|'unknown';enabled:boolean;intervalSeconds:number;catchupEnabled:boolean;timezone:string;sessions:string[];lastCheckAt:string|null;lastTriggeredAt:string|null;nextCheckAt:string|null;lastError:string|null;catchupDay:string|null;lastSlot?:string|null;calendarSource?:string|null}
export interface SyncToolDto {toolId:string;label:string;table:string;kind:'market-snapshot'|'market-calendar'|'market-range'|'finance'|'minute';status:string;startedAt:string|null;finishedAt:string|null;total:number;done:number;current?:string|null;startDate?:string|null;endDate?:string|null;freq?:string|null;error:string|null}
export interface CatalogDatasetDto {id:string;provider:string;frequency:string;latestAt:string|null;rowCount:number;status:string;updatedAt:string}
export interface BtModelSummary { modelId:string;label:string;totalReturn:number;annualReturn:number;volatility?:number;sharpe:number;maxDrawdown:number;winRate:number;benchmarkTotalReturn:number;excessReturn:number;periods:number;averageHoldings:number }
export interface BtLatestDto { run:{runId:string;startedAt:string;finishedAt:string|null;status:string;summary:{firstSignalDate:string;lastExitDate:string;models:BtModelSummary[];periodCount:number;topN:number;costs:{commissionRate:number;stampDutyRate:number}}|null;error:string|null} }
export interface BtPeriodDto { modelId:string;signalDate:string;entryDate:string;exitDate:string;holdings:number;skipped:number;grossReturn:number;netReturn:number;benchmarkReturn:number;turnover:number }
export interface BtLayerDto { modelId:string;layer:number;averageReturn:number;periods:number }
export interface BtPeriodsDto { runId:string|null;items:BtPeriodDto[];layers:BtLayerDto[] }
export interface ShortLatestDto { run:{runId:string;startedAt:string;finishedAt:string|null;status:string;summary:{startDate:string;endDate:string;signalDays:number;trades:number;skippedNoMinute:number;winRate:number;averageReturn:number;profitLossRatio:number;totalReturn:number;maxDrawdown:number;reasons:Record<string,number>}|null;error:string|null} }
export interface SelectionModelDto { id:string;label:string;minScore:number;minCoverage:number;maxCandidates:number;weights:Record<string,number> }export interface SelectionReasonDto { factor:string;label:string;score:number;contribution:number }
export interface SelectionItemDto { tradeDate:string;rank:number;code:string;name:string;industry:string;close:number;score:number;reasons:SelectionReasonDto[] }
export interface SelectionDto { model:SelectionModelDto;count:number;items:SelectionItemDto[] }

export interface StockTechnicalDto {
  code:string;
  tradeDate:string;
  close:number;
  open:number;
  high:number;
  low:number;
  volume:number;
  amount:number;
  ma5:number;
  ma10:number;
  ma20:number;
  prevMa5:number;
  prevMa10:number;
  goldenCross:boolean;
  deathCross:boolean;
  trendUp:boolean;
  v5BuySignal:boolean;
  v5SellSignal:boolean;
  ma5AboveMa10:boolean;
  closeAboveMa20:boolean;
  error?:string;
  dataDays?:number;
}
export const stockTechnicalApi={
  batch:(codes:string[])=>get<{items:StockTechnicalDto[];count:number}>(`/stock/technical?codes=${encodeURIComponent(codes.join(','))}`),
}

export interface MarketIndexDto {code:string;name:string;receivedAt:string;open:number|null;high:number|null;low:number|null;close:number;preClose:number|null;change:number|null;pctChg:number|null;volume:number|null;amount:number|null;source:string}
export interface MarketMinuteDto {code:string;freq:string;tradeTime:string;open:number|null;high:number|null;low:number|null;close:number;volume:number|null;amount:number|null;sourceUpdatedAt:string|null;receivedAt:string;source:string}
export interface MarketOverviewDto {
  indices:MarketIndexDto[];minuteSeries:MarketMinuteDto[];
  breadth:{tradeDate:string|null;up:number;down:number;flat:number;total:number};
  limits:{tradeDate:string|null;up:number;down:number;broken:number};
  exchange:Array<{tradeDate:string;marketCode:string;marketName:string;exchange:string;companyCount:number;amount:number;volume:number;totalMv:number;floatMv:number;pe:number|null;turnoverRate:number|null;transactionCount:number|null;source:string}>;
  selection:{model?:{id:string;label:string};count:number;items:Array<{tradeDate:string;rank:number;code:string;name:string;industry:string;close:number;score:number;reasons:Array<{label:string;score:number}>}>};
  backtest:{run:null|{runId:string;startedAt:string;finishedAt:string|null;status:string;summary:null|{firstSignalDate:string;lastExitDate:string;models:Array<{modelId:string;label:string;totalReturn:number;annualReturn:number;sharpe:number;maxDrawdown:number;winRate:number;benchmarkTotalReturn:number;excessReturn:number;periods:number}>}}};
  datasets:DatasetDto[];tasks:Array<{id:string;kind:string;toolId:string|null;status:string;total:number;completed:number;failed:number;received:number;written:number;source:string|null;startedAt:string;finishedAt:string|null;error:string|null}>;
}

async function get<T>(path:string,signal?:AbortSignal):Promise<T>{
  const controller=new AbortController();
  const abort=()=>controller.abort();
  signal?.addEventListener('abort',abort,{once:true});
  const timeout=window.setTimeout(()=>controller.abort(),8000);
  try{
    const response=await fetch(DQ_BASE+path,{signal:controller.signal});
    if(!response.ok)throw new Error('数据服务 HTTP '+response.status);
    return response.json() as Promise<T>;
  }catch(error){
    if(controller.signal.aborted&&!signal?.aborted)throw new Error('数据服务响应超时（8 秒），请检查数据查询服务');
    throw error;
  }finally{window.clearTimeout(timeout);signal?.removeEventListener('abort',abort);}
}
export interface DailyBarDto {tsCode:string;tradeDate:string;open:number|null;high:number|null;low:number|null;close:number|null;preClose:number|null;change:number|null;pctChg:number|null;vol:number|null;amount:number|null;source:string|null}
export interface EtfUniverseItemDto {tsCode:string;name:string|null;management:string|null;custodian:string|null;fundType:string|null;investType:string|null;benchmark:string|null;listDate:string|null;foundDate:string|null;issueDate:string|null;delistDate:string|null;mFee:number|null;cFee:number|null;pValue:number|null;eligible:number|boolean}
/**
 * 9103 只读查询客户端。
 * 注：`/factors/summary`、`/factors/latest` 已随 legacy 因子选股退役删除（2026-09-18）。
 */
export const dataApi={
  datasets:(signal?:AbortSignal)=>get<DatasetDto[]>('/datasets',signal),
  quality:(freq:string,signal?:AbortSignal)=>get<QualityDto>('/datasets/minute/'+freq+'/quality',signal),
  governance:(freq:string,signal?:AbortSignal)=>get<GovernanceDto>('/datasets/minute/'+freq+'/governance',signal),
  minuteBars:(params:{code:string;freq:string;start:string;end:string;limit?:number;offset?:number},signal?:AbortSignal)=>{
    const query=new URLSearchParams({...params,limit:String(params.limit??200),offset:String(params.offset??0)})
    return get<MinuteBarDto[]>('/minute-bars?'+query,signal)
  },
  securities:(query:string,signal?:AbortSignal)=>get<SecurityDto[]>('/securities?'+new URLSearchParams({q:query,limit:'30'}),signal),
  /** 9103 选股接口族：含动态追加的 ml_walkforward 模型发现逻辑。 */
  selectionModels:()=>get<{items:SelectionModelDto[]}>('/selection/models'),
  selection:(model='ml_walkforward',limit=50)=>get<SelectionDto>('/selection?'+new URLSearchParams({model,limit:String(limit)})),
  etfUniverse:(params?:{q?:string;eligible?:boolean;limit?:number})=>{
    const query=new URLSearchParams({limit:String(params?.limit??50)})
    if(params?.q)query.set('q',params.q)
    if(params?.eligible)query.set('eligible','true')
    return get<EtfUniverseItemDto[]>('/etf/universe?'+query)
  },
  etfBars:(params:{tsCode:string;start?:string;end?:string;limit?:number;offset?:number})=>{
    const query=new URLSearchParams({ts_code:params.tsCode,limit:String(params.limit??200),offset:String(params.offset??0)})
    if(params.start)query.set('start',params.start)
    if(params.end)query.set('end',params.end)
    return get<DailyBarDto[]>('/etf/bars?'+query)
  },
  indexBars:(params:{tsCode:string;start?:string;end?:string;limit?:number;offset?:number})=>{
    const query=new URLSearchParams({ts_code:params.tsCode,limit:String(params.limit??200),offset:String(params.offset??0)})
    if(params.start)query.set('start',params.start)
    if(params.end)query.set('end',params.end)
    return get<DailyBarDto[]>('/index/bars?'+query)
  },
  backtestLatest:()=>get<BtLatestDto>('/backtest/latest'),
  backtestPeriods:(runId?:string)=>get<BtPeriodsDto>('/backtest/periods'+(runId?'?runId='+encodeURIComponent(runId):'')),
  shortLatest:()=>get<ShortLatestDto>('/backtest/short/latest'),
  shortTrades:(runId?:string,limit=100)=>get<{runId:string|null;items:Array<Record<string,unknown>>}>('/backtest/short/trades?'+new URLSearchParams({...(runId?{run_id:runId}:{}),limit:String(limit)})),
  metaDatasets:()=>get<{count:number;items:CatalogDatasetDto[]}>('/meta/datasets'),
  marketOverview:(code='000001.SH',freq='1m',signal?:AbortSignal)=>get<MarketOverviewDto>('/market/overview?'+new URLSearchParams({code,freq}),signal),
}

/** 带超时的 fetch：原先只有 dataApi.get 有超时，sync/settings 请求会无限挂起。 */
async function fetchWithTimeout(url:string,init:RequestInit|undefined,timeoutMs:number,label:string):Promise<Response>{
  const controller=new AbortController();
  const timer=window.setTimeout(()=>controller.abort(),timeoutMs);
  try{
    return await fetch(url,{...init,signal:init?.signal??controller.signal});
  }catch(error){
    if(controller.signal.aborted)throw new Error(`${label}响应超时（${Math.round(timeoutMs/1000)} 秒）`);
    throw error;
  }finally{window.clearTimeout(timer);}
}

async function syncRequest<T>(path:string,init?:RequestInit,timeoutMs=300_000):Promise<T>{
  // 默认 5 分钟：/sync/daily 是同步请求（历史最长约 4.5 分钟）
  const response=await fetchWithTimeout(SYNC_BASE+path,{...init,headers:{...authHeaders(),...(init?.headers??{})}},timeoutMs,'同步服务')
  const body=await response.json()
  if(!response.ok)throw new Error(body?.message??body?.error??body?.detail??('同步服务 HTTP '+response.status))
  return body as T
}
export type MinuteStartCfg = {concurrency?:number;segmentDays?:number;requestTimeoutMs?:number};
function withCfg(params:Record<string,string>,cfg?:MinuteStartCfg):Record<string,string>{
  const out={...params};
  if(cfg?.concurrency)out.concurrency=String(cfg.concurrency);
  if(cfg?.segmentDays)out.segmentDays=String(cfg.segmentDays);
  if(cfg?.requestTimeoutMs)out.requestTimeoutMs=String(cfg.requestTimeoutMs);
  return out;
}
export const syncApi={
  records:(limit=100)=>syncRequest<{count:number;items:SyncRunDto[]}>('/records?limit='+limit),
  logs:(runId:string,limit=500)=>syncRequest<{runId:string;count:number;items:SyncLogDto[]}>('/records/'+encodeURIComponent(runId)+'/logs?limit='+limit),
  health:()=>syncRequest<SyncHealthDto>('/health'),
  status:()=>syncRequest<MinuteSyncStatusDto>('/sync/minute/all/status'),
  config:()=>syncRequest<SyncConfigDto>('/sync/config'),
  setConfig:(patch:{minute?:Partial<SyncConfigDto['minute']>;requestTimeoutMs?:number})=>syncRequest<SyncConfigDto>('/sync/config',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify(patch)}),
  details:(runId?:string)=>syncRequest<MinuteSyncDetailsDto>('/sync/minute/all/details?limit=200'+(runId?'&run_id='+encodeURIComponent(runId):'')),
  failures:(runId?:string)=>syncRequest<{runId:string|null;items:MinuteSyncItemDto[]}>('/sync/minute/all/failures?limit=200'+(runId?'&run_id='+encodeURIComponent(runId):'')),
  start:(input:{startDate:string;endDate:string;freq:string}&MinuteStartCfg)=>syncRequest<MinuteSyncStatusDto>('/sync/minute/all?'+new URLSearchParams(withCfg({start_date:input.startDate,end_date:input.endDate,freq:input.freq},input)),{method:'POST'}),
  startSingle:(input:{code:string;startDate:string;endDate:string;freq:string}&MinuteStartCfg)=>syncRequest<{status:string;runId:string;result:{code:string;freq:string;received:number;accepted:number;skippedSegments:number;source:string}}>('/sync/minute?'+new URLSearchParams(withCfg({code:input.code,start_date:input.startDate,end_date:input.endDate,freq:input.freq},input)),{method:'POST'}),
  retry:(runId?:string)=>syncRequest<MinuteSyncStatusDto>('/sync/minute/all/retry'+(runId?'?run_id='+encodeURIComponent(runId):''),{method:'POST'}),
  tools:()=>syncRequest<{count:number;items:SyncToolDto[]}>('/sync/tools'),
  marketScheduler:()=>syncRequest<MarketSchedulerDto>('/sync/market-scheduler'),
  startTool:(toolId:string,params?:SyncToolParams)=>syncRequest<{ok:boolean;toolId:string;status:string}>('/sync/tool/'+encodeURIComponent(toolId)+(params?'?'+new URLSearchParams(Object.fromEntries(Object.entries(params).filter(([,v])=>v!==undefined&&v!==null&&v!=='') as Array<[string,string]>)):''),{method:'POST'}),
  /** 单交易日日线 + 复权因子同步（区间补拉走 coreSyncApi.startMarketHistory）。 */
  startDaily:(tradeDate:string)=>syncRequest<{status:string;tradeDate:string;counts:Record<string,number>;runId:string;quality:string}>('/sync/daily?'+new URLSearchParams({trade_date:tradeDate}),{method:'POST'}),
}
/** 后端 /sync/tool/{tool_id} 支持的查询参数（main.py:203-205 与各分支）。
 *  原先签名只声明 4 个，导致前端永远无法给 rt_idx_min 指定 freq、也无法限制 limit/并发。 */
export type SyncToolParams = {start_date?:string;end_date?:string;code?:string;period?:string;freq?:string;limit?:number;concurrency?:number;segment_days?:number;request_timeout_ms?:number}
export interface EtfSyncStateDto {status:SyncStatus;startedAt:string|null;finishedAt:string|null;error:string|null;runId:string|null}
export interface EtfUniverseStateDto extends EtfSyncStateDto {count:number;eligible:number;excluded:number;latestAt:string|null}
export interface EtfBarStateDto extends EtfSyncStateDto {totalDays:number;completedDays:number;failedDays:number;gapDates:string[];currentDate:string|null;written:number}
export interface EtfSyncStatusDto {universe:EtfUniverseStateDto;daily:EtfBarStateDto;index:EtfBarStateDto;counts:{universe:number;eligible:number;etfBars:number;indexBars:number}}
/** ETF 池 / 日线 / 指数日线同步（9101 main.py:346-424）。原先前端零调用，整套能力不可达。 */
export const etfSyncApi={
  status:()=>syncRequest<EtfSyncStatusDto>('/sync/etf/status'),
  syncUniverse:()=>syncRequest<EtfUniverseStateDto>('/sync/etf/universe',{method:'POST'}),
  syncDaily:(tradeDate:string)=>syncRequest<EtfBarStateDto>('/sync/etf/daily?'+new URLSearchParams({trade_date:tradeDate}),{method:'POST'}),
  history:(startDate:string,endDate:string,days=260,opts?:{concurrency?:number;requestTimeoutMs?:number})=>syncRequest<EtfBarStateDto>('/sync/etf/history?'+new URLSearchParams({
    start_date:startDate,end_date:endDate,days:String(days),
    ...(opts?.concurrency?{concurrency:String(opts.concurrency)}:{}),
    ...(opts?.requestTimeoutMs?{request_timeout_ms:String(opts.requestTimeoutMs)}:{}),
  }),{method:'POST'}),
  historyStatus:()=>syncRequest<EtfBarStateDto>('/sync/etf/history/status'),
  syncIndexDaily:(tradeDate:string)=>syncRequest<EtfBarStateDto>('/sync/etf/index-daily?'+new URLSearchParams({trade_date:tradeDate}),{method:'POST'}),
  indexHistory:(startDate:string,endDate:string,days=260,opts?:{concurrency?:number;requestTimeoutMs?:number})=>syncRequest<EtfBarStateDto>('/sync/etf/index-history?'+new URLSearchParams({
    start_date:startDate,end_date:endDate,days:String(days),
    ...(opts?.concurrency?{concurrency:String(opts.concurrency)}:{}),
    ...(opts?.requestTimeoutMs?{request_timeout_ms:String(opts.requestTimeoutMs)}:{}),
  }),{method:'POST'}),
  indexHistoryStatus:()=>syncRequest<EtfBarStateDto>('/sync/etf/index-history/status'),
}
export const coreSyncApi={
  marketStatus:()=>syncRequest<GenericSyncStatusDto>('/sync/daily/history/status'),
  financeStatus:()=>syncRequest<GenericSyncStatusDto>('/sync/finance/market/status'),
  startMarketHistory:(startDate:string,endDate:string,days:number)=>syncRequest<GenericSyncStatusDto>('/sync/daily/history?'+new URLSearchParams({start_date:startDate,end_date:endDate,days:String(days)}),{method:'POST'}),
  startFinance:(quarters:number)=>syncRequest<GenericSyncStatusDto>('/sync/finance/market?quarters='+quarters,{method:'POST'}),
  startFinanceCode:(code:string,startDate:string)=>syncRequest<{status:string;code:string;result:unknown}>('/sync/finance?'+new URLSearchParams({code,start_date:startDate}),{method:'POST'}),
}

export type EngineJobStatus='queued'|'running'|'complete'|'error'|'cancelled';
export type EngineJobDto={id:string;type:string;status:EngineJobStatus;progress:number;parameters:Record<string,unknown>;result:Record<string,unknown>|null;error:string|null;created_at:string;started_at:string|null;finished_at:string|null;cancel_requested?:boolean}
async function engineRequest<T>(path:string,init?:RequestInit,timeoutMs=30_000):Promise<T>{
  // 原实现无超时：引擎卡住时请求会一直挂着（job 提交/查询都需要兜底）。
  // 长同步请求（如指定模型回测是同步长请求）由调用方显式传入更长的 timeoutMs。
  const controller=new AbortController();
  const timer=window.setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(ENGINE_BASE+path,{...init,signal:init?.signal??controller.signal,headers:{...authHeaders(),...(init?.headers??{})}})
    const body=await response.json()
    if(!response.ok){const detail=body?.detail;throw Object.assign(new Error(typeof detail==='string'?detail:(detail?.message??'计算引擎 HTTP '+response.status)),{gate:detail?.gate})}
    return body as T
  }catch(error){
    if(controller.signal.aborted)throw new Error(`计算引擎响应超时（${Math.round(timeoutMs/1000)} 秒）`);
    throw error;
  }finally{window.clearTimeout(timer);}
}
/**
 * 计算引擎任务中心客户端。
 *
 * 注：`readiness`（数据准入）与 `startBacktest`（job 化月度回测）已于 2026-09-18 移除——
 * legacy 因子选股退役后月度回测没有任何带权重的模型，准入恒为"硬阻断"，
 * 前端再挂一个永远"禁止运行"的面板只会误导用户。**准入门禁本身保留在服务端**
 * （它同时挡住 `run_monthly` 在空权重下 `used / total_weight` 的除零崩溃），
 * 直接提交 `POST /jobs{type:'backtest'}` 会得到 409 与明确原因。
 */
export const engineApi={
  /** 任务中心列表（含历史 job）。原先前端完全无法看到已提交的长任务。 */
  jobs:(limit=50)=>engineRequest<EngineJobDto[]>('/jobs?'+new URLSearchParams({limit:String(limit)})),
  /** 请求取消 queued/running 任务；后端幂等，返回 job 最新状态。 */
  cancelJob:(id:string)=>engineRequest<EngineJobDto>('/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'}),
}

export interface EtfWalkforwardDto {
  run_id:string;generated_at:string;label:string;start_date:string;end_date:string;windows:number;
  config:Record<string,unknown>;
  metrics:{windows:number;totalReturn:number;benchmarkReturn:number;excessReturn:number;annualReturn:number;sharpe:number;maxDrawdown:number;avgRankIc:number};
  holdings:{tradeDate:string;bearRegime:boolean;holdings:string[]};
  daily:Array<{tradeDate:string;nav:number;benchmark:number}>;
  status:string;error:string|null;
}
// /etf-quant/research/latest 直接返回 etf_ic_research 行（snake_case），无数据时为 {status:"none"}
export interface EtfResearchRow { factor:string;ic:number|null;rankIc:number|null;icir:number|null;icPositive:number|null;days:number|null;icStd:number|null }
export interface EtfResearchDto {
  status?:string;run_id?:string;generated_at?:string;label?:string;
  window_start?:string;window_end?:string;days?:number;results?:EtfResearchRow[];
}
export interface EtfTrainRunDto { runId:string;generatedAt:string;label:string;trainStart:string;trainEnd:string;validStart:string;validEnd:string;testStart:string;testEnd:string;rankIc:number;icMean:number;icir:number;status:string }
export interface EtfTrainDto { status:string;runId:string;generatedAt:string;label:string;trainStart:string;trainEnd:string;validStart:string;validEnd:string;testStart:string;testEnd:string;rankIc:number;icMean:number;icir:number;modelPath:string;params:Record<string,unknown>;history:EtfTrainRunDto[] }
export interface EtfBacktestDto { runId:string;generatedAt:string;startDate:string;endDate:string;summary:Record<string,unknown>;holdings:Array<{tradeDate:string;tsCode:string;weight:number;entryPrice:number}>;status:string;error:string|null }
export type EtfJobType='etf_factors'|'etf_research'|'etf_train'|'etf_backtest'|'etf_walkforward';
export const etfApi={
  walkforwardLatest:()=>engineRequest<EtfWalkforwardDto>('/etf-quant/walkforward/latest'),
  researchLatest:()=>engineRequest<EtfResearchDto>('/etf-quant/research/latest'),
  backtestLatest:()=>engineRequest<EtfBacktestDto>('/etf-quant/backtest/latest'),
  trainLatest:()=>engineRequest<EtfTrainDto>('/etf-quant/train/latest'),
  startJob:(type:EtfJobType)=>engineRequest<EngineJobDto>('/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type})}),
  job:(id:string)=>engineRequest<EngineJobDto>('/jobs/'+encodeURIComponent(id)),
}

// 股票 ML 模型（walkforward + 选股候选）
export interface StockMlMetrics { windows:number;totalReturn:number;benchmarkReturn:number;excessReturn:number;annualReturn:number;sharpe:number;maxDrawdown:number;avgRankIc:number }
export interface StockMlHolding { code:string;score:number;weight:number }
export interface StockMlWalkforwardDto { run:{runId:string;generatedAt:string;label:string;metrics:StockMlMetrics;holdings:{tradeDate:string;holdings:StockMlHolding[]}}|null }
export interface StockMlCandidate { tradeDate:string;rank:number;code:string;score:number;reasons:Array<{factor:string;label:string;score:number;contribution:number}>;computedAt:string }
export interface StockMlCandidatesDto { modelId:string;tradeDate:string|null;count:number;items:StockMlCandidate[] }
export type StockMlJobType='stock_ml_factors'|'stock_ml_walkforward'|'stock_ml_backtest'|'stock_ml_predict';
export interface StockMlModelConfig { nEstimators:number;learningRate:number;numLeaves:number;minDataInLeaf:number;subsample:number;colsampleBytree:number;randomState:number;randomStates?:number[]|null;ensembleMethod?:string|null;earlyStopping:number;validationDays:number;testDays:number;labelGrades:number }
export interface StockMlWalkforwardConfig { minTrainDays:number;stepDays:number;testDays:number;embargoDays?:number;publishedTopN?:number;publishTopN?:number }
export interface StockMlBacktestConfig { topN:number;rebalanceDays:number;transactionCostBps:number;slippageBps:number }
export interface StockMlUniverseConfig { minMarketCap:number;maxMarketCap:number;excludeST:boolean;excludeSuspended:boolean;minListDays:number }
export interface StockMlConfigDto { label:string;features:string[];featureCount:number;model:StockMlModelConfig;walkforward:StockMlWalkforwardConfig;backtest:StockMlBacktestConfig;universe:StockMlUniverseConfig }
export interface StockMlFeatureImportanceItem { feature:string;gain:number;gainPct:number;split:number;splitPct:number }
export interface StockMlWalkforwardListItem {
  runId:string;generatedAt:string;label:string;startDate:string;endDate:string;windows:number;status:string;isPublished:boolean;notes:string;
  model:{nSeeds:number;seeds:number[];ensembleMethod:string;nEstimators:number;learningRate:number;numLeaves:number;subsample:number;colsampleBytree:number;randomState:number};
  walkforward:{minTrain:number;stepDays:number;testDays:number};
  backtest:{topN:number;rebalanceDays:number};
  featureCount:number;features:string[];
  metrics:StockMlMetrics;
}
export interface StockMlWalkforwardListDto { count:number;items:StockMlWalkforwardListItem[] }
export interface StockMlFeatureImportanceDto { featureCount:number;modelFile:string;items:StockMlFeatureImportanceItem[];ensemble?:boolean;ensembleSize?:number;seeds?:number[]|null;error?:string }
export interface StockMlBacktestMetrics { totalReturn:number; benchmarkReturn:number; excessReturn:number; annualReturn:number; sharpe:number; maxDrawdown:number; startDate:string; endDate:string; tradingDays:number; modelFile:string }
export interface StockMlBacktestHolding { code:string; score:number; rank:number }
export interface StockMlBacktestDto { metrics:StockMlBacktestMetrics; daily:Array<{tradeDate:string;nav:number}>; holdings:StockMlBacktestHolding[] }
export interface StockMlBacktestParams { runId?:string; modelPath?:string; startDate?:string; endDate?:string; topN?:number }

export interface StockMlPredictItem { rank:number; code:string; score:number; momentum20:number|null; industryRank20:number|null; mktCap:number|null }
export interface StockMlPredictDto { tradeDate:string; modelFile:string; featureCount:number; candidateCount:number; count:number; items:StockMlPredictItem[] }
export interface StockMlPredictParams { runId?:string; modelPath?:string; topN?:number; tradeDate?:string }

/** V5 技术面择时回测（读 artifacts 下的回测 CSV 现场计算，可复算） */
export interface V5BacktestDto {
  status:'ok'|'none'|'empty'; variant:'long'|'short'; sourceFile?:string; generatedAt?:string;
  startDate?:string; endDate?:string; tradingDays?:number;
  initialValue?:number; finalValue?:number; totalReturn?:number; annualReturn?:number;
  sharpe?:number; maxDrawdown?:number; annualization?:string;
  curve?:Array<{date:string;value:number}>; note?:string; expectedFile?:string;
}

export interface StockMlDataFreshnessDto {signalDate:string|null;tables:Record<string,string|null>;warnings:string[]}
export interface StockMlLabelAuditDto {error?:string;[key:string]:unknown}
export const stockMlApi={
  walkforwardLatest:()=>engineRequest<StockMlWalkforwardDto>('/stock-ml/walkforward/latest'),
  walkforwardList:(limit=50)=>engineRequest<StockMlWalkforwardListDto>('/stock-ml/walkforward/list?'+new URLSearchParams({limit:String(limit)})),
  walkforwardPublish:(runId:string)=>engineRequest<{ok:boolean;runId:string;label:string;message:string;error?:string}>('/stock-ml/walkforward/publish?'+new URLSearchParams({run_id:runId}),{method:'POST'}),
  walkforwardDelete:(runId:string)=>engineRequest<{ok:boolean;runId:string;label:string;message:string;error?:string}>('/stock-ml/walkforward/delete?'+new URLSearchParams({run_id:runId}),{method:'DELETE'}),
  walkforwardUpdateNotes:(runId:string,notes:string)=>engineRequest<{ok:boolean;runId:string;label:string;notes:string;message:string;error?:string}>('/stock-ml/walkforward/notes',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({runId,notes})}),
  candidates:(limit=50)=>engineRequest<StockMlCandidatesDto>('/stock-ml/candidates?'+new URLSearchParams({limit:String(limit)})),
  config:()=>engineRequest<StockMlConfigDto>('/stock-ml/config'),
  /** 辅助因子表（行业/资金流）新鲜度：落后会在 walkforward 结果里告警，此前前端无处展示。 */
  dataFreshness:()=>engineRequest<StockMlDataFreshnessDto>('/stock-ml/data-freshness'),
  /** 口径审计（只读但耗时，实测 >25s），按需触发而非页面加载即调用。 */
  labelAudit:(params?:{label?:string;codeGlob?:string;threshold?:number})=>{
    const query=new URLSearchParams({label:params?.label??'forward_5',code_glob:params?.codeGlob??'*',threshold:String(params?.threshold??0.005)})
    return engineRequest<StockMlLabelAuditDto>('/stock-ml/label-audit?'+query,undefined,15*60*1000)
  },
  featureImportance:(topN=50)=>engineRequest<StockMlFeatureImportanceDto>('/stock-ml/feature-importance?'+new URLSearchParams({top_n:String(topN)})),
  backtest:(params:StockMlBacktestParams)=>engineRequest<StockMlBacktestDto>('/stock-ml/backtest',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(params)},15*60*1000),
  predict:(params:StockMlPredictParams)=>engineRequest<StockMlPredictDto>('/stock-ml/predict',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(params)},5*60*1000),
  /** job 化入口：带进度，前端可轮询（原同步端点保留兼容） */
  startJobWithParams:(type:StockMlJobType,parameters:Record<string,unknown>)=>engineRequest<EngineJobDto>('/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type,parameters})}),
  technicalTimingBacktest:(variant:'long'|'short'='long')=>engineRequest<V5BacktestDto>('/stock/technical-timing/backtest/latest?'+new URLSearchParams({variant})),
  startJob:(type:StockMlJobType)=>engineRequest<EngineJobDto>('/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type})}),
  job:(id:string)=>engineRequest<EngineJobDto>('/jobs/'+encodeURIComponent(id)),
}

export interface SimRunDto { run_id:string;initial_capital:number;start_date:string;commission_rate:number;status:string;created_at:string;last_signal_date:string|null;last_nav:number|null }export interface SimPositionDto { ts_code:string;shares:number;avg_cost:number;last_price:number }
export interface SimDailyDto { tradeDate:string;nav:number;cash:number;marketValue:number;positionCount:number;bearRegime:number;note:string|null }
export interface SimStatusDto { run:SimRunDto|null;positions:SimPositionDto[];latest:SimDailyDto|null;totalReturn:number|null }
export interface SimAdvanceDto { ok:boolean;waiting?:boolean;message?:string;runId:string;signalDate:string|null;bearRegime:boolean;nav:number;cash:number;marketValue:number;positions:Array<{tsCode:string;shares:number;avgCost:number;lastPrice:number}>;trades:{sold:unknown[];bought:Array<{tsCode:string;shares:number;price:number;fee:number;tradeDate:string;t1Price:boolean}>};valuationDate:string;error?:string }
/** POST /etf-quant/sim/start 返回 camelCase（etf_quant/sim.py:159），而 /sim/status 返回 snake_case——
 *  同一对象两种命名，这里显式建模，避免调用方再读不存在的 `run` 键。 */
export interface EtfSimStartDto { runId:string;initialCapital:number;startDate:string;signalDate:string|null;targetCount:number;bearRegime:boolean;nav:number;cash:number;marketValue:number }
export const etfSimApi={
  start:(initialCapital=1_000_000,startDate?:string)=>engineRequest<EtfSimStartDto>('/etf-quant/sim/start?'+new URLSearchParams({initial_capital:String(initialCapital),...(startDate?{start_date:startDate}:{})}),{method:'POST'}),
  advance:(runId:string)=>engineRequest<SimAdvanceDto>('/etf-quant/sim/advance?'+new URLSearchParams({run_id:runId}),{method:'POST'}),
  status:(runId?:string)=>engineRequest<SimStatusDto>('/etf-quant/sim/status'+(runId?'?'+new URLSearchParams({run_id:runId}):'')),
  history:(runId?:string,limit=500)=>engineRequest<{runId:string|null;items:SimDailyDto[]}>('/etf-quant/sim/history?'+new URLSearchParams({...(runId?{run_id:runId}:{}),limit:String(limit)})),
}

export interface StockSimRunDto { run_id:string;model_id:string;top_n:number;initial_capital:number;commission_rate:number;stamp_duty_rate:number;regime_filter:number|boolean;status:string;created_at:string;last_signal_date:string|null;last_nav:number|null;execution_mode?:string }
export interface StockSimPositionDto { code:string;shares:number;avgCost:number;lastPrice:number;highPrice?:number }
export interface StockSimDailyDto { signalDate:string;tradeDate:string;nav:number;cash:number;marketValue:number;positionCount:number;note:string|null }
export interface StockSimStatusDto { run:StockSimRunDto|null;positions:StockSimPositionDto[];latest:StockSimDailyDto|null;totalReturn:number|null }
export interface StockSimAdvanceDto { ok:boolean;waiting?:boolean;message?:string;runId?:string;signalDate?:string;tradeDate?:string;bearRegime?:boolean;nav?:number;cash?:number;marketValue?:number;positions?:StockSimPositionDto[];trades?:{sold:Array<{code:string;shares:number;price:number;fee:number}>;bought:Array<{code:string;shares:number;price:number;fee:number;tradeDate:string}>};error?:string }
/** `/stock/sim/runs` 列表项。`executionMode`/`legacy` 由后端返回，用于标识不能再推进的旧口径账本。 */
export interface StockSimRunListItemDto { runId:string;modelId:string;topN:number;initialCapital:number;regimeFilter:boolean;status:string;createdAt:string;lastSignalDate:string|null;lastNav:number|null;navSeries:{tradeDate:string;nav:number}[];executionMode?:string;legacy?:boolean }
export const stockSimApi={
  start:(model='low_volatility',topN=5,initialCapital=1_000_000,regimeFilter=true)=>engineRequest<{runId:string;modelId:string;topN:number;signalDate:string|null;targetCount:number;regimeFilter:boolean;nav:number;cash:number;marketValue:number}>('/stock/sim/start?'+new URLSearchParams({model,top_n:String(topN),initial_capital:String(initialCapital),regime_filter:String(regimeFilter)}),{method:'POST'}),
  advance:(runId:string)=>engineRequest<StockSimAdvanceDto>('/stock/sim/advance?'+new URLSearchParams({run_id:runId}),{method:'POST'}),
  status:(runId?:string)=>engineRequest<StockSimStatusDto>('/stock/sim/status'+(runId?'?'+new URLSearchParams({run_id:runId}):'')),
  history:(runId?:string,limit=500)=>engineRequest<{runId:string|null;items:StockSimDailyDto[]}>('/stock/sim/history?'+new URLSearchParams({...(runId?{run_id:runId}:{}),limit:String(limit)})),
  runs:(includeArchived=false)=>engineRequest<{items:StockSimRunListItemDto[]}>('/stock/sim/runs'+(includeArchived?'?include_archived=true':'')),
}

export interface RegimeMetrics { totalReturn:number;annualReturn:number;maxDrawdown:number;sharpe:number;winRate:number;finalNav:number;periods:number }
export interface RegimeModelDto { attribution:{periods:number;zeroHoldingPeriods:number;bearPeriods:number;bullPeriods:number;costDragTotal:number;avgGrossPerPeriod:number}; raw:RegimeMetrics; timed:RegimeMetrics }
export interface RegimeAnalysisDto { runId:string;maWindow:number;models:Record<string,RegimeModelDto> }
export const stockRegimeApi={
  analysis:(runId?:string)=>engineRequest<RegimeAnalysisDto>('/stock/regime-analysis'+(runId?'?'+new URLSearchParams({run_id:runId}):'')),
}

async function settingsRequest<T>(path:string,init?:RequestInit,timeoutMs=30_000):Promise<T>{const response=await fetchWithTimeout(SETTINGS_BASE+path,{...init,headers:{...authHeaders(),...(init?.headers??{})}},timeoutMs,'配置服务');const body=await response.json();if(!response.ok)throw new Error(body?.message??body?.error??body?.detail??('配置服务 HTTP '+response.status));return body as T}
export const settingsApi={
  sources:()=>get<{count:number;items:DataSourceDto[]}>('/meta/data-sources'),
  save:(items:Array<{id:string;label:string;protocol:'x-api-key'|'official';baseUrl:string;token:string;enabled:boolean;priority:number;timeoutMs:number;notes:string|null}>)=>settingsRequest<{ok:boolean;items:DataSourceDto[]}>('data-sources',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({items})}),
  saveSource:(item:Omit<DataSourceDto,'updatedAt'>)=>settingsRequest<{ok:boolean;item:DataSourceDto}>('data-sources/item',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(item)}),
  deleteSource:(id:string)=>settingsRequest<{ok:boolean}>('data-sources/item?id='+encodeURIComponent(id),{method:'DELETE'}),
  test:(id:string)=>settingsRequest<{ok:boolean;latencyMs:number;message:string}>('data-sources/test',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id})}),
  toolRoutes:()=>get<{count:number;items:ToolRouteDto[]}>('/meta/tool-routes'),
  saveToolRoutes:(items:ToolRouteDto[])=>settingsRequest<{ok:boolean;items:ToolRouteDto[]}>('tool-routes',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({items})}),
  saveToolRoute:(item:ToolRouteDto)=>settingsRequest<{ok:boolean;item:ToolRouteDto}>('tool-routes/item',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify(item)}),
  dictionary:()=>get<{datasets:DictionaryDatasetDto[];fields:DictionaryFieldDto[];mappings:DictionaryMappingDto[];types:DictionaryTypeDto[];items:DictionaryItemDto[]}>('/meta/dictionary'),
  saveDictionary:(kind:'datasets'|'fields'|'mappings',item:unknown)=>settingsRequest<{ok:boolean}>('dictionary/item?kind='+kind,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(item)}),
  deleteDictionary:(kind:'datasets'|'fields'|'mappings',key:Record<string,string>)=>settingsRequest<{ok:boolean}>('dictionary/item?kind='+kind+'&'+new URLSearchParams(key),{method:'DELETE'}),
  saveBaseDictionary:(kind:'types'|'items',item:unknown)=>settingsRequest<{ok:boolean}>('base-dictionary/item?kind='+kind,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(item)}),
  deleteBaseDictionary:(kind:'types'|'items',key:Record<string,string>)=>settingsRequest<{ok:boolean}>('base-dictionary/item?kind='+kind+'&'+new URLSearchParams(key),{method:'DELETE'}),
}

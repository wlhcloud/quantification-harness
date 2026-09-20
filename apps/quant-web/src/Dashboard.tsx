import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, App as AntApp, Badge, Button, Segmented, Skeleton, Tooltip, Typography } from "antd";
import { CheckCircleOutlined, ClockCircleOutlined, LineChartOutlined, ReloadOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { PageContainer, ProCard } from "@ant-design/pro-components";
import { dataApi, etfApi, syncApi, type EtfWalkforwardDto, type MarketIndexDto, type MarketOverviewDto, type MarketSchedulerDto } from "./api";

const INDEX_ORDER = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"];
const fmt = (value:number|null|undefined, digits=2) => value == null ? "—" : value.toLocaleString("zh-CN", {minimumFractionDigits:digits,maximumFractionDigits:digits});
const pct = (value:number|null|undefined) => value == null ? "—" : `${value >= 0 ? "+" : ""}${fmt(value,2)}%`;
const compactAmount = (value:number|null|undefined) => value == null ? "—" : `${fmt(value / 100_000_000, 1)} 亿`;
const dateText = (value:string|null|undefined) => value ? value.replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3") : "—";
const timeText = (value:string|null|undefined) => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "—";
const direction = (value:number|null|undefined) => value == null ? "flat" : value > 0 ? "up" : value < 0 ? "down" : "flat";
const sourceName = (value:string) => ({official:"Tushare Official",rds:"RDS",promax:"Promax"}[value]??value);
const isTradingTime = () => {const now=new Date(),day=now.getDay(),minute=now.getHours()*60+now.getMinutes();return day>=1&&day<=5&&((minute>=570&&minute<=690)||(minute>=780&&minute<=900))};

function MiniLine({values,tone}:{values:number[];tone:string}) {
  if(values.length<2)return <div className="market-mini-empty"/>;
  const min=Math.min(...values),max=Math.max(...values),span=max-min||1;
  const points=values.map((v,i)=>`${i/(values.length-1)*100},${28-(v-min)/span*24}`).join(" ");
  return <svg className="market-mini-line" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true"><polyline points={points} fill="none" stroke={tone} strokeWidth="1.8" vectorEffect="non-scaling-stroke"/></svg>;
}

function IntradayChart({rows,quote,intervalMinutes}:{rows:MarketOverviewDto["minuteSeries"];quote?:MarketIndexDto;intervalMinutes:number}) {
  const values=rows.map(row=>row.close).filter(Number.isFinite);
  if(values.length<2)return <div className="market-chart-empty"><ClockCircleOutlined/><b>分时数据尚未形成曲线</b><span>点击“刷新行情”获取最新分钟点；开盘后会逐步积累。</span></div>;
  const width=760,height=270,left=58,right=58,top=22,bottom=30,base=quote?.preClose||values[0];
  const rawMin=Math.min(...values,base),rawMax=Math.max(...values,base),deviation=Math.max(rawMax-base,base-rawMin,(rawMax-rawMin)*.06,.01),min=base-deviation,max=base+deviation,span=max-min;
  const times=rows.map(row=>new Date(row.tradeTime.replace(" ","T")).getTime()),firstTime=times[0],timeSpan=Math.max(times.at(-1)!-firstTime,60_000);
  const plotWidth=width-left-right,plotHeight=height-top-bottom,xAt=(index:number)=>left+(times[index]-firstTime)/timeSpan*plotWidth,yAt=(value:number)=>top+(max-value)/span*plotHeight;
  const yTicks=[0,.25,.5,.75,1].map(ratio=>({ratio,value:max-ratio*span}));
  const rawXIndexes=Array.from(new Set([0,.25,.5,.75,1].map(ratio=>Math.round(ratio*(rows.length-1)))));
  const xIndexes=rawXIndexes.reduce<number[]>((kept,index)=>{const previous=kept.at(-1);if(previous==null||xAt(index)-xAt(previous)>=55)kept.push(index);else if(index===rows.length-1)kept.splice(-1,1,index);return kept},[]);
  const gapMinutes=Math.max(5,intervalMinutes*2);
  const segments=rows.reduce<number[][]>((all,_,index)=>{if(!index||times[index]-times[index-1]>gapMinutes*60_000)all.push([]);all.at(-1)!.push(index);return all},[]);
  return <div className="market-chart-wrap"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${quote?.name??"指数"}分时走势`}>
    {yTicks.map(({ratio,value})=><g key={ratio}><line x1={left} x2={width-right} y1={top+ratio*plotHeight} y2={top+ratio*plotHeight} stroke={ratio===.5?"#c9d4e2":"#e8eef5"} strokeDasharray={ratio===.5?"4 3":undefined}/><text x={left-8} y={top+ratio*plotHeight+4} textAnchor="end">{fmt(value,2)}</text><text className={value>base?"axis-up":value<base?"axis-down":""} x={width-right+8} y={top+ratio*plotHeight+4}>{pct((value/base-1)*100)}</text></g>)}
    {xIndexes.map(index=><g key={index}><line x1={xAt(index)} x2={xAt(index)} y1={top} y2={height-bottom} stroke="#f1f4f8"/><text x={xAt(index)} y={height-8} textAnchor={index===0?"start":index===rows.length-1?"end":"middle"}>{rows[index]?.tradeTime?.slice(11,16)||"—"}</text></g>)}
    {segments.map((segment,index)=>segment.length>1?<polyline key={index} points={segment.map(i=>`${xAt(i)},${yAt(values[i])}`).join(" ")} fill="none" stroke="#1677ff" strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round"/>:<circle key={index} cx={xAt(segment[0])} cy={yAt(values[segment[0]])} r="3" fill="#1677ff"/>)}
  </svg></div>;
}

export default function Dashboard({onNavigate}:{onNavigate:(path:string)=>void}) {
  const {message}=AntApp.useApp();
  const [data,setData]=useState<MarketOverviewDto|null>(null),[loading,setLoading]=useState(true),[refreshing,setRefreshing]=useState(false),[selected,setSelected]=useState(INDEX_ORDER[0]),[frequency,setFrequency]=useState("1m"),[clock,setClock]=useState(Date.now());
  const [scheduler,setScheduler]=useState<MarketSchedulerDto|null>(null);
  const [etf,setEtf]=useState<EtfWalkforwardDto|null>(null);
  const refreshLock=useRef(false);
  const load=useCallback(async(code=selected)=>{const next=await dataApi.marketOverview(code,frequency);setData(next);},[selected,frequency]);
  useEffect(()=>{setLoading(true);Promise.all([load().catch(()=>undefined),etfApi.walkforwardLatest().then(setEtf).catch(()=>setEtf(null))]).then(()=>setLoading(false)).catch(()=>setLoading(false))},[load]);
  const refresh=useCallback(async(silent=false)=>{if(refreshLock.current)return;refreshLock.current=true;if(!silent)setRefreshing(true);try{await Promise.all([syncApi.startTool("rt_idx_k"),syncApi.startTool("rt_idx_min")]);for(let i=0;i<40;i+=1){await new Promise(resolve=>window.setTimeout(resolve,500));const tools=await syncApi.tools();const targets=tools.items.filter(x=>x.toolId==="rt_idx_k"||x.toolId==="rt_idx_min");if(targets.length===2&&targets.every(x=>x.status!=="running"&&x.status!=="queued"))break}await load();setClock(Date.now());if(!silent)message.success("实时指数已刷新")}catch(e){if(!silent)message.error(e instanceof Error?e.message:String(e))}finally{refreshLock.current=false;setRefreshing(false)}},[load,message]);
  useEffect(()=>{const readStatus=()=>syncApi.marketScheduler().then(setScheduler).catch(()=>undefined);void readStatus();const clockTimer=window.setInterval(()=>setClock(Date.now()),15_000),readTimer=window.setInterval(()=>{void readStatus();if(isTradingTime())void load()},30_000);return()=>{window.clearInterval(clockTimer);window.clearInterval(readTimer)}},[load]);
  const quote=data?.indices.find(x=>x.code===selected);
  const snapshots=useMemo(()=>INDEX_ORDER.map(code=>data?.indices.find(x=>x.code===code)).filter((x):x is MarketIndexDto=>!!x),[data]);
  const breadth=data?.breadth,total=breadth?.total||1,production=data?.datasets.filter(x=>x.id!=="minute-1m"&&!x.id.includes("minute_bars.1m"))??[],ready=production.filter(x=>x.meets_sla).length;
  const models=data?.backtest.run?.summary?.models??[],best=models.length?[...models].sort((a,b)=>b.totalReturn-a.totalReturn)[0]:null;
  const snapshotAge=quote?.receivedAt?Math.max(0,clock-new Date(quote.receivedAt).getTime()):Infinity,freshness=snapshotAge<=90_000?{status:"processing" as const,text:"实时 · 后端60秒调度"}:snapshotAge<=300_000?{status:"warning" as const,text:"行情延迟"}:{status:"error" as const,text:"行情已过期"};
  const signalDate=data?.selection.items[0]?.tradeDate,signalStale=!!signalDate&&!!breadth?.tradeDate&&signalDate<breadth.tradeDate;
  const positiveModels=models.filter(x=>x.totalReturn>0).length;
  if(loading&&!data)return <PageContainer title="总览"><Skeleton active paragraph={{rows:12}}/></PageContainer>;
  return <PageContainer className="dashboard-page" title="总览" subTitle="市场、策略与数据运行态势" extra={<Button icon={<ReloadOutlined/>} loading={refreshing} onClick={()=>void refresh()}>刷新行情</Button>}>
    {!data?<Alert type="error" showIcon title="总览数据不可用"/>:<>
      <section className="market-overview-band"><div className="dashboard-section-head"><div><h2>市场概览</h2><span>Promax · 截至 {timeText(quote?.receivedAt)}</span></div><Badge status={freshness.status} text={freshness.text}/></div>
        <div className="market-index-strip">{snapshots.map(item=><button type="button" className={`market-index-cell ${selected===item.code?"selected":""}`} key={item.code} onClick={()=>setSelected(item.code)}><span>{item.name.trim()}</span><strong>{fmt(item.close)}</strong><em className={direction(item.pctChg)}>{pct(item.pctChg)}</em><MiniLine values={[item.open??item.close,item.low??item.close,item.high??item.close,item.close]} tone={direction(item.pctChg)==="down"?"#07966b":direction(item.pctChg)==="up"?"#e5484d":"#8a98aa"}/></button>)}</div>
      </section>
      <div className="data-context-strip"><span><b>盘中行情</b>{timeText(quote?.receivedAt)} · 调度{scheduler?.status==="running"?"运行中":"不可用"}</span><span><b>上一交易日复盘</b>{dateText(breadth?.tradeDate)}</span><span className={signalStale?"context-warning":""}><b>策略信号</b>{dateText(signalDate)}{signalStale?" · 落后最近交易日":""}</span></div>
      {etf?<section className="etf-signal-band" style={{display:"flex",alignItems:"center",gap:14,flexWrap:"wrap",padding:"10px 14px",margin:"8px 0 14px",borderRadius:10,border:`1px solid ${etf.holdings?.bearRegime?"#f0dfb8":"#bce8dc"}`,background:etf.holdings?.bearRegime?"#fdf6e7":"#f0faf6"}}>
        <span style={{fontSize:22,lineHeight:1}}>{etf.holdings?.bearRegime?<SafetyCertificateOutlined style={{color:"#d9a13b"}}/>:<LineChartOutlined style={{color:"#0baa81"}}/>}</span>
        <span style={{fontSize:13,fontWeight:700}}>ETF 智能投研：{etf.holdings?.bearRegime?"空仓防御中":"持仓中"} <span style={{fontSize:11,fontWeight:400,color:"#8a94a0"}}>因子日 {dateText(etf.holdings?.tradeDate)} · {etf.windows} 窗口 · label {etf.label}</span></span>
        <span style={{fontSize:13}}>总收益 <b style={{color:"#0baa81"}}>{pct(etf.metrics?.totalReturn?etf.metrics.totalReturn*100:0)}</b></span>
        <span style={{fontSize:13}}>超额 <b style={{color:"#0baa81"}}>{pct(etf.metrics?.excessReturn?etf.metrics.excessReturn*100:0)}</b></span>
        <span style={{fontSize:13}}>Sharpe <b style={{color:"#1a232c"}}>{fmt(etf.metrics?.sharpe)}</b></span>
        <span style={{fontSize:13}}>MDD <b style={{color:"#e5524f"}}>{pct(etf.metrics?.maxDrawdown?etf.metrics.maxDrawdown*100:0)}</b></span>
        <span style={{marginLeft:"auto"}}><Button type="link" size="small" onClick={()=>onNavigate("/etf")}>进入 ETF 量化 →</Button></span>
      </section>:null}
      <div className="dashboard-main-grid"><div className="dashboard-primary">
        <ProCard className="dashboard-panel market-chart-panel" title={data.minuteSeries.length>=30?"指数分时走势":"行情采样走势"} extra={<Segmented size="small" value={selected} onChange={v=>setSelected(String(v))} options={snapshots.map(x=>({label:x.name.trim(),value:x.code}))}/>}><div className="market-quote-line"><strong className={direction(quote?.pctChg)}>{fmt(quote?.close)}</strong><span className={direction(quote?.pctChg)}>{quote?.change==null?"—":`${quote.change>=0?"+":""}${fmt(quote.change)}`}　{pct(quote?.pctChg)}</span><small>今开 {fmt(quote?.open)}　最高 {fmt(quote?.high)}　最低 {fmt(quote?.low)}　成交额 {compactAmount(quote?.amount)}</small></div><div className="chart-toolbar"><span>{data.minuteSeries.length} 个实际数据点{data.minuteSeries.length<30?" · 数据间隔过大时自动断线":" · 当日完整分钟序列"}</span><Segmented size="small" value={frequency} onChange={v=>setFrequency(String(v))} options={["1m","5m","15m","30m","60m"]}/></div><IntradayChart rows={data.minuteSeries} quote={quote} intervalMinutes={Number(frequency.slice(0,-1))}/></ProCard>
        <div className="dashboard-lower-grid"><ProCard className="dashboard-panel" title="T+1 候选" extra={<Button type="link" onClick={()=>onNavigate("/selection")}>查看全部</Button>}><div className="selection-list">{data.selection.items.map(item=><div key={item.code}><b>{item.rank}</b><span><strong>{item.name}</strong><small>{item.code} · {item.industry||"未分类"}</small></span><em>{fmt(item.score,1)}</em></div>)}{!data.selection.items.length?<Typography.Text type="secondary">暂无候选结果</Typography.Text>:null}</div><div className={`panel-asof ${signalStale?"warning":""}`}>信号日 {dateText(signalDate)}{signalStale?" · 尚未生成上一交易日信号":""}</div></ProCard>
          <ProCard className="dashboard-panel" title="策略回测摘要" extra={<Button type="link" onClick={()=>onNavigate("/backtest")}>进入回测</Button>}>{best?<><div className="strategy-disclosure">{dateText(data.backtest.run?.summary?.firstSignalDate)} 至 {dateText(data.backtest.run?.summary?.lastExitDate)} · {positiveModels}/{models.length} 个模型正收益 · 含交易成本</div><div className="strategy-lead"><span>历史区间表现最佳 · {best.label}</span><strong className={direction(best.totalReturn)}>{pct(best.totalReturn*100)}</strong><small>累计收益</small></div><div className="strategy-metrics"><span>同期基准<b>{pct(best.benchmarkTotalReturn*100)}</b></span><span>最大回撤<b className="down">{pct(best.maxDrawdown*100)}</b></span><span>夏普<b>{fmt(best.sharpe)}</b></span><span>超额<b>{pct(best.excessReturn*100)}</b></span></div></>:<Typography.Text type="secondary">暂无已完成回测</Typography.Text>}</ProCard></div>
      </div><aside className="dashboard-rail"><ProCard className="dashboard-panel breadth-panel" title="市场广度" extra={<span>截至 {dateText(breadth?.tradeDate)}</span>}><div className="breadth-row"><span>上涨<strong className="up">{breadth?.up??0}</strong></span><span>下跌<strong className="down">{breadth?.down??0}</strong></span><span>平盘<strong>{breadth?.flat??0}</strong></span></div><div className="breadth-bar"><i style={{width:`${(breadth?.up??0)/total*100}%`}}/><i className="flat" style={{width:`${(breadth?.flat??0)/total*100}%`}}/><i className="down" style={{width:`${(breadth?.down??0)/total*100}%`}}/></div><div className="limit-row"><span>涨停<b className="up">{data.limits.up}</b></span><span>跌停<b className="down">{data.limits.down}</b></span><span>炸板<b>{data.limits.broken}</b></span></div></ProCard>
        <ProCard className="dashboard-panel" title="上一交易日成交" extra={<span>{dateText(data.exchange[0]?.tradeDate)}</span>}><div className="exchange-list">{data.exchange.map(row=><div key={`${row.exchange}-${row.marketCode}`}><span>{row.exchange==="SH"?"上交所股票市场":"深交所股票市场"}</span><strong>{fmt(row.amount,1)} 亿</strong><small>{sourceName(row.source)} · 交易所统计口径</small></div>)}{!data.exchange.length?<Typography.Text type="secondary">暂无市场统计</Typography.Text>:null}</div></ProCard>
        <ProCard className="dashboard-panel" title="数据与任务" extra={<Button type="link" onClick={()=>onNavigate("/data")}>数据中心</Button>}><div className="health-summary"><span><CheckCircleOutlined/>目录状态为 ready</span><strong>{ready}/{production.length}</strong></div><div className="health-disclosure">仅表示数据集已登记，不等同于通过时效、完整性和质量SLA。</div><div className="task-list">{data.tasks.slice(0,4).map(task=><Tooltip title={task.error??task.id} key={task.id}><div><Badge status={task.status==="complete"?"success":task.status==="error"?"error":"processing"}/><span>{task.toolId??task.kind}</span><em>{task.status==="complete"?`${task.written} 条`:task.status==="error"?"失败":"运行中"}</em></div></Tooltip>)}</div></ProCard>
      </aside></div>
    </>}</PageContainer>;
}

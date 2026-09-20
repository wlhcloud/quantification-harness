# -*- coding: utf-8 -*-
"""每日股票链：① 补齐缺失交易日行情 → ② 自动推进股票 T+1 模拟盘 → ③ ETF / 股票 ML / V5 各子链。

修复链条断点：9101 的 MarketScheduler 只同步指数分钟/实时，不负责股票日线；
本脚本在 Windows 计划任务 stock-factor-daily（工作日 17:30）里完成股票侧完整链路：
  1) 读 market.db 已同步日线最大值，结合 trade_calendar 判断交易日，从缺口逐日调用
     9101 /sync/daily 补行情（收盘后 tushare 已出当日数据）；
  2) 若存在 active 股票模拟盘 run，POST advance 按 T+1 开盘自动调仓（幂等：无新信号
     或 T+1 行情未到位则 waiting，不改变任何状态）；
  3) ETF 链、股票 ML 链（辅助因子 → ML 因子 → walkforward）、V5 移动止损模拟盘。

历史说明：原第 ② 步会提交 factor_mining 重建 factor_snapshots 与 legacy 因子模型候选；
legacy 因子选股已于 2026-09-18 退役（config/factor-models.yaml 只剩 ml_walkforward），
该步骤已移除，factor_mining 任务保留供手工重建历史快照。

也可手动运行：D:\\ProdApp\\Python\\Python313\\python.exe scripts\\daily_stock_chain.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SYNC = "http://127.0.0.1:9101/api/v1"
ENGINE = "http://127.0.0.1:9102/api/v1"
ROOT = Path(__file__).resolve().parents[1]
MARKET_DB = ROOT / "data" / "market.db"
ETF_DB = ROOT / "data" / "etf-quant.db"
TIMEOUT_S = 60 * 30  # 全市场因子重建最长 30 分钟
SYNC_DAY_TIMEOUT_S = 280  # 单日全市场同步最长约 4.5 分钟


def _load_env_file() -> None:
    """从 .quant.env 读取缺失的 QUANT_* 变量。

    本脚本由 Windows 计划任务直接以 python.exe 启动，环境里通常没有这些变量；
    启用写接口鉴权后必须带上 X-API-Key（权威变量 QUANT_API_KEY）。
    已存在的环境变量优先，不覆盖。
    """
    env_file = ROOT / ".quant.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def _api_key() -> str:
    """两个服务共用同一管理 key（前端也只有一个 VITE_QUANT_API_KEY）。

    权威变量是 QUANT_API_KEY；QUANT_ENGINE_API_KEY / QUANT_SYNC_API_KEY 是 key 收敛前的
    旧名字，仍作回退（旧部署的 .quant.env 只写了旧变量时不会失效）。
    """
    for name in ("QUANT_API_KEY", "QUANT_ENGINE_API_KEY", "QUANT_SYNC_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _request(method: str, url: str, payload: dict | None = None, timeout: int = 30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _missing_trade_days() -> list[str]:
    """返回 [max(daily_bars)+1, 今天] 之间、trade_calendar 标记为 SSE 交易日的缺口日期。
    今天仅在 16:00 之后纳入（tushare 收盘后出当日数据，避免盘中空转）；错过/关机多天时
    开机补跑会一次性列出所有历史缺口逐日补齐。"""
    con = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()
        if not row or not row[0]:
            return []
        start = row[0]
        today = datetime.now().strftime("%Y%m%d")
        if start >= today:
            return []
        include_today = datetime.now().hour >= 16
        if not include_today:
            # 今天未到收盘出数时间：只看昨天及以前
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            if start >= yesterday:
                return []
            days = [r[0] for r in con.execute(
                "SELECT calendar_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 "
                "AND calendar_date>? AND calendar_date<=? ORDER BY calendar_date", (start, yesterday))]
            return days
        days = [r[0] for r in con.execute(
            "SELECT calendar_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 "
            "AND calendar_date>? AND calendar_date<=? ORDER BY calendar_date", (start, today))]
        return days
    finally:
        con.close()


def _sync_missing(days: list[str]) -> None:
    """逐日补行情。单日失败仅告警不中断：收盘前当日数据未出属正常，
    因子重建会基于已同步的最新交易日进行，失败日由下次运行自动重试。"""
    for day in days:
        print(f"  ... 同步行情 {day}", flush=True)
        try:
            r = _request("POST", f"{SYNC}/sync/daily?trade_date={day}", timeout=SYNC_DAY_TIMEOUT_S)
            q = r.get("quality")
            print(f"  ... {day} quality={q} bars={r.get('counts', {}).get('bars')}", flush=True)
            if q != "passed":
                print(f"[WARN] {day} 复权因子覆盖不足（{q}），继续但可能影响因子", flush=True)
        except urllib.error.URLError as exc:
            print(f"[WARN] 同步 {day} 失败：{exc}（下次运行自动重试）", flush=True)


def _advance_stock_sim(max_steps: int = 20) -> None:
    """若存在 active 股票模拟盘 run，循环推进直到无新信号（幂等）。

    关机多天时，advance 按信号日逐期推进，每期需要该信号日的 T+1 行情已入库；
    开机补完行情后循环推进可一次追平全部积压调仓，最多 max_steps 期。

    目标 run 的选择：优先 **ml_walkforward**（前端 T+1 选股页/模拟盘页默认展示的那个盘），
    否则退回最新 run。原实现只推"最新 run"，而技术面择时盘（有自己的推进步骤）往往是新建的，
    会把最新位置占掉，导致 ML 盘的信号长期停在旧日期。
    """
    try:
        listing = _request("GET", f"{ENGINE}/stock/sim/runs")
    except urllib.error.URLError as exc:
        print(f"[WARN] 读取股票模拟盘列表失败：{exc}（跳过自动推进）", flush=True)
        return
    runs = [r for r in (listing.get("items") or []) if r.get("status") == "active"]
    if not runs:
        print("[INFO] 无 active 股票模拟盘 run，跳过自动推进", flush=True)
        return
    target = next((r for r in reversed(runs) if r.get("modelId") == "ml_walkforward"), runs[-1])
    rid = target["runId"]
    for step in range(1, max_steps + 1):
        try:
            a = _request("POST", f"{ENGINE}/stock/sim/advance?run_id={rid}")
        except urllib.error.URLError as exc:
            print(f"[WARN] 模拟盘推进请求失败：{exc}", flush=True)
            return
        if a.get("ok"):
            print(f"[OK] 模拟盘 {rid[-12:]}（{target.get('modelId')}）推进第{step}期 T+1 {a.get('tradeDate')}："
                  f"卖 {len(a['trades']['sold'])} 买 {len(a['trades']['bought'])} "
                  f"持 {len(a['positions'])} 只，nav={a.get('nav')}", flush=True)
            continue
        if step == 1:
            print(f"[INFO] 模拟盘 {rid[-12:]}（{target.get('modelId')}）暂不可推进：{a.get('message')}", flush=True)
        else:
            print(f"[INFO] 已追平 {step - 1} 期，剩余：{a.get('message')}", flush=True)
        return


# ---------------------------------------------------------------- ETF 自动链

def _table_missing_days(table: str) -> list[str]:
    """任意行情表相对 trade_calendar 的缺口交易日（16:00 后含当日）。"""
    con = sqlite3.connect(f"file:{MARKET_DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
        if not row or not row[0]:
            return []
        start = row[0]
        today = datetime.now().strftime("%Y%m%d")
        if start >= today:
            return []
        if datetime.now().hour < 16:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            if start >= yesterday:
                return []
            return [r[0] for r in con.execute(
                "SELECT calendar_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 "
                "AND calendar_date>? AND calendar_date<=? ORDER BY calendar_date", (start, yesterday))]
        return [r[0] for r in con.execute(
            "SELECT calendar_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 "
            "AND calendar_date>? AND calendar_date<=? ORDER BY calendar_date", (start, today))]
    finally:
        con.close()


def _sync_table_missing(table: str, endpoint: str, days: list[str]) -> None:
    for day in days:
        print(f"  ... 同步 {table} {day}", flush=True)
        try:
            r = _request("POST", f"{SYNC}/{endpoint}?trade_date={day}", timeout=SYNC_DAY_TIMEOUT_S)
            print(f"  ... {day} written={r.get('written')} status={r.get('status')}", flush=True)
        except urllib.error.URLError as exc:
            print(f"[WARN] 同步 {table} {day} 失败：{exc}（下次运行自动重试）", flush=True)


def _submit_engine_job(job_type: str, timeout_s: int = TIMEOUT_S, parameters: dict | None = None) -> bool:
    """提交 9102 job 并等待完成（幂等）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        job = _request("POST", f"{ENGINE}/jobs", {"type": job_type, "parameters": parameters or {}})
    except urllib.error.URLError as exc:
        print(f"[FATAL] 无法连接 quant-engine(9102)：{exc}", flush=True)
        return False
    job_id = job["id"]
    print(f"[{stamp}] {job_type} job {job_id} ...", flush=True)
    deadline = time.time() + timeout_s
    last_progress = -1
    while time.time() < deadline:
        time.sleep(5)
        cur = _request("GET", f"{ENGINE}/jobs/{job_id}")
        status = cur.get("status")
        progress = int(cur.get("progress") or 0)
        if progress != last_progress:
            print(f"  ... {status} {progress}%", flush=True)
            last_progress = progress
        if status in ("complete", "error", "cancelled"):
            if status == "complete":
                result = cur.get("result") or {}
                print(f"[OK] {job_type} complete: {json.dumps(result, ensure_ascii=False)[:500]}", flush=True)
                return True
            print(f"[FAIL] {job_type} {status}: {cur.get('error')}", flush=True)
            return False
    print(f"[FAIL] {job_type} timeout {timeout_s}s", flush=True)
    return False


def _etf_walkforward_stale() -> bool:
    """ETF walkforward 最新持仓信号日是否落后于 ETF 行情最新交易日（无信号视为需重训）。"""
    con = sqlite3.connect(f"file:{MARKET_DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        mkt_max = con.execute("SELECT MAX(trade_date) FROM etf_daily_bars").fetchone()[0]
    finally:
        con.close()
    if not mkt_max:
        return False
    ef = sqlite3.connect(f"file:{ETF_DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        row = ef.execute(
            "SELECT holdings FROM etf_walkforward_runs WHERE status='complete' "
            "ORDER BY generated_at DESC LIMIT 1").fetchone()
    finally:
        ef.close()
    if not row:
        print("[INFO] 无历史 ETF walkforward，将全量重训", flush=True)
        return True
    try:
        hold_date = json.loads(row[0] or "{}").get("tradeDate")
    except Exception:
        hold_date = None
    stale = not hold_date or mkt_max > hold_date
    print(f"[INFO] ETF 行情最新 {mkt_max} / walkforward 信号最新 {hold_date} -> "
          f"{'需重训' if stale else '无需重训'}", flush=True)
    return stale


def _advance_etf_sim(max_steps: int = 10) -> None:
    """推进所有 active ETF 模拟盘 run（每个 run 循环直到无新信号）。"""
    ef = sqlite3.connect(f"file:{ETF_DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        runs = [r[0] for r in ef.execute(
            "SELECT run_id FROM etf_sim_runs WHERE status='active' ORDER BY created_at")]
    finally:
        ef.close()
    if not runs:
        print("[INFO] 无 active ETF 模拟盘 run，跳过自动推进", flush=True)
        return
    for rid in runs:
        for step in range(1, max_steps + 1):
            try:
                a = _request("POST", f"{ENGINE}/etf-quant/sim/advance?run_id={rid}")
            except urllib.error.URLError as exc:
                print(f"[WARN] ETF 模拟盘推进请求失败：{exc}", flush=True)
                break
            if a.get("ok"):
                print(f"[OK] ETF 模拟盘 {rid[-12:]} 推进第{step}期：持 {len(a['positions'])} 只，"
                      f"nav={a.get('nav')}", flush=True)
                continue
            print(f"[INFO] ETF 模拟盘 {rid[-12:]} 暂不可推进：{a.get('message')}", flush=True)
            break


def _etf_chain() -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ETF 自动链：④ETF行情 ⑤指数行情 ⑥因子 ⑦walkforward ⑧模拟盘", flush=True)
    etf_days = _table_missing_days("etf_daily_bars")
    if etf_days:
        print(f"[INFO] ETF 缺失 {len(etf_days)} 个交易日行情：{etf_days}", flush=True)
        _sync_table_missing("etf_daily_bars", "sync/etf/daily", etf_days)
    else:
        print("[INFO] ETF 行情已是最新，无需同步", flush=True)
    idx_days = _table_missing_days("index_daily_bars")
    if idx_days:
        print(f"[INFO] 指数缺失 {len(idx_days)} 个交易日行情：{idx_days}", flush=True)
        _sync_table_missing("index_daily_bars", "sync/etf/index-daily", idx_days)
    else:
        print("[INFO] 指数行情已是最新，无需同步", flush=True)
    if _etf_walkforward_stale():
        if not _submit_engine_job("etf_factors", timeout_s=20 * 60):
            return
        if not _submit_engine_job("etf_walkforward", timeout_s=60 * 60):
            return
    _advance_etf_sim()


def _aux_factor_stale() -> list[tuple[str, str]]:
    """辅助因子表（行业/资金流）是否需要重建：返回 [(job_type, 表名)]。

    这两张表原先只由 scripts/ 下的脚本在服务外手工跑，实测曾落后信号日 2 天，
    而 walkforward 会 LEFT JOIN 它们 → 特征静默缺失。现在纳入每日链。
    """
    factors_db = MARKET_DB.parent / "factors.db"
    mkt = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True, timeout=10)
    try:
        mkt_max = mkt.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()[0]
    finally:
        mkt.close()
    pairs = (("stock_industry_factors", "stock_industry_factors"),
             ("stock_money_flow_factors", "stock_money_flow_factors"))
    stale: list[tuple[str, str]] = []
    fac = sqlite3.connect(f"file:{factors_db}?mode=ro", uri=True, timeout=10)
    try:
        for job_type, table in pairs:
            exists = fac.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            latest = fac.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()[0] if exists else None
            if not latest:
                print(f"[INFO] {table} 为空/不存在，需要重建", flush=True)
                stale.append((job_type, table))
            elif mkt_max and latest < mkt_max:
                print(f"[INFO] {table} {latest} < 行情 {mkt_max}，需要重建", flush=True)
                stale.append((job_type, table))
            else:
                print(f"[INFO] {table} 已最新（{latest}），跳过", flush=True)
    finally:
        fac.close()
    return stale


def _stock_ml_chain() -> None:
    """股票机器学习链：①辅助因子(行业/资金流) ②ML因子(增量) ③walkforward(最近6窗口)
    —— 信号自动写入 selection_candidates(ml_walkforward)，模拟盘/回测页直接可见。"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 股票ML自动链：①辅助因子 ②ML因子 ③walkforward", flush=True)
    for job_type, table in _aux_factor_stale():
        if not _submit_engine_job(job_type, timeout_s=30 * 60):
            print(f"[WARN] {table} 重建失败，继续后续步骤（walkforward 会告警特征过期）", flush=True)
    fac = sqlite3.connect(f"file:{MARKET_DB.parent / 'factors.db'}?mode=ro", uri=True, timeout=10)
    try:
        has_table = fac.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_ml_factors'").fetchone()
        ml_max = fac.execute("SELECT MAX(trade_date) FROM stock_ml_factors").fetchone()[0] if has_table else None
    finally:
        fac.close()
    mkt_max = None
    mkt = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True, timeout=10)
    try:
        mkt_max = mkt.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()[0]
    finally:
        mkt.close()
    if not ml_max or (mkt_max and mkt_max > ml_max):
        print(f"[INFO] ML因子 {ml_max or '空'} < 行情 {mkt_max}，增量重建", flush=True)
        if not _submit_engine_job("stock_ml_factors", parameters={"incremental": True}, timeout_s=20 * 60):
            return
    else:
        print(f"[INFO] ML因子已最新（{ml_max}），跳过", flush=True)
    if _stock_walkforward_stale():
        if not _submit_engine_job("stock_walkforward", parameters={"maxWindows": 6}, timeout_s=60 * 60):
            return
    _advance_stock_sim()


def _stock_walkforward_stale() -> bool:
    """walkforward 信号日 < ML因子最新日 → 需重训（训练耗时长，每日链只跑最近6窗口）。"""
    fac = sqlite3.connect(f"file:{MARKET_DB.parent / 'factors.db'}?mode=ro", uri=True, timeout=10)
    try:
        ml_max = fac.execute("SELECT MAX(trade_date) FROM stock_ml_factors").fetchone()[0]
        row = fac.execute(
            "SELECT holdings FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
        sig = None
        if row:
            sig = json.loads(row[0]).get("tradeDate")
    finally:
        fac.close()
    stale = bool(ml_max) and sig != ml_max
    print(f"[INFO] ML因子最新 {ml_max} / walkforward信号 {sig} -> "
          f"{'需重训' if stale else '无需重训'}", flush=True)
    return stale


def _advance_technical_timing_sim() -> None:
    """推进技术面择时模拟盘（ml_technical_timing）：金叉买入/死叉或5%止损卖出。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from technical_timing_sim import advance_technical_timing, get_status
        status = get_status()
        if not status.get("run"):
            print("[INFO] 技术面择时模拟盘尚未创建，首次运行将自动创建", flush=True)
        result = advance_technical_timing()
        if result.get("ok"):
            print(f"[OK] 技术面择时模拟盘推进：信号{result.get('signalDate')} T+1 {result.get('tradeDate')} "
                  f"卖{len(result['trades']['sold'])} 买{len(result['trades']['bought'])} "
                  f"持{len(result['positions'])}只 nav={result.get('nav')}", flush=True)
            if result["trades"]["sold"]:
                reasons = {}
                for s in result["trades"]["sold"]:
                    reasons[s.get("reason", "unknown")] = reasons.get(s.get("reason", "unknown"), 0) + 1
                print(f"     卖出原因: {reasons}", flush=True)
        elif result.get("waiting"):
            print(f"[INFO] 技术面择时模拟盘暂不可推进：{result.get('message')}", flush=True)
        else:
            print(f"[WARN] 技术面择时模拟盘推进失败：{result.get('error')}", flush=True)
    except Exception as exc:
        print(f"[WARN] 技术面择时模拟盘推进异常：{exc}（跳过，不影响主链）", flush=True)


def _advance_technical_timing_v5_sim() -> None:
    """推进V5移动止损模拟盘（ml_technical_timing_v5）：金叉买入/死叉或移动止损卖出。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from technical_timing_sim_v5 import advance_technical_timing_v5, get_status
        status = get_status()
        if not status.get("run"):
            print("[INFO] V5移动止损模拟盘尚未创建，首次运行将自动创建", flush=True)
        result = advance_technical_timing_v5()
        if result.get("ok"):
            print(f"[OK] V5移动止损模拟盘推进：信号{result.get('signalDate')} T+1 {result.get('tradeDate')} "
                  f"卖{len(result['trades']['sold'])} 买{len(result['trades']['bought'])} "
                  f"持{len(result['positions'])}只 nav={result.get('nav')} "
                  f"总收益={result.get('totalReturn', 0)*100:.2f}%", flush=True)
            if result["trades"]["sold"]:
                reasons = {}
                for s in result["trades"]["sold"]:
                    reasons[s.get("reason", "unknown")] = reasons.get(s.get("reason", "unknown"), 0) + 1
                print(f"     卖出原因: {reasons}", flush=True)
            if result["positions"]:
                pos_info = [(p["code"], "止损" + str(p["stopPrice"])) for p in result["positions"]]
                print(f"     当前持仓: {pos_info}", flush=True)
        elif result.get("waiting"):
            print(f"[INFO] V5移动止损模拟盘暂不可推进：{result.get('message')}", flush=True)
        else:
            print(f"[WARN] V5移动止损模拟盘推进失败：{result.get('error')}", flush=True)
    except Exception as exc:
        print(f"[WARN] V5移动止损模拟盘推进异常：{exc}（跳过，不影响主链）", flush=True)


LOCK_PATH = ROOT / "data" / ".daily-chain.lock"
RUN_LEDGER = ROOT / "data" / "daily-chain-runs.jsonl"
STALE_LOCK_HOURS = 6.0


def _acquire_chain_lock() -> bool:
    """单实例锁：防止 30 分钟轮询 / 手工调用 / 豆包任务并发叠加执行同一条链。

    任务设置本身是 IgnoreNew，但手工或别的调度器仍可能并发，这里再加一道。
    锁文件里写 pid 与开始时间；超过 STALE_LOCK_HOURS 视为残留（上次被强杀）并接管。
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(payload.get("startedAt", ""))
        except Exception:  # noqa: BLE001
            started = None
        if started and (datetime.now() - started).total_seconds() < STALE_LOCK_HOURS * 3600:
            print(f"[SKIP] 已有每日链在运行（pid={payload.get('pid')} 起于 {payload.get('startedAt')}），本次跳过",
                  flush=True)
            return False
        print(f"[WARN] 发现残留锁（{started}），接管执行", flush=True)
        LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "startedAt": datetime.now().isoformat(timespec="seconds")}, fh)
    return True


def _release_chain_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _append_run_ledger(started: datetime, exit_code: int) -> None:
    """把每次运行写进 JSONL 台账（谁跑的、跑多久、结果），便于事后核对是否漏跑/叠加。"""
    try:
        with RUN_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "startedAt": started.isoformat(timespec="seconds"),
                "finishedAt": datetime.now().isoformat(timespec="seconds"),
                "durationSec": round((datetime.now() - started).total_seconds(), 1),
                "pid": os.getpid(),
                "exitCode": exit_code,
            }, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[WARN] 写运行台账失败：{exc}", flush=True)


def main() -> int:
    _load_env_file()  # 计划任务环境里没有 QUANT_*_API_KEY，先补上再调用带鉴权的写接口
    if not _acquire_chain_lock():
        return 0
    started = datetime.now()
    exit_code = 0
    try:
        exit_code = _run_chain()
        return exit_code
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        print(f"[ERROR] daily-stock-chain 异常终止：{exc!r}", flush=True)
        raise
    finally:
        _release_chain_lock()
        _append_run_ledger(started, exit_code)


def _run_chain() -> int:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] daily-stock-chain: "
          f"①行情 ②股票模拟盘 ③ETF行情 ④ETF因子 ⑤walkforward ⑥ETF模拟盘 "
          f"⑦辅助因子(行业/资金流) ⑧股票ML因子 ⑨股票ML walkforward ⑩V5移动止损模拟盘", flush=True)
    days = _missing_trade_days()
    if days:
        print(f"[INFO] 缺失 {len(days)} 个交易日行情：{days}", flush=True)
        _sync_missing(days)
    else:
        print("[INFO] 行情已是最新，无需同步", flush=True)
    # 原「②因子」= 提交 factor_mining 重建 factor_snapshots + legacy 因子模型候选。
    # legacy 因子选股已于 2026-09-18 退役（config/factor-models.yaml 只剩 ml_walkforward），
    # 因此这里不再调度；factor_mining 任务本身保留，需要手工重建历史快照时仍可提交。
    _advance_stock_sim()
    _etf_chain()
    _stock_ml_chain()
    # V1技术面择时模拟盘已停用（2026-09-14）：统一使用V5移动止损版
    # V1为固定5%止损无移动止损，保留函数定义以备后用，但不再每日推进
    # _advance_technical_timing_sim()
    _advance_technical_timing_v5_sim()
    print("[DONE] daily-stock-chain 完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

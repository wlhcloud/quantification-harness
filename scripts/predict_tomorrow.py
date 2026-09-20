"""查询模拟盘当前状态 + 今天候选股的技术面信号，预测明天操作"""
import sqlite3
import sys
from pathlib import Path

PROJECT = Path("D:/ProdProject/AI/quantification-harness")
sys.path.insert(0, str(PROJECT / "scripts"))
from technical_timing_sim_v5 import (
    check_golden_cross, check_death_cross, check_vol_breakout,
    compute_ma, calc_stop_loss_price, get_candidates,
    FACTORS_DB, MARKET_DB, MODEL_ID, MAX_HOLDINGS
)

con = sqlite3.connect(FACTORS_DB)
con.row_factory = sqlite3.Row
mkt = sqlite3.connect(f"file:{MARKET_DB.resolve().as_posix()}?mode=ro", uri=True)
mkt.row_factory = sqlite3.Row

# 获取最新交易日
latest = mkt.execute("SELECT MAX(trade_date) d FROM daily_bars").fetchone()["d"]
print(f"最新交易日: {latest}")
print(f"当前系统日期: 2026-09-14 (周一)")
print(f"预测目标: 明天 2026-09-15 (周二)")
print("=" * 70)

# ========== 1. 当前持仓状态 ==========
run_id = con.execute("SELECT run_id FROM stock_sim_runs WHERE model_id=? AND status='active'", (MODEL_ID,)).fetchone()["run_id"]
positions = con.execute("SELECT * FROM stock_sim_positions WHERE run_id=?", (run_id,)).fetchall()
cash = con.execute("SELECT cash FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()["cash"]
nav = con.execute("SELECT nav FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()["nav"]

print(f"\n【当前持仓】净值={nav:.4f} (+{nav-1:.2%}), 现金={cash:,.0f}")
print(f"{'代码':<12} {'持股':>6} {'成本':>8} {'现价':>8} {'最高':>8} {'止损线':>8} {'盈亏':>8} {'距止损':>8}")
print("-" * 80)

at_risk = []  # 接近止损的股票
for p in positions:
    code = p["code"]
    bar = mkt.execute("SELECT * FROM daily_bars WHERE code=? AND trade_date=?", (code, latest)).fetchone()
    if not bar:
        continue
    cur = bar["close"]
    buy_price = p["avg_cost"]
    high = p["high_price"]
    stop = calc_stop_loss_price(buy_price, high)
    pnl = (cur - buy_price) / buy_price
    dist_to_stop = (cur - stop) / cur
    print(f"{code:<12} {p['shares']:>6} {buy_price:>8.2f} {cur:>8.2f} {high:>8.2f} {stop:>8.2f} {pnl:>7.2%} {dist_to_stop:>7.2%}")
    if dist_to_stop < 0.03:
        at_risk.append((code, dist_to_stop))

# ========== 2. 今天候选池的买入信号 ==========
print(f"\n【今日候选池买入信号】({latest})")
candidates = get_candidates(con, latest)
print(f"候选股: {len(candidates)}只 (过滤后)")

buy_signals = []
for code in candidates:
    bar = mkt.execute("SELECT * FROM daily_bars WHERE code=? AND trade_date=?", (code, latest)).fetchone()
    if not bar:
        continue
    golden = check_golden_cross(mkt, code, latest)
    ma20 = compute_ma(mkt, code, latest, 20)
    golden_valid = golden and ma20 and bar["close"] > ma20
    vb = check_vol_breakout(mkt, code, latest)

    if golden_valid or vb:
        sig = []
        if golden_valid:
            sig.append("金叉")
        if vb:
            sig.append("放量突破")
        buy_signals.append((code, bar["close"], "+".join(sig)))

if buy_signals:
    print(f"触发买入信号: {len(buy_signals)}只")
    print(f"{'代码':<12} {'收盘价':>8} {'信号':<12} {'当前持仓':>8}")
    for code, price, sig in buy_signals:
        held = "已持有" if any(p["code"] == code for p in positions) else "未持有"
        print(f"{code:<12} {price:>8.2f} {sig:<12} {held:>8}")
else:
    print("今日无买入信号")

# ========== 3. 明天操作预测 ==========
print(f"\n{'='*70}")
print("【明天操作预测】")
print("=" * 70)

# 明天可能卖出
if at_risk:
    print(f"\n⚠️ 可能止损卖出 ({len(at_risk)}只接近止损线):")
    for code, dist in at_risk:
        print(f"  {code}: 距止损线仅 {dist:.2%}，明天若低开或下跌可能触发止损")
else:
    print("\n✅ 无持仓接近止损线")

# 明天可能买入
available_slots = MAX_HOLDINGS - len(positions)
new_buy = [b for b in buy_signals if not any(p["code"] == b[0] for p in positions)]
if new_buy and available_slots > 0:
    print(f"\n📈 可能买入 ({len(new_buy)}只新信号, {available_slots}个空位):")
    for code, price, sig in new_buy[:available_slots]:
        print(f"  {code}: {sig}, 收盘价{price:.2f}")
elif new_buy and available_slots == 0:
    print(f"\n📊 有{len(new_buy)}只新买入信号但持仓已满({MAX_HOLDINGS}只)，需先卖出才能买入")
    print("  候选:", ", ".join(b[0] for b in new_buy))
else:
    print("\n📉 无新买入信号")

# 明天持仓预测
print(f"\n【预测明天收盘后持仓】")
print(f"  若不触发止损: 维持当前 {len(positions)} 只持仓")
if at_risk:
    print(f"  若触发止损: 可能减少 {len(at_risk)} 只，腾出空位给新信号")

con.close()
mkt.close()

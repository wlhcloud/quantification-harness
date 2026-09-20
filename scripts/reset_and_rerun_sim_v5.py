"""重置V5模拟盘并用尾盘口径从2024年初重新推进"""
import sqlite3
import sys
import time
from pathlib import Path

PROJECT = Path("D:/ProdProject/AI/quantification-harness")
FACTORS_DB = PROJECT / "data/factors.db"
MARKET_DB = PROJECT / "data/market.db"
MODEL_ID = "ml_technical_timing_v5"

# 添加scripts目录到路径
sys.path.insert(0, str(PROJECT / "scripts"))
from technical_timing_sim_v5 import advance_technical_timing_v5

t0 = time.time()

# ========== 1. 清空V5模拟盘数据 ==========
print("清空V5模拟盘数据...")
con = sqlite3.connect(FACTORS_DB)
run_ids = [r[0] for r in con.execute(
    "SELECT run_id FROM stock_sim_runs WHERE model_id=?", (MODEL_ID,)).fetchall()]
print(f"  找到V5 run_id: {run_ids}")
for rid in run_ids:
    con.execute("DELETE FROM stock_sim_daily WHERE run_id=?", (rid,))
    con.execute("DELETE FROM stock_sim_positions WHERE run_id=?", (rid,))
    con.execute("DELETE FROM stock_sim_runs WHERE run_id=?", (rid,))
    print(f"  已删除 run_id={rid}")
con.commit()
con.close()
print("  清空完成")

# ========== 3. 逐日推进模拟盘 ==========
print("\n开始用尾盘口径重新推进模拟盘...")
mkt = sqlite3.connect(f"file:{MARKET_DB.resolve().as_posix()}?mode=ro", uri=True)
dates = [r[0] for r in mkt.execute(
    "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date>='20240101' ORDER BY trade_date").fetchall()]
mkt.close()
print(f"  待推进交易日: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")

success = 0
waiting = 0
errors = 0
for i, d in enumerate(dates):
    try:
        result = advance_technical_timing_v5()
        if result.get("ok"):
            success += 1
            if success % 50 == 0:
                nav = result.get("nav", 0)
                print(f"  进度 {i+1}/{len(dates)}: {d} 净值={nav:.4f} 持仓={result.get('positions', []) and len(result['positions'])}只")
        elif result.get("waiting"):
            waiting += 1
        else:
            errors += 1
            if errors <= 5:
                print(f"  错误 {d}: {result.get('error', result.get('message', 'unknown'))}")
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f"  异常 {d}: {e}")

# ========== 4. 最终状态 ==========
print(f"\n推进完成: 成功{success}天, 等待{waiting}天, 错误{errors}天")
con = sqlite3.connect(FACTORS_DB)
final = con.execute(
    "SELECT signal_date, trade_date, nav, cash, market_value, position_count, note "
    "FROM stock_sim_daily WHERE run_id IN (SELECT run_id FROM stock_sim_runs WHERE model_id=?) "
    "ORDER BY rowid DESC LIMIT 1", (MODEL_ID,)).fetchone()
if final:
    print(f"\n最终状态:")
    print(f"  信号日: {final[0]}")
    print(f"  成交日: {final[1]}")
    print(f"  净值: {final[2]:.6f} (收益: {final[2]-1:.2%})")
    print(f"  现金: {final[3]:.0f}")
    print(f"  市值: {final[4]:.0f}")
    print(f"  持仓: {final[5]}只")
    print(f"  备注: {final[6]}")
con.close()

print(f"\n总耗时: {time.time()-t0:.1f}s")

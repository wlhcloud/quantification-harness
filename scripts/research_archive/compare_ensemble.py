"""多种子集成 vs 单种子基线对比
从 stock_walkforward_runs 表读取所有walkforward记录，按 ensembleSize 分组对比核心指标，
验证集成是否收敛了种子敏感性（收益分布、Sharpe、回撤、IC）。"""
import sqlite3, json, statistics
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(PROJECT / "data" / "factors.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT run_id, generated_at, windows, config, metrics FROM stock_walkforward_runs "
    "WHERE status='complete' ORDER BY generated_at"
).fetchall()
con.close()

singles = []   # 单种子记录
ensembles = []  # 集成记录
for r in rows:
    try:
        cfg = json.loads(r["config"])
        m = json.loads(r["metrics"])
    except Exception:
        continue
    if not m or r["windows"] is None or int(r["windows"]) < 20:
        continue  # 只看全量窗口(约24)的记录，排除快测
    ens_size = cfg.get("ensembleSize", 0) or 0
    seed = cfg.get("model", {}).get("randomState")
    rec = {
        "runId": r["run_id"], "at": r["generated_at"], "win": r["windows"],
        "seed": seed, "ensSize": ens_size, "seeds": cfg.get("ensembleSeeds"),
        "ret": m.get("totalReturn"), "sharpe": m.get("sharpe"),
        "mdd": m.get("maxDrawdown"), "ic": m.get("avgRankIc"),
        "excess": m.get("excessReturn"),
    }
    if ens_size > 1:
        ensembles.append(rec)
    else:
        singles.append(rec)

def line(r):
    return (f"  {r['runId']} | seed={r['seed']} | 窗口{r['win']} | "
            f"收益={r['ret']*100:7.2f}% | Sharpe={r['sharpe']:6.3f} | "
            f"回撤={r['mdd']*100:7.2f}% | IC={r['ic']:.4f}")

print("=" * 92)
print("一、单种子基线（全量窗口）")
print("=" * 92)
for r in singles:
    print(line(r))

if len(singles) >= 2:
    rets = [r["ret"] for r in singles]
    sharpes = [r["sharpe"] for r in singles]
    mdds = [r["mdd"] for r in singles]
    ics = [r["ic"] for r in singles]
    print("\n  单种子统计：")
    print(f"    总收益: 均值={statistics.mean(rets)*100:.2f}%  标准差={statistics.pstdev(rets)*100:.2f}pct  极差={(max(rets)-min(rets))*100:.2f}pct  区间=[{min(rets)*100:.2f}%, {max(rets)*100:.2f}%]")
    print(f"    Sharpe: 均值={statistics.mean(sharpes):.3f}  极差={max(sharpes)-min(sharpes):.3f}")
    print(f"    最大回撤: 均值={statistics.mean(mdds)*100:.2f}%  最差={min(mdds)*100:.2f}%")
    print(f"    avgRankIC: 均值={statistics.mean(ics):.4f}")

print("\n" + "=" * 92)
print("二、多种子集成（全量窗口）")
print("=" * 92)
if not ensembles:
    print("  （暂无集成训练完成的记录）")
for r in ensembles:
    print(line(r), f"| 集成种子={r['seeds']}")

print("\n" + "=" * 92)
print("三、集成 vs 单种子 对比结论")
print("=" * 92)
if ensembles and len(singles) >= 2:
    e = ensembles[-1]
    s_rets = [r["ret"] for r in singles]
    s_sharpes = [r["sharpe"] for r in singles]
    s_mdds = [r["mdd"] for r in singles]
    s_ics = [r["ic"] for r in singles]
    print(f"  集成收益 {e['ret']*100:.2f}%  vs  单种子均值 {statistics.mean(s_rets)*100:.2f}%（区间 {min(s_rets)*100:.2f}%~{max(s_rets)*100:.2f}%）")
    print(f"  集成Sharpe {e['sharpe']:.3f}  vs  单种子均值 {statistics.mean(s_sharpes):.3f}（区间 {min(s_sharpes):.3f}~{max(s_sharpes):.3f}）")
    print(f"  集成回撤 {e['mdd']*100:.2f}%  vs  单种子均值 {statistics.mean(s_mdds)*100:.2f}%（最差 {min(s_mdds)*100:.2f}%）")
    print(f"  集成IC {e['ic']:.4f}  vs  单种子均值 {statistics.mean(s_ics):.4f}")
    # 稳定性判断
    in_range = min(s_rets) <= e["ret"] <= max(s_rets)
    print(f"\n  集成收益落在单种子区间内: {'是（集成起到平滑/收敛作用）' if in_range else '否（超出区间，需检查）'}")
    better_sharpe = e["sharpe"] > statistics.mean(s_sharpes)
    better_mdd = e["mdd"] > statistics.mean(s_mdds)  # 回撤是负数，越大越好
    print(f"  Sharpe优于单种子均值: {'是' if better_sharpe else '否'}")
    print(f"  回撤优于单种子均值(更浅): {'是' if better_mdd else '否'}")
else:
    print("  集成训练尚未完成，训练结束后重跑本脚本。")

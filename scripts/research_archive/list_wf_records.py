import sqlite3, json

con = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\factors.db')
cur = con.cursor()
cur.execute('SELECT run_id, generated_at, label, start_date, end_date, windows, metrics, status FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT 30')
rows = cur.fetchall()
print(f'共 {len(rows)} 条记录\n')
header = f'{"run_id":<35} {"时间":<20} {"标签":<15} {"窗口":<6} {"总收益":<10} {"Sharpe":<8} {"回撤":<10} {"IC":<8}'
print(header)
print('-' * 120)
for r in rows:
    run_id, gen_at, label, start, end, windows, metrics_json, status = r
    try:
        m = json.loads(metrics_json) if metrics_json else {}
        total = m.get('totalReturn', m.get('total_return', '?'))
        sharpe = m.get('sharpe', m.get('sharpeRatio', '?'))
        mdd = m.get('maxDrawdown', m.get('max_drawdown', '?'))
        ic = m.get('avgRankIc', m.get('avg_rank_ic', '?'))
        if isinstance(total, (int, float)):
            total = f'{total*100:.2f}%'
        if isinstance(mdd, (int, float)):
            mdd = f'{mdd*100:.2f}%'
        if isinstance(sharpe, (int, float)):
            sharpe = f'{sharpe:.3f}'
        if isinstance(ic, (int, float)):
            ic = f'{ic:.4f}'
        print(f'{run_id:<35} {gen_at:<20} {label:<15} {windows:<6} {total:<10} {sharpe:<8} {mdd:<10} {ic:<8}')
    except Exception as e:
        print(f'{run_id:<35} {gen_at:<20} {label:<15} {windows:<6} ERROR: {e}')
con.close()

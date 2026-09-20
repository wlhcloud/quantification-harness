import os

PROJECT = r'D:\ProdProject\AI\quantification-harness'
files = [
    'scripts/backtest_technical_timing.py',
    'scripts/backtest_technical_timing_v2.py',
    'scripts/backtest_technical_timing_v3.py',
    'scripts/backtest_technical_timing_v4.py',
    'scripts/backtest_technical_timing_v5.py',
    'scripts/backtest_technical_timing_v6.py',
    'scripts/backtest_technical_timing_v7.py',
]

for f in files:
    path = os.path.join(PROJECT, f)
    print(f'\n{"="*80}')
    print(f'文件: {f}')
    print(f'{"="*80}')
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            lines = fp.readlines()
        # 打印前25行（docstring部分）
        for i, line in enumerate(lines[:25]):
            print(line.rstrip())
    except Exception as e:
        print(f'读取失败: {e}')

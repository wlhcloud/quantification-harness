"""
修复stock-ml.yaml配置文件的YAML格式问题
问题：第1行和第10行因为中文注释编码问题导致换行符丢失
"""
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'

# 读取原始文件
with open(CONFIG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

print(f'原始文件: {len(lines)} 行')

# 修复第1行：把 label: forward_3 移到新的一行
# 原始第1行包含注释和 label: forward_3
new_lines = []
for i, line in enumerate(lines):
    if i == 0 and 'label: forward_3' in line:
        # 把注释和label分开
        # 找到 label: forward_3 的位置
        idx = line.find('label: forward_3')
        comment_part = line[:idx].rstrip()
        label_part = line[idx:].rstrip()
        new_lines.append(comment_part + '\n')
        new_lines.append(label_part + '\n')
        print(f'修复第1行: 分离注释和label')
    elif i == 9 and 'featuresOverride:' in line and 'labelRelative' in line:
        # 第10行（索引9）：把 featuresOverride: 移到新的一行
        idx = line.find('featuresOverride:')
        before_part = line[:idx].rstrip()
        features_part = line[idx:].rstrip()
        new_lines.append(before_part + '\n')
        new_lines.append(features_part + '\n')
        print(f'修复第10行: 分离labelRelative和featuresOverride')
    else:
        new_lines.append(line)

print(f'修复后: {len(new_lines)} 行')

# 保存修复后的文件
with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f'\n配置已保存: {CONFIG_PATH}')

# 验证YAML格式
import yaml
try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    print(f'\nYAML验证成功!')
    print(f'  label: {cfg.get("label")}')
    features = cfg.get('featuresOverride', [])
    print(f'  featuresOverride: {len(features)} 个特征')
    hotspot = [f for f in features if f.startswith('rank_') or f.startswith('up_') or f.startswith('limit_up') or f.startswith('relative_strength') or f.startswith('industry_rank_ret')]
    print(f'  热点因子: {len(hotspot)} 个')
except Exception as e:
    print(f'\nYAML验证失败: {e}')

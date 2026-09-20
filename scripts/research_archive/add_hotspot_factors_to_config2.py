"""
直接用文本处理方式修改stock-ml.yaml，添加热点因子
"""
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'

# 读取原始文件（二进制方式，避免编码问题）
with open(CONFIG_PATH, 'rb') as f:
    raw = f.read()

# 尝试用utf-8解码
try:
    content = raw.decode('utf-8')
    encoding = 'utf-8'
except:
    try:
        content = raw.decode('gbk')
        encoding = 'gbk'
    except:
        content = raw.decode('utf-8', errors='ignore')
        encoding = 'utf-8'

print(f'文件编码: {encoding}')
print(f'文件长度: {len(content)} 字符')

# 热点因子列表（YAML格式）
hotspot_yaml = """    # hotspot factors
    - rank_ret5
    - rank_ret10
    - rank_ret20
    - rank_amount5
    - rank_amount10
    - up_days
    - up_count5
    - limit_up_count5
    - limit_up_count10
    - relative_strength5
    - relative_strength10
    - relative_strength20
    - rank_relative_strength5
    - rank_relative_strength10
    - industry_rank_ret5
    - industry_rank_ret10
    - industry_rank_ret20
"""

# 找到 "- close_position_5d" 后面插入
marker = '- close_position_5d'
if marker in content:
    # 找到marker所在行的末尾
    idx = content.find(marker)
    line_end = content.find('\n', idx)
    # 在marker行后面插入热点因子
    new_content = content[:line_end+1] + hotspot_yaml + content[line_end+1:]
    print(f'已在 "{marker}" 后插入热点因子')
else:
    print(f'未找到 "{marker}"，尝试其他方式')
    # 找到 "- mktCap" 前面插入
    marker2 = '- mktCap'
    if marker2 in content:
        idx = content.find(marker2)
        # 找到行首
        line_start = content.rfind('\n', 0, idx) + 1
        new_content = content[:line_start] + hotspot_yaml + content[line_start:]
        print(f'已在 "{marker2}" 前插入热点因子')
    else:
        print('未找到插入点，文件内容前500字符:')
        print(content[:500])
        new_content = content

# 保存文件
with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
    f.write(new_content)

print(f'\n配置已保存: {CONFIG_PATH}')

# 验证：检查热点因子是否在文件中
with open(CONFIG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
    verify = f.read()

hotspot_factors = ['rank_ret5', 'rank_ret10', 'rank_ret20', 'up_days', 'limit_up_count5']
found = [f for f in hotspot_factors if f in verify]
print(f'\n验证: 找到 {len(found)}/{len(hotspot_factors)} 个热点因子')
print(f'  找到: {found}')

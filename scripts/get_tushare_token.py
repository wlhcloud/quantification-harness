import os
import sqlite3
import sys
sys.path.insert(0, r'D:\ProdProject\AI\quantification-harness\services\quant-sync\src')
from quant_sync.credentials import CredentialCodec

# 读取meta.db
conn = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\meta.db')
rows = conn.execute("SELECT id, label, protocol, base_url, token FROM data_source_configs").fetchall()
conn.close()

print('数据源列表:')
for r in rows:
    print(f'  id={r[0]}, label={r[1]}, protocol={r[2]}, base_url={r[3]}, token_len={len(r[4]) if r[4] else 0}')

# 尝试解密token：token 已按 QUANT_SYNC_CREDENTIAL_KEY 加密存储（enc:v1: 前缀）
# 该 key 在 .quant.env 里；这里显式从环境变量或 .quant.env 读取（原先硬编码空串，加密后会解不开）
credential_key = os.environ.get("QUANT_SYNC_CREDENTIAL_KEY", "")
if not credential_key:
    env_file = r'D:\ProdProject\AI\quantification-harness\.quant.env'
    try:
        with open(env_file, encoding='utf-8') as fh:
            for line in fh:
                if line.strip().startswith('QUANT_SYNC_CREDENTIAL_KEY='):
                    credential_key = line.split('=', 1)[1].strip()
                    break
    except OSError:
        pass
print(f'\n凭据密钥来源: {"环境变量/.quant.env" if credential_key else "未配置（明文 token 才可读）"}')
codec = CredentialCodec(credential_key)

for r in rows:
    if r[4]:
        try:
            decoded = codec.decode(r[4])
            print(f'\n{r[0]} ({r[1]}) 解密后token前10位: {decoded[:10]}...')
            print(f'  完整token: {decoded}')
        except Exception as e:
            print(f'\n{r[0]} ({r[1]}) 解密失败: {e}')
            print(f'  原始token前20位: {r[4][:20]}...')

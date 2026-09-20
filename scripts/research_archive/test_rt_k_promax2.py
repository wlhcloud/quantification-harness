"""
测试用promax代理（x-api-key协议）调用rt_k实时日线接口
"""
import json
import urllib.request
import urllib.parse

# promax代理配置
PROMAX_URL = 'https://pcd.mobcvb.cn/tushare/pro'
PROMAX_TOKEN = 'tsr_1FjRkziz3M7m0aLcTk0ZgnK03__xO3EYq0ZdwQqdwSE'

# x-api-key协议：GET请求，header带X-API-Key
params = {"ts_code": "600000.SH"}
query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
url = f"{PROMAX_URL}/rt_k?{query}"

request = urllib.request.Request(url, headers={"X-API-Key": PROMAX_TOKEN})

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
        print(f'请求成功!')
        print(f'  响应keys: {list(payload.keys())}')
        print(f'  完整响应前500字: {json.dumps(payload, ensure_ascii=False, indent=2)[:500]}')
except Exception as e:
    print(f'请求失败: {e}')
    # 试试其他endpoint格式
    for endpoint in ['rt-k', 'rt_k', 'realtime_k', 'rtk']:
        url2 = f"{PROMAX_URL}/{endpoint}?{query}"
        request2 = urllib.request.Request(url2, headers={"X-API-Key": PROMAX_TOKEN})
        try:
            with urllib.request.urlopen(request2, timeout=10) as response:
                payload2 = json.loads(response.read().decode())
                print(f'\n  endpoint={endpoint} 成功!')
                print(f'    响应: {json.dumps(payload2, ensure_ascii=False)[:300]}')
                break
        except Exception as e2:
            print(f'  endpoint={endpoint} 失败: {e2}')

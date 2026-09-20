"""
测试用promax代理调用rt_k实时日线接口
"""
import json
import urllib.request

# promax代理配置
PROMAX_URL = 'https://pcd.mobcvb.cn/tushare/pro'
PROMAX_TOKEN = 'tsr_1FjRkziz3M7m0aLcTk0ZgnK03__xO3EYq0ZdwQqdwSE'

# 调用rt_k接口
body = json.dumps({
    "api_name": "rt_k",
    "token": PROMAX_TOKEN,
    "params": {"ts_code": "600000.SH"},
    "fields": ""
}).encode()

request = urllib.request.Request(PROMAX_URL, data=body, headers={"content-type": "application/json"}, method="POST")

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
        print(f'请求成功!')
        print(f'  code: {payload.get("code")}')
        print(f'  msg: {payload.get("msg")}')
        if payload.get('data') and payload['data'].get('items'):
            print(f'  数据条数: {len(payload["data"]["items"])}')
            print(f'  字段: {payload["data"]["fields"]}')
            print(f'  第一条: {payload["data"]["items"][0]}')
        else:
            print(f'  完整响应: {json.dumps(payload, ensure_ascii=False, indent=2)[:500]}')
except Exception as e:
    print(f'请求失败: {e}')

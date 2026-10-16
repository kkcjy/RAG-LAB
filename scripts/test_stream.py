#!/usr/bin/env python3
"""
SSE 流式问答验证

验证 /api/chat/stream 真实流式输出：
  - 事件序列是否完整（session → status → sources → delta → done）
  - 是否逐字流式返回（而非一次性返回）
  - 引用来源是否正确
"""
import json
import sys
import time
import urllib.request
import urllib.error

HOST = "http://localhost:8000"


def api(method, path, body=None, token=None) -> dict:
    url = f"{HOST}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


# 登录
login = api("POST", "/api/auth/login",
            {"username": "admin", "password": "admin123456"})
token = login["access_token"]
print(f"登录成功，token 长度 {len(token)}")


def _pick_kb() -> str:
    """定位一个含已就绪文档的知识库：/tmp/kb_id.txt 优先，否则取第一个可用的。"""
    try:
        with open("/tmp/kb_id.txt") as f:
            return f.read().strip()
    except OSError:
        pass
    for k in api("GET", "/api/kb", token=token).get("items", []):
        if k.get("chunk_count", 0) > 0:
            return k["id"]
    print("没有可用的知识库。请先运行 scripts/e2e_test.py --keep 准备测试数据。")
    sys.exit(1)


kb_id = _pick_kb()
print(f"知识库: {kb_id}")

# 发起 SSE 请求
query = "年假有多少天？请说明不同司龄对应的天数。"
payload = json.dumps({"query": query, "kb_id": kb_id, "enable_web": False}).encode()
req = urllib.request.Request(f"{HOST}/api/chat/stream", data=payload, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Accept", "text/event-stream")

print(f"\n问题: {query}")
print("=" * 62)

events = []
answer = ""
first_delta_latency = None
delta_times = []
t0 = time.time()

try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        print(f"HTTP {resp.status}  Content-Type: {resp.headers.get('Content-Type')}")
        print("-" * 62)

        for raw in resp:
            line = raw.decode(errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")
            data = ev.get("data")
            events.append(etype)

            if etype == "session":
                print(f"[session] {str(data.get('session_id'))[:8]}...")

            elif etype == "status":
                print(f"[{data.get('stage','?'):<9}] {data.get('message','')}")

            elif etype == "sources":
                names = [s.get("filename") for s in data if s.get("filename")]
                print(f"[sources] {len(data)} 条: {', '.join(names)}")

            elif etype == "delta":
                if first_delta_latency is None:
                    first_delta_latency = time.time() - t0
                delta_times.append(time.time() - t0)
                answer += data
                print(data, end="", flush=True)

            elif etype == "done":
                print()
                print(f"[done]   延迟={data.get('latency_ms',0):.0f}ms "
                      f"命中={data.get('kb_hit')} 引用={data.get('chunk_count')}块")

            elif etype == "finish":
                print(f"[finish] 总耗时={data.get('latency_ms',0):.0f}ms")

            elif etype == "error":
                print(f"\n[error] {data}")

except urllib.error.HTTPError as e:
    print(f"HTTP 错误 {e.code}: {e.read().decode()[:300]}")
    sys.exit(1)

print("=" * 62)
print(f"事件序列   : {' → '.join(events)}")
print(f"回答长度   : {len(answer)} 字符")
print(f"delta 事件 : {len(delta_times)} 次")

if first_delta_latency:
    print(f"首字节延迟 : {first_delta_latency:.2f}s")
    if len(delta_times) > 1:
        span = delta_times[-1] - delta_times[0]
        print(f"流式总时长 : {span:.2f}s")
        # 判断是否真流式：delta 分散在多个时间点
        spread = len(set(round(t, 1) for t in delta_times))
        print(f"时间分布   : 跨越 {spread} 个 0.1s 时间片 "
              f"{'→ 真流式 ✓' if spread > 3 else '→ 疑似一次性返回 ✗'}")

print()
print("=" * 62)
print("完整回答")
print("=" * 62)
print(answer)

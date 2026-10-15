#!/usr/bin/env python3
"""Query 改写与相关度判定验证

测试场景：
  1. 指代消解：问"它"、"这个"能否被改写为明确主体
  2. 多问题拆解：一句话含多个子问题
  3. 口语化 → 规范化
  4. 无关问题：验证阈值/精判能正确判为未命中
"""
import json
import sys
import urllib.request
import urllib.error

HOST = "http://localhost:8000"


def api(method, path, body=None, token=None):
    url = f"{HOST}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_detail": e.read().decode()[:300]}


token = api("POST", "/api/auth/login",
            {"username": "admin", "password": "admin123456"})["access_token"]
kb_id = open("/tmp/kb_id.txt").read().strip()

cases = [
    ("指代消解", "它有多少天？", None),
    ("口语化", "那个报销的事儿咋弄啊", None),
    ("多问题拆解", "年假怎么算？报销流程是什么？", None),
    ("明确问题（对照）", "年假有多少天", None),
    ("无关问题（应未命中）", "今天北京的天气怎么样？", False),
]

print("=" * 74)
print("Query 改写 与 相关度判定 验证")
print("=" * 74)

for name, q, expect_hit in cases:
    r = api("POST", "/api/chat/retrieve", {"query": q, "kb_id": kb_id}, token)
    print(f"\n【{name}】")
    print(f"  原问题    : {q}")

    if "_error" in r:
        print(f"  错误      : {r['_error']} {r['_detail'][:120]}")
        continue

    rq = r.get("rewritten_queries", [])
    changed = rq != [q]
    print(f"  改写结果  : {rq}  {'← 已改写 ✓' if changed else '（未改动）'}")
    print(f"  判定      : kb_hit={r.get('kb_hit')}  {r.get('judge_reason','')}")
    print(f"  命中片段  : {r.get('hit_count')} 条")

    if r.get("results"):
        top = r["results"][0]
        print(f"  top1      : {top['filename']} (score={top['score']})")

    if expect_hit is not None:
        ok = r.get("kb_hit") == expect_hit
        print(f"  预期      : kb_hit={expect_hit}  {'✓ 符合' if ok else '✗ 不符'}")

print()
print("=" * 74)

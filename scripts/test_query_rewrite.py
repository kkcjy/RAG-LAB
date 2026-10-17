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

HOST = "http://localhost:18000"


def api(method, path, body=None, token=None) -> dict:
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

# 定位一个可用于检索验证的知识库：
#   1) /tmp/kb_id.txt（e2e_test.py --keep 留下的）
#   2) 已存在且含已就绪文档的库
#   3) 都没有则自动建一个并灌入测试文档
def _pick_or_create_kb() -> tuple[str, bool]:
    try:
        with open("/tmp/kb_id.txt") as f:
            return f.read().strip(), False
    except OSError:
        pass

    kbs = api("GET", "/api/kb", token=token).get("items", [])
    usable = [k for k in kbs if k.get("chunk_count", 0) > 0]
    if usable:
        return usable[0]["id"], False

    print("未发现可用知识库，自动创建并上传测试文档（首次约需 30s）...")
    kb = api("POST", "/api/kb", {"name": "_query_rewrite_tmp"}, token)
    kb_id = kb["id"]
    import pathlib
    import uuid as _uuid

    fixture_dir = pathlib.Path(__file__).resolve().parent.parent / "test_fixtures"
    for fp in sorted(fixture_dir.glob("*"))[:3]:
        boundary = _uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="'
            + fp.name.encode() + b'"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            fp.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            f"{HOST}/api/kb/{kb_id}/documents", data=body, method="POST"
        )
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:  # noqa: BLE001
            print(f"  上传 {fp.name} 失败: {e}")

    # 等索引完成
    import time as _time

    for _ in range(60):
        res = api("GET", f"/api/kb/{kb_id}/documents", token=token)
        if res.get("all_done", True) and res.get("ready", 0) > 0:
            break
        _time.sleep(2)
    return kb_id, True


kb_id, _auto_created = _pick_or_create_kb()

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

failures: list[str] = []

for name, q, expect_hit in cases:
    r = api("POST", "/api/chat/retrieve", {"query": q, "kb_id": kb_id}, token)
    print(f"\n【{name}】")
    print(f"  原问题    : {q}")

    if "_error" in r:
        print(f"  错误      : {r['_error']} {r['_detail'][:120]}")
        failures.append(f"{name}: 请求失败 {r['_error']}")
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
        if not ok:
            failures.append(
                f"{name}: 期望 kb_hit={expect_hit}，实际 {r.get('kb_hit')}"
            )

    if not rq:
        print("  (未返回改写结果)")
        failures.append(f"{name}: 未返回 rewritten_queries")

# 若本次自动建了临时库，用完即删，避免污染数据
if _auto_created:
    api("DELETE", f"/api/kb/{kb_id}", token=token)
    print(f"\n（已清理自动创建的临时知识库）")

print()
print("=" * 74)
if failures:
    print(f"✗ 验证失败（{len(failures)} 项）:")
    for f in failures:
        print(f"    - {f}")
    sys.exit(1)
print("✓ Query 改写与相关度判定 全部符合预期")
sys.exit(0)

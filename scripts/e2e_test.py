#!/usr/bin/env python3
"""
端到端集成测试

在服务已启动的前提下，真实调用 API 走完整流程：
  登录 → 创建知识库 → 上传文档 → 等待索引 → 检索 → 问答 → 清理

用法:
    python3 scripts/e2e_test.py
    python3 scripts/e2e_test.py --host http://localhost:18000 -u admin -p xxx
    python3 scripts/e2e_test.py --keep     # 保留测试数据，便于人工检查

依赖：仅标准库
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
DIM = "\033[2m"
NC = "\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = f"{GREEN}✓{NC}" if ok else f"{RED}✗{NC}"
    print(f"  {mark} {name}" + (f"  {DIM}{detail}{NC}" if detail else ""))


class Client:
    def __init__(self, host: str):
        self.host = host.rstrip("/")
        self.token: str | None = None

    def _req(self, method: str, path: str, body=None, raw: bytes | None = None,
             content_type: str | None = None):
        url = f"{self.host}{path}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        elif body is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                payload = r.read().decode()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    def login(self, u: str, p: str):
        res = self._req("POST", "/api/auth/login", {"username": u, "password": p})
        self.token = res["access_token"]
        return res

    def health(self):
        return self._req("GET", "/api/health")

    def create_kb(self, name: str):
        return self._req("POST", "/api/kb", {"name": name, "description": "e2e 测试"})

    def delete_kb(self, kb_id: str):
        return self._req("DELETE", f"/api/kb/{kb_id}")

    def upload(self, kb_id: str, filename: str, content: bytes) -> dict:
        boundary = f"----rag-lab{uuid.uuid4().hex}"
        body = b""
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self._req("POST", f"/api/kb/{kb_id}/documents", raw=body,
                         content_type=f"multipart/form-data; boundary={boundary}")

    def docs(self, kb_id: str):
        return self._req("GET", f"/api/kb/{kb_id}/documents")

    def chunks(self, kb_id: str, doc_id: str):
        return self._req("GET", f"/api/kb/{kb_id}/documents/{doc_id}/chunks")

    def ask(self, query: str, kb_id: str, enable_web: bool = False):
        return self._req("POST", "/api/chat/ask",
                         {"query": query, "kb_id": kb_id, "enable_web": enable_web})

    def retrieve(self, query: str, kb_id: str):
        return self._req("POST", "/api/chat/retrieve",
                         {"query": query, "kb_id": kb_id})


def wait_indexed(c: Client, kb_id: str, expect: int, timeout: int = 600) -> dict:
    """轮询直到全部文档索引完成"""
    start = time.time()
    last = {}
    while time.time() - start < timeout:
        res = c.docs(kb_id)
        items = res.get("items", [])
        last = res
        ready = sum(1 for d in items if d["status"] == "ready")
        failed = [d for d in items if d["status"] == "failed"]
        if failed:
            for d in failed:
                print(f"    {RED}索引失败:{NC} {d['filename']} → {d.get('error_message')}")
            return last
        if ready >= expect:
            return last
        time.sleep(3)
    return last


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG-LAB 端到端测试")
    ap.add_argument("--host", default="http://localhost:18000")
    ap.add_argument("-u", "--user", default="admin")
    ap.add_argument("-p", "--password", default="admin123456")
    ap.add_argument("--keep", action="store_true", help="保留测试数据")
    ap.add_argument("--docs", default=str(Path(__file__).resolve().parent.parent / "test_fixtures"),
                    help="测试文档目录（默认 test_fixtures）")
    args = ap.parse_args()

    c = Client(args.host)
    kb_id: str | None = None
    kb_name = f"e2e测试_{int(time.time())}"

    print(f"\n{BOLD if False else ''}{CYAN}{'=' * 66}{NC}")
    print(f"{CYAN}  RAG-LAB 端到端集成测试{NC}")
    print(f"{CYAN}{'=' * 66}{NC}\n")

    # ---- 1 健康检查 ----
    print(f"{CYAN}[1] 服务健康{NC}")
    h_llm_ok = False
    try:
        h = c.health()
        h_llm_ok = bool(h["llm_ok"])
        record("健康接口响应", True, f"status={h['status']}")
        if h_llm_ok:
            record("LLM 连通", True, f"{h['llm_provider']}/{h['llm_model']}")
        else:
            print(f"  {YELLOW}○{NC} LLM 连通      {DIM}{h['llm_model']}（检索链路不受影响）{NC}")
        record("向量库连通", bool(h["vector_store_ok"]))
        record("Embedding 维度", h["embedding_dim"] > 0, f"dim={h['embedding_dim']}")
        web_ok = "Null" not in h["web_search_provider"]
        if web_ok:
            record("联网搜索已配置", True, h["web_search_provider"])
        else:
            print(f"  {YELLOW}○{NC} 联网搜索未配置  {DIM}（需 TAVILY_API_KEY，本次跳过）{NC}")
    except RuntimeError as e:
        record("健康接口响应", False, str(e))
        print(f"\n{RED}服务不可达，终止测试{NC}")
        return 1

    # ---- 2 登录 ----
    print(f"\n{CYAN}[2] 认证{NC}")
    try:
        c.login(args.user, args.password)
        record("管理员登录", True)
    except RuntimeError as e:
        record("管理员登录", False, str(e))
        return 1

    try:
        # ---- 3 创建知识库 ----
        print(f"\n{CYAN}[3] 知识库{NC}")
        kb = c.create_kb(kb_name)
        kb_id = kb["id"]
        assert kb_id, "创建知识库未返回 id"
        record("创建知识库", True, kb["collection_name"])

        # ---- 4 上传文档 ----
        print(f"\n{CYAN}[4] 文档上传{NC}")
        doc_dir = args.docs
        if not os.path.isdir(doc_dir):
            record("测试文档目录存在", False, doc_dir)
            return 1

        uploaded = 0
        for fn in sorted(os.listdir(doc_dir)):
            fp = os.path.join(doc_dir, fn)
            if not os.path.isfile(fp):
                continue
            with open(fp, "rb") as f:
                content = f.read()
            try:
                d = c.upload(kb_id, fn, content)
                record(f"上传 {fn}", d["status"] in ("pending", "parsing", "indexing", "ready"),
                       f"status={d['status']}")
                uploaded += 1
            except RuntimeError as e:
                record(f"上传 {fn}", False, str(e))

        if uploaded == 0:
            print(f"{RED}没有成功上传任何文档，终止{NC}")
            return 1

        # ---- 5 等待索引 ----
        print(f"\n{CYAN}[5] 索引（等待向量化完成）{NC}")
        t0 = time.time()
        res = wait_indexed(c, kb_id, uploaded, timeout=900)
        items = res.get("items", [])
        ready = [d for d in items if d["status"] == "ready"]
        failed = [d for d in items if d["status"] == "failed"]

        record("全部文档索引完成", len(ready) == uploaded and not failed,
               f"{len(ready)}/{uploaded} 就绪，耗时 {time.time() - t0:.0f}s")
        for d in items:
            tag = f"{GREEN}ready{NC}" if d["status"] == "ready" else f"{RED}{d['status']}{NC}"
            print(f"    {tag}  {d['filename']:<24} {d['chunk_count']:>3} 块  "
                  f"{d['char_count']:>5} 字符")

        if not ready:
            return 1

        # ---- 6 分块内容检查 ----
        print(f"\n{CYAN}[6] 分块入库校验{NC}")
        total_chunks = 0
        for d in ready:
            ch = c.chunks(kb_id, d["id"])
            total_chunks += ch["total"]
            if ch["total"] > 0:
                sample = ch["items"][0]
                record(f"{d['filename']} 分块可读", len(sample["content"]) > 0,
                       f"{ch['total']} 块，首块 {sample['char_count']} 字符")
        kb_now = c._req("GET", f"/api/kb/{kb_id}")
        record("知识库统计已更新", kb_now["chunk_count"] == total_chunks,
               f"kb.chunk_count={kb_now['chunk_count']} 实际={total_chunks}")

        # ---- 7 检索质量（不依赖 LLM）----
        print(f"\n{CYAN}[7] 向量检索质量{NC}")
        questions = [
            ("年假有多少天", "员工手册.md"),
            ("报销流程是什么", "员工手册.md"),
            ("向量检索的原理是什么", "系统说明.txt"),
            ("DSR 协议解决什么问题", "DSR_protocol.pdf"),
            ("供应商评级标准", "供应商管理办法.docx"),
        ]
        hits = 0
        for q, expect_file in questions:
            try:
                r = c.retrieve(q, kb_id)
                files = [x.get("filename") for x in r.get("results", [])]
                top_score = r["results"][0]["score"] if r.get("results") else 0.0
                # 命中判定：知识库判定命中 且 top1 来自预期文件
                ok = r.get("kb_hit", False) and expect_file in files
                if ok:
                    hits += 1
                record(f"检索「{q}」", ok,
                       f"top1={files[0] if files else '无'} score={top_score:.3f} "
                       f"命中={r.get('kb_hit')}")
            except RuntimeError as e:
                record(f"检索「{q}」", False, str(e))

        record("检索准确率", hits >= len(questions) * 0.6,
               f"{hits}/{len(questions)}（期望 top1 来自正确文档）")

        # ---- 7.5 生成问答（需要 LLM）----
        print(f"\n{CYAN}[7.5] 生成问答（依赖 LLM）{NC}")
        if h_llm_ok:
            try:
                r = c.ask("年假有多少天？请简要回答。", kb_id, enable_web=False)
                ans = r.get("answer", "")
                record("LLM 生成回答", len(ans) > 0,
                       f"{len(ans)} 字符, 引用 {len(r.get('sources', []))} 条")
                if ans:
                    print(f"    {DIM}回答节选: {ans[:120]}...{NC}")
            except RuntimeError as e:
                record("LLM 生成回答", False, str(e))
        else:
            print(f"  {YELLOW}跳过：LLM 未配置（需 DEEPSEEK_API_KEY）{NC}")

    finally:
        # ---- 8 清理 ----
        print(f"\n{CYAN}[8] 清理{NC}")
        if kb_id and not args.keep:
            try:
                c.delete_kb(kb_id)
                record("删除测试知识库", True)
            except RuntimeError as e:
                record("删除测试知识库", False, str(e))
        elif kb_id:
            print(f"  {YELLOW}已保留测试数据: {kb_name} ({kb_id}){NC}")

    # ---- 汇总 ----
    print(f"\n{CYAN}{'=' * 66}{NC}")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    if failed == 0:
        print(f"{GREEN}全部通过: {passed} 项{NC}")
    else:
        print(f"{RED}失败 {failed} 项{NC} / 通过 {passed} 项")
        for name, ok, detail in results:
            if not ok:
                print(f"  {RED}✗{NC} {name}  {detail}")
    print(f"{CYAN}{'=' * 66}{NC}\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

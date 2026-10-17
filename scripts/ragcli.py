#!/usr/bin/env python3
"""
RAG-LAB 终端客户端

朋友通过终端连接问答的使用方式。仅依赖标准库（urllib），
不需要安装任何第三方包。

用法:
    # 交互式问答（最常用）
    python3 scripts/ragcli.py --host http://1.2.3.4:18000

    # 单次提问
    python3 scripts/ragcli.py --host http://1.2.3.4:18000 -u user1 -p pass --ask "报销流程是什么"

    # 管理知识库/用户（建库、上传文档、授权等）
    python3 scripts/ragcli.py --help
"""
from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"


class APIClient:
    def __init__(self, host: str):
        self.host = host.rstrip("/")
        self.token: str | None = None

    def _request(self, method: str, path: str, body: dict | None = None, stream: bool = False):
        url = f"{self.host}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

        try:
            resp = urllib.request.urlopen(req, timeout=300 if stream else 120)
            if stream:
                # 交给调用方逐行读取并负责关闭
                return resp
            with resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"连接失败: {e.reason}") from e

    def _request_multipart(self, path: str, field_name: str, filename: str, content: bytes):
        """纯标准库构造 multipart/form-data 上传文件（避免依赖 requests）。"""
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_name = os.path.basename(filename).encode("utf-8")
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="' + field_name.encode() + b'"; filename="'
            + safe_name + b'"\r\n',
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(f"{self.host}{path}", data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"连接失败: {e.reason}") from e

    # ---------------- 认证 ----------------
    def login(self, username: str, password: str) -> dict:
        res = self._request("POST", "/api/auth/login", {"username": username, "password": password})
        self.token = res["access_token"]
        return res

    def me(self) -> dict:
        return self._request("GET", "/api/auth/me")

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    # ---------------- 知识库 ----------------
    def list_kbs(self) -> dict:
        return self._request("GET", "/api/kb")

    def create_kb(self, name: str) -> dict:
        return self._request("POST", "/api/kb", {"name": name})

    def delete_kb(self, kb_id: str) -> dict:
        return self._request("DELETE", f"/api/kb/{kb_id}")

    def list_docs(self, kb_id: str) -> dict:
        return self._request("GET", f"/api/kb/{kb_id}/documents")

    def upload_doc(self, kb_id: str, filepath: str) -> dict:
        with open(filepath, "rb") as f:
            content = f.read()
        return self._request_multipart(
            f"/api/kb/{kb_id}/documents", "file", os.path.basename(filepath), content
        )

    def grant_permission(self, kb_id: str, user_id: str) -> dict:
        return self._request(
            "POST", f"/api/kb/{kb_id}/permissions",
            {"user_id": user_id, "can_read": True, "can_write": False},
        )

    def list_permissions(self, kb_id: str) -> dict:
        return self._request("GET", f"/api/kb/{kb_id}/permissions")

    def find_user_id(self, username: str) -> str | None:
        """按用户名查 UUID（授权接口需要 UUID）。"""
        res = self._request("GET", "/api/admin/users")
        for u in res.get("items", []):
            if u["username"] == username:
                return u["id"]
        return None

    def resolve_kb(self, ref: str) -> dict | None:
        """按名称（模糊）或 ID 定位知识库。"""
        for kb in self.list_kbs().get("items", []):
            if kb["id"] == ref or ref.lower() in kb["name"].lower():
                return kb
        return None

    # ---------------- 问答 ----------------
    def ask(self, query: str, kb_id: str, session_id: str | None = None,
            enable_web: bool = True) -> dict:
        body = {"query": query, "kb_id": kb_id, "enable_web": enable_web}
        if session_id:
            body["session_id"] = session_id
        return self._request("POST", "/api/chat/ask", body)

    def ask_stream(self, query: str, kb_id: str, session_id: str | None = None,
                   enable_web: bool = True):
        body = {"query": query, "kb_id": kb_id, "enable_web": enable_web}
        if session_id:
            body["session_id"] = session_id
        resp = self._request("POST", "/api/chat/stream", body, stream=True)
        with resp:
            for raw_line in resp:
                line = raw_line.decode(errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    yield json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue

    # ---------------- 管理 ----------------
    def list_users(self) -> dict:
        return self._request("GET", "/api/admin/users")

    def create_user(self, username: str, password: str, role: str = "user") -> dict:
        return self._request(
            "POST", "/api/admin/users",
            {"username": username, "password": password, "role": role},
        )

    def stats(self) -> dict:
        return self._request("GET", "/api/admin/stats")


# ---------------------------------------------------------------------------
def _print_index_summary(res: dict) -> None:
    """打印索引进度汇总（来自后端 DocListOut 的新字段）"""
    indexing = res.get("indexing", 0)
    ready = res.get("ready", 0)
    failed = res.get("failed", 0)
    if indexing:
        print(f"{YELLOW}  索引中 {indexing} 个"
              f"（已就绪 {ready}，失败 {failed}）—— 完成后才能检索到{NC}")
    elif failed:
        print(f"{RED}  ✗ {failed} 个文档索引失败，{ready} 个可用{NC}")
    elif ready:
        print(f"{DIM}  ✓ {ready} 个文档已就绪{NC}")


def _wait_for_index(client: APIClient, kb_id: str, kb_name: str,
                    timeout: float = 600.0) -> int:
    """轮询等所有文档索引完成。返回 0=全部就绪, 1=有失败或超时。"""
    terminal_states = {"ready", "failed"}
    start = time.time()
    last_line = ""
    while True:
        try:
            docs = client.list_docs(kb_id).get("items", [])
        except RuntimeError as e:
            print(f"\n{RED}{e}{NC}")
            return 1

        if not docs:
            print(f"{YELLOW}该知识库暂无文档{NC}")
            return 0

        done = sum(1 for d in docs if d.get("status") in terminal_states)
        ready = sum(1 for d in docs if d.get("status") == "ready")
        failed = sum(1 for d in docs if d.get("status") == "failed")
        elapsed = time.time() - start

        line = (f"\r{DIM}索引中 {done}/{len(docs)}  "
                f"(就绪 {ready}, 失败 {failed})  已用 {elapsed:.0f}s{NC}")
        print(line + " " * max(0, len(last_line) - len(line)), end="", flush=True)
        last_line = line

        if done == len(docs):
            print()
            if failed:
                print(f"{RED}✗ {failed} 个文档索引失败{NC}")
                for d in docs:
                    if d.get("status") == "failed":
                        print(f"  {d['filename']}: {d.get('error_message', '未知错误')}")
                return 1
            print(f"{GREEN}✓ {ready} 个文档已全部就绪，可以提问了{NC}")
            return 0

        if elapsed > timeout:
            print()
            print(f"{YELLOW}等待超时（{timeout:.0f}s），仍有文档未完成{NC}")
            for d in docs:
                if d.get("status") not in terminal_states:
                    print(f"  {d['filename']}: {d.get('status')}")
            return 1

        time.sleep(2)


# ---------------------------------------------------------------------------
def print_banner(user: dict, host: str) -> None:
    role = "管理员" if user.get("role") == "admin" else "普通用户"
    print(f"{GREEN}{'=' * 62}{NC}")
    print(f"{BOLD}  RAG-LAB 终端问答{NC}")
    print(f"  服务器: {host}")
    print(f"  当前用户: {user.get('username')} ({role})")
    print(f"{GREEN}{'=' * 62}{NC}")
    print(f"{DIM}  命令: /help 帮助 | /kb 选择知识库 | /new-kb 建库 | /add 上传 | "
          f"/web 联网 | /new 新会话 | /quit 退出{NC}")


def print_help() -> None:
    print(f"""
{BOLD}可用命令{NC}
  /help            显示本帮助
  /kb              列出并选择知识库
  /kb <名称或ID>   直接切换到指定知识库
  /new-kb <名称>   创建知识库并切换过去
  /add <文件...>   上传文档到当前知识库
  /docs            列出当前知识库的文档与索引状态
  /wait            等待索引完成
  /web on|off      开启/关闭联网搜索
  /new             开始新会话
  /sources         显示上一条回答的引用来源
  /quit 或 /exit   退出
""")


def interactive(client: APIClient, user: dict, kb_id: str | None, kb_name: str) -> None:
    session_id: str | None = None
    enable_web = True
    last_sources: list = []

    print_banner(user, client.host)
    print(f"  当前知识库: {CYAN}{kb_name or '未选择'}{NC}   联网: "
          f"{CYAN}{'开' if enable_web else '关'}{NC}")
    if not kb_id:
        if user.get("role") == "admin":
            print(f"{YELLOW}  当前没有知识库。输入 /new-kb <名称> 创建一个，"
                  f"再用 /add <文件> 上传文档。{NC}")
        else:
            print(f"{YELLOW}  当前没有可访问的知识库，请联系管理员授权。{NC}")
    print()

    while True:
        try:
            prompt = f"{GREEN}你{NC}> " if kb_id else f"{YELLOW}你(未选知识库){NC}> "
            query = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not query:
            continue

        # 命令处理
        if query.startswith("/"):
            parts = query.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                print("再见！")
                break

            if cmd == "/help":
                print_help()
                continue

            if cmd == "/new":
                session_id = None
                print(f"{DIM}已开始新会话{NC}")
                continue

            if cmd == "/new-kb":
                if not arg:
                    print(f"{YELLOW}用法: /new-kb <知识库名称>{NC}")
                    continue
                try:
                    kb = client.create_kb(arg)
                    kb_id, kb_name = kb["id"], kb["name"]
                    session_id = None
                    print(f"{GREEN}✓ 已创建并切换到: {kb_name}{NC}")
                    print(f"{DIM}  下一步: /add <文件路径> 上传文档{NC}")
                except RuntimeError as e:
                    print(f"{RED}{e}{NC}")
                continue

            if cmd == "/add":
                if not arg:
                    print(f"{YELLOW}用法: /add <文件路径> [更多文件...]{NC}")
                    continue
                if not kb_id:
                    print(f"{YELLOW}请先选择或创建知识库（/kb 或 /new-kb）{NC}")
                    continue
                for fp in arg.split():
                    if not os.path.isfile(fp):
                        print(f"{RED}✗ 文件不存在: {fp}{NC}")
                        continue
                    try:
                        d = client.upload_doc(kb_id, fp)
                        print(f"{GREEN}✓ 已上传: {d.get('filename', os.path.basename(fp))}{NC}"
                              f"  {DIM}(status={d.get('status')}){NC}")
                    except RuntimeError as e:
                        print(f"{RED}✗ {os.path.basename(fp)} 上传失败: {e}{NC}")
                print(f"{YELLOW}  索引为异步执行，用 /wait 等待完成后再提问{NC}")
                continue

            if cmd == "/docs":
                if not kb_id:
                    print(f"{YELLOW}请先选择知识库（/kb）{NC}")
                    continue
                try:
                    res = client.list_docs(kb_id)
                    docs = res.get("items", [])
                    if not docs:
                        print(f"{YELLOW}该知识库暂无文档（用 /add 上传）{NC}")
                        continue
                    print(f"\n{BOLD}{kb_name} 的文档（共 {len(docs)}）{NC}")
                    for d in docs:
                        flag = {"ready": "✓", "failed": "✗"}.get(d.get("status"), "…")
                        print(f"  {flag} {d['filename']:<32} {d.get('status'):<10} "
                              f"分块 {d.get('chunk_count', 0)}")
                        if d.get("error_message"):
                            print(f"      {RED}{d['error_message']}{NC}")
                    _print_index_summary(res)
                    print()
                except RuntimeError as e:
                    print(f"{RED}{e}{NC}")
                continue

            if cmd == "/wait":
                if not kb_id:
                    print(f"{YELLOW}请先选择知识库（/kb）{NC}")
                    continue
                _wait_for_index(client, kb_id, kb_name)
                continue

            if cmd == "/web":
                if arg in ("on", "off"):
                    enable_web = arg == "on"
                    print(f"{DIM}联网搜索已{'开启' if enable_web else '关闭'}{NC}")
                else:
                    print(f"{YELLOW}用法: /web on|off{NC}")
                continue

            if cmd == "/sources":
                if not last_sources:
                    print(f"{DIM}暂无来源信息{NC}")
                else:
                    print(f"\n{BOLD}引用来源{NC}")
                    for i, s in enumerate(last_sources, 1):
                        if s.get("type") == "web":
                            print(f"  [{i}] {CYAN}网络{NC} {s.get('title')}")
                            print(f"      {DIM}{s.get('url')}{NC}")
                        else:
                            print(f"  [{i}] {CYAN}知识库{NC} {s.get('filename')} "
                                  f"(相似度 {s.get('score')})")
                            print(f"      {DIM}{s.get('snippet', '')[:120]}...{NC}")
                    print()
                continue

            if cmd == "/kb":
                if not arg:
                    try:
                        kbs = client.list_kbs().get("items", [])
                    except RuntimeError as e:
                        print(f"{RED}{e}{NC}")
                        continue
                    if not kbs:
                        print(f"{YELLOW}没有可访问的知识库{NC}")
                        continue
                    print(f"\n{BOLD}可用知识库{NC}")
                    for i, kb in enumerate(kbs, 1):
                        mark = " ←" if kb["id"] == kb_id else ""
                        print(f"  {i}. {kb['name']}  "
                              f"{DIM}({kb['doc_count']} 文档 / {kb['chunk_count']} 分块){NC}{mark}")
                    sel = input(f"\n选择编号（回车取消）: ").strip()
                    if sel.isdigit() and 1 <= int(sel) <= len(kbs):
                        kb_id = kbs[int(sel) - 1]["id"]
                        kb_name = kbs[int(sel) - 1]["name"]
                        session_id = None
                        print(f"{GREEN}已切换到: {kb_name}{NC}")
                    continue
                else:
                    try:
                        kbs = client.list_kbs().get("items", [])
                    except RuntimeError as e:
                        print(f"{RED}{e}{NC}")
                        continue
                    matched = [k for k in kbs if arg.lower() in k["name"].lower() or k["id"] == arg]
                    if not matched:
                        print(f"{YELLOW}未找到匹配的知识库{NC}")
                    else:
                        kb_id = matched[0]["id"]
                        kb_name = matched[0]["name"]
                        session_id = None
                        print(f"{GREEN}已切换到: {kb_name}{NC}")
                    continue

            print(f"{YELLOW}未知命令: {cmd}（输入 /help 查看帮助）{NC}")
            continue

        # 正常提问
        if not kb_id:
            print(f"{YELLOW}请先选择知识库（输入 /kb）{NC}")
            continue

        print()
        try:
            answer_parts = []
            for event in client.ask_stream(query, kb_id, session_id, enable_web):
                etype = event.get("type")
                data = event.get("data")

                if etype == "session":
                    session_id = data.get("session_id")

                elif etype == "status":
                    stage = data.get("stage")
                    if stage in ("rewrite", "retrieve"):
                        print(f"{DIM}· {data.get('message')}{NC}")
                    elif stage == "generate":
                        print(f"{GREEN}RAG-LAB{NC}> ", end="", flush=True)

                elif etype == "sources":
                    last_sources = data

                elif etype == "delta":
                    print(data, end="", flush=True)
                    answer_parts.append(data)

                elif etype == "error":
                    print(f"\n{RED}错误: {data}{NC}")

                elif etype == "finish":
                    ms = data.get("latency_ms", 0)
                    print(f"\n{DIM}· 耗时 {ms:.0f}ms{NC}")

            if last_sources:
                kb_n = sum(1 for s in last_sources if s.get("type") == "kb")
                web_n = sum(1 for s in last_sources if s.get("type") == "web")
                tags = []
                if kb_n:
                    tags.append(f"知识库 {kb_n} 条")
                if web_n:
                    tags.append(f"网络 {web_n} 条")
                print(f"{DIM}· 引用: {' / '.join(tags)}  输入 /sources 查看详情{NC}")

        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已中断{NC}")

        print()


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="RAG-LAB 终端客户端")
    ap.add_argument("--host", default="http://localhost:18000", help="服务地址")
    ap.add_argument("-u", "--user", help="用户名")
    ap.add_argument("-p", "--password", help="密码")
    ap.add_argument("--kb", help="知识库名称或ID，启动时直接选择")
    ap.add_argument("--ask", help="单次提问后退出")
    ap.add_argument("--no-web", action="store_true", help="禁用联网搜索")
    ap.add_argument("--health", action="store_true", help="仅检查服务健康状态")
    ap.add_argument("--list-kbs", action="store_true", help="列出知识库")
    ap.add_argument("--create-kb", metavar="NAME", help="创建知识库")
    ap.add_argument("--delete-kb", action="store_true",
                    help="配合 --kb 删除知识库（含全部文档与向量）")
    ap.add_argument("--upload", nargs="+", metavar="FILE",
                    help="配合 --kb 上传一个或多个文档")
    ap.add_argument("--docs", action="store_true", help="配合 --kb 列出文档")
    ap.add_argument("--wait", action="store_true",
                    help="配合 --kb 等待所有文档索引完成")
    ap.add_argument("--grant", metavar="USERNAME", help="配合 --kb 授权用户读取")
    ap.add_argument("--perms", action="store_true", help="配合 --kb 查看授权列表")
    ap.add_argument("--list-users", action="store_true", help="列出用户（需管理员）")
    ap.add_argument("--create-user", nargs=2, metavar=("NAME", "PASSWORD"),
                    help="创建用户（需管理员）")
    ap.add_argument("--stats", action="store_true", help="系统统计（需管理员）")
    args = ap.parse_args()

    client = APIClient(args.host)

    # 健康检查（无需登录）
    if args.health:
        try:
            h = client.health()
            print(f"{GREEN}服务状态: {h['status']}{NC}")
            print(f"  LLM:       {h['llm_provider']} / {h['llm_model']}  "
                  f"{'✓' if h['llm_ok'] else '✗'}")
            print(f"  Embedding: {h['embedding_provider']} (dim={h['embedding_dim']})")
            print(f"  向量库:    {'✓' if h['vector_store_ok'] else '✗'}")
            web_ok = h.get("web_search_enabled", False)
            print(f"  联网搜索:  {h['web_search_provider']}  "
                  f"{'✓' if web_ok else '✗ 不可用'}")
            for w in h.get("warnings", []):
                print(f"{YELLOW}  ⚠ {w}{NC}")
            return 0 if h["status"] == "ok" else 1
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    # 登录
    username = args.user or input("用户名: ").strip()
    password = args.password or getpass.getpass("密码: ")

    try:
        client.login(username, password)
        user = client.me()
    except RuntimeError as e:
        print(f"{RED}登录失败: {e}{NC}")
        return 1

    # 交互模式会打印 banner（已含用户信息），其余命令这里给个登录确认。
    # 判据：除 --host/-u/-p 外的参数一旦出现，就是非交互调用。
    conn_opts = {"--host", "-u", "--user", "-p", "--password"}
    has_action = any(
        a != "-h" and (a.startswith("-") and a.split("=")[0] not in conn_opts)
        for a in sys.argv[1:]
    )
    if has_action:
        role = "管理员" if user.get("role") == "admin" else "普通用户"
        print(f"{GREEN}✓ 已登录: {user.get('username')} ({role}){NC}")

    # 管理命令
    if args.list_users:
        try:
            res = client.list_users()
            print(f"{BOLD}用户列表（共 {res['total']}）{NC}")
            print(f"  {'用户名':<20} {'角色':<8} {'状态':<10} {'创建时间'}")
            print("  " + "-" * 62)
            for u in res["items"]:
                print(f"  {u['username']:<20} {u['role']:<8} {u['status']:<10} "
                      f"{u['created_at'][:19]}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    if args.create_user:
        name, pwd = args.create_user
        try:
            u = client.create_user(name, pwd)
            print(f"{GREEN}✓ 用户已创建: {u['username']} (角色: {u['role']}){NC}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    if args.stats:
        try:
            s = client.stats()
            print(f"{BOLD}系统统计{NC}")
            print(f"  用户:      {s['users']['total']} (活跃 {s['users']['active']})")
            print(f"  知识库:    {s['knowledge_bases']}")
            print(f"  文档:      {s['documents']}")
            print(f"  分块:      {s['chunks']}")
            print(f"  会话:      {s['chat_sessions']}")
            print(f"  消息:      {s['chat_messages']}")
            print(f"  联网次数:  {s['web_search_used']}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    # ---------------- 知识库管理命令 ----------------

    if args.list_kbs:
        try:
            kbs = client.list_kbs().get("items", [])
            if not kbs:
                print(f"{YELLOW}没有可访问的知识库{NC}")
                return 0
            print(f"{BOLD}知识库（共 {len(kbs)}）{NC}")
            print(f"  {'名称':<22} {'文档':<6} {'分块':<8} {'公开':<6} {'ID'}")
            print("  " + "-" * 72)
            for kb in kbs:
                pub = "是" if kb.get("is_public") else "否"
                print(f"  {kb['name']:<22} {kb['doc_count']:<6} {kb['chunk_count']:<8} "
                      f"{pub:<6} {kb['id']}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    if args.create_kb:
        try:
            kb = client.create_kb(args.create_kb)
            print(f"{GREEN}✓ 知识库已创建: {kb['name']}{NC}")
            print(f"{DIM}  ID: {kb['id']}{NC}")
            print(f"{DIM}  下一步: --kb {kb['name']} --upload 文件路径{NC}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    # 以下命令都需要 --kb
    needs_kb = (args.upload or args.docs or args.wait or args.delete_kb
                or args.grant or args.perms)
    if needs_kb:
        if not args.kb:
            print(f"{RED}需要指定知识库: 加 --kb <名称或ID>{NC}")
            return 1
        try:
            kb = client.resolve_kb(args.kb)
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1
        if kb is None:
            print(f"{RED}未找到知识库: {args.kb}{NC}")
            return 1

        try:
            if args.delete_kb:
                res = client.delete_kb(kb["id"])
                print(f"{GREEN}✓ {res.get('message', '已删除')}{NC}")
                return 0

            if args.docs:
                res = client.list_docs(kb["id"])
                docs = res.get("items", [])
                if not docs:
                    print(f"{YELLOW}该知识库暂无文档{NC}")
                    return 0
                print(f"{BOLD}{kb['name']} 的文档（共 {len(docs)}）{NC}")
                for d in docs:
                    flag = {"ready": "✓", "failed": "✗"}.get(d.get("status"), "…")
                    print(f"  {flag} {d['filename']:<32} {d.get('status'):<10} "
                          f"分块 {d.get('chunk_count', 0)}")
                    if d.get("error_message"):
                        print(f"      {RED}{d['error_message']}{NC}")
                _print_index_summary(res)
                return 0

            if args.upload:
                ok, fail = 0, 0
                for fp in args.upload:
                    if not os.path.isfile(fp):
                        print(f"{RED}✗ 文件不存在: {fp}{NC}")
                        fail += 1
                        continue
                    try:
                        d = client.upload_doc(kb["id"], fp)
                        print(f"{GREEN}✓ 已上传: {d.get('filename', os.path.basename(fp))}"
                              f"{NC}  {DIM}(status={d.get('status')}){NC}")
                        ok += 1
                    except RuntimeError as e:
                        print(f"{RED}✗ {os.path.basename(fp)} 上传失败: {e}{NC}")
                        fail += 1
                if ok:
                    print(f"{YELLOW}  索引为异步执行，用 --wait 等待完成后再提问{NC}")
                return 1 if fail else 0

            if args.wait:
                return _wait_for_index(client, kb["id"], kb["name"])

            if args.perms:
                res = client.list_permissions(kb["id"])
                items = res.get("items", res) if isinstance(res, dict) else res
                if not items:
                    print(f"{YELLOW}该知识库暂无授权（仅创建者/管理员可访问）{NC}")
                    return 0
                print(f"{BOLD}{kb['name']} 的授权{NC}")
                for p in items:
                    print(f"  {p.get('username') or p.get('user_id')}  "
                          f"读={'是' if p.get('can_read') else '否'}")
                return 0

            if args.grant:
                uid = client.find_user_id(args.grant)
                if uid is None:
                    print(f"{RED}用户不存在: {args.grant}{NC}")
                    return 1
                client.grant_permission(kb["id"], uid)
                print(f"{GREEN}✓ 已授权 {args.grant} 只读访问 '{kb['name']}'{NC}")
                return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    # 选择知识库
    kb_id: str | None = None
    kb_name = ""
    try:
        kbs = client.list_kbs().get("items", [])

        if args.kb:
            matched = [k for k in kbs if args.kb.lower() in k["name"].lower() or k["id"] == args.kb]
            if not matched:
                print(f"{RED}未找到知识库: {args.kb}{NC}")
                return 1
            kb_id, kb_name = matched[0]["id"], matched[0]["name"]
        elif len(kbs) == 1:
            kb_id, kb_name = kbs[0]["id"], kbs[0]["name"]
        elif len(kbs) > 1:
            print(f"{BOLD}请选择知识库{NC}")
            for i, kb in enumerate(kbs, 1):
                print(f"  {i}. {kb['name']}  "
                      f"{DIM}({kb['doc_count']} 文档 / {kb['chunk_count']} 分块){NC}")
            sel = input("编号: ").strip()
            if not sel.isdigit() or not (1 <= int(sel) <= len(kbs)):
                print(f"{RED}无效选择{NC}")
                return 1
            kb_id, kb_name = kbs[int(sel) - 1]["id"], kbs[int(sel) - 1]["name"]
    except RuntimeError as e:
        print(f"{RED}{e}{NC}")
        return 1

    # 单次提问需要知识库
    if args.ask and not kb_id:
        print(f"{RED}无法提问：没有可用的知识库。{NC}")
        return 1

    # 单次提问
    if args.ask:
        try:
            res = client.ask(args.ask, kb_id, enable_web=not args.no_web)
            print(res["answer"])
            if res.get("sources"):
                print(f"\n{DIM}来源: {len(res['sources'])} 条{NC}")
            return 0
        except RuntimeError as e:
            print(f"{RED}{e}{NC}")
            return 1

    # 交互模式
    interactive(client, user, kb_id, kb_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

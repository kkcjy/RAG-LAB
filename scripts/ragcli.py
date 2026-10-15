#!/usr/bin/env python3
"""
RAG-lab 终端客户端

朋友通过终端连接问答的使用方式。仅依赖标准库（urllib），
不需要安装任何第三方包。

用法:
    # 登录并进入交互式问答
    python3 scripts/ragcli.py --host http://1.2.3.4:8000

    # 直接带账号密码（免交互输入）
    python3 scripts/ragcli.py --host http://1.2.3.4:8000 -u user1 -p pass123

    # 单次提问
    python3 scripts/ragcli.py --host http://1.2.3.4:8000 -u user1 -p pass --ask "报销流程是什么"

    # 管理命令
    python3 scripts/ragcli.py --host ... -u admin -p pass --list-users
    python3 scripts/ragcli.py --host ... -u admin -p pass --create-user bob secret123
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request

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

        if stream:
            return urllib.request.urlopen(req, timeout=300)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
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

    def list_docs(self, kb_id: str) -> dict:
        return self._request("GET", f"/api/kb/{kb_id}/documents")

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
def print_banner(user: dict, host: str) -> None:
    role = "管理员" if user.get("role") == "admin" else "普通用户"
    print(f"{GREEN}{'=' * 62}{NC}")
    print(f"{BOLD}  RAG-lab 终端问答{NC}")
    print(f"  服务器: {host}")
    print(f"  当前用户: {user.get('username')} ({role})")
    print(f"{GREEN}{'=' * 62}{NC}")
    print(f"{DIM}  命令: /help 帮助 | /kb 选择知识库 | /web 切换联网 | /new 新会话 | /quit 退出{NC}")


def print_help() -> None:
    print(f"""
{BOLD}可用命令{NC}
  /help            显示本帮助
  /kb              列出并选择知识库
  /kb <名称或ID>   直接切换到指定知识库
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
                        print(f"{GREEN}RAG-lab{NC}> ", end="", flush=True)

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
    ap = argparse.ArgumentParser(description="RAG-lab 终端客户端")
    ap.add_argument("--host", default="http://localhost:8000", help="服务地址")
    ap.add_argument("-u", "--user", help="用户名")
    ap.add_argument("-p", "--password", help="密码")
    ap.add_argument("--kb", help="知识库名称或ID，启动时直接选择")
    ap.add_argument("--ask", help="单次提问后退出")
    ap.add_argument("--no-web", action="store_true", help="禁用联网搜索")
    ap.add_argument("--health", action="store_true", help="仅检查服务健康状态")
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
            print(f"  联网搜索:  {h['web_search_provider']}")
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

    # 选择知识库
    kb_id: str | None = None
    kb_name = ""
    try:
        kbs = client.list_kbs().get("items", [])
        if not kbs:
            print(f"{YELLOW}当前账号没有可访问的知识库。{NC}")
            if user.get("role") == "admin":
                print(f"{DIM}提示: 使用 API 创建知识库，或联系管理员授权。{NC}")
                print(f"{DIM}      POST {args.host}/api/kb  {{\"name\": \"我的知识库\"}}{NC}")
            return 1

        if args.kb:
            matched = [k for k in kbs if args.kb.lower() in k["name"].lower() or k["id"] == args.kb]
            if not matched:
                print(f"{RED}未找到知识库: {args.kb}{NC}")
                return 1
            kb_id, kb_name = matched[0]["id"], matched[0]["name"]
        elif len(kbs) == 1:
            kb_id, kb_name = kbs[0]["id"], kbs[0]["name"]
        else:
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

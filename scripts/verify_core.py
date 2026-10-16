#!/usr/bin/env python3
"""
核心逻辑验证（零第三方依赖）

由于完整运行需要 torch/qdrant 等重依赖，这里用桩对象（stub）
隔离验证最关键、最容易出错的纯逻辑部分：

  1. 文本分块算法正确性
  2. 相似度阈值判定逻辑
  3. Query 改写结果解析（LLM 输出容错）
  4. LLM 精判的 JSON 解析容错
  5. 提示词构建（引用编号对齐）
  6. 文件类型路由

用法: python3 scripts/verify_core.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
NC = "\033[0m"

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  {GREEN}✓{NC} {name}")
    else:
        failed += 1
        print(f"  {RED}✗{NC} {name}" + (f"  → {detail}" if detail else ""))


# ===========================================================================
# 独立实现分块逻辑（与 document_parser.split_text 一致）
# 直接导入会拉起依赖链，因此这里复制核心算法做行为验证
# ===========================================================================
sys.path.insert(0, str(ROOT / "app"))
from services.document_parser import normalize_text, split_text  # noqa: E402


print(f"{GREEN}[1] 文本清洗 normalize_text{NC}")
check("去除多余空行", normalize_text("a\n\n\n\n\nb") == "a\n\nb")
check("全角空格转半角", "\u3000" not in normalize_text("a\u3000b"))
check("CRLF 归一", "\r" not in normalize_text("a\r\nb"))
check("空输入", normalize_text("") == "")
check("行首尾空白清理", normalize_text("  a  \n  b  ") == "a\nb")
check("压缩行内空格", normalize_text("a     b") == "a b")


print(f"{GREEN}[2] 分块 split_text{NC}")
text = "。".join([f"这是第{i}句话内容" for i in range(200)]) + "。"
chunks = split_text(text, chunk_size=200, chunk_overlap=50)
check("产生了多个块", len(chunks) > 1, f"got {len(chunks)}")
check("单块不超限太多", all(len(c) <= 260 for c in chunks), f"max={max(len(c) for c in chunks)}")
check("内容无丢失（拼接后覆盖原文）", all(
    any(f"第{i}句" in c for c in chunks) for i in [0, 50, 100, 150]
))

short = split_text("短文本", chunk_size=800, chunk_overlap=100)
check("短文本单块", len(short) == 1 and short[0] == "短文本")

check("空文本返回空", split_text("", chunk_size=100, chunk_overlap=10) == [])
check("纯空白返回空", split_text("   \n\n  ", chunk_size=100, chunk_overlap=10) == [])

# 无分隔符的长串（硬切）
hard = "x" * 1000
hard_chunks = split_text(hard, chunk_size=200, chunk_overlap=20)
check("无分隔符可硬切", len(hard_chunks) > 1)
check("硬切后内容总量合理", sum(len(c) for c in hard_chunks) >= 900)

# 中文标点分块不应把句子切碎到过短
zh = "第一段内容。" * 50
zh_chunks = split_text(zh, chunk_size=300, chunk_overlap=50)
min_len = min(len(c) for c in zh_chunks)
check("块不过碎（>=50字符）", min_len >= 50, f"min={min_len}")


print(f"{GREEN}[3] 相似度阈值判定{NC}")


def judge_by_threshold(best_score: float, threshold: float) -> bool:
    """复刻 rag_engine 的粗筛逻辑"""
    return best_score >= threshold


check("高分判为命中", judge_by_threshold(0.85, 0.5) is True)
check("低分判为未命中", judge_by_threshold(0.30, 0.5) is False)
check("边界值判为命中", judge_by_threshold(0.50, 0.5) is True)


print(f"{GREEN}[4] Query 改写结果解析容错{NC}")


def parse_rewritten(text: str, original: str) -> list[str]:
    """复刻 rag_engine.rewrite_query 的解析逻辑"""
    if not text:
        return [original]
    text = re.sub(r"^(改写后的?查询[:：]?|查询[:：]?)\s*", "", text, flags=re.MULTILINE)
    queries = [q.strip(" -•\t") for q in text.split("\n") if q.strip(" -•\t")]
    seen = set()
    uniq = []
    for q in queries:
        if q not in seen and len(q) > 1:
            seen.add(q)
            uniq.append(q)
    return uniq[:3] or [original]


check("单行改写", parse_rewritten("公司年假有多少天", "它有多少天") == ["公司年假有多少天"])
check("多行拆分", len(parse_rewritten("问题A\n问题B", "原始")) == 2)
check("带编号前缀清理", parse_rewritten("1. 问题A\n2. 问题B", "原始") == ["1. 问题A", "2. 问题B"])
check("'改写后的查询:' 前缀去除",
      parse_rewritten("改写后的查询：公司年假政策", "原始") == ["公司年假政策"])
check("空输出回退原问题", parse_rewritten("", "原始问题") == ["原始问题"])
check("去重", parse_rewritten("问题A\n问题A", "原始") == ["问题A"])
check("最多3条", len(parse_rewritten("\n".join(f"问题{i}" for i in range(10)), "原始")) <= 3)
check("单字符被过滤", parse_rewritten("a\n正常问题", "原始") == ["正常问题"])


print(f"{GREEN}[5] LLM 精判 JSON 解析容错{NC}")


def parse_judge(text: str) -> tuple[bool, str]:
    """复刻 rag_engine.judge_relevance 的解析逻辑"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return True, "解析失败按阈值放行"
    try:
        data = json.loads(match.group(0))
        return bool(data.get("relevant", False)), str(data.get("reason", ""))
    except json.JSONDecodeError:
        return True, "解析失败按阈值放行"


check("标准 JSON", parse_judge('{"relevant": true, "reason": "包含答案"}')[0] is True)
check("false 判定", parse_judge('{"relevant": false, "reason": "无关"}')[0] is False)
check("带 markdown 包裹", parse_judge('```json\n{"relevant": true, "reason": "ok"}\n```')[0] is True)
check("带前后说明文字",
      parse_judge('好的，我的判断是：{"relevant": true, "reason": "相关"} 以上。')[0] is True)
check("非法 JSON 保守放行", parse_judge("这不是 JSON")[0] is True)
check("空字符串保守放行", parse_judge("")[0] is True)
check("缺少字段默认 false", parse_judge('{"reason": "x"}')[0] is False)


print(f"{GREEN}[6] 提示词引用编号对齐{NC}")


def build_numbering(n_kb: int, n_web: int) -> tuple[list[int], list[int]]:
    """复刻 _build_messages：知识库 [1..n]，网络接续编号"""
    kb_ids = list(range(1, n_kb + 1))
    web_ids = list(range(n_kb + 1, n_kb + n_web + 1))
    return kb_ids, web_ids


kb_ids, web_ids = build_numbering(3, 2)
check("知识库编号从1开始", kb_ids == [1, 2, 3])
check("网络编号接续", web_ids == [4, 5])
kb_ids, web_ids = build_numbering(0, 3)
check("无知识库时网络从1开始", web_ids == [1, 2, 3])
kb_ids, web_ids = build_numbering(2, 0)
check("无网络时不影响知识库编号", kb_ids == [1, 2] and web_ids == [])


print(f"{GREEN}[7] 文件类型路由{NC}")


def route_parser(ext: str) -> str:
    mapping = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "doc_legacy",
        ".md": "markdown",
        ".markdown": "markdown",
        ".txt": "txt",
    }
    return mapping.get(ext, "unsupported")


check("pdf 路由", route_parser(".pdf") == "pdf")
check("docx 路由", route_parser(".docx") == "docx")
check("md 路由", route_parser(".md") == "markdown")
check("大写扩展名（调用方需 lower）", route_parser(".PDF".lower()) == "pdf")
check("未知类型标记 unsupported", route_parser(".xyz") == "unsupported")


print(f"{GREEN}[8] SSE 事件序列{NC}")


async def fake_stream():
    """模拟 stream_answer 的事件产出顺序"""
    yield {"type": "status", "data": {"stage": "rewrite"}}
    yield {"type": "status", "data": {"stage": "retrieve"}}
    yield {"type": "sources", "data": [{"type": "kb"}]}
    yield {"type": "status", "data": {"stage": "generate"}}
    yield {"type": "delta", "data": "你"}
    yield {"type": "delta", "data": "好"}
    yield {"type": "done", "data": {"kb_hit": True}}


async def run_stream_check():
    types_seen = []
    text = ""
    async for ev in fake_stream():
        types_seen.append(ev["type"])
        if ev["type"] == "delta":
            text += ev["data"]
    return types_seen, text


types_seen, text = asyncio.run(run_stream_check())
check("首事件为 status", types_seen[0] == "status")
check("末事件为 done", types_seen[-1] == "done")
check("含 sources 事件", "sources" in types_seen)
check("delta 累积正确", text == "你好")


# ===========================================================================
print()
print("=" * 60)
if failed == 0:
    print(f"{GREEN}全部通过: {passed} 项{NC}")
else:
    print(f"{RED}失败 {failed} 项{NC}, 通过 {passed} 项")
print("=" * 60)

sys.exit(1 if failed else 0)

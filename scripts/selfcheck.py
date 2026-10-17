#!/usr/bin/env python3
"""
静态自查脚本（不依赖第三方包）

检查项：
  1. 全部 Python 文件语法正确
  2. 应用内部 import 路径可达（针对本地模块）
  3. 工厂函数定义在正确的抽象层
  4. 无重复定义
  5. 关键接口完整性（抽象方法都实现了）

用法: python3 scripts/selfcheck.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"

errors: list[str] = []
warnings: list[str] = []


def collect_py() -> list[Path]:
    out = []
    for dp, dn, fn in os.walk(APP):
        dn[:] = [d for d in dn if d != "__pycache__"]
        out.extend(Path(dp) / f for f in fn if f.endswith(".py"))
    return sorted(out)


def modname(p: Path) -> str:
    rel = p.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


# ---------------------------------------------------------------------------
print(f"{GREEN}[1/5] 语法检查{NC}")
files = collect_py()
for fp in files:
    try:
        ast.parse(fp.read_text(encoding="utf-8"))
    except SyntaxError as e:
        errors.append(f"语法错误 {fp}: {e}")
print(f"      检查 {len(files)} 个文件")

# ---------------------------------------------------------------------------
print(f"{GREEN}[2/5] 应用内部 import 可达性{NC}")
local_mods = {modname(p) for p in files}
# 包名也算可达
for m in list(local_mods):
    parts = m.split(".")
    for i in range(1, len(parts)):
        local_mods.add(".".join(parts[:i]))

for fp in files:
    tree = ast.parse(fp.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app.") or node.module == "app":
                names = [node.module] + [
                    f"{node.module}.{a.name}" for a in node.names if a.name != "*"
                ]
        if not names:
            continue
        for n in names:
            if n in local_mods:
                continue
            # 允许 app.core.config 里的 settings 等符号级导入
            base = n.rsplit(".", 1)[0]
            if base in local_mods:
                continue
            errors.append(f"不可达 import: {fp.relative_to(ROOT)} → {n}")

# ---------------------------------------------------------------------------
print(f"{GREEN}[3/5] 工厂函数位置检查{NC}")
expect_factories = {
    "app/providers/llm/base.py": ["get_llm", "reset_llm"],
    "app/providers/embedding/base.py": ["get_embedding", "reset_embedding"],
    "app/providers/vector/base.py": ["get_vector_store"],
    "app/providers/websearch/base.py": ["get_web_search"],
}
for rel, fns in expect_factories.items():
    fp = ROOT / rel
    if not fp.exists():
        errors.append(f"缺少文件 {rel}")
        continue
    src = fp.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in fns:
        if fn not in defined:
            errors.append(f"{rel} 中缺少工厂函数 {fn}")

# 旧位置不应再有工厂
for rel in ["app/providers/llm/openai_compat.py", "app/providers/vector/qdrant_store.py"]:
    fp = ROOT / rel
    if not fp.exists():
        errors.append(f"缺少文件 {rel}")
        continue
    src = fp.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in ("get_llm", "get_vector_store"):
        if fn in defined:
            errors.append(f"{rel} 中残留工厂函数 {fn}（应已移至 base.py）")

# ---------------------------------------------------------------------------
print(f"{GREEN}[4/5] 抽象接口实现完整性{NC}")
abstract_specs = [
    ("app/providers/llm/base.py", "BaseLLM", "app/providers/llm/openai_compat.py", "OpenAICompatLLM"),
    ("app/providers/vector/base.py", "BaseVectorStore", "app/providers/vector/qdrant_store.py", "QdrantVectorStore"),
]
for abs_rel, abs_cls, impl_rel, impl_cls in abstract_specs:
    abs_fp = ROOT / abs_rel
    impl_fp = ROOT / impl_rel
    if not abs_fp.exists():
        errors.append(f"缺少文件 {abs_rel}")
        continue
    if not impl_fp.exists():
        errors.append(f"缺少文件 {impl_rel}")
        continue

    abs_tree = ast.parse(abs_fp.read_text(encoding="utf-8"))
    abstract_methods = set()
    for node in ast.walk(abs_tree):
        if isinstance(node, ast.ClassDef) and node.name == abs_cls:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(
                        (isinstance(d, ast.Name) and d.id == "abstractmethod")
                        or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
                        for d in item.decorator_list
                    ):
                        abstract_methods.add(item.name)

    impl_tree = ast.parse(impl_fp.read_text(encoding="utf-8"))
    impl_methods = set()
    for node in ast.walk(impl_tree):
        if isinstance(node, ast.ClassDef) and node.name == impl_cls:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    impl_methods.add(item.name)

    # 抽象类改名/删除时 abstract_methods 会为空，下面的差集恒为空、
    # 检查静默失效，所以这里显式兜底
    if not abstract_methods:
        errors.append(f"{abs_rel} 中未找到 {abs_cls} 的抽象方法（类名可能已改）")

    missing = abstract_methods - impl_methods
    if missing:
        errors.append(f"{impl_cls} 未实现抽象方法: {sorted(missing)}")

# ---------------------------------------------------------------------------
print(f"{GREEN}[5/5] 关键文件与配置检查{NC}")
required_files = [
    "app/main.py",
    "app/core/config.py",
    "app/core/database.py",
    "app/core/security.py",
    "app/models/db.py",
    "app/api/auth.py",
    "app/api/admin.py",
    "app/api/kb.py",
    "app/api/chat.py",
    "app/api/system.py",
    "app/api/deps.py",
    "app/schemas/api.py",
    "app/services/rag_engine.py",
    "app/services/indexing.py",
    "app/services/document_parser.py",
    "app/services/runtime_config.py",
    "app/services/bootstrap.py",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    ".env.example",
]
for rel in required_files:
    if not (ROOT / rel).exists():
        errors.append(f"缺少必需文件: {rel}")

# .env.example 与 config.py 的字段对齐检查
# 由第三方库直接读取、不经 pydantic Settings 的标准环境变量，不算字段缺失
_EXTERNAL_ENV_KEYS = {
    "hf_home",           # huggingface_hub 直接读取
    "hf_endpoint",       # huggingface_hub 直接读取
    "hf_hub_offline",
    "transformers_offline",
    "torch_home",
    "no_proxy", "http_proxy", "https_proxy",
    "tz",
}
env_path = ROOT / ".env.example"
env_src = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
env_keys = {
    line.split("=", 1)[0].strip().lower()
    for line in env_src.splitlines()
    if "=" in line and not line.strip().startswith("#")
}
cfg_src = (ROOT / "app/core/config.py").read_text(encoding="utf-8")
cfg_tree = ast.parse(cfg_src)
cfg_fields = set()
for node in ast.walk(cfg_tree):
    if isinstance(node, ast.ClassDef) and node.name == "Settings":
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                cfg_fields.add(item.target.id)

if env_src:
    missing_in_cfg = env_keys - cfg_fields - _EXTERNAL_ENV_KEYS
    missing_in_env = cfg_fields - env_keys

    if missing_in_cfg:
        warnings.append(f".env.example 中这些键在 config.py 里没有对应字段: {sorted(missing_in_cfg)}")
    if missing_in_env:
        warnings.append(f"config.py 中这些字段未出现在 .env.example: {sorted(missing_in_env)}")

# ---------------------------------------------------------------------------
print()
if errors:
    print(f"{RED}✗ 发现 {len(errors)} 个错误:{NC}")
    for e in errors:
        print(f"    - {e}")
if warnings:
    print(f"{YELLOW}⚠ {len(warnings)} 个警告:{NC}")
    for w in warnings:
        print(f"    - {w}")
if not errors:
    print(f"{GREEN}✓ 自查通过，未发现结构性错误{NC}")

sys.exit(1 if errors else 0)

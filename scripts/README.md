# scripts 目录说明

本目录包含启动脚本、终端客户端和验证工具。

**这些是「测试工具」，不是测试残留** —— 保留它们是让你以后改代码、调参数、
换环境时能快速验证，而不是每次手动点着测。

---

## 什么时候用哪个

| 场景 | 用这个 | 耗时 |
|---|---|---|
| 改完代码，检查有没有语法/结构错误 | `selfcheck.py` | 1 秒 |
| 改了分块逻辑、提示词、解析逻辑 | `verify_core.py` | 1 秒 |
| 改完功能，想确认整体没坏 | `e2e_test.py` | 1-2 分钟 |
| 用户反馈"答不上来"，要排查检索 | `test_query_rewrite.py` | 30 秒 |
| 怀疑流式输出有问题 | `test_stream.py` | 30 秒 |
| 首次部署 / 换机器，确认环境OK | `start.sh` + `e2e_test.py` | 5 分钟 |
| 建库 / 传文档 / 授权 / 问答 | `ragcli.py` | - |

---

## 脚本清单

### start.sh — 一键启动

```bash
./scripts/start.sh            # 启动
./scripts/start.sh --build    # 重新构建镜像后启动
```

检查 `.env` 是否存在、必填项是否为空、Docker 是否可用，然后启动并等待就绪。

---

### ragcli.py — 终端问答客户端（给用户/朋友用）

纯标准库实现，对方只要有 Python 3.8+ 就能跑，无需装任何依赖。

```bash
# 交互式问答
python3 scripts/ragcli.py --host http://服务器IP:18000

# 单次提问（适合脚本集成）
python3 scripts/ragcli.py --host http://IP:18000 -u 用户名 -p 密码 --ask "年假多少天"

# 检查服务状态（无需登录）
python3 scripts/ragcli.py --host http://IP:18000 --health
```

**管理知识库/用户**（建库、上传文档、授权、统计等）看 `--help`：

```bash
python3 scripts/ragcli.py --help
```

典型上手顺序：`--create-kb` 建库 → `--kb X --upload 文件` 传文档 →
`--kb X --wait` 等索引（必须等，否则检索不到）→ `--kb X --grant 用户` 授权 → 提问。

交互模式下的命令：`/kb` 选知识库、`/new-kb` 建库、`/add` 上传文档、`/docs` 看文档状态、
`/wait` 等索引、`/web on|off` 切联网、`/sources` 看引用来源、`/new` 新会话、`/quit` 退出。

> 没有知识库时不会退出，会进入交互模式并提示用 `/new-kb` 创建。

把 `ragcli.py` 单个文件发给朋友即可，不需要给整个项目。

---

### selfcheck.py — 静态结构自查（零依赖，秒级）

**什么时候用**：改完代码想快速确认没写错。不需要启动服务，不需要装依赖。

```bash
python3 scripts/selfcheck.py
```

检查 5 项：

1. 全部 Python 文件语法正确
2. 应用内部 import 路径可达（会不会有写错的 import）
3. 工厂函数在正确的位置（架构约定）
4. 抽象接口的实现类是否实现了全部抽象方法
5. 必需文件是否齐全、`.env.example` 与 `config.py` 字段是否对齐

---

### verify_core.py — 核心逻辑验证（零依赖，秒级）

**什么时候用**：改了分块参数、提示词模板、LLM 输出解析逻辑之后。

```bash
python3 scripts/verify_core.py
```

46 项断言，覆盖最容易出错的纯逻辑：

- 中文分块边界（会不会把句子切断、块是否过碎）
- 文本清洗（全角空格、CRLF、多余空行）
- LLM 输出容错解析（JSON 被 markdown 包裹、前后带废话、非法输入）
- Query 改写的多行结果解析
- 引用编号对齐（知识库 [1..n]，网络接续 n+1）
- SSE 事件序列顺序

**为什么这些值得单独测**：它们不依赖网络和模型，跑得飞快，
但出问题最难排查（比如 LLM 返回的 JSON 偶尔被 ``` 包裹，导致精判失效）。

---

### e2e_test.py — 端到端集成测试（需服务运行）

**什么时候用**：改完功能后做回归验证；或者部署到新环境后确认一切正常。

```bash
python3 scripts/e2e_test.py              # 用默认测试文档
python3 scripts/e2e_test.py --keep       # 保留测试数据，便于人工检查
python3 scripts/e2e_test.py --docs 路径   # 指定自己的文档目录
```

26 项断言，真实走完整链路：

```
登录 → 建知识库 → 上传 5 个文档 → 等待向量化 → 校验分块入库
     → 5 个检索问题验证命中 → LLM 生成回答 → 删除知识库
```

未配置 LLM key 时会自动跳过生成环节，仍能验证检索链路。

---

### test_stream.py — SSE 流式问答验证（需服务 + LLM key）

**什么时候用**：怀疑流式输出有问题（比如用户说"回答是一次性蹦出来的"）。

```bash
python3 scripts/test_stream.py
```

验证事件序列完整性，并判断是否**真流式**（有 128 次 delta 事件、
分散在多个时间片），而不是后端一次性返回。

知识库自动选取：优先 `/tmp/kb_id.txt`，否则取第一个含已就绪文档的库。
都没有时会提示先跑 `e2e_test.py --keep` 准备数据。

---

### test_query_rewrite.py — Query 改写与相关度判定验证

**什么时候用**：用户反馈"问什么都答不上来"或"答非所问"时排查。

```bash
python3 scripts/test_query_rewrite.py
```

无需预先准备数据：找不到可用知识库时会自动建一个临时库、上传测试文档、
等索引完成后跑验证，结束后自动删除临时库。

5 个场景，会打印每一步的中间结果：

```
【口语化】
  原问题    : 那个报销的事儿咋弄啊
  改写结果  : ['报销流程是什么', '报销怎么办理']
  判定      : kb_hit=True  片段[1]包含报销流程的具体信息...
  top1      : 员工手册.md (score=0.5697)
```

靠这个能快速判断问题出在哪一环：改写没生效？检索没命中？还是精判误判？

---

## 验证分层设计

```
      快 ←──────────────────────────────→ 慢
   selfcheck    verify_core    test_query_rewrite    e2e_test
     1秒           1秒               30秒             1-2分钟
       │            │                  │                │
   代码结构      纯逻辑           单个功能点        完整链路
   零依赖        零依赖           需服务+key       需服务
```

**建议节奏**：改代码 → `selfcheck.py`；改逻辑 → `verify_core.py`；
改完一段功能 → `e2e_test.py`。前面两个秒级，随手就能跑。

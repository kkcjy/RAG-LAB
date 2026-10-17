# 验证记录

本文档记录 RAG-LAB 的实际验证情况，区分「已实测通过」与「未验证」，
避免把未验证的部分误认为可用。

验证日期：2026-10-15（首次验证）/ 2026-10-16（代码审查与缺陷修复后复验）
         / 2026-10-17（换机复验：补配 Tavily key，端到端 25/25）
验证环境：Ubuntu 20.04 / Docker 28.1.1 / Compose v2.35.1 / 15GB 内存 / 无可用 GPU
2026-10-17 复验环境：WSL2 Ubuntu 22.04 / Docker 28.4.0 / Compose v2.39.4 /
        mirrored 网络模式（宿主机端口需避开被 Windows 占用的 8000、6379，
        本项目改发布为 backend 18000、redis 16379）

---

## 一、已验证通过

### 1.1 基础设施

| 组件 | 版本 | 状态 | 验证方式 |
|---|---|---|---|
| PostgreSQL | 16.15-alpine | healthy | `psql SELECT version()` |
| Qdrant | v1.12.4 | healthy | `GET /collections` 返回 200 |
| Redis | 7-alpine | healthy | `redis-cli ping` → PONG |
| 容器网络 | bridge | 通 | 临时容器内可解析 postgres/qdrant/redis 主机名 |

### 1.2 数据模型

```
✓ 9 张表全部建成
  users, knowledge_bases, kb_permissions, documents, chunks,
  chat_sessions, chat_messages, system_config, audit_logs
✓ 默认管理员自动创建（admin）
✓ 11 条默认系统配置写入并生效
```

### 1.3 Embedding 模型

```
✓ BAAI/bge-m3 下载完成（2.2GB，经 hf-mirror.com）
✓ 模型加载成功，向量维度 = 1024
✓ 输出示例: [0.0076, 0.0296, -0.0068, -0.0338, -0.0109]
✓ 归一化正常
```

⚠️ 重要：直连 huggingface.co 在国内网络下不可达，必须走镜像。
项目已默认配置 `HF_ENDPOINT=https://hf-mirror.com`。

### 1.4 文档解析（真实文件测试）

| 格式 | 文件 | 结果 | 解析器 |
|---|---|---|---|
| PDF | DSR_protocol.pdf | ✓ 67013 字符，97 块，英文论文无乱码 | pymupdf |
| Word | 供应商管理办法.docx | ✓ 343 字符，表格正确提取 | python-docx |
| Markdown | 员工手册.md | ✓ 763 字符 | plain |
| TXT | 系统说明.txt | ✓ 634 字符 | plain |

Word 表格提取效果：

```
[表格 1]
评级 | 综合得分 | 采购策略
A级 | 90分以上 | 优先采购，可放宽账期
B级 | 75至89分 | 正常采购
C级 | 75分以下 | 限制采购，限期整改
```

**扫描件 PDF（OCR）**

生成一份纯图片 PDF（`get_text()` 提取 0 字符）验证 OCR 兜底：

| 环节 | 结果 |
|---|---|
| 触发条件 | PyMuPDF 与 pdfplumber 均无文字 → 自动转 OCR |
| 识别引擎 | RapidOCR（PP-OCRv4，纯 ONNX，模型内置） |
| 识别结果 | ✓ 157 字符，10 行中文全部正确 |
| 耗时 | 约 3 秒/页（含引擎首次初始化） |
| 检索验证 | 用 OCR 出的内容提问 → 正确回答 |

### 1.5 分块算法

```
✓ 短文档：单块不超 chunk_size
✓ 长文档：67013 字符 → 97 块（DSR_protocol.pdf，英文论文）
✓ overlap 生效（相邻块重叠 150 字符，见第四节缺陷 17）
✓ 切分点落在段落边界，不切断句子
✓ 46 项核心逻辑断言全部通过（scripts/verify_core.py）
```

### 1.6 完整链路（端到端测试）

`scripts/e2e_test.py` 实测结果：**25/25 通过**（2026-10-17 复验，含 LLM 生成与联网搜索）

```
[1] 服务健康        ✓ 健康接口 / 向量库连通 / Embedding dim=1024
[2] 认证            ✓ 管理员登录、JWT 签发
[3] 知识库          ✓ 创建成功
[4] 文档上传        ✓ 4 种文档全部上传
[5] 索引            ✓ 4/4 就绪，100 个分块，耗时 57s
[6] 分块入库校验    ✓ 统计数与实际数一致
[7] 向量检索质量    ✓ 5/5 命中正确文档
[7.5] 生成问答      ✓ LLM 生成回答，引用 5 条（需 DEEPSEEK_API_KEY）
[8] 清理            ✓ 删除知识库，向量同步清理
```

索引耗时随机器而异：首次验证 30s，换机复验 15s；DSR 论文（97 块）约 57s。

检索质量详情：

| 查询 | 期望文档 | 实际 top1 | 相似度 |
|---|---|---|---|
| 年假有多少天 | 员工手册.md | 员工手册.md | 0.621 |
| 报销流程是什么 | 员工手册.md | 员工手册.md | 0.553 |
| 向量检索的原理是什么 | 系统说明.txt | 系统说明.txt | 0.587 |
| DSR 协议解决什么问题 | DSR_protocol.pdf | DSR_protocol.pdf | 0.613 |
| 供应商评级标准 | 供应商管理办法.docx | 供应商管理办法.docx | 0.717 |

说明：语义检索有效（"年假"匹配到"年休假制度"），阈值 0.5 设置合理。

---

## 二、已验证通过（LLM 环节）

补齐 `DEEPSEEK_API_KEY` 后，以下功能已完成实测。

### 2.1 LLM 生成回答

```
✓ 健康检查 status 由 degraded → ok，llm_ok=true
✓ 生成回答 115 字符，引用 5 条，标注 [1]
✓ 内容准确：年假天数 5/10/15 天与源文档表格完全一致
```

实际输出示例：

```
根据公司考勤与休假管理制度，员工入职满一年后享受带薪年假，年假天数根据司龄计算 [1]：

| 司龄区间 | 年假天数 |
|---|---|
| 1 年以上不满 10 年 | 5 天 |
| 10 年以上不满 20 年 | 10 天 |
| 20 年以上 | 15 天 |

补充说明：年假需提前三个工作日通过 OA 系统提交申请，经直属主管批准后方可休假；
当年未休完的年假可以结转至次年第一季度，逾期作废 [1]。
```

该结果同时验证了：**Word 表格结构经解析→向量化→检索→生成全链路无信息丢失**
（模型输出的正是 Markdown 表格），以及跨片段信息整合能力。

### 2.2 SSE 流式问答

```
✓ Content-Type: text/event-stream
✓ 事件序列完整: session → status×3 → sources → delta×128 → done → finish
✓ 128 次 delta 事件，时间分布跨越 5 个 0.1s 时间片 → 确认为真实流式
✓ 首字节延迟 2.64s（含 Query 改写 + 检索 + 首 token 生成）
✓ 引用来源标注正确
```

验证脚本：`scripts/test_stream.py`

### 2.3 Query 改写

| 场景 | 原问题 | 改写结果 | 结果 |
|---|---|---|---|
| 口语化 | 那个报销的事儿咋弄啊 | 报销流程是什么 / 报销怎么办理 | ✓ |
| 多问题拆解 | 年假怎么算？报销流程是什么？ | 年假计算方法 / 报销流程 | ✓ |
| 明确问题 | 年假有多少天 | 年假天数规定 | ✓ |
| 指代消解 | 它有多少天？ | 未改写（无上下文，无法消解） | 合理 |
| 无关问题 | 今天北京的天气怎么样？ | 北京今天天气 | ✓ |

验证脚本：`scripts/test_query_rewrite.py`

### 2.4 LLM 精判相关性

精判确实在阅读片段内容后做判断，而非仅依赖分数。实际判定理由：

```
"片段[1]包含报销流程的具体信息，包括提交时限、所需材料、审批步骤等，
 足以回答用户关于报销如何办理的问题。"
```

### 2.5 无关问题正确拒绝

```
问题: 今天北京的天气怎么样？
最高相似度: 0.367（低于阈值 0.5）
判定: kb_hit=False → 不拿无关文档硬答  ✓
```

这验证了「不必硬性 top-k」的设计目标：宁可判定未命中，也不给 LLM 喂无关内容。

### 2.6 数据一致性

```
✓ 删除知识库后，Qdrant 集合同步清理，无残留
✓ 知识库统计（doc_count / chunk_count）与实际一致
```

---

### 2.7 联网搜索（Tavily）

2026-10-17 补齐 `TAVILY_API_KEY` 后实测通过。

```
✓ 健康检查 web_search_provider 由 NullWebSearch → TavilyWebSearch，
  web_search_enabled=true，warnings 清空
  （此前会提示「已启用兜底但 provider 不可用」，即第四节缺陷 15）
✓ 知识库未命中（kb_hit=False）时自动联网兜底，used_web_search=True
✓ 返回 5 条真实网页来源，含可点击 url
✓ 回答明确标注「知识库中未找到相关内容，以下信息来自网络搜索结果」
✓ 引用编号 [1][3] 正确对应网页来源
✓ Query 自动改写：口语化问题 → 检索用查询串
✓ 搜索服务故障时不再伪装成"没有结果"：web_search_error 字段给出原因
  （第四节缺陷 36），与"搜到了但无结果"可区分
```

---

## 三、未验证

| 功能 | 依赖 | 说明 |
|---|---|---|
| 本地 vLLM 接入 | ≥8GB 且算力≥7.0 的 GPU | 本机 GPU 不满足（见第五节） |

联网搜索已于 2026-10-17 实测通过，见 2.7 节。
联网搜索的代码路径在 `app/providers/websearch/base.py`；
未配置 key 时自动降级为 `NullWebSearch`，不影响其他功能，
健康检查会给出 warning 提示（见第四节缺陷 15）。

---

## 四、验证过程中修复的缺陷

以下问题均由实测暴露，静态检查无法发现：

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | huggingface.co 不可达 | 模型无法下载，核心功能瘫痪 | 配置 hf-mirror 镜像 |
| 2 | passlib 1.7.4 与 bcrypt 4.x 冲突 | 密码模块告警 | bcrypt 锁定 4.0.1 |
| 3 | Dockerfile 缺 antiword | `.doc` 解析必然失败 | 补装 antiword + 中文字体 + libgl1 |
| 4 | app 目录强制挂载 | 生产部署缺源码时容器崩溃 | 拆分为 docker-compose.dev.yml |
| 5 | indexing.py 底部 import func | 潜在 NameError | 移至文件顶部 |
| 6 | 健康检查未做异常隔离 | 少配一个 key 导致整个服务被判不可用 | 改为逐组件降级检查 |
| 7 | RAGEngine 构造时强依赖 LLM | 没配 key 时连检索都不可用 | LLM 延迟初始化，检索链路解耦 |
| 8 | 配置问题返回 500 | 误导排查方向 | 改为 503 + 可操作提示 |

第 6、7 条是架构级问题，影响面最大 —— 修复后系统具备了优雅降级能力。

### 后续代码审查又发现的缺陷

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 9 | 删除用户时其名下知识库变孤儿 | KB 的 owner 被置 NULL，普通用户失去入口且无人能管理 | 删用户时把名下 KB 移交给执行删除的管理员 |
| 10 | `write_audit` 失败时 `db.rollback()` | 会连带回滚调用方同 session 的所有未提交变更 | 改用 SAVEPOINT（`begin_nested`）隔离，只回滚审计 |
| 11 | `list_permissions` 循环查用户 | N+1 查询，授权多时打库次数线性增长 | 改为一次 join 取全 |
| 12 | 上传目录删后留空壳 | 每次建库→删库都留空目录，长期堆积 | 删文档/删库时清理空目录 |
| 13 | `BackgroundTasks` 索引不跨重启 | 上传后立刻重启，文档永久停在 pending | 启动时补偿未完成索引的文档 |
| 14 | 扫描件 PDF 无法解析 | 图片型 PDF 无文字层，直接判失败 | 集成本地 OCR 自动兜底 |
| 15 | 联网搜索静默降级 | 没配 key 时"看着能用其实不能"，无任何提示 | 健康检查加 `warnings` + 启动 WARNING |
| 16 | 容器以 root 运行 | 上传文件在宿主机归属 root，用户手动操作需 sudo | 镜像改用非 root 用户（UID/GID=1000） |

### 2026-10-17 第三轮审查发现的缺陷

本轮由 4 个并行审查分别覆盖 `app/api`、`app/services`、`app/providers`+`app/core`、`scripts`，
逐条读代码确认后修复。修复后 46 项核心断言、端到端 26/26（`--keep` 保留数据）全部通过，
检索仍为 5/5 命中，分块数与修复前一致。

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 17 | `split_text` 自然分隔符路径未应用 `chunk_overlap` | 只有无分隔符的硬切回退用了 overlap，普通文档的重叠配置完全不生效，跨块语义被切断 | 分块合并后统一拼接前块尾部，且不超过 `chunk_size` |
| 18 | `chunk_overlap >= chunk_size` 时硬切步长 ≤ 0 | 等于时抛 `ValueError` 中断索引；大于时 `range()` 为空，整段内容被**静默丢弃**。两个参数均可由管理员在运行时改且无校验 | 入口夹取到 `[0, chunk_size // 2]` |
| 19 | 文档解析在事件循环内同步执行 | 扫描件 OCR 逐页解析期间，整个 worker 停摆，并发问答与健康检查全部阻塞 | 改用 `asyncio.to_thread` |
| 20 | 清理旧向量早于创建集合 | 集合缺失时 delete 先报错，`create_kb` 承诺的"首次上传时重试"永远走不到，文档再也无法索引 | 顺序调整为先 `ensure_collection` |
| 21 | 精判结果直接 `bool()` | 模型返回字符串 `"false"`（非空串为真）被当成命中，跳过联网兜底 | 显式处理字符串真值 |
| 22 | `/api/health` 每次都真实请求 LLM | 免鉴权接口 + 容器 HEALTHCHECK 每 30s 轮询 → 持续的计费调用；LLM 响应慢于健康检查 10s 超时时容器被误判 unhealthy | 探活结果缓存 5 分钟 |
| 23 | 健康检查未把 Embedding 计入 status | Embedding 加载失败时 `status` 仍报 ok，实际检索已经不可用 | status 纳入 `dim > 0`，并补充 warning |
| 24 | `get_embedding()` 调用在 try 之外 | 该 provider 配置不全时健康检查返回 500，违背"逐组件降级"契约 | 移入 try |
| 25 | 审计 IP 取自不可控的 `X-Forwarded-For` 且不限长 | 超过 64 字符会撑爆 `ip_address` 列宽，审计记录被静默丢弃；记录内容本身也可伪造 | 取首跳并截断 64 字符 |
| 26 | `PATCH /api/admin/users` 显式传 `null` | `status`/`role` 是 NOT NULL 列，置空触发 IntegrityError 500，且绕过"不能禁用自己"的保护 | 过滤显式 null 字段 |
| 27 | 上传先整读入内存再校验大小 | 超大文件在被拒绝前已占满内存 | 改为有界读取 `read(max+1)` |
| 28 | 删除知识库先删文件、后提交事务 | 提交失败则库记录还在、文件已被删光，不可恢复 | 先提交再清理磁盘 |
| 29 | CORS `allow_origins=*` 与 `allow_credentials=True` 同开 | 任意站点可携带凭据跨域（本项目用 Bearer，影响有限） | 改为 `False` |
| 30 | `test_stream.py` / `test_query_rewrite.py` 只打印不判定 | 功能失效时仍退出 0，无法作为回归测试使用 | 增加失败项收集与退出码 |
| 31 | `start.sh` 读 .env 时缺 `\|\| true` | .env 无 `ADMIN_USERNAME` 时脚本在打印成功横幅后异常退出 1 | 补 `\|\| true` |
| 32 | ragcli 流式请求绕过错误处理 | token 过期等场景直接抛裸 traceback，而非可读错误 | 统一错误转换并关闭响应 |
| 33 | ragcli 部分上传失败仍返回 0 | 自动化无法感知失败 | 改为任一失败即返回 1 |
| 34 | selfcheck 读文件无存在性检查 | 文件缺失时抛异常，丢失此前收集的全部诊断 | 加存在性判断与兜底 |

### 2026-10-17 第四轮：架构级问题修复

第三轮列出的四项「已知未修复」本轮全部处理完毕。

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 35 | 改密码 / 重置密码后旧 JWT 仍有效 | 没有 token 吊销机制，改密或管理员重置密码都无法让已签发的 token 立即失效（默认 1440 分钟内照样能用），与响应文案"请重新登录"不符。账号被盗后重置密码形同无效 | `users` 表新增 `token_version` 列，JWT 携带 `tv` 声明，登录时比对，不符即 401。改密、重置密码、改角色/状态时递增 |
| 36 | 组件故障被静默降级为「无结果」 | Qdrant 检索异常被吞成空列表、Tavily 失败被吞成空列表，调用方无法区分"没有匹配"与"后端挂了"，基础设施故障表现为一句"知识库无相关内容" | 新增 `app/core/errors.py`；向量库 / Embedding / 联网搜索失败改为抛 `ProviderError` 子类并统一映射 503；联网搜索失败原因写入 `web_search_error` 字段 |
| 37 | `QDRANT_VECTOR_SIZE` 是死配置 | 该字段从未被读取，集合维度实际取自 Embedding 实测输出；照文档改它没有任何效果，反而误导 | 从 `config.py` 与 `.env.example` 移除，文档改为"维度由模型自动探测" |
| 38 | 非 1024 维的 API Embedding 会失败 | API Embedding 维度硬编码 1024，与真实输出不符时集合建错、写入被 Qdrant 拒绝，且没有配置项可改 | 新增 `EMBEDDING_API_DIM`（0 = 启动预热时自动探测）；`dimension` 未知时明确报错而非返回 1024；索引流程改为先向量化、用实测维度建集合 |
| 39 | 同一故障返回两种状态码 | `/ask` 经 `except RuntimeError` 返回 503，`/retrieve` 却落到通用处理器返回 500 | 新增 `ProviderError` 专用异常处理器，统一 503 |

**旧 token 兼容性**：`tv` 声明缺失按 0 处理，升级后此前签发的 token 在用户改密前仍然有效，
不会因为这次改动把所有人踢下线。

**集合维度防护**：集合已存在且维度与当前 Embedding 不符时，空集合直接重建
（覆盖 `create_kb` 预建时维度未知的情况），已有数据则明确报错，
绝不静默清空用户数据（第四节缺陷 20 的后续加固）。

### 本轮实测验证

| 验证项 | 方法 | 结果 |
|---|---|---|
| 启动期迁移补列 | `information_schema.columns` 查询 | `token_version\|integer\|0\|NO` ✓ |
| 重置密码吊销旧 token | 建临时用户 → 登录 → 管理员重置密码 → 旧 token 调 `/me` | `token.tv=0` vs `DB=1` → **401 登录状态已失效** ✓ |
| 禁用账户吊销 token | 禁用后再用其 token | 401 ✓，admin 自己的 token 不受影响 ✓ |
| 故障与空结果可区分 | 停掉 Qdrant 后调 `/retrieve` 与 `/ask` | 正常空库 `200 hit_count=0` / 故障 **503** ✓，恢复后自动痊愈 ✓ |
| 联网搜索失败不再静默 | 桩 provider 抛 `WebSearchError` 走 `_prepare` | `web_search_error` 有值 ✓；搜索成功但无结果时为空 ✓ |
| 集合维度防护 | 空集合 / 非空集合分别用不同维度调 `ensure_collection` | 空集合自动重建 ✓；非空集合抛 `VectorStoreError` ✓ |
| 非 1024 维处理 | `OpenAICompatEmbedding(dim=0).dimension` | 明确抛 `EmbeddingError` ✓ |
| 移除死配置不破坏启动 | 服务在仍含 `QDRANT_VECTOR_SIZE` 的 .env 下启动 | 正常（`extra="ignore"`）✓ |

回归结果：`verify_core` 46/46、`e2e_test --keep` 26/26、`test_stream` 流式通过、
`test_query_rewrite` 全部符合预期。

### 2026-10-17 第五轮：更换测试 fixture 暴露的缺陷

测试 fixture 由 `白皮书.pdf`（1.7KB stub）与 `大型规章制度.md` 换成真实英文论文
`DSR_protocol.pdf`（7.4MB UWB 测距论文，97 块）后，端到端测试暴露出两个此前审查遗漏的缺陷：

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 40 | PDF 提取文本含 NUL 字节（`\x00`） | PostgreSQL TEXT 按 UTF8 存储会拒绝 `0x00`，写分块时报 `CharacterNotInRepertoireError`，整条索引失败 | `normalize_text` 统一剥离 NUL 及其余无意义控制字符（保留 `\n`/`\t`） |
| 41 | 索引失败处理器未先回滚 | 写库失败后事务处于 rolled back 状态，原 `commit()` 抛 `PendingRollbackError`，失败状态无法落库，文档永远停在 `indexing`，wait 轮询看不到 failed | except 块先 `rollback()` → 重取文档 → 置 failed → `commit()` → `raise` |

回归结果：`selfcheck` 5/5、`verify_core` 46/46、`e2e_test` 25/25、`test_stream` 流式通过、
`test_query_rewrite` 全部符合预期。

---

## 五、如何复现验证

```bash
cd RAG-LAB

# 轻量检查（秒级，零依赖）
python3 scripts/selfcheck.py      # 静态结构，5 项检查
python3 scripts/verify_core.py    # 核心逻辑，46 项断言

# 端到端（需服务运行；未配 DEEPSEEK_API_KEY 时相关断言自动跳过）
docker compose up -d                     # 生产模式
# 或
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # 开发模式

python3 scripts/e2e_test.py              # 27 项断言（含 LLM 时）
python3 scripts/e2e_test.py --keep       # 保留数据便于人工检查（26 项，跳过清理断言）
python3 scripts/test_stream.py           # SSE 流式问答
python3 scripts/test_query_rewrite.py    # Query 改写与相关度判定
```

> 后三个脚本在 2026-10-17 之前只打印结果、始终退出 0，功能失效也无法察觉；
> 现已改为校验失败时收集问题并 `exit 1`，可直接用于 CI。

测试文档位于 `test_fixtures/`，包含 PDF / Word / Markdown / TXT 四种格式。

> 注：本机（WSL2 mirrored 网络模式）宿主机 8000 被 Windows 的残留 portproxy 规则占用，
> 故 `docker-compose.yml` 将 backend 发布为 **18000**、redis 发布为 **16379**（容器内端口不变）。
> 访问地址为 http://localhost:18000/docs。

---

## 六、硬件限制说明

本机 GPU 为 Quadro M2000M（4GB 显存，Maxwell 架构 sm_52）：

- **不支持 vLLM**（vLLM 要求算力 ≥ 7.0）
- 当前部署走 DeepSeek API，**不受此限制影响**
- 后续如需本地部署 LLM，需更换为算力 ≥7.0 且显存 ≥8GB 的显卡
  （如 RTX 3060 12G、4060Ti 16G、Tesla T4、A10）
- Embedding 走 CPU，无需 GPU

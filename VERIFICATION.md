# 验证记录

本文档记录 RAG-LAB 的实际验证情况，区分「已实测通过」与「未验证」，
避免把未验证的部分误认为可用。

验证日期：2026-10-15（首次验证）/ 2026-10-16（代码审查与缺陷修复后复验）
验证环境：Ubuntu 20.04 / Docker 28.1.1 / Compose v2.35.1 / 15GB 内存 / 无可用 GPU

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
| PDF | 白皮书.pdf | ✓ 334 字符，中文无乱码 | pymupdf |
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
✓ 短文档：394-790 字符/块，单块不超限
✓ 长文档：8521 字符 → 14 块
         最短 394 / 最长 790 / 平均 606 字符
✓ overlap 生效（相邻块有 150 字符重叠）
✓ 切分点落在段落边界，不切断句子
✓ 46 项核心逻辑断言全部通过（scripts/verify_core.py）
```

### 1.6 完整链路（端到端测试）

`scripts/e2e_test.py` 实测结果：**24/24 通过**

```
[1] 服务健康        ✓ 健康接口 / 向量库连通 / Embedding dim=1024
[2] 认证            ✓ 管理员登录、JWT 签发
[3] 知识库          ✓ 创建成功
[4] 文档上传        ✓ 5 种文档全部上传
[5] 索引            ✓ 5/5 就绪，18 个分块，耗时 30s
[6] 分块入库校验    ✓ 统计数与实际数一致
[7] 向量检索质量    ✓ 5/5 命中正确文档
[8] 清理            ✓ 删除知识库，向量同步清理
```

检索质量详情：

| 查询 | 期望文档 | 实际 top1 | 相似度 |
|---|---|---|---|
| 年假有多少天 | 员工手册.md | 员工手册.md | 0.621 |
| 报销流程是什么 | 员工手册.md | 员工手册.md | 0.553 |
| 向量检索的原理是什么 | 系统说明.txt | 系统说明.txt | 0.587 |
| 技术指标里的向量维度是多少 | 白皮书.pdf | 白皮书.pdf | 0.644 |
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

## 三、未验证

| 功能 | 依赖 | 说明 |
|---|---|---|
| Tavily 联网搜索 | TAVILY_API_KEY | 本机无该 key，需注册（免费 1000 次/月） |
| 本地 vLLM 接入 | ≥8GB 且算力≥7.0 的 GPU | 本机 GPU 不满足（见第五节） |

联网搜索的代码路径已实现（`app/providers/websearch/base.py`），
未配置 key 时自动降级为 `NullWebSearch`，不影响其他功能。
配置后重启即可启用：

```bash
echo "TAVILY_API_KEY=tvly-你的key" >> .env
docker compose restart backend
python3 scripts/test_stream.py    # 提问时 enable_web=true 即可触发
```

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

---

## 五、如何复现验证

```bash
cd /home/p50/RAG-LAB

# 轻量检查（秒级，零依赖）
python3 scripts/selfcheck.py      # 静态结构，5 项检查
python3 scripts/verify_core.py    # 核心逻辑，46 项断言

# 端到端（需服务运行）
docker compose up -d                     # 生产模式
# 或
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # 开发模式

python3 scripts/e2e_test.py              # 24 项断言
python3 scripts/e2e_test.py --keep       # 保留数据便于人工检查
```

测试文档位于 `test_fixtures/`，包含 PDF / Word / Markdown / TXT 四种格式。

---

## 六、硬件限制说明

本机 GPU 为 Quadro M2000M（4GB 显存，Maxwell 架构 sm_52）：

- **不支持 vLLM**（vLLM 要求算力 ≥ 7.0）
- 当前部署走 DeepSeek API，**不受此限制影响**
- 后续如需本地部署 LLM，需更换为算力 ≥7.0 且显存 ≥8GB 的显卡
  （如 RTX 3060 12G、4060Ti 16G、Tesla T4、A10）
- Embedding 走 CPU，无需 GPU

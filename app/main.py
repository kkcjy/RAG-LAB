# FastAPI 应用入口
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, auth, chat, kb, system  # noqa: F401
from app.core.config import settings
from app.core.database import AsyncSessionLocal, close_db, init_db
from app.core.errors import ProviderError

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_LEVEL = logging.DEBUG if settings.app_env == "development" else logging.INFO

logging.basicConfig(level=_LOG_LEVEL, format=_LOG_FORMAT)
logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """把日志同时写入 data/logs/app.log（按大小轮转）。

    容器里 stdout 日志随容器销毁而丢失，落盘后便于事后排查。
    """
    try:
        log_dir = Path(settings.upload_dir).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=10 * 1024 * 1024,  # 单文件 10MB
            backupCount=5,              # 保留 5 个历史文件
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.setLevel(_LOG_LEVEL)
        logging.getLogger().addHandler(handler)
        logger.info("文件日志已启用: %s", log_dir / "app.log")
    except Exception as e:  # noqa: BLE001
        logger.warning("文件日志初始化失败（仅输出到 stdout）: %s", e)


_setup_file_logging()


# ---------------------------------------------------------------------------
# 启动/关闭
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("启动 %s (env=%s)", settings.app_name, settings.app_env)
    logger.info("LLM provider: %s", settings.llm_provider)
    logger.info("Embedding provider: %s", settings.embedding_provider)

    # 0) 联网搜索配置校验：没配 key 却开了兜底，必须显式告警
    try:
        from app.providers.websearch.base import get_web_search

        ws = get_web_search()
        if ws.enabled:
            logger.info("联网搜索: %s", ws.display_name)
        elif settings.web_search_auto_fallback:
            logger.warning(
                "联网搜索不可用但 WEB_SEARCH_AUTO_FALLBACK=true —— "
                "知识库未命中时将无法联网补充。请配置 TAVILY_API_KEY 或关闭兜底。"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("联网搜索检查失败: %s", e)

    # 1) 建表
    await init_db()

    # 2) 初始化默认管理员 + 系统配置
    try:
        from app.services.bootstrap import bootstrap_all

        async with AsyncSessionLocal() as db:
            await bootstrap_all(db)
    except Exception:  # noqa: BLE001
        logger.exception("初始化数据失败（服务仍会启动）")

    # 3) 预热向量集合 & 检查依赖
    try:
        from app.providers.vector.base import get_vector_store

        store = get_vector_store()
        ok = await store.health_check()
        logger.info("Qdrant 连接: %s", "正常" if ok else "异常")
    except Exception as e:  # noqa: BLE001
        logger.warning("Qdrant 检查失败: %s", e)

    # 4) 后台预热 embedding 模型（BGE 首次加载约需数秒~数十秒）
    async def _warmup_embedding():
        try:
            from app.providers.embedding.base import get_embedding

            ok = await get_embedding().warmup()
            logger.info("Embedding 模型预热: %s", "完成" if ok else "失败/跳过")
        except Exception as e:  # noqa: BLE001
            logger.warning("Embedding 预热异常: %s", e)

    # 5) 补偿上次进程退出时被中断的索引任务
    async def _resume_indexing():
        # 等 embedding 预热完再补偿，避免两处同时加载模型
        await warmup_task
        try:
            from app.services.indexing import resume_stuck_documents

            async with AsyncSessionLocal() as db:
                await resume_stuck_documents(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("索引补偿异常: %s", e)

    warmup_task = asyncio.create_task(_warmup_embedding())
    resume_task = asyncio.create_task(_resume_indexing())

    logger.info("服务已就绪 → http://%s:%d/docs", settings.app_host, settings.app_port)
    logger.info("=" * 60)

    yield

    warmup_task.cancel()
    resume_task.cancel()
    await close_db()
    logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description="企业级 RAG 知识库问答系统",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # 鉴权走 Bearer token 而非 Cookie，无需放行凭据；
    # 与 allow_origins=["*"] 同开会让任意站点携带凭据跨域访问。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 全局异常处理
# ---------------------------------------------------------------------------
@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError):
    """向量库 / Embedding / 联网搜索不可用。

    这类故障此前被 provider 吞成空结果，表现为"知识库无相关内容"，
    调用方无从分辨。统一返回 503，与"检索成功但没命中"区分开。
    """
    logger.error("依赖组件不可用: %s %s - %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误", "path": request.url.path},
    )


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(kb.router)
app.include_router(chat.router)


@app.get("/", summary="根路径")
async def root():
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
    }

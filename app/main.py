# FastAPI 应用入口
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, auth, chat, kb, system  # noqa: F401
from app.core.config import settings
from app.core.database import AsyncSessionLocal, close_db, init_db

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if settings.app_env == "development" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 启动/关闭
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("启动 %s (env=%s)", settings.app_name, settings.app_env)
    logger.info("LLM provider: %s", settings.llm_provider)
    logger.info("Embedding provider: %s", settings.embedding_provider)

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

    logger.info("服务已就绪 → http://%s:%d/docs", settings.app_host, settings.app_port)
    logger.info("=" * 60)

    yield

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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 全局异常处理
# ---------------------------------------------------------------------------
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

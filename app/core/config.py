# RAG-LAB 配置加载层
# 统一从 .env 读取所有配置，代码中通过 settings.XXX 访问
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "RAG-LAB"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_key: str = "dev-secret-key-please-change"
    access_token_expire_minutes: int = 1440

    # ---------- PostgreSQL ----------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "raglab"
    postgres_password: str = "raglab_pass_change_me"
    postgres_db: str = "raglab"

    # ---------- Qdrant ----------
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_api_key: Optional[str] = None

    # ---------- Redis ----------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0

    # ---------- LLM ----------
    # deepseek | openai_compatible | vllm_local
    llm_provider: str = "deepseek"
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    openai_compat_api_key: Optional[str] = None
    openai_compat_base_url: Optional[str] = None
    openai_compat_model: Optional[str] = None
    vllm_base_url: Optional[str] = None
    vllm_model: Optional[str] = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    llm_timeout: int = 120

    # ---------- Embedding ----------
    # local_bge | openai_compatible
    embedding_provider: str = "local_bge"
    # HuggingFace 端点：国内用镜像 https://hf-mirror.com
    hf_endpoint: Optional[str] = "https://hf-mirror.com"
    bge_model_path: str = "BAAI/bge-m3"
    bge_device: str = "cpu"
    bge_num_threads: int = 4
    bge_batch_size: int = 16
    bge_normalize: bool = True
    embedding_api_key: Optional[str] = None
    embedding_api_base_url: Optional[str] = None
    embedding_api_model: Optional[str] = None
    # API Embedding 的向量维度。0 = 启动预热时自动探测（推荐）。
    # 集合维度实际取自 Embedding 实测输出，此值仅用于探测前提前建集合。
    embedding_api_dim: int = 0

    # ---------- 检索 ----------
    retrieval_score_threshold: float = 0.50
    retrieval_top_k: int = 5
    retrieval_llm_judge: bool = True
    query_rewrite_enabled: bool = True
    web_search_auto_fallback: bool = True

    # ---------- 联网搜索 ----------
    web_search_provider: str = "tavily"
    tavily_api_key: Optional[str] = None
    tavily_max_results: int = 5
    tavily_search_depth: str = "basic"

    # ---------- 文档处理 ----------
    upload_dir: str = "./data/uploads"
    max_upload_size_mb: int = 50
    allowed_extensions: str = ".pdf,.docx,.doc,.md,.markdown,.txt"
    chunk_size: int = 800
    chunk_overlap: int = 150
    auto_index_on_upload: bool = True

    # ---------- OCR（扫描件 PDF） ----------
    ocr_enabled: bool = True          # 文本提取为空时自动转 OCR
    ocr_max_pages: int = 30           # 单文档最多 OCR 页数，防超长文档拖垮 CPU
    ocr_dpi: int = 200                # 渲染分辨率，越高越准越慢

    # ---------- 管理员 ----------
    admin_username: str = "admin"
    admin_password: str = "admin123456"
    admin_email: str = "admin@example.com"

    # ---------- 派生属性 ----------
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        """同步驱动，供 Alembic / 初始化脚本使用"""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def allowed_extension_list(self) -> List[str]:
        return [e.strip().lower() for e in self.allowed_extensions.split(",") if e.strip()]

    @field_validator("llm_provider")
    @classmethod
    def _check_llm_provider(cls, v: str) -> str:
        allowed = {"deepseek", "openai_compatible", "vllm_local"}
        if v not in allowed:
            raise ValueError(f"llm_provider 必须是 {allowed} 之一，当前为 {v}")
        return v

    @field_validator("embedding_provider")
    @classmethod
    def _check_embedding_provider(cls, v: str) -> str:
        allowed = {"local_bge", "openai_compatible"}
        if v not in allowed:
            raise ValueError(f"embedding_provider 必须是 {allowed} 之一，当前为 {v}")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Pydantic 请求/响应模型
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=128)


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: Optional[str] = None
    role: str
    status: str
    display_name: Optional[str] = None
    created_at: dt.datetime
    last_login_at: Optional[dt.datetime] = None


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    email: Optional[str] = Field(None, max_length=128)
    display_name: Optional[str] = Field(None, max_length=64)
    role: str = Field("user", pattern="^(admin|user)$")


class UserUpdate(BaseModel):
    email: Optional[str] = Field(None, max_length=128)
    display_name: Optional[str] = Field(None, max_length=64)
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")
    role: Optional[str] = Field(None, pattern="^(admin|user)$")


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
class KBCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    is_public: bool = False


class KBUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = None
    is_public: Optional[bool] = None
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")


class KBOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    collection_name: str
    status: str
    is_public: bool
    doc_count: int
    chunk_count: int
    created_at: dt.datetime
    updated_at: dt.datetime


class KBPermissionGrant(BaseModel):
    user_id: uuid.UUID
    can_read: bool = True
    can_write: bool = False


class KBPermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    kb_id: uuid.UUID
    can_read: bool
    can_write: bool


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
class DocOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    file_type: str
    file_size: int
    status: str
    error_message: Optional[str] = None
    chunk_count: int
    char_count: int
    created_at: dt.datetime
    indexed_at: Optional[dt.datetime] = None


class DocListOut(BaseModel):
    total: int
    items: list[DocOut]
    # 索引进度汇总：便于调用方判断"是否可以提问了"
    indexing: int = 0          # 正在处理（pending/parsing/indexing）
    ready: int = 0             # 已完成
    failed: int = 0            # 失败
    all_done: bool = True      # 是否全部到达终态（可安全提问）


# ---------------------------------------------------------------------------
# 问答
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    kb_id: Optional[uuid.UUID] = None
    session_id: Optional[uuid.UUID] = None
    enable_web: bool = True
    stream: bool = True


class SourceOut(BaseModel):
    type: str
    chunk_id: Optional[str] = None
    doc_id: Optional[str] = None
    filename: Optional[str] = None
    score: Optional[float] = None
    title: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None


class AskResponse(BaseModel):
    session_id: uuid.UUID
    answer: str
    sources: list[SourceOut] = []
    used_web_search: bool = False
    kb_hit: bool = False
    rewritten_query: Optional[str] = None
    latency_ms: float = 0.0
    # 联网搜索失败原因；非空表示"搜过但失败了"，与"没有结果"区分开
    web_search_error: Optional[str] = None


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    kb_id: Optional[uuid.UUID] = None
    message_count: int
    created_at: dt.datetime
    updated_at: dt.datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    sources: Any = None
    used_web_search: bool
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# 系统配置
# ---------------------------------------------------------------------------
class ConfigItem(BaseModel):
    key: str
    value: str
    value_type: str = "str"
    description: Optional[str] = None


class ConfigUpdate(BaseModel):
    items: dict[str, str]


class HealthOut(BaseModel):
    status: str
    version: str
    llm_provider: str
    llm_model: str
    llm_ok: bool
    embedding_provider: str
    embedding_dim: int
    vector_store_ok: bool
    web_search_provider: str
    # 联网搜索是否真正可用（配了 key 且 provider 不是 Null）
    web_search_enabled: bool = False
    # 供人或脚本快速判断的告警列表（如"未配置 TAVILY_API_KEY"）
    warnings: list[str] = []


TokenResponse.model_rebuild()

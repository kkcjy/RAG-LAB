# 知识库管理路由
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    client_ip,
    get_accessible_kb,
    get_current_admin,
    get_current_user,
    get_writable_kb,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.models.db import Document, KBPermission, KnowledgeBase, User
from app.providers.vector.base import get_vector_store
from app.schemas.api import (
    DocListOut,
    DocOut,
    KBCreate,
    KBPermissionGrant,
    KBPermissionOut,
    KBOut,
    KBUpdate,
)
from app.services.indexing import delete_document, index_document
from app.utils.audit import write_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["知识库"])


# ===========================================================================
#  知识库 CRUD
# ===========================================================================
@router.get("", summary="知识库列表（按权限过滤）")
async def list_kbs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role == "admin":
        stmt = select(KnowledgeBase)
    else:
        # 自己的 + 公开的 + 被授权的
        perm_kb_ids = (
            await db.execute(
                select(KBPermission.kb_id).where(
                    KBPermission.user_id == user.id, KBPermission.can_read.is_(True)
                )
            )
        ).scalars().all()
        stmt = select(KnowledgeBase).where(
            (KnowledgeBase.owner_id == user.id)
            | (KnowledgeBase.is_public.is_(True))
            | (KnowledgeBase.id.in_(perm_kb_ids))
        )

    rows = (await db.execute(stmt.order_by(KnowledgeBase.created_at.desc()))).scalars().all()
    return {"items": [KBOut.model_validate(kb) for kb in rows], "total": len(rows)}


@router.post("", response_model=KBOut, summary="创建知识库")
async def create_kb(
    payload: KBCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == payload.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="知识库名称已存在")

    # 生成 Qdrant 集合名
    kb_id = uuid.uuid4()
    collection_name = f"kb_{kb_id.hex[:16]}"

    kb = KnowledgeBase(
        id=kb_id,
        name=payload.name,
        description=payload.description,
        collection_name=collection_name,
        owner_id=user.id,
        is_public=payload.is_public,
        status="active",
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)

    # 预建向量集合
    try:
        from app.providers.embedding.base import get_embedding

        dim = get_embedding().dimension
        await get_vector_store().ensure_collection(collection_name, dim)
    except Exception as e:  # noqa: BLE001
        logger.warning("预创建向量集合失败（首次上传时会重试）: %s", e)

    await write_audit(
        db,
        action="create_kb",
        user_id=user.id,
        username=user.username,
        resource=kb.name,
        ip=client_ip(request),
    )
    return KBOut.model_validate(kb)


@router.get("/{kb_id}", response_model=KBOut, summary="知识库详情")
async def get_kb(kb: KnowledgeBase = Depends(get_accessible_kb)):
    return KBOut.model_validate(kb)


@router.patch("/{kb_id}", response_model=KBOut, summary="修改知识库")
async def update_kb(
    payload: KBUpdate,
    kb: KnowledgeBase = Depends(get_writable_kb),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.name and payload.name != kb.name:
        dup = (
            await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == payload.name))
        ).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=400, detail="知识库名称已存在")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(kb, field, value)

    await db.commit()
    await db.refresh(kb)

    await write_audit(
        db, action="update_kb", user_id=user.id, username=user.username, resource=kb.name
    )
    return KBOut.model_validate(kb)


@router.delete("/{kb_id}", summary="删除知识库（含全部文档与向量）")
async def delete_kb(
    kb: KnowledgeBase = Depends(get_writable_kb),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = kb.name
    collection = kb.collection_name

    # 删磁盘文件
    docs = (
        await db.execute(select(Document).where(Document.kb_id == kb.id))
    ).scalars().all()
    for d in docs:
        try:
            Path(d.stored_path).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除文件失败 %s: %s", d.stored_path, e)

    await db.delete(kb)
    await db.commit()

    # 删向量集合
    try:
        await get_vector_store().drop_collection(collection)
    except Exception as e:  # noqa: BLE001
        logger.error("删除向量集合失败 %s: %s", collection, e)

    await write_audit(
        db, action="delete_kb", user_id=user.id, username=user.username, resource=name
    )
    return {"message": f"知识库 {name} 已删除"}


# ===========================================================================
#  权限管理（仅管理员）
# ===========================================================================
@router.post("/{kb_id}/permissions", response_model=KBPermissionOut, summary="授权用户访问知识库")
async def grant_permission(
    payload: KBPermissionGrant,
    kb_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    perm = (
        await db.execute(
            select(KBPermission).where(
                KBPermission.user_id == payload.user_id, KBPermission.kb_id == kb_id
            )
        )
    ).scalar_one_or_none()

    if perm is None:
        perm = KBPermission(
            user_id=payload.user_id,
            kb_id=kb_id,
            can_read=payload.can_read,
            can_write=payload.can_write,
        )
        db.add(perm)
    else:
        perm.can_read = payload.can_read
        perm.can_write = payload.can_write

    await db.commit()
    await db.refresh(perm)

    await write_audit(
        db,
        action="grant_kb_permission",
        user_id=admin.id,
        username=admin.username,
        resource=f"{kb.name} → {target.username}",
        detail={"can_read": payload.can_read, "can_write": payload.can_write},
    )
    return KBPermissionOut.model_validate(perm)


@router.get("/{kb_id}/permissions", summary="查看知识库授权列表")
async def list_permissions(
    kb_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(KBPermission).where(KBPermission.kb_id == kb_id))
    ).scalars().all()

    items = []
    for p in rows:
        u = await db.get(User, p.user_id)
        items.append(
            {
                "user_id": str(p.user_id),
                "username": u.username if u else None,
                "can_read": p.can_read,
                "can_write": p.can_write,
            }
        )
    return {"items": items}


@router.delete("/{kb_id}/permissions/{user_id}", summary="撤销知识库授权")
async def revoke_permission(
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    perm = (
        await db.execute(
            select(KBPermission).where(
                KBPermission.user_id == user_id, KBPermission.kb_id == kb_id
            )
        )
    ).scalar_one_or_none()

    if perm is None:
        raise HTTPException(status_code=404, detail="授权记录不存在")

    await db.delete(perm)
    await db.commit()

    await write_audit(
        db, action="revoke_kb_permission", user_id=admin.id, username=admin.username,
        resource=f"kb={kb_id} user={user_id}",
    )
    return {"message": "授权已撤销"}


# ===========================================================================
#  文档管理
# ===========================================================================
@router.get("/{kb_id}/documents", response_model=DocListOut, summary="文档列表")
async def list_documents(
    kb_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    kb: KnowledgeBase = Depends(get_accessible_kb),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Document).where(Document.kb_id == kb.id)
    if keyword:
        stmt = stmt.where(Document.filename.ilike(f"%{keyword}%"))
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return DocListOut(total=total, items=[DocOut.model_validate(d) for d in rows])


async def _index_in_background(doc_id: uuid.UUID) -> None:
    """后台任务：用独立会话索引文档"""
    async with AsyncSessionLocal() as db:
        try:
            await index_document(db, doc_id)
        except Exception:  # noqa: BLE001
            logger.exception("后台索引失败 doc_id=%s", doc_id)


@router.post("/{kb_id}/documents", response_model=DocOut, summary="上传文档")
async def upload_document(
    kb_id: uuid.UUID,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    kb: KnowledgeBase = Depends(get_writable_kb),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    ext = Path(file.filename).suffix.lower()
    if ext not in settings.allowed_extension_list:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，允许：{', '.join(settings.allowed_extension_list)}",
        )

    # 读取并校验大小
    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400, detail=f"文件超过 {settings.max_upload_size_mb}MB 限制"
        )
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")

    # 落盘
    doc_id = uuid.uuid4()
    safe_name = Path(file.filename).name
    dest_dir = Path(settings.upload_dir) / str(kb.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_path = dest_dir / f"{doc_id.hex}_{safe_name}"

    try:
        stored_path.write_bytes(content)
    except Exception as e:  # noqa: BLE001
        logger.exception("保存上传文件失败")
        raise HTTPException(status_code=500, detail=f"保存文件失败: {e}") from e

    doc = Document(
        id=doc_id,
        kb_id=kb.id,
        filename=safe_name,
        stored_path=str(stored_path),
        file_type=ext,
        file_size=len(content),
        status="pending",
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await write_audit(
        db, action="upload_document", user_id=user.id, username=user.username,
        resource=f"{kb.name}/{safe_name}",
    )

    # 触发索引
    if settings.auto_index_on_upload:
        background.add_task(_index_in_background, doc.id)

    return DocOut.model_validate(doc)


@router.post("/{kb_id}/documents/{doc_id}/reindex", summary="重新索引文档")
async def reindex_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    background: BackgroundTasks,
    kb: KnowledgeBase = Depends(get_writable_kb),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb.id:
        raise HTTPException(status_code=404, detail="文档不存在")

    background.add_task(_index_in_background, doc.id)
    return {"message": "已提交重新索引任务", "doc_id": str(doc.id)}


@router.delete("/{kb_id}/documents/{doc_id}", summary="删除文档")
async def delete_doc(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    kb: KnowledgeBase = Depends(get_writable_kb),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb.id:
        raise HTTPException(status_code=404, detail="文档不存在")

    filename = doc.filename
    await delete_document(db, doc.id)

    await write_audit(
        db, action="delete_document", user_id=user.id, username=user.username,
        resource=f"{kb.name}/{filename}",
    )
    return {"message": f"文档 {filename} 已删除"}


@router.get("/{kb_id}/documents/{doc_id}/chunks", summary="查看文档分块（调试用）")
async def list_chunks(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    kb: KnowledgeBase = Depends(get_accessible_kb),
    db: AsyncSession = Depends(get_db),
):
    from app.models.db import Chunk

    doc = await db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb.id:
        raise HTTPException(status_code=404, detail="文档不存在")

    stmt = select(Chunk).where(Chunk.doc_id == doc_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Chunk.chunk_index).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "chunk_index": c.chunk_index,
                "vector_id": c.vector_id,
                "char_count": c.char_count,
                "content": c.content,
            }
            for c in rows
        ],
    }

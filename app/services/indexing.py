# 索引服务：文档 → 解析 → 分块 → 向量化 → 写入 Qdrant + PostgreSQL
#   另含：删除时的向量/空目录清理、进程重启后的未完成索引补偿
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import Chunk, Document, KnowledgeBase
from app.providers.embedding.base import get_embedding
from app.providers.vector.base import VectorRecord, get_vector_store
from app.services.document_parser import parse_document, split_text

logger = logging.getLogger(__name__)


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def index_document(db: AsyncSession, doc_id: uuid.UUID) -> dict:
    """
    完整索引流程。返回统计信息。
    幂等：重复调用会先清掉旧 chunk 再重建。
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"文档不存在: {doc_id}")

    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None:
        raise ValueError(f"知识库不存在: {doc.kb_id}")

    store = get_vector_store()
    embedder = get_embedding()

    # 1) 标记解析中
    doc.status = "parsing"
    doc.error_message = None
    await db.commit()

    try:
        # 2) 解析
        parsed = parse_document(doc.stored_path)
        if not parsed.text.strip():
            parser_used = (parsed.meta or {}).get("parser", "")
            if parser_used == "ocr_failed":
                raise ValueError(
                    "文档解析后内容为空，且 OCR 识别失败。"
                    "请检查文件是否损坏，或转为文字版后重新上传。"
                )
            if parser_used == "none":
                raise ValueError(
                    "文档解析后内容为空（OCR 已禁用）。"
                    "该文件可能是扫描件或加密文件，"
                    "请在 .env 中设置 OCR_ENABLED=true 后重试。"
                )
            raise ValueError(
                "文档解析后内容为空。该文件可能是扫描件（图片构成的 PDF），"
                "且 OCR 未能识别出文字；请确认文件清晰度，"
                "或转为文字版（如 Word/TXT）后重新上传。"
            )

        # 记录解析方式，便于排查（扫描件走的 OCR 会标注出来）
        meta = parsed.meta or {}
        if meta.get("parser") == "rapidocr":
            ocr_pages = meta.get("ocr_pages", 0)
            truncated = meta.get("ocr_truncated", False)
            logger.info(
                "文档经 OCR 识别: %s（%d 页%s）",
                doc.filename,
                ocr_pages,
                "，因超出上限已截断" if truncated else "",
            )

        # 3) 分块
        chunks = split_text(
            parsed.text,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
        if not chunks:
            raise ValueError("分块结果为空")

        doc.status = "indexing"
        doc.char_count = len(parsed.text)
        doc.chunk_count = len(chunks)
        await db.commit()

        # 4) 清掉旧数据（幂等重建）
        await _purge_document_vectors(db, doc.id, kb.collection_name)

        # 5) 向量化
        embedder_dim = embedder.dimension
        await store.ensure_collection(kb.collection_name, embedder_dim)

        vectors = await embedder.embed_documents(chunks)

        # 6) 构造向量记录 + 关系库分块记录
        records: list[VectorRecord] = []
        chunk_rows: list[Chunk] = []

        for idx, (content, vec) in enumerate(zip(chunks, vectors)):
            vid = str(uuid.uuid4())
            payload = {
                "content": content,
                "doc_id": str(doc.id),
                "kb_id": str(doc.kb_id),
                "filename": doc.filename,
                "chunk_index": idx,
                "file_type": doc.file_type,
            }
            records.append(VectorRecord(id=vid, vector=vec, payload=payload))
            chunk_rows.append(
                Chunk(
                    doc_id=doc.id,
                    kb_id=doc.kb_id,
                    vector_id=vid,
                    chunk_index=idx,
                    content=content,
                    char_count=len(content),
                    meta={"chunk_index": idx},
                )
            )

        # 7) 写向量库
        await store.upsert(kb.collection_name, records)

        # 8) 写关系库
        db.add_all(chunk_rows)

        doc.status = "ready"
        doc.indexed_at = dt.datetime.now(dt.timezone.utc)
        await db.flush()

        # 9) 更新知识库统计
        await _refresh_kb_stats(db, kb.id)
        await db.commit()

        logger.info("文档索引完成: %s (%d chunks)", doc.filename, len(chunks))
        return {"doc_id": str(doc.id), "chunks": len(chunks), "chars": len(parsed.text)}

    except Exception as e:  # noqa: BLE001
        logger.exception("文档索引失败: %s", doc.filename)
        doc.status = "failed"
        doc.error_message = str(e)[:1000]
        await db.commit()
        raise


async def _purge_document_vectors(db: AsyncSession, doc_id: uuid.UUID, collection: str) -> None:
    """删除某文档已存在的向量与分块"""
    store = get_vector_store()
    await store.delete(collection, filters={"doc_id": str(doc_id)})
    await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await db.flush()


def remove_kb_dir(kb_id: uuid.UUID) -> None:
    """删除知识库的整个上传目录（删库时用）。非空也会一并清掉。"""
    dest_dir = Path(settings.upload_dir) / str(kb_id)
    try:
        shutil.rmtree(dest_dir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除上传目录失败 %s: %s", dest_dir, e)


def _remove_file_and_empty_dir(stored_path: str) -> None:
    """删除单个文档文件；若其所在的知识库目录随之变空，则一并删除。"""
    p = Path(stored_path)
    try:
        p.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除文件失败 %s: %s", stored_path, e)
        return
    # rmdir 仅在目录为空时成功；库中还有其它文档时会抛 OSError，静默忽略
    try:
        p.parent.rmdir()
    except OSError:
        pass


async def delete_document(db: AsyncSession, doc_id: uuid.UUID) -> None:
    """删除文档：向量 + 分块 + 文件 + 记录"""
    doc = await db.get(Document, doc_id)
    if doc is None:
        return

    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is not None:
        await _purge_document_vectors(db, doc.id, kb.collection_name)

    # 删磁盘文件，并清掉空掉的知识库目录
    _remove_file_and_empty_dir(doc.stored_path)

    await db.delete(doc)
    await db.flush()

    if kb is not None:
        await _refresh_kb_stats(db, kb.id)
    await db.commit()


async def _refresh_kb_stats(db: AsyncSession, kb_id: uuid.UUID) -> None:
    """重算知识库的文档数与分块数"""
    doc_count = (
        await db.execute(
            select(func.count()).select_from(Document).where(
                Document.kb_id == kb_id, Document.status == "ready"
            )
        )
    ).scalar_one()

    chunk_count = (
        await db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id)
        )
    ).scalar_one()

    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .values(doc_count=doc_count, chunk_count=chunk_count)
    )
    await db.flush()


STUCK_STATUSES = ("pending", "parsing", "indexing")


async def resume_stuck_documents(db: AsyncSession, limit: int = 50) -> int:
    """补偿未完成的索引任务。

    上传后的索引由 FastAPI BackgroundTasks 触发，进程重启会丢失，
    导致文档永久停在 pending/parsing/indexing。启动时重跑这些文档。
    """
    rows = (
        await db.execute(
            select(Document.id)
            .where(Document.status.in_(STUCK_STATUSES))
            .limit(limit)
        )
    ).scalars().all()

    if not rows:
        return 0

    logger.info("发现 %d 个未完成索引的文档，开始补偿", len(rows))
    ok = 0
    for doc_id in rows:
        try:
            await index_document(db, doc_id)
            ok += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("补偿索引失败 doc_id=%s: %s", doc_id, e)
    logger.info("索引补偿完成: %d/%d", ok, len(rows))
    return ok

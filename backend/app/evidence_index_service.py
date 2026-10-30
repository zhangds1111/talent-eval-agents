from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.milvus_store import EvidenceRecord
from app.milvus_store import MilvusEvidenceStore
from app.model_provider import get_embedding_model
from app.models import ChunkingRun, ChunkingStatus, Document, DocumentChunk, DocumentVersion, EvidenceIndexJob, IndexStatus


logger = logging.getLogger(__name__)


def build_evidence_records(
    *,
    document: Any,
    chunks: Sequence[Any],
    vectors: Sequence[list[float]],
    embedding_model: str,
) -> list[EvidenceRecord]:
    if len(chunks) != len(vectors):
        raise ValueError(f"vector count {len(vectors)} does not match chunk count {len(chunks)}")
    records: list[EvidenceRecord] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        records.append(
            EvidenceRecord(
                chunk_id=str(chunk.id),
                sku_id=chunk.sku_id,
                document_version_id=str(chunk.document_version_id),
                tenant_id=document.tenant_id,
                document_type=chunk.document_type,
                permission_scope=chunk.permission_scope,
                embedding_model=embedding_model,
                content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                content=chunk.content,
                embedding=vector,
                parent_chunk_id=str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
            )
        )
    return records


def index_chunks(
    *,
    document: Any,
    chunks: Sequence[Any],
    embedding_model: str,
    embedder: Any,
    store: Any,
) -> int:
    store.ensure_collection()
    vectors = embedder.embed_documents([chunk.content for chunk in chunks])
    records = build_evidence_records(
        document=document,
        chunks=chunks,
        vectors=vectors,
        embedding_model=embedding_model,
    )
    return store.upsert(records)


def get_evidence_store() -> MilvusEvidenceStore:
    settings = get_settings()
    return MilvusEvidenceStore.connect(
        uri=settings.milvus_uri,
        token=settings.milvus_token,
        collection_name=settings.milvus_collection,
        dimension=settings.embedding_dimension,
    )


def run_index_job(db: Session, job_id: Any, version_id: Any) -> EvidenceIndexJob:
    job = db.get(EvidenceIndexJob, job_id)
    if not job:
        raise ValueError("索引任务不存在")
    job.status = IndexStatus.RUNNING
    job.started_at = datetime.now(UTC)
    db.commit()
    try:
        version = db.get(DocumentVersion, version_id)
        if not version:
            raise ValueError("文档版本不存在")
        document = db.get(Document, version.document_id)
        if not document:
            raise ValueError("文档不存在")
        run_id = db.scalar(
            select(ChunkingRun.id)
            .join(DocumentChunk, DocumentChunk.chunking_run_id == ChunkingRun.id)
            .where(
                DocumentChunk.document_version_id == version.id,
                DocumentChunk.chunk_level == "child",
                ChunkingRun.status == ChunkingStatus.SUCCEEDED,
            )
            .order_by(ChunkingRun.created_at.desc())
            .limit(1)
        )
        if not run_id:
            raise ValueError("文档尚无可用的成功切片")
        chunks = db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.chunking_run_id == run_id, DocumentChunk.chunk_level == "child")
            .order_by(DocumentChunk.position)
        ).all()
        embedder = get_embedding_model()
        if embedder is None:
            raise ValueError("DASHSCOPE_API_KEY 未配置，无法生成证据向量")
        store = get_evidence_store()
        store.ensure_collection()
        store.delete_version(tenant_id=document.tenant_id, document_version_id=str(version.id))
        job.indexed_count = index_chunks(
            document=document,
            chunks=chunks,
            embedding_model=job.embedding_model,
            embedder=embedder,
            store=store,
        )
        job.status = IndexStatus.SUCCEEDED
        job.error_message = None
    except Exception as exc:
        job.status = IndexStatus.FAILED
        job.error_message = str(exc)[:2000]
        logger.exception("evidence_index_job_failed job_id=%s version_id=%s", job_id, version_id)
    job.finished_at = datetime.now(UTC)
    db.commit()
    db.refresh(job)
    return job

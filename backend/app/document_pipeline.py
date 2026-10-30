from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy.orm import Session

from app.chunk_service import create_chunking_run
from app.models import ChunkingStatus, Document, DocumentVersion, EvidenceIndexJob, IndexStatus, ParseJob, ParseStatus


DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100


@dataclass(frozen=True)
class PipelineStatusSnapshot:
    parse_status: str | None
    chunk_status: str | None
    index_status: str | None
    pipeline_status: str


def queue_parse_job(db: Session, redis_client: Redis | Any, version_id: UUID) -> ParseJob:
    job = ParseJob(document_version_id=version_id, parser_name="queued", status=ParseStatus.PENDING)
    db.add(job)
    db.commit()
    redis_client.rpush("product:parse:queue", f"{job.id}:{version_id}")
    return job


def queue_index_job(db: Session, redis_client: Redis | Any, version_id: UUID, *, settings: Any) -> EvidenceIndexJob:
    job = EvidenceIndexJob(
        document_version_id=version_id,
        status=IndexStatus.PENDING,
        embedding_model=settings.embedding_model,
        collection_name=settings.milvus_collection,
    )
    db.add(job)
    db.commit()
    redis_client.rpush("product:index:queue", f"{job.id}:{version_id}")
    return job


def continue_document_pipeline(
    db: Session,
    *,
    store: Any,
    redis_client: Redis | Any,
    version_id: UUID,
    settings: Any,
) -> EvidenceIndexJob:
    version = db.get(DocumentVersion, version_id)
    if not version:
        raise ValueError("文档版本不存在")
    document = db.get(Document, version.document_id)
    if not document:
        raise ValueError("文档不存在")
    create_chunking_run(
        db,
        store,
        document,
        version,
        chunk_size=DEFAULT_CHUNK_SIZE,
        chunk_overlap=DEFAULT_CHUNK_OVERLAP,
    )
    return queue_index_job(db, redis_client, version.id, settings=settings)


def summarize_pipeline_status(*, parse_job: Any = None, chunk_run: Any = None, index_job: Any = None) -> PipelineStatusSnapshot:
    parse_status = getattr(parse_job, "status", None)
    chunk_status = getattr(chunk_run, "status", None)
    index_status = getattr(index_job, "status", None)

    if parse_status == ParseStatus.FAILED or chunk_status in {ChunkingStatus.FAILED, "failed"} or index_status == IndexStatus.FAILED:
        pipeline_status = "failed"
    elif index_status == IndexStatus.SUCCEEDED:
        pipeline_status = "ready"
    elif index_status in {IndexStatus.PENDING, IndexStatus.RUNNING, "pending", "running"}:
        pipeline_status = "indexing"
    elif chunk_status in {ChunkingStatus.PENDING, ChunkingStatus.RUNNING, "pending", "running"}:
        pipeline_status = "chunking"
    elif chunk_status in {ChunkingStatus.SUCCEEDED, "succeeded", "partially_succeeded"}:
        pipeline_status = "chunked"
    elif parse_status in {ParseStatus.PENDING, ParseStatus.RUNNING, "pending", "running"}:
        pipeline_status = "parsing"
    elif parse_status == ParseStatus.SUCCEEDED:
        pipeline_status = "parsed"
    else:
        pipeline_status = "uploaded"

    return PipelineStatusSnapshot(
        parse_status=parse_status.value if hasattr(parse_status, "value") else parse_status,
        chunk_status=chunk_status.value if hasattr(chunk_status, "value") else chunk_status,
        index_status=index_status.value if hasattr(index_status, "value") else index_status,
        pipeline_status=pipeline_status,
    )

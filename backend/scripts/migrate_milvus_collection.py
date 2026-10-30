"""把旧证据集合（字段 candidate_id）复制到新集合（字段 sku_id），不重新计算向量。

用法：uv run python scripts/migrate_milvus_collection.py --source talent_evidence_v2
目标集合取 settings.milvus_collection（默认 product_evidence_v1）。content_sparse 由 BM25 Function 写入时重新生成。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.milvus_store import EvidenceRecord, MilvusEvidenceStore  # noqa: E402

FIELDS = [
    "chunk_id", "document_version_id", "tenant_id", "document_type", "permission_scope",
    "embedding_model", "content_hash", "content", "parent_chunk_id", "page_start", "page_end",
    "is_active", "embedding",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    settings = get_settings()
    if args.source == settings.milvus_collection:
        raise SystemExit("source and target collection are the same; set MILVUS_COLLECTION to the new name")
    target = MilvusEvidenceStore.connect(
        uri=settings.milvus_uri, token=settings.milvus_token,
        collection_name=settings.milvus_collection, dimension=settings.embedding_dimension,
    )
    target.ensure_collection()
    client = target.client
    if not client.has_collection(args.source):
        raise SystemExit(f"source collection {args.source} not found")
    client.load_collection(args.source)
    iterator = client.query_iterator(
        collection_name=args.source, filter="", output_fields=FIELDS + ["candidate_id"], batch_size=200,
    )
    total = 0
    while True:
        batch = iterator.next()
        if not batch:
            iterator.close()
            break
        records = [
            EvidenceRecord(**{field: row.get(field) for field in FIELDS}, sku_id=row["candidate_id"])
            for row in batch
        ]
        total += target.upsert(records)
    print({"source": args.source, "target": settings.milvus_collection, "copied": total})


if __name__ == "__main__":
    main()

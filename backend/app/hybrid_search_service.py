from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.milvus_store import EvidenceFilter, EvidenceSearchResult


@dataclass(frozen=True)
class RankedEvidence:
    chunk_id: str
    sku_id: str
    content: str
    rrf_score: float
    rerank_score: float
    metadata: dict[str, Any]


def rerank_evidence(
    *,
    query: str,
    skus: list[EvidenceSearchResult],
    reranker: Any,
    top_n: int,
) -> list[RankedEvidence]:
    if not skus:
        return []
    hits = reranker.rerank(
        query=query,
        documents=[item.content for item in skus],
        top_n=min(top_n, len(skus)),
    )
    ranked: list[RankedEvidence] = []
    for hit in hits:
        item = skus[hit.index]
        ranked.append(
            RankedEvidence(
                chunk_id=item.chunk_id,
                sku_id=item.sku_id,
                content=item.content,
                rrf_score=item.score,
                rerank_score=hit.score,
                metadata=item.metadata,
            )
        )
    return ranked


def hybrid_search_evidence(
    *,
    query: str,
    filters: EvidenceFilter,
    store: Any,
    embedder: Any,
    reranker: Any,
    limit: int = 10,
    ef: int = 80,
    rrf_k: int = 60,
    rerank_top_n: int = 20,
) -> list[RankedEvidence]:
    query_vector = list(embedder.embed_query(query))
    fused = store.hybrid_search(
        query_text=query,
        query_vector=query_vector,
        filters=filters,
        limit=rerank_top_n,
        ef=ef,
        rrf_k=rrf_k,
    )
    ranked = rerank_evidence(query=query, skus=fused, reranker=reranker, top_n=rerank_top_n)
    return ranked[:limit]

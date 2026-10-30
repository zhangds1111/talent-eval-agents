from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker


class MilvusClientProtocol(Protocol):
    def has_collection(self, collection_name: str) -> bool: ...

    def create_schema(self, **kwargs: Any) -> Any: ...

    def prepare_index_params(self) -> Any: ...

    def create_collection(self, **kwargs: Any) -> Any: ...

    def upsert(self, **kwargs: Any) -> Any: ...

    def search(self, **kwargs: Any) -> Any: ...

    def hybrid_search(self, **kwargs: Any) -> Any: ...

    def delete(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class EvidenceRecord:
    chunk_id: str
    sku_id: str
    document_version_id: str
    tenant_id: str
    document_type: str
    permission_scope: str
    embedding_model: str
    content_hash: str
    content: str
    embedding: list[float]
    parent_chunk_id: str | None
    page_start: int | None
    page_end: int | None
    is_active: bool = True


@dataclass(frozen=True)
class EvidenceFilter:
    tenant_id: str
    permission_scopes: list[str]
    sku_ids: list[str] | None = None
    document_types: list[str] | None = None
    document_version_id: str | None = None


@dataclass(frozen=True)
class EvidenceSearchResult:
    chunk_id: str
    sku_id: str
    content: str
    score: float
    metadata: dict[str, Any]


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _in_expression(field: str, values: Sequence[str]) -> str:
    return f"{field} in [{', '.join(_literal(value) for value in values)}]"


def build_filter_expression(filters: EvidenceFilter) -> str:
    if not filters.tenant_id:
        raise ValueError("tenant_id is required")
    if not filters.permission_scopes:
        raise ValueError("permission_scopes must not be empty")
    parts = [
        f"tenant_id == {_literal(filters.tenant_id)}",
        _in_expression("permission_scope", filters.permission_scopes),
    ]
    if filters.sku_ids:
        parts.append(_in_expression("sku_id", filters.sku_ids))
    if filters.document_types:
        parts.append(_in_expression("document_type", filters.document_types))
    if filters.document_version_id:
        parts.append(f"document_version_id == {_literal(filters.document_version_id)}")
    parts.append("is_active == true")
    return " and ".join(parts)


class MilvusEvidenceStore:
    output_fields = [
        "chunk_id",
        "sku_id",
        "document_version_id",
        "tenant_id",
        "document_type",
        "permission_scope",
        "embedding_model",
        "content_hash",
        "content",
        "parent_chunk_id",
        "page_start",
        "page_end",
    ]

    def __init__(
        self,
        *,
        client: MilvusClientProtocol,
        collection_name: str,
        dimension: int,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.dimension = dimension

    @classmethod
    def connect(cls, *, uri: str, token: str, collection_name: str, dimension: int) -> "MilvusEvidenceStore":
        return cls(
            client=MilvusClient(uri=uri, token=token),
            collection_name=collection_name,
            dimension=dimension,
        )

    def ensure_collection(self) -> None:
        if self.client.has_collection(self.collection_name):
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=36)
        schema.add_field(field_name="sku_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="document_version_id", datatype=DataType.VARCHAR, max_length=36)
        schema.add_field(field_name="tenant_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="document_type", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="permission_scope", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="embedding_model", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field(field_name="parent_chunk_id", datatype=DataType.VARCHAR, max_length=36, nullable=True)
        schema.add_field(field_name="page_start", datatype=DataType.INT64, nullable=True)
        schema.add_field(field_name="page_end", datatype=DataType.INT64, nullable=True)
        schema.add_field(field_name="is_active", datatype=DataType.BOOL)
        schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field(field_name="content_sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="content_bm25",
                function_type=FunctionType.BM25,
                input_field_names=["content"],
                output_field_names=["content_sparse"],
            )
        )
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_name="evidence_embedding_hnsw",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 128},
        )
        index_params.add_index(
            field_name="content_sparse",
            index_name="evidence_content_bm25",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
            consistency_level="Bounded",
        )

    def upsert(self, records: Sequence[EvidenceRecord]) -> int:
        if not records:
            return 0
        for record in records:
            if len(record.embedding) != self.dimension:
                raise ValueError(
                    f"embedding dimension {len(record.embedding)} does not match collection dimension {self.dimension}"
                )
        self.client.upsert(collection_name=self.collection_name, data=[asdict(record) for record in records])
        return len(records)

    def _to_results(self, rows: Any) -> list[EvidenceSearchResult]:
        results: list[EvidenceSearchResult] = []
        for hit in rows[0] if rows else []:
            entity = dict(hit.get("entity") or {})
            results.append(
                EvidenceSearchResult(
                    chunk_id=str(hit.get("id") or entity.get("chunk_id", "")),
                    sku_id=str(entity.get("sku_id", "")),
                    content=str(entity.get("content", "")),
                    score=float(hit.get("distance", 0.0)),
                    metadata=entity,
                )
            )
        return results

    def search(
        self,
        query_vector: list[float],
        *,
        filters: EvidenceFilter,
        limit: int = 10,
        ef: int = 80,
        consistency_level: str = "Bounded",
    ) -> list[EvidenceSearchResult]:
        if len(query_vector) != self.dimension:
            raise ValueError(
                f"query dimension {len(query_vector)} does not match collection dimension {self.dimension}"
            )
        rows = self.client.search(
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field="embedding",
            filter=build_filter_expression(filters),
            limit=limit,
            output_fields=self.output_fields,
            search_params={"metric_type": "COSINE", "params": {"ef": ef}},
            consistency_level=consistency_level,
        )
        return self._to_results(rows)

    def sparse_search(
        self,
        query_text: str,
        *,
        filters: EvidenceFilter,
        limit: int = 10,
        consistency_level: str = "Bounded",
    ) -> list[EvidenceSearchResult]:
        rows = self.client.search(
            collection_name=self.collection_name,
            data=[query_text],
            anns_field="content_sparse",
            filter=build_filter_expression(filters),
            limit=limit,
            output_fields=self.output_fields,
            search_params={"metric_type": "BM25"},
            consistency_level=consistency_level,
        )
        return self._to_results(rows)

    def hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        *,
        filters: EvidenceFilter,
        limit: int = 10,
        ef: int = 80,
        rrf_k: int = 60,
        consistency_level: str = "Bounded",
    ) -> list[EvidenceSearchResult]:
        if len(query_vector) != self.dimension:
            raise ValueError(
                f"query dimension {len(query_vector)} does not match collection dimension {self.dimension}"
            )
        filter_expression = build_filter_expression(filters)
        dense_req = AnnSearchRequest(
            data=[query_vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": ef}},
            limit=limit * 2,
            filter=filter_expression,
        )
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field="content_sparse",
            param={"metric_type": "BM25"},
            limit=limit * 2,
            filter=filter_expression,
        )
        rows = self.client.hybrid_search(
            collection_name=self.collection_name,
            reqs=[dense_req, sparse_req],
            ranker=RRFRanker(k=rrf_k),
            limit=limit,
            output_fields=self.output_fields,
            consistency_level=consistency_level,
        )
        return self._to_results(rows)

    def delete_version(self, *, tenant_id: str, document_version_id: str) -> None:
        if not self.client.has_collection(self.collection_name):
            return
        expression = (
            f"tenant_id == {_literal(tenant_id)} and "
            f"document_version_id == {_literal(document_version_id)}"
        )
        self.client.delete(collection_name=self.collection_name, filter=expression)

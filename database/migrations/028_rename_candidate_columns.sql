-- 选品化改名：candidate_id 统一为 sku_id
ALTER TABLE documents RENAME COLUMN candidate_id TO sku_id;
ALTER TABLE document_chunks RENAME COLUMN candidate_id TO sku_id;
ALTER INDEX IF EXISTS idx_document_chunks_candidate RENAME TO idx_document_chunks_sku;
ALTER TABLE evaluation_annotations RENAME COLUMN golden_candidate TO golden_sku;

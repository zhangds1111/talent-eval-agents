from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    sku_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    document_type: str = Field(min_length=1, max_length=64)
    source: str = "manual_upload"
    permission_scope: str = "hr_private"
    confidentiality_level: str = "internal"

from typing import Literal
from pydantic import BaseModel, Field, field_validator


class DocumentInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=500_000)
    collection: str = Field(default="general", min_length=1, max_length=100)
    source: str = Field(default="", max_length=1000)

    @field_validator("external_id", "title", "content", "collection")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class BatchInput(BaseModel):
    documents: list[DocumentInput] = Field(min_length=1, max_length=100)


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["hybrid", "keyword", "vector"] = "hybrid"
    limit: int = Field(8, ge=1, le=50)
    collection: str | None = Field(None, max_length=100)

    @field_validator("query")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class TenantInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class KeyInput(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    scope: Literal["read", "write"] = "read"

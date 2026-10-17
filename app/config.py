from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./distributedrag.db"
    search_backend: str = "local"
    opensearch_url: str = "http://localhost:9200"
    opensearch_urls: str = ""
    opensearch_username: str = ""
    opensearch_password: str = ""
    index_name: str = "distributedrag-v1"
    index_shards: int = Field(4, ge=1, le=128)
    index_replicas: int = Field(0, ge=0, le=5)
    redis_url: str = ""
    admin_key: str = ""
    embedding_provider: str = "hash"
    embedding_url: str = "http://localhost:8080/v1"
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimensions: int = Field(384, ge=32, le=4096)
    llm_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.3-70b-versatile"
    chunk_words: int = Field(240, ge=50, le=1000)
    chunk_overlap: int = Field(40, ge=0)
    lease_seconds: int = Field(90, ge=3)
    max_attempts: int = Field(5, ge=1, le=20)
    max_document_chars: int = Field(500_000, ge=1000)
    max_upload_bytes: int = Field(10_000_000, ge=1000)
    max_pending_jobs: int = Field(10_000, ge=1)
    cache_ttl: int = Field(60, ge=1)
    local_max_chunks: int = Field(20_000, ge=1)
    rate_limit: int = Field(120, ge=1)

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.chunk_overlap >= self.chunk_words:
            raise ValueError("chunk_overlap must be smaller than chunk_words")
        if self.search_backend not in {"local", "opensearch"}:
            raise ValueError("search_backend must be local or opensearch")
        if self.embedding_provider not in {"hash", "openai"}:
            raise ValueError("embedding_provider must be hash or openai")
        return self


@lru_cache
def get_settings():
    return Settings()

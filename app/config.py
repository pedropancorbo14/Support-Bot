from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del servicio. Todo se lee de variables de entorno (o de `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"

    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://support:support@localhost:5432/support",
    )

    llm_base_url: str = "http://ollama:11434/v1"
    llm_api_key: SecretStr | None = None
    llm_chat_model: str = "qwen3:8b"
    llm_embedding_model: str = "nomic-embed-text"
    llm_temperature: float = Field(default=0.3, ge=0, le=2)
    llm_num_ctx: int = Field(default=8192, gt=0)
    llm_timeout: float = Field(default=60.0, gt=0)
    llm_reasoning_effort: str | None = None

    embedding_dimensions: int = Field(default=768, gt=0)
    embedding_document_prefix: str = "search_document: "
    embedding_query_prefix: str = "search_query: "

    chunk_size_tokens: int = Field(default=500, gt=0)
    chunk_overlap_tokens: int = Field(default=50, ge=0)

    rag_top_k: int = Field(default=4, gt=0)
    rag_min_score: float = Field(default=0.5, ge=-1, le=1)

    worker_poll_interval: float = Field(default=2.0, gt=0)

    system_prompt_path: Path = Path("prompts/system.md")
    history_max_messages: int = Field(default=10, ge=0)
    history_max_tokens: int = Field(default=2000, ge=0)

    public_base_url: str = "http://localhost:8000"
    webhook_timeout: float = Field(default=10.0, gt=0)
    webhook_max_attempts: int = Field(default=8, gt=0)
    webhook_retry_base_seconds: float = Field(default=30.0, gt=0)

    redis_url: str = "redis://localhost:6379/0"
    jwt_max_ttl_seconds: int = Field(default=900, gt=0)
    jwt_leeway_seconds: int = Field(default=30, ge=0)
    rate_limit_user_per_minute: int = Field(default=30, gt=0)
    rate_limit_ip_per_minute: int = Field(default=120, gt=0)
    cors_cache_seconds: float = Field(default=30.0, ge=0)
    widget_poll_interval: float = Field(default=2.0, gt=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _check_chunking(self) -> Self:
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS debe ser menor que CHUNK_SIZE_TOKENS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

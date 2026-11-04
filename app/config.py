from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del servicio. Todo se lee de variables de entorno (o de `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

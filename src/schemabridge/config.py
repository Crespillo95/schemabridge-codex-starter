"""Typed runtime configuration."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings.

    Secrets are intentionally not given non-empty defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = Field(default="development", alias="SCHEMABRIDGE_ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="SCHEMABRIDGE_LOG_LEVEL")
    max_query_rows: int = Field(default=500, ge=1, le=10_000, alias="SCHEMABRIDGE_MAX_QUERY_ROWS")
    statement_timeout_ms: int = Field(
        default=5_000,
        ge=100,
        le=60_000,
        alias="SCHEMABRIDGE_STATEMENT_TIMEOUT_MS",
    )
    max_query_tables: int = Field(default=3, ge=1, le=3, alias="SCHEMABRIDGE_MAX_QUERY_TABLES")
    draft_store_path: Path = Field(
        default=Path(".local/schemabridge.db"),
        alias="SCHEMABRIDGE_DRAFT_STORE_PATH",
    )
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    postgres_reader_user: str = Field(
        default="schemabridge_reader",
        alias="POSTGRES_READER_USER",
    )
    datahub_gms_url: str = Field(default="http://localhost:8080", alias="DATAHUB_GMS_URL")
    datahub_gms_token: str | None = Field(default=None, alias="DATAHUB_GMS_TOKEN")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    llm_model: str | None = Field(default=None, alias="SCHEMABRIDGE_LLM_MODEL")


def get_settings() -> Settings:
    """Build settings at the composition boundary."""

    return Settings()

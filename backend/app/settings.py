"""Typed backend configuration, resolved relative to the repository, not cwd."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    """A safe startup error that never includes secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    api_port: int = Field(default=4000, ge=1, le=65535)
    web_origin: str = "http://localhost:3000"
    mongodb_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017/ai_engagement")
    business_config_dir: Path = REPOSITORY_ROOT / "businesses"
    ai_provider: Literal["gemini"] = "gemini"
    ai_chat_enabled: bool = True
    conversational_replies: bool = True
    gemini_api_key: SecretStr | None = None
    # Generation is configured for M07 qualification, not implemented by these settings.
    gemini_chat_model: str = "gemini-3.5-flash-lite"
    gemini_summary_model: str = "gemini-3.5-flash-lite"
    gemini_embedding_model: Literal["gemini-embedding-001"] = "gemini-embedding-001"
    embedding_dimensions: int = Field(default=1536, ge=128, le=3072)
    rag_top_k: int = Field(default=4, ge=1, le=20)
    rag_min_score: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    rag_max_chunks: int = Field(default=499, ge=1, le=499)
    query_embedding_ttl_seconds: float = Field(default=120, ge=0, le=600, allow_inf_nan=False)
    rag_snapshot_ttl_seconds: float = Field(default=30, ge=0, le=300, allow_inf_nan=False)
    chunk_tokens: int = Field(default=400, ge=8, le=2048)
    chunk_overlap: int = Field(default=60, ge=0)
    agent_max_rounds: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=10, ge=1, le=50)
    provider_chat_concurrency: int = Field(default=2, ge=1, le=16)
    provider_background_concurrency: int = Field(default=1, ge=1, le=8)
    provider_queue_limit: int = Field(default=8, ge=0, le=64)
    provider_queue_seconds: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)
    provider_generation_seconds: float = Field(default=45, gt=0, le=45, allow_inf_nan=False)
    provider_embedding_seconds: float = Field(default=35, gt=0, le=35, allow_inf_nan=False)
    provider_generation_attempt_seconds: float = Field(default=20, gt=0, le=20, allow_inf_nan=False)
    provider_embedding_attempt_seconds: float = Field(default=15, gt=0, le=15, allow_inf_nan=False)
    provider_attempts: int = Field(default=2, ge=1, le=3)
    provider_retry_base_seconds: float = Field(default=0.25, gt=0, le=5, allow_inf_nan=False)
    provider_retry_max_seconds: float = Field(default=5, gt=0, le=10, allow_inf_nan=False)
    automation_batch_jobs: int = Field(default=4, ge=1, le=16)
    automation_batch_ms: int = Field(default=1000, ge=10, le=5000)
    automation_poll_ms: int = Field(default=2000, ge=100)
    automation_enabled: bool = True
    automation_max_attempts: int = Field(default=3, ge=1, le=5)
    automation_retry_seconds: int = Field(default=30, ge=1, le=300)
    enable_test_failures: bool = False
    # Server-owned business -> secret mapping. Empty disables the local mock ingress.
    mock_channel_keys: dict[str, SecretStr] = Field(default_factory=dict, repr=False)

    @field_validator("mock_channel_keys")
    @classmethod
    def validate_mock_keys(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        keys = [item.get_secret_value() for item in value.values()]
        if any(
            not 32 <= len(key) <= 128 or not key.isascii() or key.strip() != key for key in keys
        ):
            raise ValueError("Mock keys require 32-128 ASCII characters without edge whitespace")
        if len(keys) != len(set(keys)):
            raise ValueError("Mock keys must be unique per business")
        return value

    @field_validator("business_config_dir")
    @classmethod
    def resolve_business_path(cls, value: Path) -> Path:
        return (REPOSITORY_ROOT / value).resolve() if not value.is_absolute() else value.resolve()

    @field_validator("gemini_chat_model", "gemini_summary_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,100}", value):
            raise ValueError("Use a bare Gemini model ID, not a URL or path")
        return value

    @field_validator("web_origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        _ = parsed.port  # Reject malformed port values rather than accept an invalid origin.
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WEB_ORIGIN must be one HTTP(S) origin without credentials or a path")
        return value.rstrip("/")

    @field_validator("mongodb_uri")
    @classmethod
    def validate_database_uri(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.hostname:
            raise ValueError("MONGODB_URI must be a MongoDB connection string")
        return value

    @model_validator(mode="after")
    def validate_boundaries(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_tokens:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_TOKENS")
        if self.provider_generation_attempt_seconds > self.provider_generation_seconds or (
            self.provider_embedding_attempt_seconds > self.provider_embedding_seconds
        ):
            raise ValueError("Provider attempt budget must not exceed its total call budget")
        if self.provider_retry_base_seconds > self.provider_retry_max_seconds:
            raise ValueError("Provider retry base must not exceed the maximum retry delay")
        if self.app_env == "production" and self.enable_test_failures:
            raise ValueError("Test failure injection cannot be enabled in production")
        return self


def load_settings() -> Settings:
    """Keep startup errors useful without echoing secret-bearing environment input."""
    try:
        return Settings()
    except (ValidationError, ValueError):
        raise ConfigurationError(
            "Invalid backend configuration. Check .env.example and documented setting limits."
        ) from None

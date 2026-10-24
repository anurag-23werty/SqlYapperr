"""Application configuration.

Settings come from environment variables (optionally loaded from ``.env``).
App settings use the ``SQLY_`` prefix; the Groq key also accepts the standard
``GROQ_API_KEY`` name. Settings are immutable after start-up: nothing at runtime,
and in particular nothing produced by the LLM, can change limits or policy.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_PREFIX = "SQLY_"
# Variables accepted without the prefix (conventional names).
_ALIASES = {"groq_api_key": "GROQ_API_KEY"}

# Hard ceiling from the specification. Configuration may lower it, never raise it.
HARD_MAX_CORRECTIONS = 2


class Settings(BaseModel):
    """Validated, frozen runtime settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: Literal["development", "production"] = "development"

    # Storage
    data_dir: Path = Path("data")
    db_path: Path = Path("data/sample.db")       # bundled sample dataset (read-only demo)
    app_db_path: Path = Path("data/app.db")      # users, sessions, datasets, query log
    auto_seed: bool = True

    # Auth & cookies
    secret_key: SecretStr = SecretStr("dev-insecure-change-me")
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 90)
    cookie_secure: bool = False                  # must be true behind HTTPS
    cookie_samesite: Literal["lax", "strict"] = "lax"
    allow_signup: bool = True
    password_min_length: int = Field(default=10, ge=8, le=128)
    login_rate_per_minute: int = Field(default=10, ge=1, le=1_000)
    chat_rate_per_minute: int = Field(default=20, ge=1, le=1_000)

    # Uploads
    max_upload_mb: int = Field(default=20, ge=1, le=500)
    max_files_per_dataset: int = Field(default=10, ge=1, le=50)
    max_rows_per_table: int = Field(default=500_000, ge=1, le=10_000_000)
    max_columns_per_table: int = Field(default=100, ge=1, le=500)
    max_datasets_per_user: int = Field(default=20, ge=1, le=1_000)

    # Result limits
    max_rows: int = Field(default=100, ge=1, le=10_000)
    default_limit: int = Field(default=50, ge=1, le=10_000)
    max_offset: int = Field(default=10_000, ge=0, le=1_000_000)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=50_000_000)

    # Execution limits (separate concern from authorization)
    query_timeout_ms: int = Field(default=3_000, ge=10, le=60_000)
    progress_handler_ops: int = Field(default=1_000, ge=100, le=1_000_000)

    # Request limits
    max_body_bytes: int = Field(default=16_384, ge=256, le=1_048_576)
    max_question_chars: int = Field(default=1_000, ge=1, le=10_000)
    max_sql_chars: int = Field(default=5_000, ge=16, le=100_000)

    # Query complexity
    max_nesting_depth: int = Field(default=3, ge=0, le=10)
    max_joins: int = Field(default=6, ge=0, le=20)
    max_ast_nodes: int = Field(default=600, ge=10, le=10_000)
    max_set_operations: int = Field(default=3, ge=0, le=10)
    max_string_literal_chars: int = Field(default=256, ge=1, le=10_000)

    # LLM (Groq, OpenAI-compatible API). "mock" works offline for demos and tests.
    llm_provider: Literal["groq", "mock"] = "groq"
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Llama 3.x models were retired from Groq's free tier on 2026-08-16; GPT-OSS replaces them.
    groq_sql_model: str = "openai/gpt-oss-120b"       # drafts SQL
    groq_fast_model: str = "openai/gpt-oss-20b"       # routing + answer summaries
    groq_reasoning_effort: Literal["low", "medium", "high"] = "medium"  # for SQL drafting (GPT-OSS only)
    llm_timeout_s: float = Field(default=30.0, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    max_corrections: int = Field(default=HARD_MAX_CORRECTIONS, ge=0, le=HARD_MAX_CORRECTIONS)
    summarize_results: bool = True
    history_turns: int = Field(default=3, ge=0, le=10)

    # Operations
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    rate_limit_backend: Literal["memory", "shared"] = "shared"   # shared = across all workers on the host
    metrics_token: SecretStr | None = None                       # bearer token for GET /metrics
    otel_exporter_endpoint: str | None = None                    # e.g. http://jaeger:4318 (OTLP/HTTP)
    otel_service_name: str = "sqlyapper"

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.default_limit > self.max_rows:
            raise ValueError("default_limit must be <= max_rows")
        if self.llm_provider == "groq" and not (self.groq_api_key and self.groq_api_key.get_secret_value()):
            raise ValueError("GROQ_API_KEY is required when SQLY_LLM_PROVIDER=groq (or set SQLY_LLM_PROVIDER=mock)")
        if self.env == "production":
            if self.secret_key.get_secret_value() in {"", "dev-insecure-change-me"} or len(
                self.secret_key.get_secret_value()
            ) < 32:
                raise ValueError("SQLY_SECRET_KEY must be set to a random value of 32+ characters in production")
            if not self.cookie_secure:
                raise ValueError("SQLY_COOKIE_SECURE must be true in production")
        return self

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for name in cls.model_fields:
            raw = env.get(_PREFIX + name.upper())
            if (raw is None or raw == "") and name in _ALIASES:
                raw = env.get(_ALIASES[name])
            if raw is not None and raw != "":
                values[name] = raw
        return cls.model_validate(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    from app.env import load_env_file

    load_env_file(".env")
    return Settings.from_env()

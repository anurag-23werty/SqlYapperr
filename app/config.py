"""Application configuration.

All settings come from environment variables prefixed with ``SQLY_``. Settings
are immutable after construction: nothing at runtime (and in particular nothing
produced by the LLM) can change limits or security policy.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_PREFIX = "SQLY_"

# Hard ceiling from the Phase 1 specification. Configuration may lower it, never raise it.
HARD_MAX_CORRECTIONS = 2


class Settings(BaseModel):
    """Validated, frozen runtime settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Database
    db_path: Path = Path("data/sqlyapper.db")
    auto_seed: bool = True

    # Result limits
    max_rows: int = Field(default=100, ge=1, le=10_000)
    default_limit: int = Field(default=50, ge=1, le=10_000)
    max_offset: int = Field(default=10_000, ge=0, le=1_000_000)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=50_000_000)

    # Execution resource limits (separate concern from authorization)
    query_timeout_ms: int = Field(default=2_000, ge=10, le=60_000)
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

    # LLM
    llm_provider: Literal["mock", "anthropic"] = "mock"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_timeout_s: float = Field(default=30.0, gt=0, le=300)
    max_corrections: int = Field(default=HARD_MAX_CORRECTIONS, ge=0, le=HARD_MAX_CORRECTIONS)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.default_limit > self.max_rows:
            raise ValueError("default_limit must be <= max_rows")
        if self.llm_provider != "mock" and (not self.llm_model or not self.llm_api_key):
            raise ValueError("llm_model and llm_api_key are required for non-mock providers")
        return self

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for name in cls.model_fields:
            raw = env.get(_PREFIX + name.upper())
            if raw is not None and raw != "":
                values[name] = raw
        return cls.model_validate(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()

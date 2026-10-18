"""Request/response models. Unknown fields are rejected on input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatRequest(_Strict):
    question: str = Field(min_length=1, max_length=10_000, description="Natural-language question")


class ValidateRequest(_Strict):
    sql: str = Field(min_length=1, max_length=100_000, description="SQL candidate to validate (never executed)")


class Issue(BaseModel):
    code: str
    message: str


class ValidateResponse(BaseModel):
    request_id: str
    valid: bool
    safe_sql: str | None = None
    effective_limit: int | None = None
    applied_default_limit: bool = False
    issues: list[Issue] = []
    executed: Literal[False] = False


class Attempt(BaseModel):
    attempt: int
    stage: str
    outcome: str
    codes: list[str] = []
    messages: list[str] = []


class ChatResponse(BaseModel):
    request_id: str
    status: Literal["ok", "rejected", "error"]
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    truncated: bool = False
    applied_default_limit: bool = False
    duration_ms: float | None = None
    attempts: list[Attempt] = []
    error: Issue | None = None


class ColumnOut(BaseModel):
    name: str
    type: str
    primary_key: bool


class ForeignKeyOut(BaseModel):
    column: str
    references: str


class TableOut(BaseModel):
    name: str
    columns: list[ColumnOut]
    foreign_keys: list[ForeignKeyOut]


class SchemaResponse(BaseModel):
    tables: list[TableOut]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    llm_provider: str
    version: str


class ErrorResponse(BaseModel):
    request_id: str
    error: Issue

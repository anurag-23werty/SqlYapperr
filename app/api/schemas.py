"""Request/response models. Unknown fields are rejected on input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ auth
class SignUpRequest(_Strict):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class SignInRequest(_Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    email: str
    name: str


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


# -------------------------------------------------------------- datasets
class DatasetOut(BaseModel):
    id: str
    name: str
    created_at: float
    tables: list[dict[str, Any]]


class ColumnVisibilityRequest(_Strict):
    table: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    column: str | None = Field(default=None, max_length=64, pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    hidden: bool


# ------------------------------------------------------------------ chat
class ChatRequest(_Strict):
    question: str = Field(min_length=1, max_length=10_000)
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ValidateRequest(_Strict):
    sql: str = Field(min_length=1, max_length=100_000)


class Issue(BaseModel):
    code: str
    message: str


class Attempt(BaseModel):
    attempt: int
    stage: str
    outcome: str
    codes: list[str] = []
    messages: list[str] = []


class Usage(BaseModel):
    latency_ms: float = 0.0
    generations: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatResponse(BaseModel):
    request_id: str
    conversation_id: str
    kind: Literal["answer", "out_of_scope", "chitchat", "blocked", "rejected", "error"]
    message: str | None = None
    answer: str | None = None
    sql: str | None = None
    explanation: str | None = None
    assumptions: list[str] = []
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    truncated: bool = False
    applied_default_limit: bool = False
    duration_ms: float | None = None
    plan: list[str] = []
    attempts: list[Attempt] = []
    error_code: str | None = None
    usage: Usage = Usage()


class ValidateResponse(BaseModel):
    request_id: str
    valid: bool
    safe_sql: str | None = None
    effective_limit: int | None = None
    applied_default_limit: bool = False
    issues: list[Issue] = []
    executed: Literal[False] = False


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    llm_provider: str
    graph_engine: Literal["langgraph", "builtin"]
    version: str

"""HTTP endpoints. Handlers are sync so blocking SQLite work runs in the thread pool."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    Attempt,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    Issue,
    SchemaResponse,
    ValidateRequest,
    ValidateResponse,
)
from app.container import Container

router = APIRouter()


def _container(request: Request) -> Container:
    return request.app.state.container


def _request_id(request: Request) -> str:
    return request.state.request_id


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    c = _container(request)
    db_ok = c.database_ok()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="ok" if db_ok else "unavailable",
        llm_provider=c.generator.name,
        version=c.version,
    )


@router.get("/schema", response_model=SchemaResponse)
def schema(request: Request) -> SchemaResponse:
    return SchemaResponse.model_validate(_container(request).schema.to_dict())


@router.post("/validate", response_model=ValidateResponse)
def validate(body: ValidateRequest, request: Request) -> ValidateResponse:
    """Validate a SQL candidate. This endpoint never touches the database."""
    c = _container(request)
    if len(body.sql) > c.settings.max_sql_chars:
        raise HTTPException(status_code=422, detail=f"sql exceeds {c.settings.max_sql_chars} characters")
    result = c.validator.validate(body.sql)
    return ValidateResponse(
        request_id=_request_id(request),
        valid=result.ok,
        safe_sql=result.safe_sql,
        effective_limit=result.query.effective_limit if result.query else None,
        applied_default_limit=result.query.applied_default_limit if result.query else False,
        issues=[Issue(code=i.code, message=i.message) for i in result.issues],
    )


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
    c = _container(request)
    if len(body.question) > c.settings.max_question_chars:
        raise HTTPException(status_code=422, detail=f"question exceeds {c.settings.max_question_chars} characters")
    outcome = c.pipeline.run(body.question, request_id=_request_id(request))
    res = outcome.result
    return ChatResponse(
        request_id=outcome.request_id,
        status=outcome.status,
        sql=outcome.sql,
        explanation=outcome.explanation,
        columns=res.columns if res else [],
        rows=res.rows if res else [],
        row_count=res.row_count if res else 0,
        truncated=res.truncated if res else False,
        applied_default_limit=outcome.applied_default_limit,
        duration_ms=res.duration_ms if res else None,
        attempts=[
            Attempt(attempt=a.attempt, stage=a.stage, outcome=a.outcome, codes=list(a.codes), messages=list(a.messages))
            for a in outcome.attempts
        ],
        error=Issue(code=outcome.error_code, message=outcome.error_message or "")
        if outcome.error_code
        else None,
    )

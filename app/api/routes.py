"""HTTP endpoints. Handlers are sync so blocking SQLite/LLM work runs in the thread pool."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse

from app.agent.graph import AgentDeps, run_agent
from app.api.deps import client_ip, csrf_session, current_session, request_id, same_origin_only, services
from app.api.schemas import (
    Attempt,
    ChatRequest,
    ChatResponse,
    ColumnVisibilityRequest,
    DatasetOut,
    HealthResponse,
    Issue,
    SessionOut,
    SignInRequest,
    SignUpRequest,
    UserOut,
    ValidateRequest,
    ValidateResponse,
)
from app.auth.service import SESSION_COOKIE, AuthError, IssuedSession
from app.storage.app_db import SessionRecord
from app.storage.datasets import IngestError, public_schema

router = APIRouter()
STATIC_INDEX = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"

Session = Annotated[SessionRecord, Depends(current_session)]
CsrfSession = Annotated[SessionRecord, Depends(csrf_session)]


# ------------------------------------------------------------------ pages


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_INDEX, media_type="text/html")


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> RawResponse:
    return RawResponse(status_code=204)


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    svc = services(request)
    return HealthResponse(status="ok", llm_provider=svc.llm.name,
                          graph_engine="langgraph" if svc.use_langgraph else "builtin", version=svc.version)


# ------------------------------------------------------------------- auth


def _session_response(request: Request, issued: IssuedSession) -> JSONResponse:
    svc = services(request)
    body = SessionOut(user=UserOut(id=issued.user.id, email=issued.user.email, name=issued.user.name),
                      csrf_token=issued.csrf_token)
    resp = JSONResponse(body.model_dump())
    resp.set_cookie(
        SESSION_COOKIE, issued.token, max_age=svc.auth.ttl_seconds, path="/", httponly=True,
        secure=svc.settings.cookie_secure, samesite=svc.settings.cookie_samesite,
    )
    return resp


def _auth_error(err: AuthError) -> HTTPException:
    return HTTPException(status_code=err.status, detail={"code": err.code, "message": err.message})


@router.post("/api/auth/signup", dependencies=[Depends(same_origin_only)])
def sign_up(body: SignUpRequest, request: Request) -> JSONResponse:
    svc = services(request)
    if not svc.settings.allow_signup:
        raise HTTPException(status_code=403, detail="sign-up is disabled")
    if not svc.login_limiter.allow(f"signup:{client_ip(request)}"):
        raise HTTPException(status_code=429, detail="too many attempts, try again in a minute")
    try:
        issued = svc.auth.sign_up(body.email, body.name, body.password)
    except AuthError as err:
        raise _auth_error(err) from None
    if svc.settings.db_path.exists():
        try:
            svc.datasets.add_sample(issued.user.id, svc.settings.db_path)
        except Exception:  # pragma: no cover - sample data is a convenience only
            pass
    return _session_response(request, issued)


@router.post("/api/auth/signin", dependencies=[Depends(same_origin_only)])
def sign_in(body: SignInRequest, request: Request) -> JSONResponse:
    svc = services(request)
    keys = (f"login-ip:{client_ip(request)}", f"login-email:{body.email.strip().lower()}")
    if not all(svc.login_limiter.allow(k) for k in keys):
        raise HTTPException(status_code=429, detail="too many attempts, try again in a minute")
    try:
        issued = svc.auth.sign_in(body.email, body.password)
    except AuthError as err:
        raise _auth_error(err) from None
    return _session_response(request, issued)


@router.post("/api/auth/signout")
def sign_out(request: Request, session: CsrfSession) -> JSONResponse:
    services(request).auth.sign_out(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/api/auth/me", response_model=SessionOut)
def me(session: Session) -> SessionOut:
    return SessionOut(user=UserOut(id=session.user.id, email=session.user.email, name=session.user.name),
                      csrf_token=session.csrf_token)


# --------------------------------------------------------------- datasets


def _dataset_out(record) -> DatasetOut:  # type: ignore[no-untyped-def]
    return DatasetOut(id=record.id, name=record.name, created_at=record.created_at,
                      tables=public_schema(record.meta)["tables"])


@router.get("/api/datasets", response_model=list[DatasetOut])
def list_datasets(request: Request, session: Session) -> list[DatasetOut]:
    return [_dataset_out(r) for r in services(request).app_db.list_datasets(session.user.id)]


@router.post("/api/datasets", response_model=DatasetOut)
def upload_dataset(
    request: Request,
    session: CsrfSession,
    files: Annotated[list[UploadFile], File(description="one or more CSV files; each becomes a table")],
    name: Annotated[str, Form(max_length=80)] = "My dataset",
) -> DatasetOut:
    svc = services(request)
    payload: list[tuple[str, bytes]] = []
    budget = svc.settings.max_upload_mb * 1024 * 1024
    for f in files[: svc.settings.max_files_per_dataset + 1]:
        data = f.file.read(budget + 1)
        budget -= len(data)
        if budget < 0:
            raise HTTPException(status_code=413, detail=f"upload exceeds {svc.settings.max_upload_mb} MB")
        payload.append((f.filename or "data.csv", data))
    try:
        record = svc.datasets.create_from_csv(session.user.id, name, payload)
    except IngestError as err:
        raise HTTPException(status_code=422, detail=err.message) from None
    return _dataset_out(record)


@router.get("/api/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: str, request: Request, session: Session) -> DatasetOut:
    record = services(request).app_db.get_dataset(session.user.id, dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    out = _dataset_out(record)
    return out.model_copy(update={"tables": public_schema(record.meta)["tables"]})


@router.patch("/api/datasets/{dataset_id}/visibility", response_model=DatasetOut)
def set_visibility(dataset_id: str, body: ColumnVisibilityRequest, request: Request, session: CsrfSession) -> DatasetOut:
    svc = services(request)
    try:
        svc.datasets.set_hidden(session.user.id, dataset_id, body.table, body.column, body.hidden)
    except KeyError:
        raise HTTPException(status_code=404, detail="dataset, table or column not found") from None
    record = svc.app_db.get_dataset(session.user.id, dataset_id)
    assert record is not None
    return _dataset_out(record)


@router.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, request: Request, session: CsrfSession) -> dict[str, bool]:
    if not services(request).datasets.delete(session.user.id, dataset_id):
        raise HTTPException(status_code=404, detail="dataset not found")
    return {"ok": True}


# ------------------------------------------------------------------- chat


@router.post("/api/datasets/{dataset_id}/chat", response_model=ChatResponse)
def chat(dataset_id: str, body: ChatRequest, request: Request, session: CsrfSession) -> ChatResponse:
    svc = services(request)
    if len(body.question) > svc.settings.max_question_chars:
        raise HTTPException(status_code=422, detail=f"question exceeds {svc.settings.max_question_chars} characters")
    if not svc.chat_limiter.allow(f"chat:{session.user.id}"):
        raise HTTPException(status_code=429, detail="too many questions, slow down a little")
    ctx = svc.datasets.get_context(session.user.id, dataset_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    rid = request_id(request)
    conversation_id = body.conversation_id or uuid.uuid4().hex
    history = svc.app_db.recent_turns(session.user.id, dataset_id, conversation_id, svc.settings.history_turns)

    state = run_agent(AgentDeps(svc.llm, svc.settings, ctx), body.question, history=history, request_id=rid,
                      use_langgraph=svc.use_langgraph)

    codes = [c for a in state.get("attempts", []) for c in a.get("codes", [])]
    svc.app_db.log_query(
        request_id=rid, user_id=session.user.id, dataset_id=dataset_id, conversation_id=conversation_id,
        question=state.get("question", body.question), sql=state.get("sql"),
        status="ok" if state["kind"] == "answer" else state["kind"], codes=codes or [state.get("error_code") or ""],
    )
    return ChatResponse(
        request_id=rid, conversation_id=conversation_id, kind=state["kind"], message=state.get("message"),
        answer=state.get("answer"), sql=state.get("sql"), explanation=state.get("explanation"),
        assumptions=state.get("assumptions", []), columns=state.get("columns", []), rows=state.get("rows", []),
        row_count=state.get("row_count", 0), truncated=state.get("truncated", False),
        applied_default_limit=state.get("applied_default_limit", False), duration_ms=state.get("duration_ms"),
        plan=state.get("plan", []),
        attempts=[Attempt(**{k: a.get(k, [] if k in ("codes", "messages") else None) for k in
                             ("attempt", "stage", "outcome", "codes", "messages")}) for a in state.get("attempts", [])],
        error_code=state.get("error_code"),
    )


@router.post("/api/datasets/{dataset_id}/validate", response_model=ValidateResponse)
def validate(dataset_id: str, body: ValidateRequest, request: Request, session: CsrfSession) -> ValidateResponse:
    """Run the SQL guard on a candidate. Never executes anything."""
    svc = services(request)
    if len(body.sql) > svc.settings.max_sql_chars:
        raise HTTPException(status_code=422, detail=f"sql exceeds {svc.settings.max_sql_chars} characters")
    ctx = svc.datasets.get_context(session.user.id, dataset_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    result = AgentDeps(svc.llm, svc.settings, ctx).validator.validate(body.sql)
    return ValidateResponse(
        request_id=request_id(request), valid=result.ok, safe_sql=result.safe_sql,
        effective_limit=result.query.effective_limit if result.query else None,
        applied_default_limit=result.query.applied_default_limit if result.query else False,
        issues=[Issue(code=i.code, message=i.message) for i in result.issues],
    )


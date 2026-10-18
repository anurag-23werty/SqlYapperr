"""The agent, as a LangGraph state machine.

    guard_input ──blocked──────────────────────────────────────────────► END
         │
       route ──out_of_scope / chitchat / unsafe───────────────────────► END
         │
    generate_sql ◄──────────── feedback (max 2 corrections) ─────┐
         │  └─unanswerable / LLM down──────────────────────────►END│
     guard_sql ──rejected──────────────────────────────────────────┤
         │ (only the re-serialized, validated AST continues)       │
      execute ──retryable failure────────────────────────────────┘
         │  └─timeout / denied / unavailable ─────────────────► END
     summarize ─────────────────────────────────────────────────► END

The LLM never touches the database. It only produces a candidate string. The
guard parses it, checks every node, and emits fresh SQL from the validated
tree; only that SQL is executed, under the SQLite authorizer.

The same node and router functions run either under LangGraph (default) or
under a small built-in runner (``use_langgraph=False``), which keeps every
transition unit-testable without the framework installed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from app.agent import prompts
from app.agent.llm import LLMClient, LLMError
from app.config import HARD_MAX_CORRECTIONS, Settings
from app.execution.query_executor import ExecutionError, ExecutionErrorCode, ExecutionLimits, QueryExecutor
from app.security.ast_validator import SQLValidator, ValidatorConfig
from app.security.limits import LimitPolicy
from app.security.prompt_guard import inspect_question
from app.storage.datasets import DatasetContext

log = logging.getLogger(__name__)

FINISH = "__end__"
Kind = Literal["answer", "out_of_scope", "chitchat", "blocked", "rejected", "error"]
_STOP_EXEC = {
    ExecutionErrorCode.TIMEOUT, ExecutionErrorCode.AUTHORIZATION_DENIED, ExecutionErrorCode.DATABASE_UNAVAILABLE,
}


class AgentState(TypedDict, total=False):
    request_id: str
    question: str
    history: list[tuple[str, str]]
    # progress
    generations: int
    feedback: list[str]
    attempts: list[dict[str, Any]]
    candidate: dict[str, Any] | None
    validated: Any | None           # ValidatedQuery (opaque capability)
    # outcome
    kind: Kind
    message: str
    sql: str | None
    explanation: str | None
    assumptions: list[str]
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: float | None
    plan: list[str]
    applied_default_limit: bool
    answer: str | None
    error_code: str | None


@dataclass
class AgentDeps:
    llm: LLMClient
    settings: Settings
    ctx: DatasetContext

    def __post_init__(self) -> None:
        s = self.settings
        self.budget = 1 + max(0, min(s.max_corrections, HARD_MAX_CORRECTIONS))
        self.validator = SQLValidator(
            self.ctx.policy,
            LimitPolicy(max_rows=s.max_rows, default_limit=s.default_limit, max_offset=s.max_offset),
            ValidatorConfig(
                max_sql_chars=s.max_sql_chars, max_nesting_depth=s.max_nesting_depth, max_joins=s.max_joins,
                max_ast_nodes=s.max_ast_nodes, max_set_operations=s.max_set_operations,
                max_string_literal_chars=s.max_string_literal_chars,
            ),
            physical_columns=self.ctx.physical_columns,
        )
        self.executor = QueryExecutor(
            self.ctx.db_path, self.ctx.policy,
            ExecutionLimits(max_rows=s.max_rows, max_response_bytes=s.max_response_bytes,
                            timeout_ms=s.query_timeout_ms, progress_ops=s.progress_handler_ops),
        )


def suggestions(meta: dict[str, Any], limit: int = 3) -> list[str]:
    """Deterministic example questions built from the authorized schema."""
    out: list[str] = []
    for t in meta["tables"]:
        if t.get("hidden"):
            continue
        cols = [c for c in t["columns"] if not c.get("hidden")]
        nums = [c["name"] for c in cols if c["type"] in {"INTEGER", "REAL"} and not c["name"].endswith("id")]
        cats = [c["name"] for c in cols if c.get("categorical")]
        dates = [c["name"] for c in cols if c.get("semantic") in {"date", "datetime"}]
        if cats and nums:
            out.append(f"What is the total {nums[0].replace('_', ' ')} by {cats[0].replace('_', ' ')} in {t['name']}?")
        if dates:
            out.append(f"How many {t['name'].replace('_', ' ')} were there per month?")
        out.append(f"How many rows are in {t['name']}?")
    return out[:limit]


# ---------------------------------------------------------------- nodes


def build_nodes(deps: AgentDeps) -> dict[str, Callable[[AgentState], dict[str, Any]]]:
    s = deps.settings
    meta = deps.ctx.meta

    def guard_input(state: AgentState) -> dict[str, Any]:
        verdict = inspect_question(state["question"], s.max_question_chars)
        if verdict.action == "block":
            log.info("input blocked request_id=%s category=%s", state["request_id"], verdict.category)
            return {"kind": "blocked", "message": verdict.message, "error_code": verdict.category.upper()}
        return {"question": verdict.text}

    def route(state: AgentState) -> dict[str, Any]:
        try:
            out = deps.llm.complete_json(
                purpose="route", model=s.groq_fast_model, system=prompts.ROUTER_SYSTEM,
                user=prompts.router_user(state["question"], meta, state.get("history", [])), max_tokens=600,
                reasoning_effort="low",
            )
            intent = str(out.get("intent", "data_question"))
        except LLMError as err:
            if err.kind in {"auth", "rate_limited", "model_not_found"}:
                return {"kind": "error", "message": err.message, "error_code": f"LLM_{err.kind.upper()}"}
            intent = "data_question"  # router is advisory; the SQL guard is the real boundary
        if intent == "out_of_scope":
            tips = suggestions(meta)
            return {"kind": "out_of_scope", "error_code": "OUT_OF_SCOPE",
                    "message": "I can only answer questions about this dataset. Try: " + " | ".join(tips)}
        if intent == "chitchat":
            tips = suggestions(meta)
            return {"kind": "chitchat",
                    "message": "Hi! Ask me anything about this dataset in plain English, for example: "
                               + " | ".join(tips)}
        if intent == "unsafe":
            return {"kind": "blocked", "error_code": "UNSAFE_REQUEST",
                    "message": "I can't help with that. I only run read-only questions on the data you can see."}
        return {}

    def generate_sql(state: AgentState) -> dict[str, Any]:
        generations = state.get("generations", 0) + 1
        attempts = list(state.get("attempts", []))
        feedback = list(state.get("feedback", []))
        try:
            out = deps.llm.complete_json(
                purpose="sql", model=s.groq_sql_model, system=prompts.SQL_SYSTEM.format(max_rows=s.max_rows),
                user=prompts.sql_user(state["question"], meta, state.get("history", []), feedback), max_tokens=2500,
                reasoning_effort=s.groq_reasoning_effort,
            )
        except LLMError as err:
            attempts.append({"attempt": generations, "stage": "generation", "outcome": "failed", "codes": [f"LLM_{err.kind.upper()}"]})
            if err.kind == "bad_output":
                feedback.append("Your previous reply was not valid JSON in the required format.")
                return {"generations": generations, "attempts": attempts, "feedback": feedback, "candidate": None}
            return {"generations": generations, "attempts": attempts, "candidate": None,
                    "kind": "error", "message": err.message, "error_code": f"LLM_{err.kind.upper()}"}

        if out.get("answerable") is False or not out.get("sql"):
            attempts.append({"attempt": generations, "stage": "generation", "outcome": "unanswerable", "codes": []})
            reason = str(out.get("explanation") or "the dataset does not contain that information")[:300]
            return {"generations": generations, "attempts": attempts, "candidate": None, "kind": "out_of_scope",
                    "error_code": "NOT_ANSWERABLE",
                    "message": f"I can't answer that from this dataset: {reason}"}
        candidate = {
            "sql": str(out["sql"])[: s.max_sql_chars + 1],
            "explanation": str(out.get("explanation") or "")[:400],
            "assumptions": [str(a)[:200] for a in (out.get("assumptions") or [])][:5]
            if isinstance(out.get("assumptions"), list) else [],
        }
        return {"generations": generations, "attempts": attempts, "candidate": candidate}

    def guard_sql(state: AgentState) -> dict[str, Any]:
        attempts = list(state.get("attempts", []))
        result = deps.validator.validate(state["candidate"]["sql"])
        if not result.ok or result.query is None:
            codes = [i.code for i in result.issues]
            messages = [i.message for i in result.issues]
            attempts.append({"attempt": state["generations"], "stage": "validation", "outcome": "rejected",
                             "codes": codes, "messages": messages})
            log.info("candidate rejected request_id=%s codes=%s", state["request_id"], codes)
            return {"attempts": attempts, "validated": None,
                    "feedback": list(state.get("feedback", [])) + messages}
        return {"attempts": attempts, "validated": result.query}

    def execute(state: AgentState) -> dict[str, Any]:
        attempts = list(state.get("attempts", []))
        query = state["validated"]
        try:
            res = deps.executor.execute(query, request_id=state["request_id"])
        except ExecutionError as err:
            attempts.append({"attempt": state["generations"], "stage": "execution", "outcome": "failed",
                             "codes": [err.code], "messages": [err.public_message]})
            if err.code in _STOP_EXEC:
                if err.code == ExecutionErrorCode.AUTHORIZATION_DENIED:
                    log.warning("SECURITY authorizer denied guard-approved SQL request_id=%s", state["request_id"])
                return {"attempts": attempts, "validated": None, "kind": "error", "sql": query.sql,
                        "error_code": err.code, "message": err.public_message}
            return {"attempts": attempts, "validated": None,
                    "feedback": list(state.get("feedback", [])) + [
                        "The query failed at execution time; check column types, functions and date formats."]}
        attempts.append({"attempt": state["generations"], "stage": "execution", "outcome": "succeeded", "codes": []})
        cand = state["candidate"] or {}
        return {
            "attempts": attempts, "kind": "answer", "sql": query.sql, "explanation": cand.get("explanation"),
            "assumptions": cand.get("assumptions", []), "columns": res.columns, "rows": res.rows,
            "row_count": res.row_count, "truncated": res.truncated, "duration_ms": res.duration_ms,
            "plan": res.plan, "applied_default_limit": query.applied_default_limit, "error_code": None,
        }

    def summarize(state: AgentState) -> dict[str, Any]:
        fallback = _fallback_summary(state)
        if not s.summarize_results:
            return {"answer": fallback}
        try:
            out = deps.llm.complete_json(
                purpose="summary", model=s.groq_fast_model, system=prompts.SUMMARY_SYSTEM,
                user=prompts.summary_user(state["question"], state["columns"], state["rows"], state["truncated"]),
                max_tokens=600, reasoning_effort="low",
            )
            answer = " ".join(str(out.get("answer") or "").split())[:600]
        except LLMError:
            answer = ""
        return {"answer": answer or fallback}

    return {"guard_input": guard_input, "route": route, "generate_sql": generate_sql,
            "guard_sql": guard_sql, "execute": execute, "summarize": summarize}


def _fallback_summary(state: AgentState) -> str:
    n = state.get("row_count", 0)
    if n == 0:
        return "The query ran successfully but returned no rows."
    more = " (showing the first rows)" if state.get("truncated") else ""
    return f"Found {n} row{'s' if n != 1 else ''}{more}."


# --------------------------------------------------------------- routing


def build_routes(deps: AgentDeps) -> dict[str, tuple[Callable[[AgentState], str], list[str]]]:
    def budget_left(state: AgentState) -> bool:
        return state.get("generations", 0) < deps.budget

    def after_guard_input(state: AgentState) -> str:
        return FINISH if state.get("kind") == "blocked" else "route"

    def after_route(state: AgentState) -> str:
        return FINISH if state.get("kind") else "generate_sql"

    def after_generate(state: AgentState) -> str:
        if state.get("kind"):
            return FINISH
        if state.get("candidate"):
            return "guard_sql"
        return "generate_sql" if budget_left(state) else FINISH

    def after_guard_sql(state: AgentState) -> str:
        if state.get("validated") is not None:
            return "execute"
        return "generate_sql" if budget_left(state) else FINISH

    def after_execute(state: AgentState) -> str:
        if state.get("kind") == "answer":
            return "summarize"
        if state.get("kind"):
            return FINISH
        return "generate_sql" if budget_left(state) else FINISH

    return {
        "guard_input": (after_guard_input, ["route", FINISH]),
        "route": (after_route, ["generate_sql", FINISH]),
        "generate_sql": (after_generate, ["guard_sql", "generate_sql", FINISH]),
        "guard_sql": (after_guard_sql, ["execute", "generate_sql", FINISH]),
        "execute": (after_execute, ["summarize", "generate_sql", FINISH]),
        "summarize": (lambda _s: FINISH, [FINISH]),
    }


ENTRY = "guard_input"


def compile_langgraph(deps: AgentDeps) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(AgentState)
    for name, fn in build_nodes(deps).items():
        graph.add_node(name, fn)
    graph.add_edge(START, ENTRY)
    for source, (router, destinations) in build_routes(deps).items():
        path_map = {d: (END if d == FINISH else d) for d in destinations}
        graph.add_conditional_edges(source, router, path_map)
    return graph.compile()


def _run_local(deps: AgentDeps, state: AgentState) -> AgentState:
    nodes, routes = build_nodes(deps), build_routes(deps)
    node = ENTRY
    for _ in range(32):  # hard stop; the real path is <= 12 steps
        state.update(nodes[node](state))  # type: ignore[typeddict-item]
        nxt = routes[node][0](state)
        if nxt not in routes[node][1]:
            raise RuntimeError(f"invalid transition {node} -> {nxt}")
        if nxt == FINISH:
            return state
        node = nxt
    raise RuntimeError("agent exceeded step budget")


def run_agent(
    deps: AgentDeps, question: str, history: list[tuple[str, str]] | None = None,
    request_id: str | None = None, use_langgraph: bool = True,
) -> AgentState:
    initial: AgentState = {
        "request_id": request_id or uuid.uuid4().hex, "question": question, "history": history or [],
        "generations": 0, "feedback": [], "attempts": [], "candidate": None, "validated": None,
    }
    if use_langgraph:
        final = compile_langgraph(deps).invoke(initial, {"recursion_limit": 30})
    else:
        final = _run_local(deps, initial)
    if not final.get("kind"):
        final["kind"] = "rejected"
        final["error_code"] = "RETRY_BUDGET_EXHAUSTED"
        final["message"] = (
            f"I couldn't build a safe query after {deps.budget} attempts. Try rephrasing or being more specific."
        )
    final.pop("validated", None)
    return final


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True

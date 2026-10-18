"""Bounded generate -> validate -> execute loop.

Invariants:

* A candidate is executed only after the AST validator accepted it, and only
  in its re-serialized form (``ValidatedQuery``).
* At most ``1 + max_corrections`` generations (``max_corrections`` <= 2).
* Feedback to the model is built from fixed templates only.
* Timeouts, database-level authorization denials and an unavailable database
  stop the loop immediately: retrying would only help an attacker probe.
* The model never receives, and can never alter, the policy or the limits.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.agent.llm_interface import Feedback, GenerationError, GenerationRequest, SQLGenerator
from app.config import HARD_MAX_CORRECTIONS
from app.database.introspection import AuthorizedSchema, render_schema_for_prompt, select_relevant_schema
from app.execution.query_executor import ExecutionError, ExecutionErrorCode, QueryExecutor, QueryResult
from app.security.ast_validator import SQLValidator

log = logging.getLogger(__name__)

_STOP_CODES = frozenset(
    {ExecutionErrorCode.TIMEOUT, ExecutionErrorCode.AUTHORIZATION_DENIED, ExecutionErrorCode.DATABASE_UNAVAILABLE}
)


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    stage: Literal["generation", "validation", "execution"]
    outcome: Literal["failed", "rejected", "succeeded"]
    codes: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    sql: str | None = None  # validated SQL only; rejected candidates are not echoed


@dataclass(frozen=True)
class ChatOutcome:
    request_id: str
    status: Literal["ok", "rejected", "error"]
    attempts: tuple[AttemptRecord, ...]
    sql: str | None = None
    explanation: str | None = None
    result: QueryResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    applied_default_limit: bool = False


def sanitize_question(text: str, max_chars: int) -> str:
    """Normalize and strip control characters (keeps newlines and tabs)."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return text.strip()[:max_chars]


@dataclass
class ChatPipeline:
    generator: SQLGenerator
    validator: SQLValidator
    executor: QueryExecutor
    schema: AuthorizedSchema
    max_rows: int
    max_corrections: int = HARD_MAX_CORRECTIONS
    max_question_chars: int = 1_000
    _budget: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        # The spec ceiling cannot be raised by configuration.
        self._budget = 1 + max(0, min(self.max_corrections, HARD_MAX_CORRECTIONS))

    def run(self, question: str, request_id: str | None = None) -> ChatOutcome:
        request_id = request_id or uuid.uuid4().hex
        question = sanitize_question(question, self.max_question_chars)
        schema_text = render_schema_for_prompt(select_relevant_schema(self.schema, question))
        feedback: list[Feedback] = []
        attempts: list[AttemptRecord] = []

        for attempt in range(1, self._budget + 1):
            request = GenerationRequest(
                question=question, schema_text=schema_text, max_rows=self.max_rows, feedback=tuple(feedback)
            )
            # 1. generate
            try:
                candidate = self.generator.generate(request)
            except GenerationError:
                rec = AttemptRecord(attempt, "generation", "failed", ("GENERATION_FAILED",),
                                    ("the model did not return a valid structured response",))
                attempts.append(rec)
                feedback.append(Feedback(attempt, "generation", rec.codes, rec.messages))
                continue

            # 2. validate (never execute a rejected candidate)
            validation = self.validator.validate(candidate.sql)
            if not validation.ok or validation.query is None:
                codes = tuple(i.code for i in validation.issues)
                messages = tuple(i.message for i in validation.issues)
                attempts.append(AttemptRecord(attempt, "validation", "rejected", codes, messages))
                feedback.append(Feedback(attempt, "validation", codes, messages))
                log.info("candidate rejected request_id=%s attempt=%d codes=%s", request_id, attempt, codes)
                continue

            # 3. execute the validated, re-serialized SQL
            try:
                result = self.executor.execute(validation.query, request_id=request_id)
            except ExecutionError as err:
                rec = AttemptRecord(attempt, "execution", "failed", (err.code,), (err.public_message,),
                                    sql=validation.query.sql)
                attempts.append(rec)
                if err.code in _STOP_CODES:
                    if err.code == ExecutionErrorCode.AUTHORIZATION_DENIED:
                        log.warning("SECURITY: authorizer denied a validator-approved query request_id=%s", request_id)
                    return ChatOutcome(request_id, "error", tuple(attempts), sql=validation.query.sql,
                                       error_code=err.code, error_message=err.public_message)
                feedback.append(Feedback(attempt, "execution", rec.codes, rec.messages))
                continue

            attempts.append(AttemptRecord(attempt, "execution", "succeeded", sql=validation.query.sql))
            return ChatOutcome(
                request_id=request_id,
                status="ok",
                attempts=tuple(attempts),
                sql=validation.query.sql,
                explanation=candidate.explanation,
                result=result,
                applied_default_limit=validation.query.applied_default_limit,
            )

        return ChatOutcome(
            request_id=request_id,
            status="rejected",
            attempts=tuple(attempts),
            error_code="RETRY_BUDGET_EXHAUSTED",
            error_message=f"no safe query could be produced after {self._budget} attempts",
        )

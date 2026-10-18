"""Composition root: wires settings, policy and components together once.

Everything built here is immutable or stateless, so it is safe to share across
concurrent requests. Per-request security state (connection, authorizer,
progress handler) is created inside the executor for each query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent.correction_loop import ChatPipeline
from app.agent.llm_interface import SQLGenerator
from app.agent.sql_generator import AnthropicSQLGenerator, MockSQLGenerator
from app.config import Settings
from app.database.connection import DatabaseUnavailable, open_readonly_connection
from app.database.introspection import AuthorizedSchema, introspect, physical_columns
from app.database.seed import seed_database
from app.execution.query_executor import ExecutionLimits, QueryExecutor
from app.security.ast_validator import SQLValidator, ValidatorConfig
from app.security.limits import LimitPolicy
from app.security.policies import DEFAULT_POLICY, AccessPolicy

log = logging.getLogger(__name__)

VERSION = "0.1.0"


@dataclass(frozen=True)
class Container:
    settings: Settings
    policy: AccessPolicy
    schema: AuthorizedSchema
    validator: SQLValidator
    executor: QueryExecutor
    generator: SQLGenerator
    pipeline: ChatPipeline
    version: str = VERSION

    def database_ok(self) -> bool:
        try:
            conn = open_readonly_connection(self.settings.db_path)
        except DatabaseUnavailable:
            return False
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
        finally:
            conn.close()


def build_generator(settings: Settings) -> SQLGenerator:
    if settings.llm_provider == "anthropic":
        assert settings.llm_api_key is not None and settings.llm_model is not None
        return AnthropicSQLGenerator(
            api_key=settings.llm_api_key.get_secret_value(), model=settings.llm_model, timeout_s=settings.llm_timeout_s
        )
    return MockSQLGenerator()


def build_container(
    settings: Settings, generator: SQLGenerator | None = None, policy: AccessPolicy = DEFAULT_POLICY
) -> Container:
    if settings.auto_seed and not settings.db_path.exists():
        log.info("database missing; seeding sample data")
        seed_database(settings.db_path)

    schema = introspect(settings.db_path, policy)
    validator = SQLValidator(
        policy,
        LimitPolicy(max_rows=settings.max_rows, default_limit=settings.default_limit, max_offset=settings.max_offset),
        ValidatorConfig(
            max_sql_chars=settings.max_sql_chars,
            max_nesting_depth=settings.max_nesting_depth,
            max_joins=settings.max_joins,
            max_ast_nodes=settings.max_ast_nodes,
            max_set_operations=settings.max_set_operations,
            max_string_literal_chars=settings.max_string_literal_chars,
        ),
        physical_columns=physical_columns(settings.db_path, policy),
    )
    executor = QueryExecutor(
        settings.db_path,
        policy,
        ExecutionLimits(
            max_rows=settings.max_rows,
            max_response_bytes=settings.max_response_bytes,
            timeout_ms=settings.query_timeout_ms,
            progress_ops=settings.progress_handler_ops,
        ),
    )
    generator = generator or build_generator(settings)
    pipeline = ChatPipeline(
        generator=generator,
        validator=validator,
        executor=executor,
        schema=schema,
        max_rows=settings.max_rows,
        max_corrections=settings.max_corrections,
        max_question_chars=settings.max_question_chars,
    )
    return Container(settings, policy, schema, validator, executor, generator, pipeline)

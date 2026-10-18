from __future__ import annotations

from pathlib import Path

import pytest

from app.database.introspection import introspect, physical_columns
from app.database.seed import seed_database
from app.execution.query_executor import ExecutionLimits, QueryExecutor
from app.security.ast_validator import SQLValidator, ValidatorConfig
from app.security.limits import LimitPolicy
from app.security.policies import DEFAULT_POLICY


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return seed_database(tmp_path_factory.mktemp("db") / "test.db", overwrite=True)


@pytest.fixture(scope="session")
def limit_policy() -> LimitPolicy:
    return LimitPolicy(max_rows=100, default_limit=50, max_offset=10_000)


@pytest.fixture(scope="session")
def validator(db_path: Path, limit_policy: LimitPolicy) -> SQLValidator:
    return SQLValidator(
        DEFAULT_POLICY, limit_policy, ValidatorConfig(), physical_columns=physical_columns(db_path, DEFAULT_POLICY)
    )


@pytest.fixture(scope="session")
def executor(db_path: Path) -> QueryExecutor:
    return QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(max_rows=100, timeout_ms=2_000))


@pytest.fixture(scope="session")
def schema(db_path: Path):
    return introspect(db_path, DEFAULT_POLICY)


def codes(result) -> set[str]:
    return {i.code for i in result.issues}


@pytest.fixture(scope="session")
def sample_ctx(db_path: Path):
    from app.storage.datasets import context_from_meta, describe_sqlite

    meta = describe_sqlite(db_path, hidden_tables={"admin_users"})
    return context_from_meta("d" * 32, "Sample shop", db_path, meta)


@pytest.fixture(scope="session")
def mock_settings(tmp_path_factory: pytest.TempPathFactory):
    from app.config import Settings

    d = tmp_path_factory.mktemp("settings")
    return Settings(llm_provider="mock", data_dir=d, db_path=d / "sample.db", app_db_path=d / "app.db")

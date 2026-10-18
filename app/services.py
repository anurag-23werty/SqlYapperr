"""Composition root: builds every long-lived component once at start-up.

Everything here is immutable or internally synchronized, so it is safe to
share across concurrent requests. Per-request security state (dataset policy,
validator, connection, authorizer, deadline) is created inside the agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.agent.graph import langgraph_available
from app.agent.llm import GroqClient, LLMClient, MockLLM
from app.auth.ratelimit import RateLimiter
from app.auth.service import AuthService
from app.config import Settings
from app.database.seed import seed_database
from app.storage.app_db import AppDB
from app.storage.datasets import DatasetService, IngestLimits

log = logging.getLogger(__name__)
VERSION = "0.2.0"


@dataclass(frozen=True)
class Services:
    settings: Settings
    app_db: AppDB
    auth: AuthService
    datasets: DatasetService
    llm: LLMClient
    use_langgraph: bool
    login_limiter: RateLimiter
    chat_limiter: RateLimiter
    version: str = VERSION


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "groq":
        assert settings.groq_api_key is not None
        return GroqClient(
            api_key=settings.groq_api_key.get_secret_value(), base_url=settings.groq_base_url,
            timeout_s=settings.llm_timeout_s, max_retries=settings.llm_max_retries,
        )
    log.warning("SQLY_LLM_PROVIDER=mock: using the offline mock model (set GROQ_API_KEY for real answers)")
    return MockLLM()


def build_services(settings: Settings, llm: LLMClient | None = None, use_langgraph: bool | None = None) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_seed and not Path(settings.db_path).exists():
        seed_database(settings.db_path)
    app_db = AppDB(settings.app_db_path)
    lg = langgraph_available() if use_langgraph is None else use_langgraph
    if use_langgraph is None and not lg:
        log.warning("langgraph is not installed; using the built-in graph runner")
    return Services(
        settings=settings,
        app_db=app_db,
        auth=AuthService(app_db, settings.secret_key.get_secret_value(), settings.session_ttl_hours,
                         settings.password_min_length),
        datasets=DatasetService(
            app_db, settings.users_dir,
            IngestLimits(max_files=settings.max_files_per_dataset, max_rows=settings.max_rows_per_table,
                         max_columns=settings.max_columns_per_table, max_bytes=settings.max_upload_mb * 1024 * 1024),
            settings.max_datasets_per_user,
        ),
        llm=llm or build_llm(settings),
        use_langgraph=lg,
        login_limiter=RateLimiter(settings.login_rate_per_minute),
        chat_limiter=RateLimiter(settings.chat_rate_per_minute),
    )

"""
Electa Systems — Governance Execution API
Configuration: centralised settings via environment variables.
"""

from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://electa:electa@localhost:5432/electa"

    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = "change-me-in-production-use-a-long-random-string"
    API_KEY_HEADER: str = "X-Electa-API-Key"
    REQUIRE_CRYPTO_SIGNATURES: bool = False

    # ── Event system ──────────────────────────────────────────────────────────
    EVENT_QUEUE_MAX_SIZE: int = 1000
    WEBHOOK_MAX_RETRIES: int = 3
    WEBHOOK_RETRY_BACKOFF_SECONDS: float = 2.0
    WEBHOOK_TIMEOUT_SECONDS: float = 10.0

    # ── Governance defaults ───────────────────────────────────────────────────
    DEFAULT_QUORUM_PCT: float = 0.51
    DEFAULT_MAJORITY_PCT: float = 0.50
    DEFAULT_SUPERMAJORITY_PCT: float = 0.6667

    # ── Scheduler ────────────────────────────────────────────────────────────
    SCHEDULER_INTERVAL_SECONDS: float = 30.0

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_VOTE_RPM: int = 60
    RATE_LIMIT_GENERAL_RPM: int = 300

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    # ── Pagination ────────────────────────────────────────────────────────────
    DEFAULT_PAGE_SIZE: int = 50
    MAX_PAGE_SIZE: int = 500


settings = Settings()

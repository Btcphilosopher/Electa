"""
Electa Systems — Shared Test Fixtures
Single in-memory SQLite database and FastAPI AsyncClient shared across
all test modules. DATABASE_URL is set before any app module is imported.
"""

import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app

# ── Shared in-memory engine ───────────────────────────────────────────────────

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[get_db] = _override_get_db

# Patch AsyncSessionLocal so the scheduler and webhook service use the
# same in-memory engine rather than trying to connect to Postgres.
import database as _db_module
import services.scheduler as _sched_module

_db_module.engine = _engine
_db_module.AsyncSessionLocal = _Session
_sched_module.AsyncSessionLocal = _Session  # type: ignore


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

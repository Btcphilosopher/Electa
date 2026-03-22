"""
Electa Systems — Database
Async SQLAlchemy engine, session factory, and declarative Base.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import settings

_url = settings.DATABASE_URL
_is_sqlite = _url.startswith("sqlite")
_pool_kwargs: dict = {} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=not _is_sqlite,
    **_pool_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

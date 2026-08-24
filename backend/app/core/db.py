# sets up your postresql database connection engine using SQLAlchemy 2.0 ans asyncpg. it handles connection pooling, async session creation, FastAPI request injection, and health-checks.
import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# Create the async engine targeting PostgreSQL using the asyncpg driver
engine: AsyncEngine = create_async_engine(
    settings.async_database_url,  # reads postgres connection string from the settings
    echo=(
        settings.LOG_LEVEL.upper() == "DEBUG"
    ),  # automatically prints raw DQL queries to the console if you rlog level is set to DEBUG
    future=True,
    pool_pre_ping=True,  # Verifies connection validity before retrieving from pool
    pool_size=10,
    max_overflow=20,
)

# Session maker factory for generating async sessions across API endpoints
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session per request.

    Guarantees session cleanup and rollback upon uncaught exceptions.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as exc:
            await session.rollback()
            logger.error("Database session error encountered: %s", exc)
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Pings the database to verify active connectivity.

    Used by deep health-check endpoints during application startup.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Database connectivity health check failed: %s", exc)
        return False

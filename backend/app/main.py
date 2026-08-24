"""The sole ASGI entrypoint: `app.main:app` (ADR-014).

`backend/main.py` used to sit alongside this module with divergent behaviour and an
import that had never resolved; it is deleted (defect D-010). Every launcher — the
Makefile, the Dockerfile, compose, CI — points here.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_v1_router
from app.core.config import settings, warn_unconsumed_env
from app.core.db import check_db_connection

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI Lifespan context manager handling application startup & shutdown."""
    logger.info("Initializing %s (environment=%s)...", settings.PROJECT_NAME, settings.ENVIRONMENT)

    # Surface configuration that looks like ours but is consumed by nothing, rather
    # than letting `extra="ignore"` swallow it silently (defect D-002).
    warn_unconsumed_env(logger)

    # Ping database on startup
    db_healthy = await check_db_connection()
    if db_healthy:
        logger.info("Database connectivity check: OK")
    else:
        logger.warning(
            "Database connectivity check: FAILED — is the stack up? (make up). Target: %s",
            settings.POSTGRES_SERVER,
        )

    yield

    logger.info("Shutting down %s...", settings.PROJECT_NAME)


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Cross-Origin Resource Sharing (ARCHITECTURE.md §13.2).
#
# An explicit origin allowlist, not "*": a wildcard origin combined with
# allow_credentials=True is invalid per the CORS specification — browsers reject the
# response outright — and would be unsafe if they did not (defect D-007).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    max_age=600,
)

# Mount primary API router
app.include_router(api_v1_router, prefix=settings.API_V1_STR)


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Root endpoint returning basic system info."""
    return {
        "title": settings.PROJECT_NAME,
        "docs": "/docs",
        "health": f"{settings.API_V1_STR}/health/deep",
    }

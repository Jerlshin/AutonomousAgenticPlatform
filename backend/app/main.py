"""The sole ASGI entrypoint: `app.main:app` (ADR-014).

`backend/main.py` used to sit alongside this module with divergent behaviour and an
import that had never resolved; it is deleted (defect D-010). Every launcher — the
Makefile, the Dockerfile, compose, CI — points here.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_v1_router
from app.core import metrics
from app.core.config import settings, warn_unconsumed_env
from app.core.db import check_db_connection
from app.core.logging import configure_logging
from app.core.redis import close_redis
from app.worker.queue import close_arq_pool

# Structured JSON to stdout with the §12.3 redaction processor, installed before anything
# else can log. `basicConfig` used to sit here; it is what this replaces.
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI Lifespan context manager handling application startup & shutdown."""
    logger.info(
        "Initializing %s (environment=%s)...",
        settings.PROJECT_NAME,
        settings.ENVIRONMENT,
    )

    # Surface configuration that looks like ours but is consumed by nothing, rather
    # than letting `extra="ignore"` swallow it silently (defect D-002).
    warn_unconsumed_env(logger)

    # §13.2's posture, restated at startup. `Settings._check_network_exposure` already
    # refuses to construct with a LAN bind and no token, so reaching here without one
    # means loopback — but a token-less API is still worth one line in the log, because
    # "why is this open?" is a question an operator should never have to read code for.
    if not settings.PLATFORM_API_TOKEN:
        logger.warning(
            "PLATFORM_API_TOKEN is unset: the API is unauthenticated. This is permitted "
            "only because HOST=%s is loopback and ENVIRONMENT=%s. Run `make init-secrets` "
            "before exposing this process to anything.",
            settings.HOST,
            settings.ENVIRONMENT,
        )

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

    # Both pools are created lazily on first use, so closing them is a no-op on an API
    # process that never dispatched a run or opened a WebSocket. Leaving them open holds
    # sockets against Redis's connection limit across a reload loop.
    await close_arq_pool()
    await close_redis()


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

# Added last, so it is the *outermost* middleware: Starlette applies these in reverse, and
# a latency histogram that excluded the time CORS and the router spent would be measuring
# the handler rather than the request (ARCHITECTURE.md §12.1).
app.add_middleware(metrics.PrometheusMiddleware)

# Mount primary API router
app.include_router(api_v1_router, prefix=settings.API_V1_STR)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint (§12.1).

    Deliberately outside `API_V1_STR` and outside the bearer-token dependency. It is
    mounted where Prometheus's default scrape config expects it, and it is unauthenticated
    for the same reason `/health` is: the API binds to loopback, the scraper is a sibling
    container with no token, and a metrics endpoint that needed a credential would be one
    more secret to distribute for data that names no run and carries no user content.
    """
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Root endpoint returning basic system info."""
    return {
        "title": settings.PROJECT_NAME,
        "docs": "/docs",
        "health": f"{settings.API_V1_STR}/health/deep",
    }

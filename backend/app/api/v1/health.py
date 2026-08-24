"""Health endpoints.

The deep check distinguishes *hard* dependencies (postgres, redis, qdrant — the API
cannot serve without them) from *soft* ones (mlflow, ollama — a run will fail, but the
API is up). A hard failure returns HTTP 503 so container orchestration and monitoring
can act on it; a soft failure returns 200 with `status="degraded"`.

Previously every response was 200 and a non-200 from MLflow was recorded as
`status="healthy", message="Server reachable"` — defect D-013.
"""

import asyncio
import logging

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Response, status as http_status
from sqlalchemy import text

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.common import DeepHealthResponse, HealthCheckResponse, ServiceStatus

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-probe timeout. Probes run concurrently, so this also bounds the whole endpoint.
PROBE_TIMEOUT_S = 3.0


@router.get("", response_model=HealthCheckResponse, summary="Shallow Health Check")
async def health_check() -> HealthCheckResponse:
    """Returns basic service availability without pinging downstream dependencies."""
    return HealthCheckResponse(status="ok", environment=settings.ENVIRONMENT)


async def _check_postgres() -> ServiceStatus:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return ServiceStatus(status="healthy", message="Connected", required=True)
    except Exception as exc:
        return ServiceStatus(status="unhealthy", message=str(exc), required=True)


async def _check_redis() -> ServiceStatus:
    client = None
    try:
        client = aioredis.from_url(settings.REDIS_URL, socket_timeout=PROBE_TIMEOUT_S)
        await client.ping()
        return ServiceStatus(status="healthy", message="PONG received", required=True)
    except Exception as exc:
        return ServiceStatus(status="unhealthy", message=str(exc), required=True)
    finally:
        if client is not None:
            await client.aclose()


async def _check_http(
    name: str,
    url: str,
    *,
    required: bool,
    ok_message: str,
) -> ServiceStatus:
    """Probe an HTTP dependency, treating any non-200 as unhealthy."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(url)
        if response.status_code == 200:
            return ServiceStatus(status="healthy", message=ok_message, required=required)
        return ServiceStatus(
            status="unhealthy",
            message=f"HTTP {response.status_code} from {url}",
            required=required,
        )
    except Exception as exc:
        logger.debug("Health probe for %s failed: %s", name, exc)
        return ServiceStatus(status="unhealthy", message=str(exc), required=required)


async def _check_ollama() -> ServiceStatus:
    result = await _check_http(
        "ollama",
        f"{settings.OLLAMA_BASE_URL}/api/version",
        required=False,
        ok_message="Reachable",
    )
    if result.status == "healthy":
        # Re-fetch is wasteful; read the version off a second cheap call only on success.
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                version = (await client.get(f"{settings.OLLAMA_BASE_URL}/api/version")).json()
            result.message = f"Version {version.get('version', 'unknown')}"
        except Exception:  # noqa: BLE001 — the probe already succeeded; detail is optional
            pass
    return result


@router.get("/deep", response_model=DeepHealthResponse, summary="Deep Dependency Health Check")
async def deep_health_check(response: Response) -> DeepHealthResponse:
    """Pings PostgreSQL, Redis, Qdrant, MLflow, and Ollama concurrently.

    Returns 503 when a hard dependency is down, 200 otherwise — `degraded` if a soft
    dependency (MLflow, Ollama) is unreachable.
    """
    names = ("postgres", "redis", "qdrant", "mlflow", "ollama")
    results = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_http(
            "qdrant",
            f"{settings.QDRANT_URL}/healthz",
            required=True,
            ok_message="Cluster ready",
        ),
        _check_http(
            "mlflow",
            f"{settings.MLFLOW_TRACKING_URI}/health",
            required=False,
            ok_message="Tracking server online",
        ),
        _check_ollama(),
    )
    services = dict(zip(names, results, strict=True))

    hard_down = [n for n, s in services.items() if s.required and s.status != "healthy"]
    soft_down = [n for n, s in services.items() if not s.required and s.status != "healthy"]

    if hard_down:
        overall = "unhealthy"
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("Deep health check: hard dependencies down: %s", ", ".join(hard_down))
    elif soft_down:
        overall = "degraded"
    else:
        overall = "healthy"

    return DeepHealthResponse(status=overall, services=services)

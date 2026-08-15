import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.common import DeepHealthResponse, HealthCheckResponse, ServiceStatus

router = APIRouter()


@router.get("", response_model=HealthCheckResponse, summary="Shallow Health Check")
async def health_check() -> HealthCheckResponse:
    """Returns basic service availability without pinging downstream dependencies."""
    return HealthCheckResponse(status="ok", environment=settings.ENVIRONMENT)


@router.get("/deep", response_model=DeepHealthResponse, summary="Deep Dependency Health Check")
async def deep_health_check() -> DeepHealthResponse:
    """Pings PostgreSQL, Redis, Qdrant, MLflow, and Ollama to verify container connectivity."""
    services: dict[str, ServiceStatus] = {}
    overall_healthy = True

    # 1. PostgreSQL Check
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        services["postgres"] = ServiceStatus(status="healthy", message="Connected")
    except Exception as exc:
        overall_healthy = False
        services["postgres"] = ServiceStatus(status="unhealthy", message=str(exc))

    # 2. Redis Check
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        services["redis"] = ServiceStatus(status="healthy", message="PONG received")
    except Exception as exc:
        overall_healthy = False
        services["redis"] = ServiceStatus(status="unhealthy", message=str(exc))

    # 3. Qdrant Check
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{settings.QDRANT_URL}/healthz")
            if res.status_code == 200:
                services["qdrant"] = ServiceStatus(status="healthy", message="Cluster ready")
            else:
                overall_healthy = False
                services["qdrant"] = ServiceStatus(status="unhealthy", message=f"HTTP {res.status_code}")
    except Exception as exc:
        overall_healthy = False
        services["qdrant"] = ServiceStatus(status="unhealthy", message=str(exc))

    # 4. MLflow Check
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{settings.MLFLOW_TRACKING_URI}/health")
            if res.status_code == 200:
                services["mlflow"] = ServiceStatus(status="healthy", message="Tracking server online")
            else:
                services["mlflow"] = ServiceStatus(status="healthy", message="Server reachable")
    except Exception as exc:
        services["mlflow"] = ServiceStatus(status="unhealthy", message=str(exc))

    # 5. Ollama Check
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{settings.OLLAMA_BASE_URL}/api/version")
            if res.status_code == 200:
                version = res.json().get("version", "unknown")
                services["ollama"] = ServiceStatus(status="healthy", message=f"Version {version}")
            else:
                overall_healthy = False
                services["ollama"] = ServiceStatus(status="unhealthy", message=f"HTTP {res.status_code}")
    except Exception as exc:
        overall_healthy = False
        services["ollama"] = ServiceStatus(status="unhealthy", message=f"Native host check failed: {exc}")

    return DeepHealthResponse(
        status="healthy" if overall_healthy else "degraded",
        services=services,
    )
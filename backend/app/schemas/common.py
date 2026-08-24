from typing import Any

from pydantic import BaseModel, Field


# Returns schema for basic health or shallow ping endpoints
class HealthCheckResponse(BaseModel):
    """Shallow system status response."""

    status: str = Field(
        default="ok", json_schema_extra={"example": "ok"}
    )  # indicates the web server process itself is responsive
    environment: str = Field(
        ..., json_schema_extra={"example": "development"}
    )  # indicates the enviroment (prod, dev, test)


# health of one specific dependency (e.g. postgres, redis, qdrant, etc.)
class ServiceStatus(BaseModel):
    """Detailed health status for an individual infrastructure service."""

    status: str = Field(
        ..., json_schema_extra={"example": "healthy"}
    )  # "healthy" or "unhealthy"
    message: str | None = Field(
        default=None, json_schema_extra={"example": "Connected successfully"}
    )  # optional error/success message
    # Hard dependencies (postgres, redis, qdrant) make /health/deep return 503 when they
    # fail; soft ones (mlflow, ollama) only degrade it.
    required: bool = Field(default=True, json_schema_extra={"example": True})


# Aggregate response returned by /api/v1/health/deep.
class DeepHealthResponse(BaseModel):
    """Deep system status response evaluating all downstream container dependencies."""

    status: str = Field(
        ..., json_schema_extra={"example": "healthy"}
    )  # "healthy" | "degraded" | "unhealthy"
    services: dict[
        str, ServiceStatus
    ]  # It returns the overal platform status alonside a dict of Key-Value mapping each service name to its ServiceStatus


# Unified evelope structure for generic API actions (deletions, cancellations, triggering async events)
class StandardResponse(BaseModel):
    """Generic status response payload."""

    success: bool = True
    message: str
    data: dict[str, Any] | None = None

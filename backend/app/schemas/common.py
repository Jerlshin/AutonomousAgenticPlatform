from typing import Any, Optional
from pydantic import BaseModel, Field

# Returns schema for basic health or shallow ping endpoints
class HealthCheckResponse(BaseModel):
    """Shallow system status response."""

    status: str = Field(default="ok", example="ok") # indicates the web server process itself is responsive
    environment: str = Field(..., example="development") # indicates the enviroment (prod, dev, test)

# health of one specific dependency (e.g. postgres, redis, qdrant, etc.)
class ServiceStatus(BaseModel):
    """Detailed health status for an individual infrastructure service."""

    status: str = Field(..., example="healthy") # status string
    message: Optional[str] = Field(default=None, example="Connected successfully") # optional error/success message

# Aggregate response returned by /api/v1/health/deep.
class DeepHealthResponse(BaseModel):
    """Deep system status response evaluating all downstream container dependencies."""

    status: str = Field(..., example="healthy")
    services: dict[str, ServiceStatus] # It returns the overal platform status alonside a dict of Key-Value mapping each service name to its ServiceStatus

# Unified evelope structure for generic API actions (deletions, cancellations, triggering async events)
class StandardResponse(BaseModel):
    """Generic status response payload."""

    success: bool = True
    message: str
    data: Optional[dict[str, Any]] = None
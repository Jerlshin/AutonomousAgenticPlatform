from typing import Optional

from pydantic import BaseModel, Field


class WorkflowRunRequest(BaseModel):
    user_request: str = Field(..., min_length=3)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)


class HealthResponse(BaseModel):
    status: str
    service: str

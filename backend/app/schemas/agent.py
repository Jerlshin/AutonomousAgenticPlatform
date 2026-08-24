from typing import Any

from pydantic import BaseModel, Field


# declares shema for incoming request when testing a single agent directly via an API route without running the entire langGraph pipeline
class AgentExecutionRequest(BaseModel):
    """Payload for directly invoking an individual agent node for isolated testing."""

    agent_name: str = Field(  # which node to run
        ...,  # means required
        json_schema_extra={"example": "planner"},
        description="Target agent node (planner, coder, researcher, etc.).",
    )
    input_data: dict[str, Any] = Field(
        ...,  # mandatory dict containing the exact input state or arguments required by that specific agent node
        description="Parameters or message payload passed directly into the agent node.",
    )


# decalres the output schema retrned to the client after the agent finishes its node execution
class AgentExecutionResponse(BaseModel):
    """Response schema returned by an isolated agent node invocation."""

    agent_name: str
    status: str = Field(default="completed", json_schema_extra={"example": "completed"})
    output_data: dict[str, Any]
    logs: list[str] | None = None

from typing import TypedDict, List, Optional, Dict
from core.models import (
    PlanStep,
    CodeArtifact,
    ExecutionLog,
    ExecutionResult,
    AgentEvent,
)
from core.enums import WorkflowStatus

class AgentState(TypedDict):
    task_id: str
    user_request: str
    
    # Planning
    current_plan: List[PlanStep]
    active_step_id: Optional[str]
    
    # Generated artifacts
    generated_artifacts: List[CodeArtifact]
    
    # Runtime
    latest_execution: Optional[ExecutionResult]
    exeution_logs: List[ExecutionLog]
    
    # Cognitive memory
    retrieved_context: List[str]
    reflection_notes: List[str]
    
    # Lifecycle
    retry_count: int
    iterations: int
    
    # State
    status: WorkflowStatus
    
    # Metrics
    metrics: Dict[str, float]

    
    
    
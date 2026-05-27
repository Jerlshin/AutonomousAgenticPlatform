from typing import Any, Dict, List, Optional, TypedDict
from core.models import (
    AgentEvent,
    CodeArtifact,
    EvaluationReport,
    ExecutionLog,
    ExecutionResult,
    ExperimentRecord,
    PlanStep,
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
    execution_logs: List[ExecutionLog]
    
    # Cognitive memory
    retrieved_context: List[str]
    reflection_notes: List[str]
    
    # Lifecycle
    retry_count: int
    iterations: int
    
    # State
    status: WorkflowStatus
    events: List[AgentEvent]
    
    # Metrics
    metrics: Dict[str, float]
    evaluation: Optional[EvaluationReport]
    experiment_records: List[ExperimentRecord]
    errors: List[str]
    max_retries: int
    metadata: Dict[str, Any]

    
    
    

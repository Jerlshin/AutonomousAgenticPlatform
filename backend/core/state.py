import operator
from typing import Any, Dict, List, Optional, TypedDict, Annotated
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
    generated_artifacts: Annotated[List[CodeArtifact], operator.add]
    
    # Runtime
    latest_execution: Optional[ExecutionResult]
    execution_logs: Annotated[List[ExecutionLog], operator.add]
    
    # Cognitive memory
    retrieved_context: Annotated[List[str], operator.add]
    reflection_notes: Annotated[List[str], operator.add]
    
    # Lifecycle
    retry_count: int
    iterations: int
    
    # State
    status: WorkflowStatus
    events: Annotated[List[AgentEvent], operator.add]
    
    # Metrics
    metrics: Dict[str, float]
    evaluation: Optional[EvaluationReport]
    experiment_records: Annotated[List[ExperimentRecord], operator.add]
    errors: Annotated[List[str], operator.add]
    max_retries: int
    metadata: Dict[str, Any]

    
    
    

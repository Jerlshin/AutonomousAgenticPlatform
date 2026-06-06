"""
Using string enums makes the multi-agent code significantly more robust and type-safe
* Eliminates silent typos
* Auto-completion and code readability
* Native JSON serialization
"""

from enum import Enum

# Tracks the lifecycle or current phase of an execution pipeline
class WorkflowStatus(str, Enum):
    INITIALIZED = "initialized"
    PLANNED = "planned"
    RESEARCHING = "researching"
    PLANNING = "planning"
    CODING = "coding"
    EXECUTING = "executing"
    DEBUGGING = "debugging"
    LOGGING = "logging"
    EVALUATING = "evaluating"
    REFLECTING = "reflecting"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_INPUT = "waiting_for_input"

# Types of events that occur inside your architecture
class EventType(str, Enum):
    STATE_UPDATE = "state_update"
    PLAN_CREATED = "plan_created"
    RESEARCH_COMPLETED = "research_completed"
    CODE_GENERATED = "code_generated"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    DEBUG_PATCH_CREATED = "debug_patch_created"
    MLFLOW_LOGGED = "mlflow_logged"
    EVALUATION_COMPLETED = "evaluation_completed"
    ERROR = "error"
    REFLECTION = "reflection"

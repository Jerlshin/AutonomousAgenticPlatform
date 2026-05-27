from agents.base import BaseAgent
from core.enums import EventType, WorkflowStatus
from core.events import create_event
from tools.mlflow_tracker import experiment_tracker


class MLOpsAgent(BaseAgent):
    name = "mlops_agent"

    def run(self, state):
        artifact = self.latest_artifact(state)
        execution = state.get("latest_execution")
        metrics = {
            "iterations": float(state["iterations"]),
            "retry_count": float(state["retry_count"]),
            "execution_success": 1.0 if execution and execution.success else 0.0,
            "execution_time": float(execution.execution_time or 0.0) if execution else 0.0,
        }
        record = experiment_tracker.log_run(
            task_id=state["task_id"],
            status=state["status"].value if hasattr(state["status"], "value") else str(state["status"]),
            metrics=metrics,
            params={
                "request": state["user_request"],
                "max_retries": state["max_retries"],
            },
            artifacts=[artifact.filename] if artifact else [],
        )
        event = create_event(
            event_type=EventType.MLFLOW_LOGGED,
            source_agent=self.name,
            payload={"run_id": record.run_id, "metrics": metrics},
        )
        return {
            "metrics": {**state["metrics"], **metrics},
            "experiment_records": state["experiment_records"] + [record],
            "status": WorkflowStatus.LOGGING,
            "events": state["events"] + [event],
        }

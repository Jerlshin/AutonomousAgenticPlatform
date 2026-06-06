import asyncio
from agents.base import BaseAgent
from core.enums import EventType, WorkflowStatus
from core.events import create_event
from core.models import ExecutionLog
from tools.sandbox import sandbox


class ExecutionAgent(BaseAgent):
    name = "execution_agent"

    async def run(self, state):
        artifact = self.latest_artifact(state)
        if artifact is None:
            event = create_event(
                event_type=EventType.ERROR,
                source_agent=self.name,
                payload={"message": "No generated artifact is available for execution."},
            )
            return {
                "status": WorkflowStatus.FAILED,
                "errors": ["No generated artifact is available for execution."],
                "events": [event],
            }

        started = create_event(
            event_type=EventType.EXECUTION_STARTED,
            source_agent=self.name,
            payload={"artifact_id": artifact.artifact_id, "filename": artifact.filename},
        )

        # 
        result = await sandbox.execute(artifact)
        completed = create_event(
            event_type=EventType.EXECUTION_COMPLETED,
            source_agent=self.name,
            payload={
                "success": result.success,
                "exit_code": result.exit_code,
                "execution_time": result.execution_time,
            },
        )
        log_level = "info" if result.success else "error"
        log_message = result.stdout if result.success else result.stderr
        return {
            "latest_execution": result,
            "execution_logs": [
                ExecutionLog(level=log_level, message=log_message or "")
            ],
            "status": WorkflowStatus.EXECUTING,
            "events": [started, completed],
        }

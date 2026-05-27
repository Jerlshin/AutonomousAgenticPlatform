from agents.base import BaseAgent
from core.enums import EventType, WorkflowStatus
from core.events import create_event
from core.models import CodeArtifact
from llms.providers import debugger_llm


class DebugAgent(BaseAgent):
    name = "debug_agent"

    def run(self, state):
        artifact = self.latest_artifact(state)
        execution = state.get("latest_execution")
        if artifact is None or execution is None:
            event = create_event(
                event_type=EventType.ERROR,
                source_agent=self.name,
                payload={"message": "Debugging requires an artifact and execution result."},
            )
            return {
                "status": WorkflowStatus.FAILED,
                "errors": state["errors"] + ["Debugging requires an artifact and execution result."],
                "events": state["events"] + [event],
            }

        prompt = f"""
        You are the Debug Agent for an autonomous AI R&D platform.
        Fix the Python program so it runs successfully and still satisfies the user request.

        User Request:
        {state['user_request']}

        Current Code:
        {artifact.content}

        stdout:
        {execution.stdout}

        stderr:
        {execution.stderr}

        Rules:
        - Return ONLY corrected executable Python code.
        - No markdown.
        - No explanations.
        """
        patched_code = debugger_llm.invoke(prompt).strip()
        patched_artifact = CodeArtifact(
            filename=artifact.filename,
            language=artifact.language,
            content=patched_code,
        )
        event = create_event(
            event_type=EventType.DEBUG_PATCH_CREATED,
            source_agent=self.name,
            payload={
                "previous_artifact_id": artifact.artifact_id,
                "artifact_id": patched_artifact.artifact_id,
                "retry_count": state["retry_count"] + 1,
            },
        )
        return {
            "generated_artifacts": state["generated_artifacts"] + [patched_artifact],
            "retry_count": state["retry_count"] + 1,
            "status": WorkflowStatus.DEBUGGING,
            "events": state["events"] + [event],
        }

from agents.base import BaseAgent
from core.enums import EventType, WorkflowStatus
from core.events import create_event
from core.models import EvaluationReport
from llms.providers import evaluator_llm


class EvaluationAgent(BaseAgent):
    name = "evaluation_agent"

    def run(self, state):
        execution = state.get("latest_execution")
        artifact = self.latest_artifact(state)
        passed = bool(execution and execution.success)
        score = 1.0 if passed else 0.0
        llm_summary = ""

        if artifact and execution:
            prompt = f"""
            You are the Evaluation Agent for an autonomous AI R&D platform.
            Review whether the final code and execution output satisfy the user request.

            User Request:
            {state['user_request']}

            Code:
            {artifact.content}

            stdout:
            {execution.stdout}

            stderr:
            {execution.stderr}

            Return a concise verdict in 2 to 4 sentences.
            """
            llm_summary = evaluator_llm.invoke(prompt)

        report = EvaluationReport(
            passed=passed,
            score=score,
            summary=llm_summary or ("Execution succeeded." if passed else "Execution failed."),
            checks={
                "execution_success": passed,
                "exit_code": execution.exit_code if execution else None,
                "artifact_present": artifact is not None,
            },
        )
        event = create_event(
            event_type=EventType.EVALUATION_COMPLETED,
            source_agent=self.name,
            payload={"passed": report.passed, "score": report.score},
        )
        return {
            "evaluation": report,
            "metrics": {**state["metrics"], "evaluation_score": report.score},
            "status": WorkflowStatus.COMPLETED if report.passed else WorkflowStatus.FAILED,
            "events": state["events"] + [event],
        }

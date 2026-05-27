import re
from agents.base import BaseAgent
from core.models import CodeArtifact
from core.events import create_event
from core.enums import EventType, WorkflowStatus
from llms.providers import coder_llm


class CodingAgent(BaseAgent):
    name = "coding_agent"

    async def run(self, state):
        plan_text = "\n".join(
            [
                f"- {step.description}"
                for step in state["current_plan"]
            ]
        )
        context_text = "\n\n".join(state.get("retrieved_context", [])) or "No retrieved context."

        prompt = f"""
        You are a senior AI software engineer.

        Generate clean, production-grade Python code that satisfies the user request.

        User Request:
        {state['user_request']}

        Execution Plan:
        {plan_text}

        Retrieved Context:
        {context_text}

        Rules:
        - Return ONLY executable Python code.
        - No markdown.
        - No explanations.
        - Prefer Python standard library unless the request requires a common package.
        - Include a small __main__ path or direct executable logic.
        """

        generated_code = await coder_llm.invoke(prompt)

        match = re.search(r"```(?:python)?\n?(.*?)\n?```", generated_code, re.DOTALL)
        extracted_code = match.group(1).strip() if match else generated_code.strip()

        artifact = CodeArtifact(
            filename="generated_script.py",
            content=extracted_code,
        )

        event = create_event(
            event_type=EventType.CODE_GENERATED,
            source_agent=self.name,
            payload={
                "artifact_id": artifact.artifact_id,
                "filename": artifact.filename,
            },
        )

        return {
            "generated_artifacts": [artifact],
            "iterations": state["iterations"] + 1,
            "status": WorkflowStatus.CODING,
            "events": [event],
        }

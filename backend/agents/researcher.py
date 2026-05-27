from agents.base import BaseAgent
from core.enums import EventType, WorkflowStatus
from core.events import create_event
from llms.providers import researcher_llm
from tools.research import research_tool


class ResearchAgent(BaseAgent):
    name = "research_agent"

    async def run(self, state):
        local_hits = await research_tool.search(state["user_request"], limit=5)
        plan_text = "\n".join(step.description for step in state.get("current_plan", []))
        synthesis_prompt = f"""
        You are the Research Agent for a local autonomous AI R&D platform.
        Summarize only the context needed by the Coding and Evaluation agents.

        User Request:
        {state['user_request']}

        Plan:
        {plan_text}

        Local Search Hits:
        {local_hits or ['No local hits found.']}

        Rules:
        - Return 3 to 6 concise implementation notes.
        - Mention assumptions and likely dependencies.
        """
        synthesis = await researcher_llm.invoke(synthesis_prompt)
        context = local_hits + [synthesis]
        event = create_event(
            event_type=EventType.RESEARCH_COMPLETED,
            source_agent=self.name,
            payload={"context_items": len(context)},
        )
        return {
            "retrieved_context": context,
            "status": WorkflowStatus.RESEARCHING,
            "events": [event],
        }

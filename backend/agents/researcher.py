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
        You are the Research Agent for an autonomous AI platform.
        Analyze the user's request and plan to determine if external APIs requiring API keys/authentication are needed.
        If an API key is required and not provided in the request, you must prompt the user for it.
        
        User Request:
        {state['user_request']}

        Plan:
        {plan_text}

        Local Search Hits:
        {local_hits or ['No local hits found.']}

        Rules:
        - Return ONLY a valid JSON object.
        - "requires_human_input": true if an API key is missing and required, otherwise false.
        - "human_query": If requires_human_input is true, provide a polite question asking for the key (e.g., "Please provide your NewsAPI key, or type 'NO' to fallback to public alternatives."). If false, set to null.
        - "synthesis": A concise string with 3-6 implementation notes for the Coding agent based on the search hits and request.
        """
        import json
        import re
        
        response = await researcher_llm.invoke(synthesis_prompt, json_mode=True)
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            response = match.group(0)
            
        try:
            parsed = json.loads(response)
            if not isinstance(parsed, dict):
                parsed = {"requires_human_input": False, "human_query": None, "synthesis": str(parsed)}
        except json.JSONDecodeError:
            parsed = {"requires_human_input": False, "human_query": None, "synthesis": response}
            
        context = local_hits + [parsed.get("synthesis", "")]
        event = create_event(
            event_type=EventType.RESEARCH_COMPLETED,
            source_agent=self.name,
            payload={"context_items": len(context), "requires_human_input": parsed.get("requires_human_input", False)},
        )
        return {
            "retrieved_context": context,
            "human_query": parsed.get("human_query"),
            "status": WorkflowStatus.WAITING_FOR_INPUT if parsed.get("requires_human_input") else WorkflowStatus.RESEARCHING,
            "events": [event],
        }

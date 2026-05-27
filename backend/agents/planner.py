import json
import re
from agents.base import BaseAgent
from core.models import PlanStep
from core.events import create_event
from core.enums import EventType, WorkflowStatus
from llms.providers import planner_llm

class PlannerAgent(BaseAgent):
    name = "planner_agent"
    
    async def run(self, state):
        prompt = f"""
        You are the Planner Agent for an autonomous AI research and development platform.
        
        Break the user request into 3 to 5 concrete execution steps for the other agents.
        Include research, coding, execution/debugging, and evaluation when relevant.
        
        Request:
        {state['user_request']}
        
        Rules:
        - Return a JSON object with a single key "steps", which is a list of strings.
        - Keep each step concise and actionable.
        """
        
        response = await planner_llm.invoke(prompt, json_mode=True)
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            response = match.group(0)
        try:
            parsed = json.loads(response)
            raw_steps = parsed.get("steps", [])
        except json.JSONDecodeError:
            raw_steps = []
        raw_steps = raw_steps[:5] or ["Generate and validate a Python implementation for the request."]
        structured_steps = []
        
        for idx, step in enumerate(raw_steps):
            # The LLM might return a list of strings or a list of dicts like {"step": "..."}
            desc = step.get("step", str(step)) if isinstance(step, dict) else str(step)
            structured_steps.append(
                PlanStep(
                    title=f"Step {idx+1}",
                    description=desc,
                )
            )
        
        event = create_event(
            event_type=EventType.PLAN_CREATED,
            source_agent=self.name,
            payload={
                "steps_created": len(structured_steps),
            }
        )
        return {
            "current_plan": structured_steps,
            "status": WorkflowStatus.PLANNED,
            "events": [event],
        }

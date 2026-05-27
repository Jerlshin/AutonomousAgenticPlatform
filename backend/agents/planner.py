from agents.base import BaseAgent
from core.models import PlanStep
from core.events import create_event
from core.enums import EventType, WorkflowStatus
from llms.providers import planner_llm

class PlannerAgent(BaseAgent):
    name = "planner_agent"
    
    def run(self, state):
        prompt = f"""
        You are the Planner Agent for an autonomous AI research and development platform.
        
        Break the user request into 3 to 5 concrete execution steps for the other agents.
        Include research, coding, execution/debugging, and evaluation when relevant.
        
        Request:
        {state['user_request']}
        
        Rules:
        - Return one step per line.
        - No markdown bullets.
        - Keep each line concise and actionable.
        """
        
        response = planner_llm.invoke(prompt)
        raw_steps = [
            step.strip(" -0123456789.")
            for step in response.strip().split("\n")
            if step.strip()
        ]
        raw_steps = raw_steps[:5] or ["Generate and validate a Python implementation for the request."]
        structured_steps = []
        
        for idx, step in enumerate(raw_steps):
            structured_steps.append(
                PlanStep(
                    title=f"Step {idx+1}",
                    description=step,
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
            "events": state["events"] + [event],
        }

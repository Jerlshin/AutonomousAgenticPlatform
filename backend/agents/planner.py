from agents.base import BaseAgent
from core.models import PlanStep
from core.events import create_event
from core.enums import EventType, WorkflowStatus
from llms.providers import planner_llm

class PlannerAgent(BaseAgent):
    name = "planner_agent"
    
    def run(self, state):
        print("\n[Planner Agent] Creating execution strategy...")
        
        prompt = f"""
        You are an elite AI systems planner.
        
        Break the following request into a maximum of 5 highly logical execution steps.
        
        Request:
        {state['user_request']}
        
        Rules:
        - Return concise 
        """
        
        response = planner_llm.invoke(prompt)
        raw_steps = [step.strip() for step in response.strip().split("\n") if step.strip()]
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
        
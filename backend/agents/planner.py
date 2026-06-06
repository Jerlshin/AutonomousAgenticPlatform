import json
import re
from agents.base import BaseAgent
from core.models import PlanStep
from core.events import create_event
from core.enums import EventType, WorkflowStatus
from llms.providers import planner_llm

# Inheritance from the BaseAgent class, which provides common functionality for all agents in the system.
class PlannerAgent(BaseAgent):
    name = "planner_agent"
    
    # Declares that this function is an Asynchronous function. Because AI inferences take time - this allows the backend server to process other user interactions
    async def run(self, state):
        # state: represents the global, shared dictionary recording the current lifecycle status of the runtime workspace
        
        # Prompt for the Planner Agent
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

        # get the LLM response
        # You ensure it returns in JSON format
        # 'async' makes a function a "coroutine" - (capable of pausing), while 'await' is actual pause button
        response = await planner_llm.invoke(prompt, json_mode=True) # pauses the specific function until underlying Ollama model finishes generating text.
        
        # The LLM output is Dict["steps": List[str]] - but we need to be defensive in parsing it, since LLMs can be unpredictable. We use regex to extract the JSON portion of the response, then parse it.
        match = re.search(
            r"\{.*\}",  # searches for an opening brace, any text in between, and a closing brace
            response,
            re.DOTALL,  # normally, the dot (.) in regex does not match newline characters. By using re.DOTALL, we allow it to match across multiple lines, which is important for JSON that may be pretty-printed with line breaks.
        )
        
        if match:
            response = match.group(0) # if match found, extracts the exact text trapped between first { and last }
        try:
            parsed = json.loads(response)
            raw_steps = parsed.get("steps", [])
        except json.JSONDecodeError:
            raw_steps = []
        
        raw_steps = raw_steps[:5] or ["Generate and validate a Python implementation for the request."]
        structured_steps = []
        
        # Shape-shifting and normalization
        for idx, step in enumerate(raw_steps):
            # The LLM might return a list of strings or a list of dicts like {"step": "..."}
            desc = step.get("step", str(step)) if isinstance(step, dict) else str(step) # normalization.

            # Converts raw data into a data class
            structured_steps.append(
                PlanStep(
                    title=f"Step {idx+1}",
                    description=desc,
                )
            )
        
        # creates a unified telemetry object recording that a plan was computed, along with a metadata payload tracking how many
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

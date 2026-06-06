from abc import ABC, abstractmethod
from typing import Any, Dict

from core.state import AgentState

# Abstract Base Class
class BaseAgent(ABC): # inherits from ABC. it is now an abstract blueprint
    name: str   # This enforces that every child must declare a "name" variable as a string
    
    @abstractmethod  # If any new class wants to inherit from "BaseAgent", it MUST provide its own implementation for this "run" method.
    async def run(self, state: AgentState) -> Dict[str, Any]: # ensuring that all these should be there
        pass 

    @staticmethod # unlike normal class methods, a static method doesn't require a reference to the active object (self). It is essentially a normal helper function wrapper inside the class wrapper just for organization.
    def latest_artifact(state: AgentState):
        artifacts = state.get("generated_artifacts", [])
        return artifacts[-1] if artifacts else None

from abc import ABC, abstractmethod
from typing import Any, Dict

from core.state import AgentState

class BaseAgent(ABC):
    name: str
    
    @abstractmethod
    async def run(self, state: AgentState) -> Dict[str, Any]:
        pass

    @staticmethod
    def latest_artifact(state: AgentState):
        artifacts = state.get("generated_artifacts", [])
        return artifacts[-1] if artifacts else None

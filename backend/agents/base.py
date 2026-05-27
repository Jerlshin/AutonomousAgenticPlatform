from abc import ABC, abstractmethod
from core.state import AgentState

class BaseAgent(ABC):
    name: str
    
    @abstractmethod
    def run(self, state: AgentState) -> dict:
        pass
    

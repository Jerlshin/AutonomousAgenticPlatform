from typing import List

class EphemeralMemory:
    def __init__(self):
        self.context_store: List[str] = []
    
    def add(self, content: str):
        self.context_store.append(content)
    
    def retrieve(self) -> List[str]:
        return self.context_store[-5:]

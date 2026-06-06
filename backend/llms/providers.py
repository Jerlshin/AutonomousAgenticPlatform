from core.config import settings
from tools.ollama import ollama_client

"""
### Notes:

* Synchronous - blocking
* Asychronous - non-blocking
"""

# This module defines LLM provider classes and initializes instances for different agent roles.
class LocalOllamaLLM:
    def __init__(self, model: str, temperature: float = 0.0):
        self.model = model
        self.temperature = temperature

    async def invoke(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        return await ollama_client.generate(
            model=self.model,
            prompt=prompt,
            temperature=self.temperature,
            system=system,
            json_mode=json_mode,
        )

# Initialize LLM instances
planner_llm = LocalOllamaLLM(settings.planner_model, temperature=0.1)
researcher_llm = LocalOllamaLLM(settings.researcher_model, temperature=0.1)
coder_llm = LocalOllamaLLM(settings.coder_model, temperature=0.0)
debugger_llm = LocalOllamaLLM(settings.debugger_model, temperature=0.0)
evaluator_llm = LocalOllamaLLM(settings.evaluator_model, temperature=0.0)

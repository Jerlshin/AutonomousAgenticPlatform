from core.config import settings
from tools.ollama import ollama_client


class LocalOllamaLLM:
    def __init__(self, model: str, temperature: float = 0.0):
        self.model = model
        self.temperature = temperature

    def invoke(self, prompt: str, system: str | None = None) -> str:
        return ollama_client.generate(
            model=self.model,
            prompt=prompt,
            temperature=self.temperature,
            system=system,
        )


planner_llm = LocalOllamaLLM(settings.planner_model, temperature=0.1)
researcher_llm = LocalOllamaLLM(settings.researcher_model, temperature=0.1)
coder_llm = LocalOllamaLLM(settings.coder_model, temperature=0.0)
debugger_llm = LocalOllamaLLM(settings.debugger_model, temperature=0.0)
evaluator_llm = LocalOllamaLLM(settings.evaluator_model, temperature=0.0)

from langchain_community.llms import Ollama


planner_llm = Ollama(
    model="llama3.1:8b",
    temperature=0.1,
)


coder_llm = Ollama(
    model="qwen2.5-coder:3b",
    temperature=0.0,
)
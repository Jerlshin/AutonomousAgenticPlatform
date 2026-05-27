import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    planner_model: str = os.getenv("PLANNER_MODEL", "llama3.1:8b")
    coder_model: str = os.getenv("CODER_MODEL", "qwen2.5-coder:3b")
    researcher_model: str = os.getenv("RESEARCHER_MODEL", "llama3.1:8b")
    debugger_model: str = os.getenv("DEBUGGER_MODEL", "qwen2.5-coder:3b")
    evaluator_model: str = os.getenv("EVALUATOR_MODEL", "llama3.1:8b")
    sandbox_timeout_seconds: int = int(os.getenv("SANDBOX_TIMEOUT_SECONDS", "20"))
    use_docker_sandbox: bool = os.getenv("USE_DOCKER_SANDBOX", "false").lower() == "true"
    sandbox_image: str = os.getenv("SANDBOX_IMAGE", "autonomous-ai-sandbox:latest")
    max_retries: int = int(os.getenv("MAX_AGENT_RETRIES", "2"))
    local_memory_path: str = os.getenv("LOCAL_MEMORY_PATH", "docs")
    experiment_log_path: str = os.getenv("EXPERIMENT_LOG_PATH", "mlops/experiment_runs.jsonl")


settings = Settings()

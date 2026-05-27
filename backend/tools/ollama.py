import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from core.config import settings


class OllamaError(RuntimeError):
    pass


@dataclass
class OllamaClient:
    base_url: str = settings.ollama_base_url

    def generate(self, model: str, prompt: str, temperature: float = 0.0, system: Optional[str] = None) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise OllamaError(f"Unable to reach Ollama at {self.base_url}: {exc}") from exc

        text = body.get("response")
        if not isinstance(text, str):
            raise OllamaError(f"Ollama returned an unexpected payload: {body}")
        return text.strip()


ollama_client = OllamaClient()

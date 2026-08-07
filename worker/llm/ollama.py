import logging
import os

import httpx

from .base import AbstractLLMBackend, GenerateResult, LLMBackendError

logger = logging.getLogger("worker.llm.ollama")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")

_client = httpx.Client(timeout=300.0)


class OllamaBackend(AbstractLLMBackend):
    def __init__(self) -> None:
        self.model = os.environ.get("DEFAULT_MODEL", "tinyllama")

    def generate(self, prompt: str, temperature: float, max_tokens: int) -> GenerateResult:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            response = _client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            generated_text = data["response"]
            eval_duration_ns = data["eval_duration"]
            tokens_generated = data["eval_count"]
        except Exception as exc:
            raise LLMBackendError(str(exc), retryable=True) from exc

        total_time_ms = int(eval_duration_ns / 1e6)
        tokens_per_second = (
            tokens_generated / (total_time_ms / 1000) if total_time_ms > 0 else 0.0
        )

        return GenerateResult(
            generated_text=generated_text,
            total_time_ms=total_time_ms,
            tokens_generated=tokens_generated,
            tokens_per_second=tokens_per_second,
        )

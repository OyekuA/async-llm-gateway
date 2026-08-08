import os
import random
import time

from .base import AbstractLLMBackend, GenerateResult, LLMBackendError


class MockBackend(AbstractLLMBackend):
    def __init__(self) -> None:
        self._failure_rate = float(os.environ.get("MOCK_FAILURE_RATE", "0.0"))

    def generate(
        self, prompt: str, temperature: float, max_tokens: int, model: str
    ) -> GenerateResult:
        if random.random() < self._failure_rate:
            raise LLMBackendError("Simulated OOM", retryable=False)

        start = time.perf_counter()
        time.sleep(random.uniform(2, 6))
        total_time_ms = int(round((time.perf_counter() - start) * 1000))

        generated_text = "This is a canned mock response."
        tokens_generated = random.randint(min(32, max_tokens), max_tokens)
        tokens_per_second = tokens_generated / max(total_time_ms, 1) * 1000

        return GenerateResult(
            generated_text=generated_text,
            total_time_ms=total_time_ms,
            tokens_generated=tokens_generated,
            tokens_per_second=tokens_per_second,
        )

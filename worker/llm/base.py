from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class GenerateResult:
    generated_text: str
    total_time_ms: int
    tokens_generated: int
    tokens_per_second: float


class LLMBackendError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class AbstractLLMBackend(ABC):
    @abstractmethod
    def generate(self, prompt: str, temperature: float, max_tokens: int) -> GenerateResult:
        raise NotImplementedError

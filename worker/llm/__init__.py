import os

from .base import AbstractLLMBackend, GenerateResult, LLMBackendError
from .mock import MockBackend
from .ollama import OllamaBackend

__all__ = [
    "AbstractLLMBackend",
    "GenerateResult",
    "LLMBackendError",
    "MockBackend",
    "OllamaBackend",
    "get_backend",
]


def get_backend() -> AbstractLLMBackend:
    if os.environ.get("MOCK_MODE", "true").lower() == "true":
        return MockBackend()
    return OllamaBackend()

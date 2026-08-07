from .base import AbstractLLMBackend, GenerateResult


class OllamaBackend(AbstractLLMBackend):
    def generate(self, prompt: str, temperature: float, max_tokens: int) -> GenerateResult:
        raise NotImplementedError("OllamaBackend is implemented in ticket #12")

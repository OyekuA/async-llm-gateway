import json
import logging
import os
import time

import httpx

from .base import AbstractLLMBackend, GenerateResult, LLMBackendError
from .mock import MockBackend
from .ollama import OLLAMA_URL, OllamaBackend

logger = logging.getLogger("worker.llm")

TAGS_ATTEMPTS = 30
TAGS_INTERVAL_S = 2
MODEL_PULL_TIMEOUT = int(os.environ.get("MODEL_PULL_TIMEOUT", "600"))

_model_pulled = False
_ready_logged = False

__all__ = [
    "AbstractLLMBackend",
    "GenerateResult",
    "LLMBackendError",
    "MockBackend",
    "OllamaBackend",
    "get_backend",
]


def _fetch_tags() -> dict:
    last_error = None
    for attempt in range(TAGS_ATTEMPTS):
        try:
            response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Ollama not ready (attempt %d/%d): %s", attempt + 1, TAGS_ATTEMPTS, exc
            )
            if attempt < TAGS_ATTEMPTS - 1:
                time.sleep(TAGS_INTERVAL_S)
    raise LLMBackendError(
        f"Ollama unreachable after {TAGS_ATTEMPTS} attempts: {last_error}",
        retryable=True,
    )


def _model_in_tags(tags: dict, model: str) -> bool:
    names = [entry.get("name", "") for entry in tags.get("models", [])]
    return model in names or f"{model}:latest" in names


def _pull_model(model: str) -> None:
    deadline = time.monotonic() + MODEL_PULL_TIMEOUT
    last_percent = -1
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "POST", f"{OLLAMA_URL}/api/pull", json={"name": model}
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    if time.monotonic() > deadline:
                        raise LLMBackendError(
                            f"Timed out pulling model {model} after "
                            f"{MODEL_PULL_TIMEOUT}s",
                            retryable=True,
                        )
                    data = json.loads(line)
                    status = data.get("status")
                    if status == "error":
                        raise LLMBackendError(
                            data.get("error", f"Failed to pull model {model}"),
                            retryable=True,
                        )
                    if status == "success":
                        logger.info("Pulling model %s: complete", model)
                        return
                    if status == "downloading":
                        completed = data.get("completed") or 0
                        total = data.get("total") or 0
                        percent = int(completed / total * 100) if total else 0
                        if percent != last_percent:
                            last_percent = percent
                            logger.info(
                                "Pulling model %s: downloading %d%% (%dMB/%dMB)",
                                model,
                                percent,
                                int(completed / 1024 / 1024),
                                int(total / 1024 / 1024),
                            )
        raise LLMBackendError(
            f"Pull of model {model} finished without success status",
            retryable=True,
        )
    except LLMBackendError:
        raise
    except Exception as exc:
        raise LLMBackendError(f"Failed to pull model {model}: {exc}", retryable=True) from exc


def get_backend() -> AbstractLLMBackend:
    if os.environ.get("MOCK_MODE", "true").lower() == "true":
        return MockBackend()

    global _model_pulled, _ready_logged
    model = os.environ.get("DEFAULT_MODEL", "tinyllama")
    tags = _fetch_tags()
    if not _model_in_tags(tags, model) and not _model_pulled:
        _pull_model(model)
        _model_pulled = True
    if not _ready_logged:
        logger.info("Using backend: ollama (%s)", model)
        _ready_logged = True
    return OllamaBackend()

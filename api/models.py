import os
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_VALID_MODELS = "tinyllama,llama3,llama3:8b"


def _valid_models() -> frozenset[str]:
    raw = os.environ.get("VALID_MODELS", DEFAULT_VALID_MODELS)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4096)
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=256, ge=1, le=4096)
    model: Optional[str] = Field(default="tinyllama")

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: Optional[str]) -> Optional[str]:
        if value is None or os.environ.get("MOCK_MODE", "true") == "true":
            return value
        allowed = _valid_models()
        if value not in allowed:
            raise ValueError(
                f"model '{value}' is not in VALID_MODELS; valid models: {', '.join(sorted(allowed))}"
            )
        return value


class GenerateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    status_url: str


class GenerateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    generated_text: str
    model: str
    total_time_ms: int
    tokens_generated: int
    tokens_per_second: float


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["QUEUED", "PROCESSING", "SUCCESS", "FAILURE", "TIMEOUT"]
    result: Optional[GenerateResult] = None
    error: Optional[str] = None
    retryable: Optional[bool] = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    redis: str
    db: str

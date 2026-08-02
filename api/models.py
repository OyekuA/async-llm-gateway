from typing import Optional

from pydantic import BaseModel


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class GenerateResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    error: Optional[str] = None


class SyncGenerateResponse(BaseModel):
    task_id: str
    status: str
    generated_text: Optional[str] = None

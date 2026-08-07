import datetime
import logging
import uuid

from fastapi import APIRouter, Request

from common.database import (
    commit_with_retry_async,
    execute_with_retry_async,
    get_connection_async,
)
from ..celery_app import app as celery_app
from ..errors import InfrastructureUnavailable
from ..models import GenerateRequest, GenerateResponse

router = APIRouter()

logger = logging.getLogger("api.routes.generate")


def _enqueue_task(task_id: str) -> None:
    try:
        celery_app.send_task("worker.tasks.run_inference", args=[task_id])
    except Exception:
        logger.warning(
            "Redis unreachable; task %s left QUEUED in SQLite", task_id, exc_info=True
        )


@router.post("/generate", response_model=GenerateResponse, status_code=202)
async def generate(body: GenerateRequest, request: Request) -> GenerateResponse:
    if not request.app.state.db_ready:
        raise InfrastructureUnavailable("task database is unavailable")

    task_id = str(uuid.uuid4())
    created_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    conn = await get_connection_async()
    try:
        await execute_with_retry_async(
            await conn.cursor(),
            "INSERT INTO tasks (task_id, status, prompt, temperature, max_tokens, "
            "model, created_at, started_at, completed_at, retryable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "QUEUED",
                body.prompt,
                body.temperature,
                body.max_tokens,
                body.model,
                created_at,
                None,
                None,
                0,
            ),
        )
        await commit_with_retry_async(conn)
    finally:
        await conn.close()

    _enqueue_task(task_id)

    return GenerateResponse(task_id=task_id, status_url=f"/status/{task_id}")

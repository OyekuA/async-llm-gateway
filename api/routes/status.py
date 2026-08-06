import datetime
import os

from fastapi import APIRouter, HTTPException, Request

from common.database import (
    commit_with_retry_async,
    execute_with_retry_async,
    get_connection_async,
)
from ..errors import InfrastructureUnavailable
from ..models import GenerateResult, StatusResponse

router = APIRouter()

_SELECT_SQL = (
    "SELECT task_id, status, prompt, generated_text, model, total_time_ms, "
    "tokens_generated, tokens_per_second, error, created_at, retryable "
    "FROM tasks WHERE task_id = ?"
)
_TIMEOUT_UPDATE_SQL = (
    "UPDATE tasks SET status='TIMEOUT', completed_at=?, error=? "
    "WHERE task_id=? AND status IN ('QUEUED','PROCESSING')"
)


def _task_ttl() -> int:
    return int(os.environ.get("TASK_TTL", "300"))


def _is_stale(created_at: str, ttl: int) -> bool:
    try:
        parsed = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.datetime.now(datetime.timezone.utc) - parsed
    return age.total_seconds() > ttl


def _build_response(row) -> StatusResponse:
    status = row["status"]
    if status == "SUCCESS":
        return StatusResponse(
            status="SUCCESS",
            result=GenerateResult(
                prompt=row["prompt"],
                generated_text=row["generated_text"],
                model=row["model"],
                total_time_ms=row["total_time_ms"],
                tokens_generated=row["tokens_generated"],
                tokens_per_second=row["tokens_per_second"],
            ),
        )
    if status == "FAILURE":
        return StatusResponse(
            status="FAILURE",
            error=row["error"],
            retryable=bool(row["retryable"]),
        )
    if status == "TIMEOUT":
        return StatusResponse(status="TIMEOUT", error=row["error"])
    return StatusResponse(status=status)


@router.get(
    "/status/{task_id}",
    response_model=StatusResponse,
    response_model_exclude_none=True,
)
async def get_status(task_id: str, request: Request) -> StatusResponse:
    if not request.app.state.db_ready:
        raise InfrastructureUnavailable("task database is unavailable")

    ttl = _task_ttl()
    timeout_error = f"Task exceeded time limit of {ttl}s"

    conn = await get_connection_async()
    try:
        row = await (
            await execute_with_retry_async(await conn.cursor(), _SELECT_SQL, (task_id,))
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")

        if row["status"] in ("QUEUED", "PROCESSING") and _is_stale(row["created_at"], ttl):
            completed_at = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            cursor = await execute_with_retry_async(
                await conn.cursor(),
                _TIMEOUT_UPDATE_SQL,
                (completed_at, timeout_error, task_id),
            )
            await commit_with_retry_async(conn)
            if cursor.rowcount == 1:
                return StatusResponse(status="TIMEOUT", error=timeout_error)
            row = await (
                await execute_with_retry_async(await conn.cursor(), _SELECT_SQL, (task_id,))
            ).fetchone()

        return _build_response(row)
    finally:
        await conn.close()

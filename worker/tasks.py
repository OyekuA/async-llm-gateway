import datetime
import logging

from celery import shared_task

from common.database import (
    commit_with_retry,
    execute_with_retry,
    get_connection,
)
from .llm import LLMBackendError, get_backend

logger = logging.getLogger("worker.tasks")

_SELECT_TASK_SQL = (
    "SELECT prompt, temperature, max_tokens, model FROM tasks WHERE task_id = ?"
)
_CLAIM_SQL = (
    "UPDATE tasks SET status='PROCESSING', started_at=? "
    "WHERE task_id=? AND status='QUEUED'"
)
_SUCCESS_SQL = (
    "UPDATE tasks SET status='SUCCESS', generated_text=?, total_time_ms=?, "
    "tokens_generated=?, tokens_per_second=?, completed_at=? "
    "WHERE task_id=? AND status='PROCESSING'"
)
_FAILURE_SQL = (
    "UPDATE tasks SET status='FAILURE', error=?, retryable=?, completed_at=? "
    "WHERE task_id=? AND status='PROCESSING'"
)

_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_TOKENS = 256


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _mark_failure(conn, task_id: str, error: str, retryable: int) -> None:
    cursor = execute_with_retry(
        conn.cursor(), _FAILURE_SQL, (error, retryable, _utc_now(), task_id)
    )
    commit_with_retry(conn)
    if cursor.rowcount == 0:
        logger.warning(
            "task %s no longer PROCESSING (already TIMEOUT?); dropping failure",
            task_id,
        )


@shared_task(bind=True, max_retries=0, acks_late=True)
def run_inference(self, task_id: str):
    conn = get_connection()
    try:
        cursor = execute_with_retry(conn.cursor(), _SELECT_TASK_SQL, (task_id,))
        row = cursor.fetchone()
        if row is None:
            return

        temperature = (
            row["temperature"]
            if row["temperature"] is not None
            else _DEFAULT_TEMPERATURE
        )
        max_tokens = (
            row["max_tokens"] if row["max_tokens"] is not None else _DEFAULT_MAX_TOKENS
        )

        claimed = execute_with_retry(conn.cursor(), _CLAIM_SQL, (_utc_now(), task_id))
        if claimed.rowcount == 0:
            logger.warning(
                "task %s not claimable (duplicate delivery or already TIMEOUT); aborting",
                task_id,
            )
            return
        commit_with_retry(conn)

        try:
            result = get_backend().generate(
                row["prompt"], temperature, max_tokens, row["model"]
            )
        except LLMBackendError as exc:
            _mark_failure(conn, task_id, str(exc), int(exc.retryable))
            return
        except Exception as exc:
            logger.exception("unexpected error in run_inference for task %s", task_id)
            _mark_failure(conn, task_id, str(exc), 0)
            return

        cursor = execute_with_retry(
            conn.cursor(),
            _SUCCESS_SQL,
            (
                result.generated_text,
                result.total_time_ms,
                result.tokens_generated,
                result.tokens_per_second,
                _utc_now(),
                task_id,
            ),
        )
        if cursor.rowcount == 0:
            logger.warning(
                "task %s no longer PROCESSING (already TIMEOUT?); dropping result",
                task_id,
            )
        commit_with_retry(conn)
    finally:
        conn.close()

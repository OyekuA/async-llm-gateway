import datetime
import logging
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.database import DB_PATH


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _insert_task(task_id: str, status: str = "QUEUED", created_at=None) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, prompt, model, created_at) "
            "VALUES (?, ?, ?, 'tinyllama', ?)",
            (task_id, status, "probe", created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _row_status(task_id: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()


def test_generate_returns_202_and_persists_queued_when_redis_down(client, caplog):
    with caplog.at_level(logging.WARNING, logger="api.routes.generate"):
        resp = client.post("/generate", json={"prompt": "hello world"})
    assert resp.status_code == 202
    body = resp.json()
    task_id = body["task_id"]
    uuid.UUID(task_id)
    assert body["status_url"] == f"/status/{task_id}"
    assert any("left QUEUED" in record.message for record in caplog.records)
    row = _row_status(task_id)
    assert row is not None and row[0] == "QUEUED"


def test_fresh_task_reports_queued(client):
    resp = client.post("/generate", json={"prompt": "still queued"})
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]
    status = client.get(f"/status/{task_id}")
    assert status.status_code == 200
    assert status.json() == {"status": "QUEUED"}


def test_status_unknown_task_returns_404(client):
    resp = client.get(f"/status/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Task not found"}


def test_generate_blank_prompt_returns_422(client):
    resp = client.post("/generate", json={"prompt": "   "})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("prompt" in str(item.get("loc")) for item in detail)


def test_stale_queued_task_times_out_exactly_once(client, monkeypatch):
    monkeypatch.setenv("TASK_TTL", "1")
    task_id = str(uuid.uuid4())
    created_at = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_task(task_id, created_at=created_at)

    first = client.get(f"/status/{task_id}")
    assert first.status_code == 200
    assert first.json() == {"status": "TIMEOUT", "error": "Task exceeded time limit of 1s"}

    second = client.get(f"/status/{task_id}")
    assert second.status_code == 200
    assert second.json() == {"status": "TIMEOUT", "error": "Task exceeded time limit of 1s"}

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT status, completed_at FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "TIMEOUT"
    assert row[1] is not None

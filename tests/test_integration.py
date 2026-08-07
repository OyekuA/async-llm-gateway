import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_COMPOSE_TEMPLATE = """
services:
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --appendfsync everysec
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  api:
    build:
      context: {repo}
      dockerfile: Dockerfile
    ports:
      - "127.0.0.1:{port}:8000"
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DB_PATH=/data/task_state.db
      - OLLAMA_URL=http://ollama:11434
      - MOCK_MODE=true
      - TASK_TTL={ttl}
      - LOG_LEVEL=info
    volumes:
      - data:/data
    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 20s

  worker:
    build:
      context: {repo}
      dockerfile: Dockerfile
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DB_PATH=/data/task_state.db
      - OLLAMA_URL=http://ollama:11434
      - MOCK_MODE=true
      - MOCK_FAILURE_RATE={rate}
      - LOG_LEVEL=info
    volumes:
      - data:/data
    depends_on:
      redis:
        condition: service_healthy
      api:
        condition: service_healthy
    command: celery -A worker.celery_app worker --concurrency=4 --pool=prefork --loglevel=info

volumes:
  data:
"""


def _compose_cmd():
    if shutil.which("docker") is None:
        return None
    for cmd in (["docker", "compose"], ["docker-compose"]):
        version_args = ["version"] if cmd == ["docker", "compose"] else ["--version"]
        try:
            res = subprocess.run(
                [*cmd, *version_args], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode == 0:
            return cmd
    return None


pytestmark = pytest.mark.skipif(
    _compose_cmd() is None,
    reason="docker (or docker compose) is not available on this host; "
    "integration tests cannot run",
)


class ComposeStack:
    def __init__(self, scenario: str, task_ttl: str, failure_rate: str, port: int):
        self.scenario = scenario
        self.port = port
        self._tmp = tempfile.mkdtemp(prefix=f"compose_{scenario}_")
        self._file = Path(self._tmp) / "compose.yml"
        self._file.write_text(
            _COMPOSE_TEMPLATE.format(
                repo=REPO_ROOT.as_posix(), port=port, ttl=task_ttl, rate=failure_rate
            ),
            encoding="utf-8",
        )
        self._cmd = _compose_cmd() + ["-p", f"async_gw_{scenario}", "-f", str(self._file)]

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self._cmd, *args], capture_output=True, text=True, timeout=600
        )

    def up(self) -> None:
        res = self.run("up", "-d", "--build")
        if res.returncode != 0:
            raise RuntimeError(f"compose up failed:\n{res.stdout}\n{res.stderr}")

    def wait_healthy(self, timeout: float = 180.0) -> None:
        url = f"http://127.0.0.1:{self.port}/health"
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(2)
        raise RuntimeError(
            f"api not healthy within {timeout:.0f}s at {url}: {last_error}"
        )

    def down(self) -> None:
        self.run("down", "-v", "--remove-orphans")

    def stop_service(self, service: str) -> None:
        res = self.run("stop", service)
        if res.returncode != 0:
            raise RuntimeError(f"compose stop {service} failed:\n{res.stderr}")

    def python(self, script: str) -> str:
        res = self.run("exec", "-T", "worker", "python", "-c", script)
        if res.returncode != 0:
            raise RuntimeError(f"exec failed on worker:\n{res.stdout}\n{res.stderr}")
        return res.stdout

    def db_status(self, task_id: str) -> str:
        script = (
            "import sqlite3;"
            "r=sqlite3.connect('/data/task_state.db')"
            f".execute('select status from tasks where task_id=?',('{task_id}',)).fetchone();"
            "print(r[0] if r else 'MISSING')"
        )
        return self.python(script).strip().splitlines()[-1]


@pytest.fixture()
def stack():
    stacks = []

    def _make(scenario: str, task_ttl: str, failure_rate: str, port: int) -> ComposeStack:
        s = ComposeStack(scenario, task_ttl, failure_rate, port)
        stacks.append(s)
        s.up()
        s.wait_healthy()
        return s

    yield _make
    for s in reversed(stacks):
        s.down()


def _post_generate(port: int, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_status(port: int, task_id: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/status/{task_id}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _poll_status(
    port: int, task_id: str, expected: str, timeout: float = 15.0, interval: float = 0.5
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        code, body = _get_status(port, task_id)
        if code == 200 and body.get("status") == expected:
            return body
        last = (code, body)
        time.sleep(interval)
    raise AssertionError(
        f"task {task_id} did not reach {expected!r} within {timeout:.0f}s; "
        f"last response: {last}"
    )


def test_happy_path_submit_poll_success(stack):
    s = stack("default", task_ttl="30", failure_rate="0.0", port=18000)
    code, body = _post_generate(s.port, {"prompt": "hello integration"})
    assert code == 202
    task_id = body["task_id"]
    uuid.UUID(task_id)
    assert body["status_url"] == f"/status/{task_id}"

    result = _poll_status(s.port, task_id, "SUCCESS", timeout=15)
    res = result["result"]
    assert res["prompt"] == "hello integration"
    assert res["generated_text"] == "This is a canned mock response."
    assert res["model"] == "tinyllama"
    assert res["total_time_ms"] >= 0
    assert res["tokens_generated"] >= 1
    assert res["tokens_per_second"] >= 0
    assert "error" not in result
    assert "retryable" not in result


def test_worker_failure_reports_retryable_false(stack):
    s = stack("failure", task_ttl="30", failure_rate="1.0", port=18001)
    code, body = _post_generate(s.port, {"prompt": "boom"})
    assert code == 202
    task_id = body["task_id"]

    result = _poll_status(s.port, task_id, "FAILURE", timeout=15)
    assert result["error"] == "Simulated OOM"
    assert result["retryable"] is False
    assert "result" not in result


def test_generate_survives_redis_down(stack):
    s = stack("redis_down", task_ttl="30", failure_rate="0.0", port=18002)
    s.stop_service("redis")

    code, body = _post_generate(s.port, {"prompt": "redis is down"})
    assert code == 202
    task_id = body["task_id"]
    assert body["status_url"] == f"/status/{task_id}"

    assert s.db_status(task_id) == "QUEUED"


def test_status_unknown_task_returns_404(stack):
    s = stack("default", task_ttl="30", failure_rate="0.0", port=18000)
    code, body = _get_status(s.port, str(uuid.uuid4()))
    assert code == 404
    assert body == {"detail": "Task not found"}


def test_generate_blank_prompt_returns_422(stack):
    s = stack("default", task_ttl="30", failure_rate="0.0", port=18000)
    code, body = _post_generate(s.port, {"prompt": "   "})
    assert code == 422
    assert "detail" in body


def test_idle_queued_task_expires(stack):
    s = stack("expiry", task_ttl="5", failure_rate="0.0", port=18003)
    task_id = str(uuid.uuid4())
    script = (
        "import sqlite3;"
        "c=sqlite3.connect('/data/task_state.db');"
        f"c.execute('insert into tasks(task_id,status,prompt) values(?,?,?)',('{task_id}','QUEUED','idle'));"
        "c.commit()"
    )
    s.python(script)

    time.sleep(6)
    code, body = _get_status(s.port, task_id)
    assert code == 200
    assert body["status"] == "TIMEOUT"
    assert body["error"] == "Task exceeded time limit of 5s"
    assert "result" not in body


def test_timeout_not_resurrected_by_late_worker_success(stack):
    s = stack("race", task_ttl="2", failure_rate="0.0", port=18004)
    code, body = _post_generate(s.port, {"prompt": "race"})
    assert code == 202
    task_id = body["task_id"]

    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        code, last = _get_status(s.port, task_id)
        assert code == 200
        if last["status"] == "TIMEOUT":
            break
        time.sleep(0.2)
    assert last is not None and last["status"] == "TIMEOUT", f"expected TIMEOUT, last={last}"

    time.sleep(6)
    for _ in range(3):
        code, again = _get_status(s.port, task_id)
        assert code == 200
        assert again["status"] == "TIMEOUT", f"state resurrected from TIMEOUT: {again}"
        time.sleep(1)

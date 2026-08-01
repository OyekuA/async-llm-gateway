"""Standalone test: creates the DB, inserts a row, reads it back, verifies WAL
mode is active, then cleans up.

Run: python test_db_init.py
"""

import os
import asyncio
import shutil
import sqlite3
import sys
import tempfile
import threading
import time as _time
import uuid

TEMP_DIR = tempfile.mkdtemp(prefix="task_db_init_")
os.environ["DB_PATH"] = os.path.join(TEMP_DIR, "task_state.db")

from common import database as db  # noqa: E402

_retry_sleeps = []
_sleep_lock = threading.Lock()


class _CountingTime:

    def __init__(self, real_time):
        self._real = real_time

    def sleep(self, seconds):
        with _sleep_lock:
            _retry_sleeps.append(seconds)
        self._real.sleep(seconds)

    def __getattr__(self, name):
        return getattr(self._real, name)


db.time = _CountingTime(_time)


def _slept_since(start):
    with _sleep_lock:
        return len(_retry_sleeps) - start


def _new_raw_connection():
    conn = sqlite3.connect(db.DB_PATH, timeout=0)
    conn.execute("PRAGMA busy_timeout=0;").fetchone()
    return conn


def _busy_error():
    err = sqlite3.OperationalError("database is locked")
    err.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return err


def check_basic_init_and_readback():
    print("== basic init / insert / readback ==")
    conn = db.get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode == "wal", f"expected WAL mode, got {mode!r}"
        print("WAL mode active: OK")

        busy = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert busy == 5000, f"expected busy_timeout=5000, got {busy}"
        print("busy_timeout=5000: OK")

        task_id = str(uuid.uuid4())
        cur = conn.cursor()
        db.execute_with_retry(
            cur,
            "INSERT INTO tasks (task_id, prompt, temperature, max_tokens) "
            "VALUES (?, ?, ?, ?)",
            (task_id, "hello world", 0.7, 128),
        )
        conn.commit()
        print("insert: OK")

        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert row is not None, "row not found after insert"
        assert row["status"] == "QUEUED", f"expected default QUEUED, got {row['status']!r}"
        assert row["model"] == "tinyllama", f"expected default tinyllama, got {row['model']!r}"
        assert row["retryable"] == 0, f"expected retryable default 0, got {row['retryable']!r}"
        assert row["prompt"] == "hello world"
        assert row["temperature"] == 0.7 and row["max_tokens"] == 128
        assert row["created_at"], "expected created_at default timestamp"
        print("readback + column defaults: OK")

        conn2 = db.get_connection()
        try:
            count = conn2.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert count == 1, f"expected 1 row from second connection, got {count}"
            print("second connection sees row: OK")
        finally:
            conn2.close()

        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tasks_status'"
        ).fetchone()
        assert table is not None, "tasks table missing"
        assert index is not None, "idx_tasks_status index missing"
        print("schema objects (table + index): OK")
    finally:
        conn.close()


def check_retry_then_success():
    print("== deterministic retry: lock released mid-way ==")
    blocker = _new_raw_connection()
    result = {}
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        start = len(_retry_sleeps)

        def work():
            writer = _new_raw_connection()
            try:
                cur = writer.cursor()
                db.execute_with_retry(
                    cur,
                    "INSERT INTO tasks (task_id, prompt) VALUES (?, ?)",
                    ("retry-ok", "prompt"),
                )
                writer.commit()
                result["rowcount"] = cur.rowcount
            finally:
                writer.close()

        t = threading.Thread(target=work)
        t.start()
        deadline = _time.perf_counter() + 10
        while _slept_since(start) < 2:
            assert _time.perf_counter() < deadline, "timed out waiting for retries"
            _time.sleep(0.01)
        blocker.rollback()
        t.join(timeout=10)
        assert not t.is_alive(), "worker thread did not finish in time"
        assert result["rowcount"] == 1, "insert did not land after retry"
        sleeps = _slept_since(start)
        assert sleeps == 2, f"expected 2 backoff sleeps (3 attempts), got {sleeps}"
        print(f"retry succeeded after 3 attempts, 2 backoff sleeps: OK")
    finally:
        blocker.close()


def check_reraises_after_exhaustion():
    print("== deterministic retry: exhaustion re-raises ==")
    blocker = _new_raw_connection()
    writer = _new_raw_connection()
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        start = len(_retry_sleeps)
        t0 = _time.perf_counter()
        try:
            db.execute_with_retry(
                writer.cursor(),
                "INSERT INTO tasks (task_id, prompt) VALUES (?, ?)",
                ("retry-fail", "prompt"),
            )
            raise AssertionError("expected OperationalError to propagate")
        except sqlite3.OperationalError as exc:
            assert "database is locked" in str(exc).lower(), f"unexpected error: {exc}"
        elapsed = _time.perf_counter() - t0
        sleeps = _slept_since(start)
        assert sleeps == 4, f"expected 4 backoff sleeps (5 attempts), got {sleeps}"
        assert elapsed >= 1.4, f"expected >= 100+200+400+800ms of backoff, took {elapsed:.2f}s"
        print(f"5 attempts, 4 backoff sleeps (100/200/400/800ms), re-raised: OK")
    finally:
        blocker.rollback()
        blocker.close()
        writer.close()


def check_non_lock_errors_propagate_immediately():
    print("== deterministic: non-lock OperationalError propagates immediately ==")
    conn = _new_raw_connection()
    try:
        start = len(_retry_sleeps)
        try:
            db.execute_with_retry(conn.cursor(), "SELECT * FROM no_such_table")
            raise AssertionError("expected OperationalError to propagate")
        except sqlite3.OperationalError as exc:
            assert "no such table" in str(exc).lower(), f"unexpected error: {exc}"
        sleeps = _slept_since(start)
        assert sleeps == 0, f"expected 0 backoff sleeps, got {sleeps}"
        print("non-lock error propagated without retry: OK")
    finally:
        conn.close()


def check_commit_with_retry():
    print("== commit_with_retry: busy once, then success ==")
    calls = {"n": 0}

    class FakeConn:
        def commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _busy_error()

    start = len(_retry_sleeps)
    db.commit_with_retry(FakeConn())
    assert calls["n"] == 2, f"expected 2 commit attempts, got {calls['n']}"
    assert _slept_since(start) == 1, "expected 1 backoff sleep"
    print("commit retried once and succeeded: OK")

    print("== commit_with_retry: always busy re-raises ==")
    calls2 = {"n": 0}

    class AlwaysBusyConn:
        def commit(self):
            calls2["n"] += 1
            raise _busy_error()

    start = len(_retry_sleeps)
    try:
        db.commit_with_retry(AlwaysBusyConn())
        raise AssertionError("expected OperationalError to propagate")
    except sqlite3.OperationalError as exc:
        assert "database is locked" in str(exc).lower()
    assert calls2["n"] == 5, f"expected 5 commit attempts, got {calls2['n']}"
    assert _slept_since(start) == 4, "expected 4 backoff sleeps"
    print("commit re-raised after 5 attempts: OK")


async def check_async_smoke():
    print("== async (aiosqlite) smoke: fresh DB init / insert / readback ==")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db.DB_PATH + suffix)
        except FileNotFoundError:
            pass

    conn = await db.get_connection_async()
    try:
        mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
        assert mode == "wal", f"expected WAL mode on async connection, got {mode!r}"
        busy = (await (await conn.execute("PRAGMA busy_timeout;")).fetchone())[0]
        assert busy == 5000, f"expected busy_timeout=5000, got {busy}"
        print("async connection PRAGMAs: OK")

        task_id = str(uuid.uuid4())
        cur = await conn.cursor()
        await db.execute_with_retry_async(
            cur,
            "INSERT INTO tasks (task_id, prompt, temperature, max_tokens) "
            "VALUES (?, ?, ?, ?)",
            (task_id, "hello async", 0.5, 64),
        )
        await db.commit_with_retry_async(conn)
        print("async insert + commit: OK")

        row = await (
            await conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        ).fetchone()
        assert row is not None, "async readback found no row"
        assert row["status"] == "QUEUED", f"unexpected status {row['status']!r}"
        assert row["model"] == "tinyllama"
        assert row["retryable"] == 0
        assert row["prompt"] == "hello async"
        assert row["created_at"], "expected created_at default timestamp"
        print("async readback + defaults: OK")

        table = await (
            await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            )
        ).fetchone()
        assert table is not None, "tasks table missing after async init"
        print("async schema init on fresh DB: OK")
    finally:
        await conn.close()


def main():
    print(f"DB_PATH: {db.DB_PATH}")
    check_basic_init_and_readback()
    print()
    check_retry_then_success()
    print()
    check_reraises_after_exhaustion()
    print()
    check_non_lock_errors_propagate_immediately()
    print()
    check_commit_with_retry()
    print()
    asyncio.run(check_async_smoke())
    print()
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

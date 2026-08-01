"""Standalone test: creates the DB, inserts a row, reads it back, verifies WAL
mode is active, then cleans up.

Run: python test_db_init.py
"""

import os
import shutil
import sys
import tempfile
import uuid

TEMP_DIR = tempfile.mkdtemp(prefix="task_db_init_")
os.environ["DB_PATH"] = os.path.join(TEMP_DIR, "task_state.db")

from common import database as db  # noqa: E402


def main():
    print(f"DB_PATH: {db.DB_PATH}")
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

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

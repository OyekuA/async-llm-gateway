"""Single shared source of truth for the SQLite task state database.

Both the api and worker processes import from this module — no duplicate
connection/schema logic elsewhere.
"""

import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "/data/task_state.db")

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MS = [100, 200, 400, 800, 1600]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    status             TEXT NOT NULL DEFAULT 'QUEUED'
                       CHECK(status IN ('QUEUED','PROCESSING','SUCCESS','FAILURE','TIMEOUT')),
    prompt             TEXT NOT NULL,
    temperature        REAL,
    max_tokens         INTEGER,
    model              TEXT NOT NULL DEFAULT 'tinyllama',
    generated_text     TEXT,
    total_time_ms      INTEGER,
    tokens_generated   INTEGER,
    tokens_per_second  REAL,
    error              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at         TEXT,
    completed_at       TEXT,
    retryable          INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


def init_db(conn=None):
    """Create the tasks table and status index if they do not exist (idempotent).

    Accepts an optional open connection; otherwise opens (and closes) its own.
    """
    close_after = conn is None
    if close_after:
        conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(_SCHEMA_SQL)
    finally:
        if close_after:
            conn.close()


def get_connection() -> sqlite3.Connection:
    """Open a connection with WAL mode, busy timeout, and Row row_factory.

    The schema is initialized (idempotently) on every connection, so the first
    connection creates the tasks table and index.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    init_db(conn)
    return conn


def execute_with_retry(cursor, sql, params=()):
    """Execute a statement, retrying on "database is locked" with backoff.

    Retries up to MAX_ATTEMPTS times total, sleeping RETRY_BACKOFF_MS
    (exponential: 100ms, 200ms, 400ms, ...) between attempts. Re-raises the
    last error if all attempts fail; non-lock errors propagate immediately.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return cursor.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF_MS[attempt] / 1000)

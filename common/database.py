"""Single shared source of truth for the SQLite task state database.

Both the api and worker processes import from this module — no duplicate
connection/schema logic elsewhere.

Provides two parallel paths over the same schema:

- Sync (`sqlite3`) — used by Celery workers (prefork pool).
- Async (`aiosqlite`) — used by the FastAPI event loop so SQLite I/O never
  blocks it.
"""

import asyncio
import os
import sqlite3
import time

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/data/task_state.db")

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MS = [100, 200, 400, 800, 1600]

_PRAGMA_WAL = "PRAGMA journal_mode=WAL;"
_PRAGMA_BUSY_TIMEOUT = "PRAGMA busy_timeout=5000;"

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


def _backoff_ms(attempt: int) -> int:
    return RETRY_BACKOFF_MS[min(attempt, len(RETRY_BACKOFF_MS) - 1)]


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == sqlite3.SQLITE_BUSY
    return "database is locked" in str(exc).lower()


def _ensure_wal(conn) -> None:
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    if mode != "wal":
        execute_with_retry(conn.cursor(), _PRAGMA_WAL).fetchone()


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_PRAGMA_BUSY_TIMEOUT).fetchone()
    _ensure_wal(conn)
    init_db(conn)
    return conn


def execute_with_retry(cursor, sql, params=()):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return cursor.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            time.sleep(_backoff_ms(attempt) / 1000)


def commit_with_retry(conn):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return conn.commit()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            time.sleep(_backoff_ms(attempt) / 1000)


async def init_db_async(conn=None):
    close_after = conn is None
    if close_after:
        conn = await aiosqlite.connect(DB_PATH)
    try:
        await conn.executescript(_SCHEMA_SQL)
    finally:
        if close_after:
            await conn.close()


async def _ensure_wal_async(conn) -> None:
    mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
    if mode != "wal":
        await (await execute_with_retry_async(await conn.cursor(), _PRAGMA_WAL)).fetchone()


async def get_connection_async():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await (await conn.execute(_PRAGMA_BUSY_TIMEOUT)).fetchone()
    await _ensure_wal_async(conn)
    await init_db_async(conn)
    return conn


async def execute_with_retry_async(cursor, sql, params=()):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await cursor.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_backoff_ms(attempt) / 1000)


async def commit_with_retry_async(conn):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await conn.commit()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_backoff_ms(attempt) / 1000)

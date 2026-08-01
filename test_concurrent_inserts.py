"""Standalone stress test: fires N concurrent INSERTs from separate threads,
each using its own get_connection(), and verifies no
sqlite3.OperationalError("database is locked") propagates to the caller.

Logs a retry-count histogram (retries == how many backoff sleeps the
execute_with_retry wrapper performed before the insert succeeded).

Run: python test_concurrent_inserts.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time as _time

TEMP_DIR = tempfile.mkdtemp(prefix="task_db_concurrent_")
os.environ["DB_PATH"] = os.path.join(TEMP_DIR, "task_state.db")

from common import database as db  # noqa: E402


class _CountingTime:
    """Shim for db.time that counts per-thread sleeps (== retries)."""

    def __init__(self, real_time):
        self._real = real_time
        self._local = threading.local()

    def sleep(self, seconds):
        self._local.retries = getattr(self._local, "retries", 0) + 1
        self._real.sleep(seconds)

    def __getattr__(self, name):
        return getattr(self._real, name)


db.time = _CountingTime(_time)


def run_batch(n, expected_total):
    errors = []
    histogram = {}
    guard = threading.Lock()

    def worker(i):
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            db.execute_with_retry(
                cur,
                "INSERT INTO tasks (task_id, prompt) VALUES (?, ?)",
                (f"task-{n}-{i}", f"prompt-{n}-{i}"),
            )
            conn.commit()
            retries = getattr(db.time._local, "retries", 0)
            with guard:
                histogram[retries] = histogram.get(retries, 0) + 1
        except Exception as exc:
            with guard:
                errors.append((i, type(exc).__name__, str(exc)))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    locked = [
        e for e in errors
        if e[1] == "OperationalError" and "database is locked" in e[2].lower()
    ]

    check = db.get_connection()
    try:
        total = check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        check.close()

    first_attempt = histogram.get(0, 0)
    retried = sum(v for k, v in histogram.items() if k > 0)

    print(f"N={n}: inserted={total}/{n} first-attempt={first_attempt} retried={retried} "
          f"errors={len(errors)} locked-errors={len(locked)}")
    print(f"  retry histogram: {dict(sorted(histogram.items()))}")

    assert total == expected_total, f"expected {expected_total} rows (cumulative), found {total}"
    assert len(errors) == 0, f"{len(errors)} thread(s) raised: {errors[:3]}"
    assert len(locked) == 0, f"{len(locked)} 'database is locked' errors propagated"
    assert first_attempt + retried == n, "histogram does not account for all inserts"
    return histogram


def main():
    print(f"DB_PATH: {db.DB_PATH}")
    all_histograms = {}
    expected_total = 0
    for n in (50, 200, 500):
        expected_total += n
        all_histograms[n] = run_batch(n, expected_total)
        print()

    first_attempt_total = sum(h.get(0, 0) for h in all_histograms.values())
    retried_total = sum(
        sum(v for k, v in h.items() if k > 0) for h in all_histograms.values()
    )
    print(f"TOTAL: {first_attempt_total} first-attempt, {retried_total} after retry, "
          f"{first_attempt_total + retried_total} inserts, 0 locked errors propagated")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

"""Ramped-concurrency benchmark client for the async LLM gateway.

Usage:
    python benchmark/run.py --mode sync --concurrency 10 50 100
    python benchmark/run.py --mode async --target http://127.0.0.1:8000
"""

import argparse
import asyncio
import json
import logging
import math
import time
from pathlib import Path

import httpx

logger = logging.getLogger("benchmark")

REQUEST_BODY = {
    "prompt": "Explain quantum computing in one paragraph",
    "temperature": 0.7,
    "max_tokens": 256,
    "model": "tinyllama",
}

TERMINAL_FAILURES = ("FAILURE", "TIMEOUT")


def percentile(values, pct):
    """Linear-interpolation percentile over values (numpy.percentile semantics)."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = (pct / 100.0) * (n - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    frac = rank - lower
    return ordered[lower] * (1.0 - frac) + ordered[upper] * frac


def compute_metrics(concurrency, success_count, failure_count, latencies_ms,
                    post_response_times_ms, wall_seconds):
    p50 = percentile(latencies_ms, 50)
    p95 = percentile(latencies_ms, 95)
    p99 = percentile(latencies_ms, 99)
    p99_post = percentile(post_response_times_ms, 99) if post_response_times_ms else None
    return {
        "concurrency": concurrency,
        "success_count": success_count,
        "failure_count": failure_count,
        "drop_rate": round(failure_count / concurrency, 4) if concurrency else 0.0,
        "p50_latency_ms": round(p50, 2) if p50 is not None else None,
        "p95_latency_ms": round(p95, 2) if p95 is not None else None,
        "p99_latency_ms": round(p99, 2) if p99 is not None else None,
        "throughput_req_per_sec": round(success_count / wall_seconds, 2) if wall_seconds else 0.0,
        "p99_post_response_time_ms": round(p99_post, 2) if p99_post is not None else None,
    }


async def _submit_task(client):
    submitted = time.perf_counter()
    try:
        resp = await client.post("/generate", json=REQUEST_BODY)
    except httpx.HTTPError as exc:
        logger.warning("POST /generate failed: %s", exc)
        return None
    received = time.perf_counter()
    if resp.status_code != 202:
        logger.warning("POST /generate returned HTTP %s", resp.status_code)
        return None
    try:
        task_id = resp.json()["task_id"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Malformed POST /generate response: %s", exc)
        return None
    return task_id, submitted, received


async def _poll_task(client, task_id, submitted, poll_interval, timeout):
    while True:
        if time.perf_counter() - submitted >= timeout:
            return "FAILURE", None
        try:
            resp = await client.get(f"/status/{task_id}")
        except httpx.HTTPError as exc:
            logger.warning("GET /status/%s failed: %s", task_id, exc)
            await asyncio.sleep(poll_interval)
            continue
        if resp.status_code == 200:
            try:
                status = resp.json().get("status")
            except ValueError:
                status = None
            if status == "SUCCESS":
                return "SUCCESS", (time.perf_counter() - submitted) * 1000.0
            if status in TERMINAL_FAILURES:
                return "FAILURE", None
        await asyncio.sleep(poll_interval)


async def run_async_level(client, concurrency, poll_interval, timeout):
    start = time.perf_counter()
    submissions = await asyncio.gather(*(_submit_task(client) for _ in range(concurrency)))

    tasks = []
    post_times_ms = []
    for result in submissions:
        if result is None:
            continue
        task_id, submitted, received = result
        tasks.append((task_id, submitted))
        post_times_ms.append((received - submitted) * 1000.0)

    failures = concurrency - len(tasks)
    latencies_ms = []
    outcomes = await asyncio.gather(
        *(_poll_task(client, task_id, submitted, poll_interval, timeout)
          for task_id, submitted in tasks)
    )
    for outcome in outcomes:
        if outcome[0] == "SUCCESS":
            latencies_ms.append(outcome[1])
        else:
            failures += 1

    wall_seconds = time.perf_counter() - start
    return concurrency - failures, failures, latencies_ms, post_times_ms, wall_seconds


async def run_sync_level(client, concurrency):
    start = time.perf_counter()

    async def one():
        submitted = time.perf_counter()
        try:
            resp = await client.post("/generate-sync", json=REQUEST_BODY)
        except httpx.HTTPError as exc:
            logger.warning("POST /generate-sync failed: %s", exc)
            return False, None
        elapsed_ms = (time.perf_counter() - submitted) * 1000.0
        if resp.status_code != 200:
            logger.warning("POST /generate-sync returned HTTP %s", resp.status_code)
            return False, None
        return True, elapsed_ms

    outcomes = await asyncio.gather(*(one() for _ in range(concurrency)))
    latencies_ms = [elapsed for ok, elapsed in outcomes if ok]
    failures = sum(1 for ok, _ in outcomes if not ok)
    wall_seconds = time.perf_counter() - start
    return len(latencies_ms), failures, latencies_ms, None, wall_seconds


def write_results(output, mode, target, levels):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "target": target, "levels": levels}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


async def _run_levels(args, client):
    levels = []
    poll_interval = args.poll_interval / 1000.0
    for n in args.concurrency:
        start = time.perf_counter()
        if args.mode == "async":
            raw = await run_async_level(client, n, poll_interval, args.timeout)
        else:
            raw = await run_sync_level(client, n)
        level = compute_metrics(n, *raw)
        levels.append(level)
        write_results(args.output, args.mode, args.target, levels)
        logger.info("concurrency %s: %s (level took %.1fs)", n, level, time.perf_counter() - start)
    return levels


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="http://localhost:8000")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[10, 50, 100, 200, 500])
    parser.add_argument("--mode", required=True, choices=["async", "sync"])
    parser.add_argument("--output", default="benchmark/results.json")
    parser.add_argument("--poll-interval", type=int, default=200, help="ms between status polls")
    parser.add_argument("--timeout", type=int, default=120, help="seconds per task before timeout")
    return parser.parse_args(argv)


async def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    limits = httpx.Limits(
        max_connections=max(args.concurrency),
        max_keepalive_connections=max(args.concurrency),
    )
    async with httpx.AsyncClient(
        base_url=args.target, timeout=httpx.Timeout(args.timeout), limits=limits
    ) as client:
        await _run_levels(args, client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted; completed levels preserved in results file")
        raise SystemExit(130)

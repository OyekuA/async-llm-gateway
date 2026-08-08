import logging
import os
import time

import redis
from celery import Celery

from common.database import DB_PATH, init_db
from common.logging import setup_logging

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
MOCK_MODE = os.environ.get("MOCK_MODE", "true").lower() == "true"

REDIS_READY_ATTEMPTS = 10
REDIS_READY_INTERVAL_S = 1

setup_logging(LOG_LEVEL)

logger = logging.getLogger("worker")

app = Celery("worker")
app.conf.broker_url = REDIS_URL
app.conf.task_serializer = "json"
app.conf.result_backend = None
app.conf.task_always_eager = False
app.conf.include = ["worker.tasks"]


def wait_for_redis() -> None:
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
    for attempt in range(REDIS_READY_ATTEMPTS):
        try:
            client.ping()
            return
        except Exception:
            if attempt == REDIS_READY_ATTEMPTS - 1:
                raise
            time.sleep(REDIS_READY_INTERVAL_S)


wait_for_redis()
logger.info("Redis reachable at %s", REDIS_URL)

init_db()
logger.info("Database ready at %s", DB_PATH)
if MOCK_MODE:
    logger.info("Using backend: mock")


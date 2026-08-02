import datetime
import json
import logging
import os
from contextlib import asynccontextmanager

import aiosqlite
import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.database import DB_PATH, get_connection_async
from .errors import InfrastructureUnavailable
from .routes import generate, status, sync_generate

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")

logger = logging.getLogger("api")

redis_client = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        payload = {
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False


setup_logging()


async def _db_connected() -> bool:
    try:
        conn = await aiosqlite.connect(DB_PATH)
        try:
            await (await conn.execute("SELECT 1")).fetchone()
        finally:
            await conn.close()
        return True
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client

    try:
        redis_client = redis.from_url(REDIS_URL, socket_connect_timeout=1)
        await redis_client.ping()
        logger.info("Redis reachable at %s", REDIS_URL)
    except Exception:
        logger.warning("Redis unreachable at %s - continuing without it", REDIS_URL)

    try:
        conn = await get_connection_async()
        try:
            logger.info("SQLite database ready at %s", DB_PATH)
        finally:
            await conn.close()
        app.state.db_ready = True
    except Exception:
        app.state.db_ready = False
        logger.error(
            "SQLite init failed at %s - GET /status will return 503",
            DB_PATH,
            exc_info=True,
        )

    yield

    if redis_client is not None:
        await redis_client.aclose()


app = FastAPI(lifespan=lifespan)
app.state.db_ready = False


@app.exception_handler(InfrastructureUnavailable)
async def infrastructure_unavailable_handler(request: Request, exc: InfrastructureUnavailable):
    logger.error(
        "Infrastructure unavailable: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(status_code=503, content={"detail": "Infrastructure unavailable"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    redis_status = "disconnected"
    try:
        await redis_client.ping()
        redis_status = "connected"
    except Exception:
        pass

    db_status = "connected" if await _db_connected() else "disconnected"
    return {"status": "ok", "redis": redis_status, "db": db_status}


app.include_router(generate.router)
app.include_router(status.router)
app.include_router(sync_generate.router)

import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = redis.from_url(REDIS_URL, socket_connect_timeout=1)
    yield
    await redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    redis_status = "disconnected"
    try:
        await redis_client.ping()
        redis_status = "connected"
    except Exception:
        pass

    return {"status": "ok", "redis": redis_status}

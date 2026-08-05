import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .events.subscriber import start_subscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = start_subscriber()
    yield
    if _client:
        _client.loop_stop()
        _client.disconnect()


app = FastAPI(
    title="Analytics Service",
    description="MQTT subscriber. Logs all kv/user/# + kv/match/# events into MongoDB and daily CSV files.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "analytics-service"}

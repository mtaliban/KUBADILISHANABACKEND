import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.config import settings
from .routes import matches
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
    title="Match Service",
    description="Reverse-matches users by cadre + geography. Subscribes to user.* events, publishes match.found.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(matches.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "match-service"}

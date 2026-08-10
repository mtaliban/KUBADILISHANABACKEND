"""Shared fixtures for backend tests.

Tests use mongomock-motor (in-memory Mongo) so no MongoDB daemon is needed.
MQTT publishing is stubbed so tests never touch the network.
"""
import pytest
from mongomock_motor import AsyncMongoMockClient
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.locations.routes as loc_routes
from app.modules.auth.routes import router as auth_router
from app.modules.locations.routes import router as loc_router


@pytest.fixture
def mongo_client():
    """Fresh in-memory Mongo per test."""
    return AsyncMongoMockClient()


@pytest.fixture
async def db(mongo_client):
    """Empty database with the same unique indexes the real app relies on."""
    database = mongo_client["kv_test"]
    await database.users.create_index("phone_primary", unique=True)
    await database.cadres.create_index("code", unique=True)
    await database.regions.create_index("id", unique=True)
    await database.districts.create_index("id", unique=True)
    return database


@pytest.fixture
def app(db, monkeypatch):
    """Minimal FastAPI app with the routers under test, wired to the in-memory DB."""
    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(loc_router)

    # Route modules resolve `get_db` from their module namespace at call time.
    # monkeypatch auto-restores these after each test.
    monkeypatch.setattr(auth_routes, "get_db", lambda: db)
    monkeypatch.setattr(loc_routes, "get_db", lambda: db)

    # `locations.cached` normally talks to Redis; bypass it with an identity loader.
    async def fake_cached(key, loader, ttl=None):
        return await loader()

    monkeypatch.setattr(loc_routes, "cached", fake_cached)
    return application


@pytest.fixture
async def client(app):
    """Async HTTP client against the in-memory FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def published_events(monkeypatch):
    """Capture MQTT publishes instead of hitting a broker."""
    events: list[tuple[str, dict]] = []

    def fake_publish(topic: str, payload: dict, qos: int = 1):
        events.append((topic, payload))

    monkeypatch.setattr(auth_routes, "publish", fake_publish)
    return events


from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from .config import settings

_async_client: AsyncIOMotorClient | None = None
_sync_client: MongoClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _async_client
    if _async_client is None:
        _async_client = AsyncIOMotorClient(settings.mongo_uri)
    return _async_client


def get_db():
    return get_client().get_default_database()


def get_sync_db():
    """Sync client for background threads (MQTT subscriber)."""
    global _sync_client
    if _sync_client is None:
        _sync_client = MongoClient(settings.mongo_uri)
    return _sync_client.get_default_database()

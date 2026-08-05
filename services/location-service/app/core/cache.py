import json
from typing import Any, Callable, Awaitable
from redis.asyncio import Redis
from .config import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def cached(key: str, loader: Callable[[], Awaitable[Any]], ttl: int | None = None) -> Any:
    r = get_redis()
    cached_val = await r.get(key)
    if cached_val is not None:
        return json.loads(cached_val)
    value = await loader()
    await r.setex(key, ttl or settings.cache_ttl_seconds, json.dumps(value, default=str))
    return value

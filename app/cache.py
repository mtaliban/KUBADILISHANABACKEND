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
    """Redis cache yenye fallback salama: ikiwa Redis haipo/kushindwa,
    tunarudi kwenye loader (mikwepo haivunjiki) na tuseti cache —
    tukirudi mara inayofuata tutajaribu tena. Hii inamanisha caching
    HAIWEZI kuvunja mfumo wakati wowote."""
    r = None
    try:
        r = get_redis()
        val = await r.get(key)
        if val is not None:
            return json.loads(val)
    except Exception:
        r = None  # Redis iko chini — endelea moja kwa moja bila cache
    value = await loader()
    if r is not None:
        try:
            await r.setex(key, ttl or settings.cache_ttl_seconds, json.dumps(value, default=str))
        except Exception:
            pass  # cache imeshindwa kuandikwa — siyo mbaya, data bado inarudi
    return value

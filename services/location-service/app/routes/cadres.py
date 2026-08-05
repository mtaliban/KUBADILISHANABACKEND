from typing import Literal, Optional
from fastapi import APIRouter, Query
from ..core.db import get_db
from ..core.cache import cached

router = APIRouter(prefix="/cadres", tags=["cadres"])


@router.get("")
async def list_cadres(category: Optional[Literal["health", "education"]] = Query(None)):
    key = f"cadres:{category or 'all'}"
    async def _load():
        query = {"category": category} if category else {}
        cursor = get_db().cadres.find(query, {"_id": 0}).sort("display_name", 1)
        return [doc async for doc in cursor]
    return await cached(key, _load)


@router.get("/subjects")
async def list_subjects(level: Optional[str] = Query("Secondary")):
    key = f"subjects:{level or 'all'}"
    async def _load():
        query = {"level": level} if level else {}
        cursor = get_db().subjects.find(query, {"_id": 0}).sort("name", 1)
        return [doc async for doc in cursor]
    return await cached(key, _load)

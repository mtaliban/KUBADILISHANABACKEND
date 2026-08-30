#!/usr/bin/env python3
"""Futa mikoa batili (kama "JUMP") kwenye MongoDB.
Run: python -m scripts.cleanup_invalid_regions
"""
import asyncio
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings
from motor.motor_asyncio import AsyncIOMotorClient

VALID_REGIONS = {
    "arusha", "coast", "dar es salaam", "dodoma", "geita", "iringa",
    "kagera", "katavi", "kigoma", "kilimanjaro", "lindi", "manyara",
    "mara", "mbeya", "morogoro", "mtwara", "mwanza", "njombe",
    "rukwa", "ruvuma", "shinyanga", "simiyu", "singida", "songwe",
    "tabora", "tanga",
}

async def main():
    s = Settings()
    client = AsyncIOMotorClient(s.mongo_uri)
    db = client[s.mongo_db]

    cursor = db.regions.find({})
    deleted = 0
    async for r in cursor:
        name = r.get("name", "").strip().lower()
        if name not in VALID_REGIONS:
            await db.regions.delete_one({"_id": r["_id"]})
            print(f"  Deleted: {r.get('name')} (id={r.get('id')})")
            deleted += 1

    if deleted:
        # Bust cache
        print(f"\nDeleted {deleted} invalid region(s).")
    else:
        print("No invalid regions found.")

    client.close()

if __name__ == "__main__":
    asyncio.run(main())

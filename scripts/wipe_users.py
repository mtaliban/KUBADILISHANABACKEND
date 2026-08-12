#!/usr/bin/env python3
"""Wipe ALL non-admin users + their data (matches, messages, notifications,
page_views, call_logs, event_log entries) — ADMINI HUHIFADHIWA.

Run ndani ya backend container (sawa na seed_data.py):
    docker cp scripts/wipe_users.py kv_backend:/tmp/wipe_users.py
    docker exec kv_backend env MONGO_URI="mongodb://admin:...@mongodb:27017/kubadilishana_vituo?authSource=admin" \
        python3 /tmp/wipe_users.py

Hii ni data ya uwongo / seed — kusafisha kabisa kabla ya kuanza rasmi.
"""
import os
from pymongo import MongoClient

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin",
)


def main():
    client = MongoClient(MONGO_URI)
    db = client.get_default_database()
    print(f"Connected: {db.name}")

    ids = [str(u["_id"]) for u in db.users.find({"is_admin": {"$ne": True}}, {"_id": 1})]
    print(f"Watumiaji wasio-admin: {len(ids)}")

    if ids:
        from bson import ObjectId
        oids = [ObjectId(i) for i in ids]
        print(f"  users:            {db.users.delete_many({'_id': {'$in': oids}}).deleted_count}")
        print(f"  matches:          {db.matches.delete_many({'$or': [{'user_a_id': {'$in': ids}}, {'user_b_id': {'$in': ids}}]}).deleted_count}")
        print(f"  messages:         {db.messages.delete_many({'$or': [{'from_user_id': {'$in': ids}}, {'to_user_id': {'$in': ids}}]}).deleted_count}")
        print(f"  notifications:    {db.notifications.delete_many({'user_id': {'$in': ids}}).deleted_count}")
        print(f"  page_views:       {db.page_views.delete_many({'user_id': {'$in': ids}}).deleted_count}")
        print(f"  call_logs:        {db.call_logs.delete_many({'$or': [{'from_user_id': {'$in': ids}}, {'to_user_id': {'$in': ids}}]}).deleted_count}")
        print(f"  event_log:        {db.event_log.delete_many({'actor_user_id': {'$in': ids}}).deleted_count}")

    admins = list(db.users.find({"is_admin": True}, {"full_name": 1, "email": 1, "phone_primary": 1}))
    print(f"\nAdmini waliosalia ({len(admins)}):")
    for a in admins:
        print(f"  👑 {a.get('full_name')} | {a.get('email')} | {a.get('phone_primary')}")
    print("\n✅ Wipe imekamilika — database iko safi (admini tu).")


if __name__ == "__main__":
    main()

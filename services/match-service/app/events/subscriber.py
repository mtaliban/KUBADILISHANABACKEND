"""MQTT subscriber: on user.registered / user.destination_changed / user.station_changed,
recompute matches for that user and store them (also publishes kv/match/found)."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from bson import ObjectId
from paho.mqtt import client as mqtt
from pymongo import MongoClient
from ..core.config import settings
from ..matching import match_score

logger = logging.getLogger(__name__)

TOPIC_USER_REGISTERED = "kv/user/registered"
TOPIC_USER_DEST_CHANGED = "kv/user/destination_changed"
TOPIC_USER_STATION_CHANGED = "kv/user/station_changed"
TOPIC_MATCH_FOUND = "kv/match/found"

_mongo = MongoClient(settings.mongo_uri)
_db = _mongo.get_default_database()


def _recompute_matches_for(user_id: str, publisher: mqtt.Client) -> int:
    """Sync (blocking) — runs inside MQTT thread. Returns number of matches saved."""
    user = _db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return 0

    query = {
        "_id": {"$ne": user["_id"]},
        "category": user["category"],
        "cadre_code": user["cadre_code"],
        "status": "active",
    }
    saved = 0
    now = datetime.now(timezone.utc)
    for candidate in _db.users.find(query):
        score = match_score(user, candidate)
        if score <= 0:
            continue
        # normalize pair so (a, b) == (b, a)
        pair = tuple(sorted([str(user["_id"]), str(candidate["_id"])]))
        _db.matches.update_one(
            {"user_a_id": pair[0], "user_b_id": pair[1]},
            {"$set": {
                "user_a_id": pair[0],
                "user_b_id": pair[1],
                "score": score,
                "matched_at": now,
                "status": "new",
            }},
            upsert=True,
        )
        saved += 1
        payload = json.dumps({
            "event": "match.found",
            "user_a_id": pair[0],
            "user_b_id": pair[1],
            "score": score,
            "occurred_at": now.isoformat(),
        }, default=str)
        publisher.publish(TOPIC_MATCH_FOUND, payload, qos=1)
    logger.info(f"recomputed matches for {user_id}: {saved} found")
    return saved


def _on_connect(client, userdata, flags, rc, props):
    logger.info(f"match-service MQTT connected rc={rc}")
    client.subscribe([
        (TOPIC_USER_REGISTERED, 1),
        (TOPIC_USER_DEST_CHANGED, 1),
        (TOPIC_USER_STATION_CHANGED, 1),
    ])


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        logger.exception(f"bad payload on {msg.topic}: {e}")
        return

    user_id = payload.get("user_id")
    if not user_id:
        logger.warning(f"no user_id on {msg.topic}")
        return
    try:
        _recompute_matches_for(user_id, client)
    except Exception as e:
        logger.exception(f"match recompute failed: {e}")


def start_subscriber() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="match-service-sub")
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    client.loop_start()
    return client

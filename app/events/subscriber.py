"""Background MQTT subscribers running inside the same backend process.

Two responsibilities:
1) Analytics: log every event to Mongo `event_log` and append to daily CSV.
2) Matching: on user.registered / destination_changed / station_changed,
   recompute matches and publish kv/match/found.
"""
import csv
import json
import logging
import ssl
from datetime import datetime, timezone
from pathlib import Path
from bson import ObjectId
from paho.mqtt import client as mqtt
from ..config import settings
from ..db import get_sync_db
from ..modules.matches.matching import match_score
from .topics import (
    TOPIC_ALL, TOPIC_USER_REGISTERED,
    TOPIC_USER_DESTINATION_CHANGED, TOPIC_USER_STATION_CHANGED,
    TOPIC_MATCH_FOUND,
)

logger = logging.getLogger(__name__)
_sub: mqtt.Client | None = None


def _csv_path_for_today() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    Path(settings.csv_output_dir).mkdir(parents=True, exist_ok=True)
    return Path(settings.csv_output_dir) / f"events_{d}.csv"


def _append_csv(row: dict) -> None:
    path = _csv_path_for_today()
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["occurred_at", "topic", "event_type", "actor_user_id", "payload_json"], extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def _log_event(msg) -> None:
    db = get_sync_db()
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        logger.exception(f"bad payload on {msg.topic}: {e}")
        return
    now = datetime.now(timezone.utc)
    event_type = payload.get("event", "unknown")
    actor = payload.get("user_id") or payload.get("user_a_id") or payload.get("from_user_id")
    db.event_log.insert_one({
        "event_type": event_type, "topic": msg.topic,
        "actor_user_id": actor, "payload": payload, "occurred_at": now,
    })
    _append_csv({
        "occurred_at": now.isoformat(), "topic": msg.topic,
        "event_type": event_type, "actor_user_id": actor or "",
        "payload_json": json.dumps(payload, default=str),
    })


def _recompute_matches(user_id: str, client: mqtt.Client) -> int:
    db = get_sync_db()
    user = db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return 0
    q = {"_id": {"$ne": user["_id"]}, "category": user["category"], "cadre_code": user["cadre_code"], "status": "active"}
    now = datetime.now(timezone.utc)
    saved = 0
    for cand in db.users.find(q):
        score = match_score(user, cand)
        if score <= 0:
            continue
        pair = tuple(sorted([str(user["_id"]), str(cand["_id"])]))
        db.matches.update_one(
            {"user_a_id": pair[0], "user_b_id": pair[1]},
            {"$set": {"user_a_id": pair[0], "user_b_id": pair[1], "score": score, "matched_at": now, "status": "new"}},
            upsert=True,
        )
        saved += 1
        client.publish(TOPIC_MATCH_FOUND, json.dumps({
            "event": "match.found",
            "user_a_id": pair[0], "user_b_id": pair[1],
            "score": score, "occurred_at": now.isoformat(),
        }), qos=1)
    logger.info(f"recomputed matches for {user_id}: {saved}")
    return saved


def _on_connect(client, userdata, flags, rc, props):
    logger.info(f"backend MQTT subscriber connected rc={rc}")
    client.subscribe(TOPIC_ALL, qos=1)


def _on_message(client, userdata, msg):
    # 1) always log
    try:
        _log_event(msg)
    except Exception as e:
        logger.exception(f"log_event failed: {e}")

    # 2) matching triggers
    if msg.topic in (TOPIC_USER_REGISTERED, TOPIC_USER_DESTINATION_CHANGED, TOPIC_USER_STATION_CHANGED):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            uid = payload.get("user_id")
            if uid:
                _recompute_matches(uid, client)
        except Exception as e:
            logger.exception(f"match trigger failed: {e}")


def start_subscriber() -> mqtt.Client:
    global _sub
    _sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{settings.mqtt_client_prefix}-sub")
    if settings.mqtt_username:
        _sub.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_use_tls:
        _sub.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    _sub.on_connect = _on_connect
    _sub.on_message = _on_message
    _sub.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    _sub.loop_start()
    return _sub


def stop_subscriber():
    global _sub
    if _sub:
        _sub.loop_stop()
        _sub.disconnect()
        _sub = None

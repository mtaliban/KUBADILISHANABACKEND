"""MQTT subscriber that logs every event to Mongo `event_log` and appends to daily CSV."""
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from paho.mqtt import client as mqtt
from pymongo import MongoClient
from ..core.config import settings

logger = logging.getLogger(__name__)

TOPIC_ALL_USER = "kv/user/#"
TOPIC_ALL_MATCH = "kv/match/#"

_mongo = MongoClient(settings.mongo_uri)
_db = _mongo.get_default_database()


def _csv_path_for_today() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    Path(settings.csv_output_dir).mkdir(parents=True, exist_ok=True)
    return Path(settings.csv_output_dir) / f"events_{d}.csv"


def _append_csv(row: dict) -> None:
    path = _csv_path_for_today()
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["occurred_at", "topic", "event_type", "actor_user_id", "payload_json"],
            extrasaction="ignore",
        )
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _on_connect(client, userdata, flags, rc, props):
    logger.info(f"analytics MQTT connected rc={rc}")
    client.subscribe([(TOPIC_ALL_USER, 1), (TOPIC_ALL_MATCH, 1)])


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        logger.exception(f"invalid payload on {msg.topic}: {e}")
        return

    now = datetime.now(timezone.utc)
    event_type = payload.get("event", "unknown")
    actor = payload.get("user_id") or payload.get("user_a_id")

    doc = {
        "event_type": event_type,
        "topic": msg.topic,
        "actor_user_id": actor,
        "payload": payload,
        "occurred_at": now,
    }
    try:
        _db.event_log.insert_one(doc)
    except Exception as e:
        logger.exception(f"failed to insert event_log: {e}")

    try:
        _append_csv({
            "occurred_at": now.isoformat(),
            "topic": msg.topic,
            "event_type": event_type,
            "actor_user_id": actor or "",
            "payload_json": json.dumps(payload, default=str),
        })
    except Exception as e:
        logger.exception(f"failed to append CSV: {e}")

    logger.info(f"logged event: {event_type} from {actor}")


def start_subscriber() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="analytics-service-sub")
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    client.loop_start()
    return client

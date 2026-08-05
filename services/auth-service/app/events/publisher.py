"""MQTT publisher — thin async wrapper on paho-mqtt."""
import json
import logging
from paho.mqtt import client as mqtt
from ..core.config import settings

logger = logging.getLogger(__name__)

_client: mqtt.Client | None = None


def get_publisher() -> mqtt.Client:
    global _client
    if _client is not None and _client.is_connected():
        return _client
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="auth-service-pub")
    _client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    _client.loop_start()
    return _client


def publish(topic: str, payload: dict, qos: int = 1) -> None:
    try:
        client = get_publisher()
        message = json.dumps(payload, default=str)
        info = client.publish(topic, message, qos=qos)
        info.wait_for_publish(timeout=2.0)
        logger.info(f"published {topic}: {payload.get('event')}")
    except Exception as e:
        logger.exception(f"failed to publish {topic}: {e}")

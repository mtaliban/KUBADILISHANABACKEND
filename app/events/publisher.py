"""Singleton MQTT publisher for the whole backend. Supports Mosquitto (local),
HiveMQ Cloud, EMQX Cloud — anything MQTT 5 compatible."""
import json
import logging
import ssl
from paho.mqtt import client as mqtt
from ..config import settings

logger = logging.getLogger(__name__)
_client: mqtt.Client | None = None


def get_publisher() -> mqtt.Client:
    global _client
    if _client is not None and _client.is_connected():
        return _client
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{settings.mqtt_client_prefix}-pub")
    if settings.mqtt_username:
        _client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_use_tls:
        _client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    _client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    _client.loop_start()
    return _client


def publish(topic: str, payload: dict, qos: int = 1) -> None:
    try:
        c = get_publisher()
        msg = c.publish(topic, json.dumps(payload, default=str), qos=qos)
        msg.wait_for_publish(timeout=2.0)
        logger.info(f"published {topic}: {payload.get('event')}")
    except Exception as e:
        logger.exception(f"failed to publish {topic}: {e}")

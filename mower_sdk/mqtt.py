"""MQTT client module.

Provides MQTT connection, subscription and device status updates.
"""

import asyncio
import importlib
import json
import logging
import uuid
from urllib.parse import urlparse
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from paho.mqtt import client as mqtt_client

from mower_sdk._deprecation import warn_legacy
from mower_sdk.errors import MowerMQTTError, ERROR_MESSAGES  # noqa: F401
from mower_sdk.models import Device, DeviceStatus  # noqa: F401

if TYPE_CHECKING:
    from mower_sdk.legacy.mqtt_v1 import MowerMQTT as MowerMQTT
    from mower_sdk.legacy.utils import parse_json as parse_json

# The public surface upstream published from this module: the classes it
# defined plus the names it imported and callers picked up as aliases.
# MowerMQTTError, ERROR_MESSAGES and DeviceStatus were used only by
# MowerMQTT and are kept as inventory aliases (Phase 1 Q6); MowerMQTT and
# parse_json now live in mower_sdk.legacy and are served by __getattr__.
__all__ = [
    "MowerMQTT",
    "NavimowMQTT",
    "Device",
    "DeviceStatus",
    "ERROR_MESSAGES",
    "MowerMQTTError",
    "parse_json",
]

_LOGGER = logging.getLogger(__name__)


def _build_web_client_id(username: str | None) -> str:
    base = username or "unknown"
    rand = uuid.uuid4().hex[:10]
    return f"web_{base}_{rand}"


def _mask_secret(value: str | None) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def _format_auth_headers(headers: dict[str, str] | None) -> str:
    if not headers:
        return "<none>"
    safe = {}
    for key, val in headers.items():
        if key.lower() == "authorization":
            safe[key] = _mask_secret(val)
        else:
            safe[key] = val
    return str(safe)


class NavimowMQTT:
    """Navimow MQTT client for cloud topics."""

    def __init__(
        self,
        broker: str,
        port: int,
        username: str | None,
        password: str | None,
        records: list[Device],
        ws_path: str | None = None,
        auth_headers: dict[str, str] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        keepalive_seconds: int = 2400,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
    ) -> None:
        parsed = urlparse(broker)
        self.broker = parsed.hostname or broker
        self.port = parsed.port or port
        self.username = username
        self.password = password
        self.records = records
        self.loop = loop or asyncio.get_event_loop()
        self.ws_path = ws_path
        self.auth_headers = auth_headers
        self._use_tls = bool(ws_path) or parsed.scheme == "wss"
        self._client_id = _build_web_client_id(self.username)
        self.keepalive_seconds = max(30, int(keepalive_seconds))
        self.reconnect_min_delay = max(0, int(reconnect_min_delay))
        self.reconnect_max_delay = max(self.reconnect_min_delay, int(reconnect_max_delay))

        self.on_connected: Callable[[], Awaitable[None]] | None = None
        self.on_ready: Callable[[], Awaitable[None]] | None = None
        self.on_message: Callable[[str, bytes, str], Awaitable[None]] | None = None
        self.on_disconnected: Callable[[], Awaitable[None]] | None = None

        # self.client is assigned before it is configured: the callback lookups
        # in _configure_client may read it, and a subclass can rely on that.
        transport = "websockets" if self.ws_path else "tcp"
        self.client = mqtt_client.Client(client_id=self._client_id, transport=transport)
        self._configure_client(self.client)
        _LOGGER.info(
            "NavimowMQTT init: broker=%s port=%s ws_path=%s tls=%s client_id=%s",
            self.broker,
            self.port,
            self.ws_path,
            self._use_tls,
            self._client_id,
        )

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected()

    def _configure_client(self, client: mqtt_client.Client) -> None:
        """Apply the current credentials, WebSocket options, TLS, reconnect delays and callbacks."""
        if self.username and self.password:
            client.username_pw_set(self.username, self.password)
        if self.ws_path:
            client.ws_set_options(path=self.ws_path, headers=self.auth_headers or {})
        if self._use_tls:
            client.tls_set()
        client.reconnect_delay_set(
            min_delay=self.reconnect_min_delay, max_delay=self.reconnect_max_delay
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

    def _build_new_client(self) -> mqtt_client.Client:
        """Build a new paho MQTT client with the latest credentials and configuration.

        Not called by __init__, which configures self.client in place, so an
        override here takes effect on the next rebuild, not at construction.
        """
        transport = "websockets" if self.ws_path else "tcp"
        client = mqtt_client.Client(client_id=self._client_id, transport=transport)
        self._configure_client(client)
        return client

    def update_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        """Update the MQTT credentials.

        Unchanged values are ignored. While connected, changed values are only stored on
        this object; the live paho client and its connection are left alone, so hourly
        OAuth token rotation does not force a disconnect. Stored values reach the broker
        only when the paho client is next rebuilt, which this method does on a later call
        made while disconnected; paho's own automatic reconnect reuses the existing client
        and its original credentials. While disconnected, changed values rebuild the paho
        client and start an asynchronous reconnect.

        paho-mqtt's ws_set_options only takes effect before a connection is established,
        so new WebSocket headers always need a rebuilt client.
        """
        changed = False
        if username is not None and username != self.username:
            self.username = username
            changed = True
        if password is not None and password != self.password:
            self.password = password
            changed = True
        if auth_headers is not None and auth_headers != self.auth_headers:
            self.auth_headers = auth_headers
            changed = True

        if not changed:
            return

        if self.client.is_connected():
            # The connection is healthy: store the new credentials and leave the live client alone.
            # They reach the broker when the client is next rebuilt, on a later call made while
            # disconnected; paho's automatic reconnect reuses this client as it is. Not disconnecting
            # on purpose: hourly token rotation would otherwise cause needless reconnects and
            # "device unavailable".
            _LOGGER.info(
                "NavimowMQTT credentials updated while connected (will apply on next reconnect): broker=%s port=%s",
                self.broker,
                self.port,
            )
            return

        # Disconnected: rebuild the client with the new credentials and reconnect now.
        _LOGGER.info(
            "NavimowMQTT credentials updated while disconnected, rebuilding and reconnecting: broker=%s port=%s",
            self.broker,
            self.port,
        )
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

        self.client = self._build_new_client()
        self.connect_async()

    def connect_async(self) -> None:
        if not self.is_connected:
            _LOGGER.info(
                "NavimowMQTT connect details: transport=%s broker=%s port=%s ws_path=%s tls=%s username=%s auth_headers=%s",
                "websockets" if self.ws_path else "tcp",
                self.broker,
                self.port,
                self.ws_path,
                self._use_tls,
                _mask_secret(self.username),
                _format_auth_headers(self.auth_headers),
            )
            _LOGGER.info(
                "NavimowMQTT connecting: broker=%s port=%s ws_path=%s",
                self.broker,
                self.port,
                self.ws_path,
            )
            self.client.connect_async(self.broker, self.port, self.keepalive_seconds)
            self.client.loop_start()

    def disconnect(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
        _LOGGER.info(
            "NavimowMQTT disconnect requested: broker=%s port=%s",
            self.broker,
            self.port,
        )

    def _get_device_ids(self) -> list[str]:
        device_ids: list[str] = []
        for device in self.records:
            device_id = getattr(device, "id", None)
            if device_id:
                device_ids.append(device_id)
        return device_ids

    def subscribe_all(self, product_key: str = "", device_name: str = "") -> None:  # noqa: ARG002
        """Subscribe to the state, event and attributes topics of every known device.

        product_key and device_name are ignored; they are kept, optional, so
        callers and overrides written against the original signature keep
        working.
        """
        device_ids = self._get_device_ids()
        if not device_ids:
            _LOGGER.warning(
                "NavimowMQTT subscribing cloud topics with wildcard: no device ids available"
            )
            self.client.subscribe("/downlink/vehicle/+/realtimeDate/state")
            self.client.subscribe("/downlink/vehicle/+/realtimeDate/event")
            self.client.subscribe("/downlink/vehicle/+/realtimeDate/attributes")
            return

        _LOGGER.info(
            "NavimowMQTT subscribing cloud topics for %d device(s)", len(device_ids)
        )
        for device_id in device_ids:
            self.client.subscribe(f"/downlink/vehicle/{device_id}/realtimeDate/state")
            self.client.subscribe(f"/downlink/vehicle/{device_id}/realtimeDate/event")
            self.client.subscribe(
                f"/downlink/vehicle/{device_id}/realtimeDate/attributes"
            )

    def unsubscribe_all(self, product_key: str = "", device_name: str = "") -> None:  # noqa: ARG002
        """Unsubscribe from the topics subscribe_all subscribed to.

        product_key and device_name are ignored, as in subscribe_all.
        """
        device_ids = self._get_device_ids()
        if not device_ids:
            _LOGGER.info("NavimowMQTT unsubscribing cloud topics (wildcard)")
            self.client.unsubscribe("/downlink/vehicle/+/realtimeDate/state")
            self.client.unsubscribe("/downlink/vehicle/+/realtimeDate/event")
            self.client.unsubscribe("/downlink/vehicle/+/realtimeDate/attributes")
            return

        _LOGGER.info(
            "NavimowMQTT unsubscribing cloud topics for %d device(s)", len(device_ids)
        )
        for device_id in device_ids:
            self.client.unsubscribe(f"/downlink/vehicle/{device_id}/realtimeDate/state")
            self.client.unsubscribe(f"/downlink/vehicle/{device_id}/realtimeDate/event")
            self.client.unsubscribe(
                f"/downlink/vehicle/{device_id}/realtimeDate/attributes"
            )

    def _schedule(self, coro: Awaitable[None]) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(asyncio.create_task, coro)
        else:
            _LOGGER.debug("Event loop not running, skip scheduling MQTT callback")

    def _on_connect(self, _client, _userdata, _flags, rc) -> None:
        if rc != 0:
            _LOGGER.error("MQTT connection failed: rc=%s", rc)
            return
        _LOGGER.info(
            "NavimowMQTT connected: broker=%s port=%s",
            self.broker,
            self.port,
        )
        # Called with both arguments so an override with the original two-argument
        # signature keeps working.
        self.subscribe_all("", "")

        if self.on_connected is not None:
            self._schedule(self.on_connected())
        if self.on_ready is not None:
            self._schedule(self.on_ready())

    def _on_disconnect(self, _client, _userdata, _rc) -> None:
        _LOGGER.debug(
            "NavimowMQTT disconnected: broker=%s port=%s rc=%s",
            self.broker,
            self.port,
            _rc,
        )
        if self.on_disconnected is not None:
            self._schedule(self.on_disconnected())

    def _parse_topic(self, topic: str) -> tuple[str | None, str | None]:
        parts = topic.split("/")
        if parts and parts[0] == "":
            parts = parts[1:]
        if len(parts) != 5:
            return None, None
        if parts[0] != "downlink" or parts[1] != "vehicle":
            return None, None
        if parts[3] != "realtimeDate":
            return None, None
        return parts[2], parts[4]

    def _on_message(self, _client, _userdata, msg) -> None:
        topic = msg.topic
        device_id, _ = self._parse_topic(topic)

        payload_bytes = msg.payload
        _LOGGER.debug(
            "NavimowMQTT payload: topic=%s payload=%s",
            topic,
            (payload_bytes or b"").decode("utf-8", errors="replace"),
        )
        _LOGGER.debug(
            "NavimowMQTT message: topic=%s bytes=%d device=%s",
            topic,
            len(payload_bytes or b""),
            device_id,
        )
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None

        if isinstance(payload, dict) and device_id:
            # Re-encoded on purpose: on_message(topic, bytes, device_id) is a public
            # contract, consumers decode the bytes themselves, and integrations wrap
            # this slot expecting the device_id to be present in the payload.
            payload.setdefault("device_id", device_id)
            payload_bytes = json.dumps(payload).encode("utf-8")

        if self.on_message is not None and device_id:
            self._schedule(self.on_message(topic, payload_bytes, device_id))

    def publish_command(self, device_id: str, payload: dict[str, Any]) -> None:
        topic = f"navimow/{device_id}/command"
        self.client.publish(topic, json.dumps(payload))


# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
_LEGACY_NAMES = {
    "MowerMQTT": ("mqtt_v1", "MowerMQTT"),
    "parse_json": ("utils", "parse_json"),
}


def __getattr__(name: str) -> Any:
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module."""
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value

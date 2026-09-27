"""Generation-1 MQTT client, kept for compatibility.

MowerMQTT was extracted verbatim from mower_sdk/mqtt.py (lines 49 to 454 at
the fork point 6596aa0). It subscribes to placeholder topics, its async_connect
is a no-op, its subscribe blocks until disconnect, and nothing on the live path
uses it. The class body is unchanged; only this import block is new. The
client-id and masking helpers stay in core mqtt.py, shared with NavimowMQTT.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from paho.mqtt import client as mqtt_client

from mower_sdk.errors import ERROR_MESSAGES, MowerMQTTError
from mower_sdk.legacy.utils import parse_json
from mower_sdk.models import DeviceStatus
from mower_sdk.mqtt import _build_web_client_id, _format_auth_headers, _mask_secret

_LOGGER = logging.getLogger(__name__)


class MowerMQTT:
    """MQTT client.

    Provides MQTT connection, subscription and device status updates, with synchronous and asynchronous interfaces.

    Attributes:
        broker: MQTT broker address
        port: MQTT broker port
        username: MQTT username (optional)
        password: MQTT password (optional)
        status_cache: Device status cache
        _async_client: Asynchronous MQTT client
        _sync_client: Synchronous MQTT client
        _callbacks: Callback registry
    """

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        ws_path: str | None = None,
        auth_headers: dict[str, str] | None = None,
        keepalive_seconds: int = 2400,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
    ):
        """Initialize the MQTT client.

        Args:
            broker: MQTT broker address
            port: MQTT broker port
            username: MQTT username (optional)
            password: MQTT password (optional)
        """
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.ws_path = ws_path
        self.auth_headers = auth_headers
        # Keepalive is the MQTT protocol-level liveness check (PINGREQ/PINGRESP), preferred over application-level heartbeat messages.
        # The default is 40 minutes, so protocol traffic reaches a broker or load balancer that drops connections idle for an hour.
        self.keepalive_seconds = max(30, int(keepalive_seconds))
        self.reconnect_min_delay = max(0, int(reconnect_min_delay))
        self.reconnect_max_delay = max(self.reconnect_min_delay, int(reconnect_max_delay))
        self._use_tls = bool(ws_path)
        self._client_id = _build_web_client_id(self.username)
        self.status_cache: dict[str, DeviceStatus] = {}
        self._async_client: mqtt_client.Client | None = None
        self._sync_client: mqtt_client.Client | None = None
        self._async_stop_event: asyncio.Event | None = None
        self._callbacks: dict[str, dict[str, Callable]] = {}
        self._connected = False

    def configure_wss(
        self,
        mqtt_host: str,
        mqtt_url: str,
        username: str | None,
        password: str | None,
        auth_headers: dict[str, str] | None,
        port: int = 443,
    ) -> None:
        """Configure the WSS connection parameters."""
        parsed = urlparse(mqtt_host)
        host = parsed.hostname or mqtt_host
        self.broker = host
        self.port = parsed.port or port
        self.ws_path = mqtt_url
        self.username = username
        self.password = password
        self.auth_headers = auth_headers
        self._use_tls = True

    def _build_client(self) -> mqtt_client.Client:
        transport = "websockets" if self.ws_path else "tcp"
        client = mqtt_client.Client(client_id=self._client_id, transport=transport)
        if self.username and self.password:
            client.username_pw_set(self.username, self.password)
        if self.ws_path:
            client.ws_set_options(path=self.ws_path, headers=self.auth_headers or {})
        if self._use_tls:
            client.tls_set()
        # Reconnect backoff after a dropped connection (paho applies it with loop_start + connect_async).
        client.reconnect_delay_set(
            min_delay=self.reconnect_min_delay, max_delay=self.reconnect_max_delay
        )
        _LOGGER.debug(
            "MQTT client built: transport=%s broker=%s port=%s ws_path=%s tls=%s client_id=%s",
            transport,
            self.broker,
            self.port,
            self.ws_path,
            self._use_tls,
            self._client_id,
        )
        return client

    def _get_status_topic(self, device_id: str) -> str:
        """Return the device status topic.

        Args:
            device_id: Device ID

        Returns:
            Topic path
        """
        # TODO: adjust to the real MQTT topic format
        return f"device/{device_id}/status"

    def _get_event_topic(self, device_id: str) -> str:
        """Return the device event topic.

        Args:
            device_id: Device ID

        Returns:
            Topic path
        """
        # TODO: adjust to the real MQTT topic format
        return f"device/{device_id}/event"

    async def async_connect(self) -> None:
        """Connect to the MQTT broker asynchronously.

        Raises:
            MowerMQTTError: If the connection fails
        """
        try:
            # The connection is made on subscribe; this only marks the configuration as valid
            self._connected = True
        except Exception as e:
            raise MowerMQTTError(
                f"{ERROR_MESSAGES['MQTT_CONNECTION_FAILED']}: {str(e)}"
            ) from e

    def connect(self) -> None:
        """Connect to the MQTT broker synchronously.

        Raises:
            MowerMQTTError: If the connection fails
        """
        try:
            self._sync_client = self._build_client()
            _LOGGER.info(
                "MQTT connect details (sync): transport=%s broker=%s port=%s ws_path=%s tls=%s username=%s auth_headers=%s",
                "websockets" if self.ws_path else "tcp",
                self.broker,
                self.port,
                self.ws_path,
                self._use_tls,
                _mask_secret(self.username),
                _format_auth_headers(self.auth_headers),
            )

            def on_connect(client, userdata, flags, rc):
                if rc == 0:
                    self._connected = True
                    _LOGGER.info(
                        "MQTT connected (sync): broker=%s port=%s",
                        self.broker,
                        self.port,
                    )
                else:
                    raise MowerMQTTError(
                        f"{ERROR_MESSAGES['MQTT_CONNECTION_FAILED']}: Return code {rc}"
                    )

            self._sync_client.on_connect = on_connect
            _LOGGER.info(
                "MQTT connecting (sync): broker=%s port=%s ws_path=%s",
                self.broker,
                self.port,
                self.ws_path,
            )
            self._sync_client.connect(self.broker, self.port, self.keepalive_seconds)
            self._sync_client.loop_start()
        except Exception as e:
            raise MowerMQTTError(
                f"{ERROR_MESSAGES['MQTT_CONNECTION_FAILED']}: {str(e)}"
            ) from e

    async def async_subscribe_device(
        self,
        device_id: str,
        on_status_update: Callable[[DeviceStatus], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Subscribe to a device's status and events asynchronously.

        Args:
            device_id: Device ID
            on_status_update: Status update callback
            on_event: Event callback

        Raises:
            MowerMQTTError: If the subscription fails
        """
        status_topic = self._get_status_topic(device_id)
        event_topic = self._get_event_topic(device_id)

        # Store the callbacks
        self._callbacks[device_id] = {
            "status": on_status_update,
            "event": on_event,
        }

        loop = asyncio.get_running_loop()
        self._async_stop_event = asyncio.Event()
        try:
            self._async_client = self._build_client()
            _LOGGER.info(
                "MQTT connect details (async): transport=%s broker=%s port=%s ws_path=%s tls=%s username=%s auth_headers=%s device=%s",
                "websockets" if self.ws_path else "tcp",
                self.broker,
                self.port,
                self.ws_path,
                self._use_tls,
                _mask_secret(self.username),
                _format_auth_headers(self.auth_headers),
                device_id,
            )

            def on_connect(_client, _userdata, _flags, rc) -> None:
                if rc != 0:
                    _LOGGER.error("MQTT connection failed: rc=%s", rc)
                    return
                self._connected = True
                _LOGGER.info(
                    "MQTT connected (async): broker=%s port=%s device=%s",
                    self.broker,
                    self.port,
                    device_id,
                )
                _LOGGER.info(
                    "MQTT subscribing (async): %s, %s",
                    status_topic,
                    event_topic,
                )
                _client.subscribe(status_topic)
                _client.subscribe(event_topic)

            def on_message(_client, _userdata, msg) -> None:
                try:
                    payload_text = (msg.payload or b"").decode("utf-8", errors="replace")
                    _LOGGER.debug(
                        "MQTT payload (async): topic=%s payload=%s",
                        msg.topic,
                        payload_text,
                    )
                    payload = parse_json(msg.payload)
                    topic = msg.topic
                    _LOGGER.debug(
                        "MQTT message (async): topic=%s bytes=%d device=%s",
                        topic,
                        len(msg.payload or b""),
                        device_id,
                    )
                    if topic == status_topic:
                        status = DeviceStatus.from_dict(payload)
                        self.status_cache[device_id] = status
                        callback = self._callbacks.get(device_id, {}).get("status")
                        if callback:
                            loop.call_soon_threadsafe(callback, status)
                    elif topic == event_topic:
                        callback = self._callbacks.get(device_id, {}).get("event")
                        if callback:
                            loop.call_soon_threadsafe(callback, payload)
                except Exception as e:
                    _LOGGER.exception("Error processing MQTT message: %s", e)

            def on_disconnect(_client, _userdata, _rc) -> None:
                _LOGGER.debug(
                    "MQTT disconnected (async): broker=%s port=%s device=%s",
                    self.broker,
                    self.port,
                    device_id,
                )
                if self._async_stop_event:
                    self._async_stop_event.set()

            self._async_client.on_connect = on_connect
            self._async_client.on_message = on_message
            self._async_client.on_disconnect = on_disconnect
            _LOGGER.info(
                "MQTT connecting (async): broker=%s port=%s ws_path=%s device=%s",
                self.broker,
                self.port,
                self.ws_path,
                device_id,
            )
            self._async_client.connect(self.broker, self.port, self.keepalive_seconds)
            self._async_client.loop_start()

            await self._async_stop_event.wait()
        except Exception as e:
            raise MowerMQTTError(
                f"{ERROR_MESSAGES['MQTT_SUBSCRIBE_FAILED']}: {str(e)}"
            ) from e

    def subscribe_device(
        self,
        device_id: str,
        on_status_update: Callable[[DeviceStatus], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Subscribe to a device's status and events synchronously.

        Args:
            device_id: Device ID
            on_status_update: Status update callback
            on_event: Event callback

        Raises:
            MowerMQTTError: If the subscription fails
        """
        if not self._sync_client:
            self.connect()

        status_topic = self._get_status_topic(device_id)
        event_topic = self._get_event_topic(device_id)

        def on_message(client, userdata, msg):
            try:
                payload_text = (msg.payload or b"").decode("utf-8", errors="replace")
                _LOGGER.debug(
                    "MQTT payload (sync): topic=%s payload=%s",
                    msg.topic,
                    payload_text,
                )
                payload = parse_json(msg.payload)
                topic = msg.topic
                _LOGGER.debug(
                    "MQTT message (sync): topic=%s bytes=%d device=%s",
                    topic,
                    len(msg.payload or b""),
                    device_id,
                )

                if topic == status_topic:
                    # Handle a status update
                    status = DeviceStatus.from_dict(payload)
                    self.status_cache[device_id] = status

                    if on_status_update:
                        on_status_update(status)

                elif topic == event_topic:
                    # Handle an event
                    if on_event:
                        on_event(payload)

            except Exception as e:
                # Log the error and keep processing
                print(f"Error processing MQTT message: {e}")

        try:
            self._sync_client.on_message = on_message
            _LOGGER.info(
                "MQTT subscribing (sync): %s, %s",
                status_topic,
                event_topic,
            )
            self._sync_client.subscribe(status_topic)
            self._sync_client.subscribe(event_topic)

            # Store the callbacks
            self._callbacks[device_id] = {
                "status": on_status_update,
                "event": on_event,
            }
        except Exception as e:
            raise MowerMQTTError(
                f"{ERROR_MESSAGES['MQTT_SUBSCRIBE_FAILED']}: {str(e)}"
            ) from e

    def get_cached_status(self, device_id: str) -> DeviceStatus | None:
        """Return the cached device status.

        Args:
            device_id: Device ID

        Returns:
            Device status, or None if none is cached
        """
        return self.status_cache.get(device_id)

    async def async_disconnect(self) -> None:
        """Disconnect from the MQTT broker asynchronously."""
        if self._async_client:
            self._async_client.loop_stop()
            self._async_client.disconnect()
        if self._async_stop_event:
            self._async_stop_event.set()
        self._connected = False
        self._async_client = None

    def disconnect(self) -> None:
        """Disconnect from the MQTT broker synchronously."""
        if self._sync_client:
            self._sync_client.loop_stop()
            self._sync_client.disconnect()
            self._connected = False

"""Navimow SDK facade for MQTT-based integration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mower_sdk.errors import MowerUnsupportedOperationError
from mower_sdk.models import (
    DeviceAttributesMessage,
    DeviceCommandMessage,
    DeviceEventMessage,
    DeviceStateMessage,
)
from mower_sdk.mqtt import NavimowMQTT, _resolve_event_loop

_LOGGER = logging.getLogger(__name__)

# The REST command that does what each MQTT command was meant to do, for the
# message of the error that refuses the MQTT command. Blade height has none.
_REST_ALTERNATIVES: dict[str, str] = {
    "start_mowing": "MowerCommand.START",
    "pause": "MowerCommand.PAUSE",
    "return_to_base": "MowerCommand.DOCK",
}


class NavimowSDK:
    """SDK facade.

    Notes:
        - on_state/on_event/on_attributes callbacks are synchronous.
        - callbacks are invoked from the MQTT thread/event loop context.
          Home Assistant must switch to hass loop via call_soon_threadsafe or
          run_coroutine_threadsafe.
        - a callback that raises is logged with its traceback and does not
          stop delivery of the same message to the callbacks after it.
        - the event loop is ``loop`` if given, else the loop running at
          construction, else the loop set as current with
          ``asyncio.set_event_loop()`` at that time, else the same two at the
          first ``connect()``. A facade constructed and connected with no
          running or current loop must be given ``loop=``; a callback that
          arrives while no loop is bound is dropped with a warning.
        - get_cached_state and get_cached_attributes return the last message
          seen for a device; get_cached_state_age, get_cached_attributes_age
          and get_cached_state_received_at say when it arrived, so a consumer
          can tell a stale cache from a fresh one (the cloud's REST status
          lags the mower by one to two minutes, so observation times, not
          receipt order, decide which reading is newer).
        - start_mowing, pause, return_to_base and set_blade_height publish to
          the MQTT command topic, which the broker accepts and no mower has
          been seen to act on. They raise MowerUnsupportedOperationError
          unless the facade is constructed with
          allow_experimental_mqtt_commands=True. Start, pause and dock have a
          supported REST path in MowerAPI.async_send_command; nothing
          supported sets the blade height.
    """

    def __init__(
        self,
        broker: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        ws_path: str | None = None,
        auth_headers: dict[str, str] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        records: list[Any] | None = None,
        keepalive_seconds: int = 2400,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
        allow_experimental_mqtt_commands: bool = False,
    ) -> None:
        self._loop = _resolve_event_loop(loop)
        self._allow_experimental_mqtt_commands = allow_experimental_mqtt_commands
        self._mqtt = NavimowMQTT(
            broker=broker,
            port=port,
            username=username,
            password=password,
            records=records or [],
            ws_path=ws_path,
            auth_headers=auth_headers,
            loop=self._loop,
            keepalive_seconds=keepalive_seconds,
            reconnect_min_delay=reconnect_min_delay,
            reconnect_max_delay=reconnect_max_delay,
        )
        self._mqtt.on_message = self._on_mqtt_message

        self._state_callbacks: list[Callable[[DeviceStateMessage], None]] = []
        self._event_callbacks: list[Callable[[DeviceEventMessage], None]] = []
        self._attributes_callbacks: list[Callable[[DeviceAttributesMessage], None]] = []

        self._state_cache: dict[str, DeviceStateMessage] = {}
        self._attributes_cache: dict[str, DeviceAttributesMessage] = {}
        # When each cached message arrived: time.monotonic() for ages, and the
        # UTC wall-clock time of the state message for consumers that compare
        # observation times across sources.
        self._state_cache_updated_at: dict[str, float] = {}
        self._attributes_cache_updated_at: dict[str, float] = {}
        self._state_cache_received_at: dict[str, datetime] = {}

    @property
    def mqtt(self) -> NavimowMQTT:
        """The MQTT client: its connection hooks, counters, reasons and message times."""
        return self._mqtt

    def connect(self) -> None:
        """Connect to MQTT broker and start consuming."""
        self._mqtt.connect_async()

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        self._mqtt.disconnect()

    def update_mqtt_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        """Update the MQTT credentials.

        Unchanged values are ignored and None means "keep", so a password-only or
        headers-only update is merged with the stored values. While connected, the
        merged values are set on the live paho client and the connection is kept on
        purpose, so hourly OAuth token rotation does not force a disconnect; paho uses
        them at its next connect, automatic reconnects included. While disconnected,
        changed values rebuild the paho client and start an asynchronous reconnect.

        Used after an OAuth token refresh to update the MQTT WebSocket auth header,
        and to update the MQTT username/password issued by the server.
        """
        self._mqtt.update_credentials(
            username=username,
            password=password,
            auth_headers=auth_headers,
        )

    def on_state(self, callback: Callable[[DeviceStateMessage], None]) -> None:
        self._state_callbacks.append(callback)

    def on_event(self, callback: Callable[[DeviceEventMessage], None]) -> None:
        self._event_callbacks.append(callback)

    def on_attributes(self, callback: Callable[[DeviceAttributesMessage], None]) -> None:
        self._attributes_callbacks.append(callback)

    def get_cached_state(self, device_id: str) -> DeviceStateMessage | None:
        return self._state_cache.get(device_id)

    def get_cached_attributes(self, device_id: str) -> DeviceAttributesMessage | None:
        return self._attributes_cache.get(device_id)

    def get_cached_state_age(self, device_id: str) -> float | None:
        """Seconds since the cached state message for device_id arrived, or None without one."""
        updated_at = self._state_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_attributes_age(self, device_id: str) -> float | None:
        """Seconds since the cached attributes message for device_id arrived, or None without one."""
        updated_at = self._attributes_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_state_received_at(self, device_id: str) -> datetime | None:
        """The UTC time the cached state message for device_id arrived, or None without one."""
        return self._state_cache_received_at.get(device_id)

    async def _on_mqtt_message(
        self, topic: str, payload: bytes, device_id: str
    ) -> None:
        try:
            payload_dict = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload_dict, dict):
            return

        payload_dict.setdefault("device_id", device_id)
        parts = topic.split("/")
        if parts and parts[0] == "":
            parts = parts[1:]
        if len(parts) != 5:
            return
        if parts[0] != "downlink" or parts[1] != "vehicle":
            return
        if parts[3] != "realtimeDate":
            return
        channel = parts[4]

        if channel == "state":
            msg = DeviceStateMessage.from_dict(payload_dict)
            self._state_cache[msg.device_id] = msg
            self._state_cache_updated_at[msg.device_id] = time.monotonic()
            self._state_cache_received_at[msg.device_id] = datetime.now(UTC)
            self._dispatch(self._state_callbacks, msg, channel)
            return
        if channel == "event":
            msg = DeviceEventMessage.from_dict(payload_dict)
            self._dispatch(self._event_callbacks, msg, channel)
            return
        if channel == "attributes":
            msg = DeviceAttributesMessage.from_dict(payload_dict)
            self._attributes_cache[msg.device_id] = msg
            self._attributes_cache_updated_at[msg.device_id] = time.monotonic()
            self._dispatch(self._attributes_callbacks, msg, channel)

    @staticmethod
    def _dispatch(callbacks: list[Callable[[Any], None]], message: Any, channel: str) -> None:
        """Call each callback with the message; one that raises is logged and the rest still run.

        Without this, the exception escaped the task _schedule created, the later
        callbacks were skipped, and the only trace was asyncio's "Task exception
        was never retrieved" at loop shutdown or garbage collection.
        """
        for callback in list(callbacks):
            try:
                callback(message)
            except Exception:
                _LOGGER.exception(
                    "Navimow %s callback %r failed for device %s",
                    channel,
                    callback,
                    message.device_id,
                )

    def _send_mqtt_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        """Publish a DeviceCommandMessage for device_id, if experimental MQTT commands are allowed.

        Raises:
            MowerUnsupportedOperationError: unless the facade was constructed with
                allow_experimental_mqtt_commands=True. Raised before the MQTT
                client is touched; the message names the supported alternative.
            RuntimeError: if the MQTT client is not connected. A connect is
                started first, so a later call can succeed.
        """
        if not self._allow_experimental_mqtt_commands:
            rest_command = _REST_ALTERNATIVES.get(command)
            alternative = (
                f"Use MowerAPI.async_send_command(device_id, {rest_command}) over REST instead, or"
                if rest_command is not None
                else "No supported call sets the blade height (the REST API has no such command);"
            )
            raise MowerUnsupportedOperationError(
                f"MQTT command {command!r} not sent: NavimowSDK publishes it to "
                f"navimow/{device_id}/command, a topic the broker accepts and no mower has been "
                f"seen to act on. {alternative} construct NavimowSDK with "
                "allow_experimental_mqtt_commands=True to publish anyway."
            )
        message = DeviceCommandMessage(
            id=f"cmd-{uuid.uuid4()}", device_id=device_id, command=command, params=params
        )
        if not self._mqtt.is_connected:
            self._mqtt.connect_async()
            _LOGGER.error(
                "MQTT not connected, command not sent: %s for device %s",
                message.command,
                message.device_id,
            )
            raise RuntimeError("MQTT not connected")
        self._mqtt.publish_command(message.device_id, message.to_dict())
        _LOGGER.debug(
            "Published command %s for device %s",
            message.command,
            message.device_id,
        )

    @property
    def is_connected(self) -> bool:
        return self._mqtt.is_connected

    def start_mowing(self, device_id: str) -> None:
        self._send_mqtt_command(device_id, "start_mowing", {})

    def pause(self, device_id: str) -> None:
        self._send_mqtt_command(device_id, "pause", {})

    def return_to_base(self, device_id: str) -> None:
        self._send_mqtt_command(device_id, "return_to_base", {})

    def set_blade_height(self, device_id: str, height: int) -> None:
        self._send_mqtt_command(device_id, "set_blade_height", {"height": height})

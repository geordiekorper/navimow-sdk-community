"""Navimow SDK facade for MQTT-based integration."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mower_sdk.errors import ERROR_MESSAGES, MowerAPIError, MowerUnsupportedOperationError
from mower_sdk.location import REASON_PRIORITY, LocationDecoder, _plausible
from mower_sdk.models import (
    STATE_KNOWN_FIELDS,
    DeviceAttributesMessage,
    DeviceCommandMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceLocationMessage,
    DeviceStateMessage,
    MqttConnectionInfo,
    RejectedMessage,
    SkippedLocationEntry,
    _broker_endpoint,
    _credential,
    mower_time_ms,
)
from mower_sdk.mqtt import (
    NavimowMQTT,
    _decode_json,
    _original_payload,
    _resolve_event_loop,
    parse_topic,
)

if TYPE_CHECKING:
    from mower_sdk.api import MowerAPI

_LOGGER = logging.getLogger(__name__)

# The REST command that does what each MQTT command was meant to do, for the
# message of the error that refuses the MQTT command. Blade height has none.
_REST_ALTERNATIVES: dict[str, str] = {
    "start_mowing": "MowerCommand.START",
    "pause": "MowerCommand.PAUSE",
    "return_to_base": "MowerCommand.DOCK",
}
# What the refusal says when there is no REST alternative.
_NO_ALTERNATIVE: dict[str, str] = {
    "set_blade_height": (
        "No supported call sets the blade height (the REST API has no such command);"
    ),
}
_NO_ALTERNATIVE_KNOWN = "No supported alternative is known;"
# The constructor arguments NavimowSDK.from_connection_info takes from the connection info.
_CONNECTION_INFO_ARGUMENTS = ("broker", "port", "ws_path", "username", "password")


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
          arrives while no loop is bound is dropped with a warning. The
          ``loop`` property reads the MQTT client's binding. A closed
          ``loop=`` raises ValueError, and connecting from a running loop
          other than the bound one raises RuntimeError.
        - on_rejected reports every message that was not applied, or was
          applied with something unknown in it: a state, event or attributes
          payload that is not a JSON object (unparsable), a state payload with
          a field outside STATE_KNOWN_FIELDS (unknown_field, still applied),
          and the location channel's reasons, with each location entry that
          was not applied in RejectedMessage.skipped. With reject_late_state=True, a
          state message whose timestamp is implausible (implausible_time) or
          older than the device's newest accepted one (stale) is not applied;
          one without a timestamp is. Every delivered message carries
          received_at, the UTC receipt time.
        - keepalive_seconds defaults to 60: idle links die after about ten
          minutes, and a ping a minute keeps them alive and finds a dead one
          within about two minutes (NavimowMQTT says more).
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
        keepalive_seconds: int = 60,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
        allow_experimental_mqtt_commands: bool = False,
        subscribe_location: bool = False,
        extra_topics: list[str] | None = None,
        reject_late_state: bool = False,
    ) -> None:
        self._allow_experimental_mqtt_commands = allow_experimental_mqtt_commands
        self._reject_late_state = reject_late_state
        # device id -> the newest accepted state timestamp (mower milliseconds), kept
        # only with reject_late_state.
        self._state_marks: dict[str, int] = {}
        self._mqtt = NavimowMQTT(
            broker=broker,
            port=port,
            username=username,
            password=password,
            records=records or [],
            ws_path=ws_path,
            auth_headers=auth_headers,
            loop=_resolve_event_loop(loop),
            keepalive_seconds=keepalive_seconds,
            reconnect_min_delay=reconnect_min_delay,
            reconnect_max_delay=reconnect_max_delay,
            subscribe_location=subscribe_location,
            extra_topics=extra_topics,
        )
        self._mqtt.on_message = self._on_mqtt_message

        self._state_callbacks: list[Callable[[DeviceStateMessage], None]] = []
        self._event_callbacks: list[Callable[[DeviceEventMessage], None]] = []
        self._attributes_callbacks: list[Callable[[DeviceAttributesMessage], None]] = []
        self._location_callbacks: list[Callable[[DeviceLocationMessage], None]] = []
        self._rejected_callbacks: list[Callable[[RejectedMessage], None]] = []
        self._raw_callbacks: list[Callable[[str, bytes], None]] = []
        self._seen_callbacks: list[Callable[[str, str, datetime], None]] = []
        self._location = LocationDecoder()
        self._credentials_lock = asyncio.Lock()
        self._credentials_attempted_at: float | None = None

        self._state_cache: dict[str, DeviceStateMessage] = {}
        self._attributes_cache: dict[str, DeviceAttributesMessage] = {}
        # When each cached message arrived: time.monotonic() for ages, and the
        # UTC wall-clock time of the state message for consumers that compare
        # observation times across sources.
        self._state_cache_updated_at: dict[str, float] = {}
        self._attributes_cache_updated_at: dict[str, float] = {}
        self._state_cache_received_at: dict[str, datetime] = {}

    @classmethod
    def from_connection_info(
        cls,
        info: MqttConnectionInfo,
        *,
        access_token: str | None,
        records: list[Any],
        auth_headers: dict[str, str] | None = None,
        **options: Any,
    ) -> NavimowSDK:
        """A facade for the broker the credential reply names, connected the way the cloud expects.

        info is MowerAPI.async_get_mqtt_connection_info()'s result, records the
        account's devices. The broker, port, WebSocket path, username and password
        come from info; passing any of them in options is a TypeError. The other
        options (loop, keepalive_seconds, subscribe_location, reject_late_state,
        ...) go to the constructor unchanged. With an access_token the bearer
        header Authorization: Bearer <access_token> is merged into auth_headers,
        replacing an Authorization header given there in any spelling; with
        access_token=None no Authorization header is added and auth_headers is
        passed as given. After an OAuth token refresh, pass the new header with
        update_mqtt_credentials(auth_headers=...) as usual.

        Raises:
            TypeError: options names an argument info supplies.
            ValueError: info has no WebSocket path (ws_path is "").
        """
        owned = [name for name in _CONNECTION_INFO_ARGUMENTS if name in options]
        if owned:
            raise TypeError(
                f"NavimowSDK.from_connection_info() takes {', '.join(owned)} "
                "from the connection info; use the constructor to choose them"
            )
        # Decision: the factory builds the one connection the cloud has been seen to
        # serve, TLS over WebSocket on the reply's port (443 unless named) with the
        # bearer at the upgrade, so a reply without a WebSocket path cannot be built
        # here and is refused rather than guessed at (with "/mqtt", say). A consumer
        # that wants plain TCP or another scheme uses the constructor, whose rules for
        # broker, port, ws_path and TLS are unchanged.
        if not info.ws_path:
            raise ValueError(
                "NavimowSDK.from_connection_info(): the connection info names no "
                "WebSocket path (mqttUrl); use the constructor to choose one"
            )
        headers = {
            key: value
            for key, value in (auth_headers or {}).items()
            if access_token is None or key.lower() != "authorization"
        }
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        return cls(
            broker=info.broker,
            port=info.port,
            username=info.username,
            password=info.password,
            ws_path=info.ws_path,
            auth_headers=headers,
            records=records,
            **options,
        )

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop the callbacks run on: the MQTT client's, which it may bind at connect."""
        return self._mqtt.loop

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
        *,
        broker: str | None = None,
        port: int | None = None,
        ws_path: str | None = None,
        force_reconnect: bool = False,
    ) -> None:
        """Update the MQTT credentials, and the broker's address when it moved.

        Unchanged values are ignored and None means "keep", so a password-only or
        headers-only update is merged with the stored values. While connected, the
        merged values are set on the live paho client and the connection is kept on
        purpose, so hourly OAuth token rotation does not force a disconnect; paho uses
        them at its next connect, automatic reconnects included. While disconnected,
        changed values rebuild the paho client and start an asynchronous reconnect.
        force_reconnect=True rebuilds and reconnects in any case (NavimowMQTT.rebuild).
        broker, port and ws_path (None means keep) that differ from the client's
        rebuild it on the new address, connected or not (NavimowMQTT.update_credentials).
        The rebuilding paths block and must be called off the event loop; every
        path, like connect(), disconnect() and the command methods, waits while a
        rebuild runs on another thread.

        Used after an OAuth token refresh to update the MQTT WebSocket auth header,
        and to update the MQTT username/password issued by the server.
        """
        self._mqtt.update_credentials(
            username=username,
            password=password,
            auth_headers=auth_headers,
            broker=broker,
            port=port,
            ws_path=ws_path,
            force_reconnect=force_reconnect,
        )

    async def async_refresh_broker_credentials(
        self,
        api: MowerAPI,
        *,
        auth_headers: dict[str, str] | None = None,
        force_reconnect: bool = False,
        cooldown: float = 65.0,
    ) -> bool:
        """Fetch the broker username and password from the cloud and apply them.

        When to call it: at startup, then connect() (or construct the facade
        with from_connection_info instead); and after on_connect_fail, where paho's thread
        is still retrying and uses the applied values at its next attempt, or
        at once with force_reconnect=True. Never on a timer, and never on an
        OAuth token refresh, which is update_mqtt_credentials(auth_headers=...)
        alone. It does not refresh the OAuth token: do that first, and pass the
        new bearer header as auth_headers.

        The endpoint allows about one call a minute, so a call within cooldown
        seconds of the last attempt (a failed one included, since the cloud
        counted it) returns False without a request; concurrent calls run one
        at a time. Otherwise userName and pwdInfo from the reply are applied,
        as strings, through update_mqtt_credentials(..., force_reconnect=...),
        run in the default executor because its rebuilding paths block, and
        True is returned. When the reply names a broker host, port or WebSocket
        path (read as MqttConnectionInfo reads them) that differs from the
        client's, they are applied too, and the client is rebuilt on the new
        address whether or not it is connected; a value the reply does not name
        is kept, and a broker the reply names but that cannot be read is logged
        and kept while the credentials are still applied. It does not start a
        connection of its own: unchanged values without force_reconnect leave
        the client alone. Changed values
        on a client that is not connected go through a rebuild, which connects,
        as update_mqtt_credentials always has; a connect() after it is then a
        no-op.

        The loop the call is made from is bound to the MQTT client if none is,
        before any executor work, so the callbacks of a facade constructed
        outside a loop go to the caller's loop. A client bound to another loop
        raises RuntimeError.

        Raises:
            RuntimeError: The client is bound to another event loop.
            MowerAPIError: The request failed (MowerRateLimitedError: too early),
                or the reply carried no credentials.
        """
        running = asyncio.get_running_loop()
        if self._mqtt.loop is None:
            self._mqtt.loop = running
        elif self._mqtt.loop is not running:
            raise RuntimeError(
                f"NavimowSDK is bound to event loop {self._mqtt.loop!r}; "
                f"async_refresh_broker_credentials() was called from another, {running!r}"
            )
        async with self._credentials_lock:
            now = time.monotonic()
            if (
                self._credentials_attempted_at is not None
                and now - self._credentials_attempted_at < cooldown
            ):
                return False
            self._credentials_attempted_at = now
            info = await api.async_get_mqtt_user_info()
            # Read like MqttConnectionInfo reads them (text when present), but without
            # its broker requirement: a reply that names no broker still carries
            # credentials for the one the client has.
            username = _credential(info.get("userName")) if isinstance(info, dict) else None
            password = _credential(info.get("pwdInfo")) if isinstance(info, dict) else None
            if username is None and password is None:
                raise MowerAPIError(
                    f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: no broker credentials in the reply"
                )
            broker = port = ws_path = None
            try:
                broker, port, ws_path = _broker_endpoint(info)
            except MowerAPIError as exc:
                _LOGGER.warning(
                    "Navimow credential reply: broker address kept, the reply's cannot be read: %s",
                    exc,
                )
            # Decision: the reply is authoritative for the broker. A host, port or path
            # it names that differs from the client's rebuilds the client on the new
            # address, dropping a live connection; this helper runs after a failed
            # connect, so the client is normally retrying against the old address
            # already, and on a host the cloud no longer names it never succeeds. What
            # the reply does not name (the port, when mqttHost has none) is kept rather
            # than defaulted, so a client built for another port or transport is not
            # moved by a reply that says nothing about it.
            await running.run_in_executor(
                None,
                functools.partial(
                    self.update_mqtt_credentials,
                    username,
                    password,
                    auth_headers,
                    broker=broker,
                    port=port,
                    ws_path=ws_path,
                    force_reconnect=force_reconnect,
                ),
            )
            return True

    def on_state(self, callback: Callable[[DeviceStateMessage], None]) -> None:
        self._state_callbacks.append(callback)

    def on_event(self, callback: Callable[[DeviceEventMessage], None]) -> None:
        self._event_callbacks.append(callback)

    def on_attributes(self, callback: Callable[[DeviceAttributesMessage], None]) -> None:
        self._attributes_callbacks.append(callback)

    def on_location(self, callback: Callable[[DeviceLocationMessage], None]) -> None:
        """Call callback with each applied entry of a location message, in the order applied.

        Each message carries the merged record as it stood after its entry. The
        channel is subscribed only with subscribe_location=True.
        """
        self._location_callbacks.append(callback)

    def on_rejected(self, callback: Callable[[RejectedMessage], None]) -> None:
        """Call callback once for each message that was not applied cleanly.

        That is a message that was not applied, or was applied with something
        unknown in it. The callback runs after whatever was applied has been
        delivered.
        """
        self._rejected_callbacks.append(callback)

    def on_raw(self, callback: Callable[[str, bytes], None]) -> None:
        """Call callback(topic, payload) for every MQTT message, on any topic.

        The payload is the bytes as received.
        Nothing extra runs per message until the first raw callback is registered.
        """
        self._raw_callbacks.append(callback)
        self._mqtt.on_raw = self._on_mqtt_raw

    def on_message_seen(self, callback: Callable[[str, str, datetime], None]) -> None:
        """Call callback(device_id, channel, received_at) for every message on a device's topic.

        Every message whose topic names a device and a channel counts, whatever
        its payload and whether or not it is applied, with the UTC receipt time
        mqtt.last_message_at() records for it. Nothing extra runs per message
        until the first such callback is registered.
        """
        self._seen_callbacks.append(callback)
        self._mqtt.on_message_seen = self._on_mqtt_message_seen

    def get_cached_location(self, device_id: str) -> DeviceLocation | None:
        """The merged location record for device_id.

        None before any location entry or restore.
        """
        return self._location.get(device_id)

    def restore_location(self, device_id: str, location: DeviceLocation) -> None:
        """Install a location record persisted earlier (DeviceLocation.to_dict / from_dict).

        Call it before connecting, so a late entry older than what was applied
        before a restart is rejected as stale rather than applied.
        """
        self._location.restore(device_id, location)

    def get_cached_state(self, device_id: str) -> DeviceStateMessage | None:
        return self._state_cache.get(device_id)

    def get_cached_attributes(self, device_id: str) -> DeviceAttributesMessage | None:
        return self._attributes_cache.get(device_id)

    def get_cached_state_age(self, device_id: str) -> float | None:
        """Seconds since the cached state message for device_id arrived, or None without one."""
        updated_at = self._state_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_attributes_age(self, device_id: str) -> float | None:
        """Seconds since the cached attributes message for device_id arrived.

        None without one.
        """
        updated_at = self._attributes_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_state_received_at(self, device_id: str) -> datetime | None:
        """The UTC time the cached state message for device_id arrived, or None without one."""
        return self._state_cache_received_at.get(device_id)

    async def _on_mqtt_raw(self, topic: str, payload: bytes) -> None:
        """Call each raw callback with the topic and bytes.

        One that raises is logged and the rest still run.
        """
        for callback in list(self._raw_callbacks):
            try:
                callback(topic, payload)
            except Exception:
                _LOGGER.exception("Navimow raw callback %r failed for topic %s", callback, topic)

    async def _on_mqtt_message_seen(
        self, device_id: str, channel: str, received_at: datetime
    ) -> None:
        """Call each message-seen callback; one that raises is logged and the rest still run."""
        for callback in list(self._seen_callbacks):
            try:
                callback(device_id, channel, received_at)
            except Exception:
                _LOGGER.exception(
                    "Navimow message-seen callback %r failed for device %s channel %s",
                    callback,
                    device_id,
                    channel,
                )

    async def _on_mqtt_message(self, topic: str, payload: bytes, device_id: str) -> None:
        _, channel = parse_topic(topic)
        if channel == "location":
            self._on_location_message(topic, payload, device_id)
            return

        if channel not in ("state", "event", "attributes"):
            return

        received_at = datetime.now(UTC)
        payload_dict = _decode_json(payload)
        if not isinstance(payload_dict, dict):
            self._reject(
                channel, topic, device_id, "unparsable", ["unparsable"], payload, received_at
            )
            return

        payload_dict.setdefault("device_id", device_id)

        if channel == "state":
            self._on_state_payload(topic, payload, payload_dict, received_at)
            return
        if channel == "event":
            msg = DeviceEventMessage.from_dict(payload_dict)
            msg.received_at = received_at
            msg.original = _original_payload(payload)
            self._dispatch(self._event_callbacks, msg, channel)
            return
        msg = DeviceAttributesMessage.from_dict(payload_dict)
        msg.received_at = received_at
        msg.original = _original_payload(payload)
        self._attributes_cache[msg.device_id] = msg
        self._attributes_cache_updated_at[msg.device_id] = time.monotonic()
        self._dispatch(self._attributes_callbacks, msg, channel)

    def _on_state_payload(
        self, topic: str, payload: bytes, payload_dict: dict[str, Any], received_at: datetime
    ) -> None:
        """Apply a state payload unless the late-state filter blocks it; report what it earned.

        A key outside STATE_KNOWN_FIELDS earns unknown_field and never blocks. With
        reject_late_state, a timestamp outside the plausibility window earns
        implausible_time and one older than the device's newest accepted timestamp
        earns stale; both block, and a blocked message names the blocking reason as
        its reason. A message without a timestamp is applied. A payload whose fields
        cannot be read into a DeviceStateMessage (metrics sent as a number, say) is
        unparsable: reported, and neither applied nor cached.
        """
        reasons = []
        if not payload_dict.keys() <= STATE_KNOWN_FIELDS:
            reasons.append("unknown_field")
        try:
            msg = DeviceStateMessage.from_dict(payload_dict)
        except (TypeError, ValueError):
            ordered = [reason for reason in REASON_PRIORITY if reason in ("unparsable", *reasons)]
            self._reject(
                "state",
                topic,
                payload_dict["device_id"],
                "unparsable",
                ordered,
                payload,
                received_at,
            )
            return
        msg.received_at = received_at
        msg.original = _original_payload(payload)
        # The one reason, if any, that keeps the message from being applied.
        blocked = None
        stamp = mower_time_ms(msg.timestamp) if self._reject_late_state else None
        if stamp is not None:
            mark = self._state_marks.get(msg.device_id)
            if not _plausible(stamp, round(received_at.timestamp() * 1000)):
                blocked = "implausible_time"
            elif mark is not None and stamp < mark:
                blocked = "stale"
        if blocked is not None:
            reasons.append(blocked)
        else:
            if stamp is not None:
                self._state_marks[msg.device_id] = stamp  # not stale, so at or above the mark
            self._state_cache[msg.device_id] = msg
            self._state_cache_updated_at[msg.device_id] = time.monotonic()
            self._state_cache_received_at[msg.device_id] = received_at
            self._dispatch(self._state_callbacks, msg, "state")
        if reasons:
            # A message that was not applied names the reason that blocked it; one that
            # was applied names unknown_field.
            ordered = [reason for reason in REASON_PRIORITY if reason in reasons]
            self._reject(
                "state", topic, msg.device_id, blocked or ordered[0], ordered, payload, received_at
            )

    def _on_location_message(self, topic: str, payload: bytes, device_id: str) -> None:
        received_at = datetime.now(UTC)
        # A payload that is not JSON decodes to None, which decode() reports as unparsable.
        value = _decode_json(payload)
        if isinstance(value, dict) and value.get("device_id") == device_id:
            # The MQTT client adds device_id to an object payload; the mower did not send it.
            del value["device_id"]
        parsed = self._location.decode(device_id, value, received_at)
        for message in parsed.messages:
            self._dispatch(self._location_callbacks, message, "location")
        if parsed.reasons:
            self._reject(
                "location",
                topic,
                device_id,
                parsed.reason,
                parsed.reasons,
                payload,
                received_at,
                skipped=tuple(parsed.skipped),
            )

    def _reject(
        self,
        channel: str,
        topic: str,
        device_id: str,
        reason: str | None,
        reasons: list[str],
        payload: bytes,
        received_at: datetime,
        skipped: tuple[SkippedLocationEntry, ...] = (),
    ) -> None:
        rejected = RejectedMessage(
            channel=channel,
            topic=topic,
            device_id=device_id,
            reason=reason or reasons[0],
            reasons=tuple(reasons),
            payload=payload,
            received_at=received_at,
            skipped=skipped,
            original=_original_payload(payload),
        )
        self._dispatch(self._rejected_callbacks, rejected, "rejected")

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
                else _NO_ALTERNATIVE.get(command, _NO_ALTERNATIVE_KNOWN)
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

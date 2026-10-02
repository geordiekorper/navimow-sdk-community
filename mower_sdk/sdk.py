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
    """Facade over the cloud's MQTT channels: typed messages, callbacks and caches.

    A facade owns one NavimowMQTT client (the mqtt property). Each message on
    a device's state, event, attributes or location topic is decoded into a
    typed message and handed to the callbacks registered with on_state,
    on_event, on_attributes and on_location. The latest state message, the
    latest attributes message and the merged location record of each device
    are kept for the get_cached_* methods, and a message that was not applied
    cleanly goes to the on_rejected callbacks. on_raw sees every message as
    received, and on_message_seen every arrival on a device's topic.

    Use: construct it, or build it from the cloud's credential reply with
    from_connection_info; register the callbacks; then connect(). After an
    OAuth token refresh, pass the new header to update_mqtt_credentials; after
    a failed connect, async_refresh_broker_credentials fetches the broker
    credentials again.

    Callbacks are synchronous functions. They are called on the bound event
    loop (the loop property), in the order they were registered: the MQTT
    client hands each message over from paho's network thread, so no callback
    runs on that thread. A consumer whose own loop is not the bound one
    switches to its loop with call_soon_threadsafe or
    run_coroutine_threadsafe. A callback that
    raises is logged with its traceback and does not stop delivery of the same
    message to the callbacks after it. Every delivered message carries
    received_at, the UTC receipt time. The caches are written on the bound
    loop as well.

    The MQTT command methods (start_mowing, pause, return_to_base and
    set_blade_height) raise MowerUnsupportedOperationError unless the facade
    was constructed with allow_experimental_mqtt_commands=True; the supported
    way to command a mower is MowerAPI.async_send_command.

    Attributes:
        loop: The event loop the callbacks run on, or None while none is bound.
        mqtt: The NavimowMQTT client the facade owns.
        is_connected: Whether the MQTT client is connected to the broker.
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
        """Create the facade and its MQTT client; nothing connects until connect().

        Args:
            broker: The broker's host name, or a URL: its host is used, its
                port, when it has one, wins over port, and a wss:// scheme
                turns TLS on.
            port: The broker's port.
            username: The MQTT user name; None for none. The user name and
                password are set on the client only when both are given; an
                empty string is a value.
            password: The MQTT password; None for none.
            ws_path: The WebSocket path. With one, the connection is MQTT over
                WebSocket with TLS; with None or "", MQTT over TCP, with TLS
                only for a wss:// broker.
            auth_headers: Headers sent at the WebSocket upgrade, such as the
                OAuth bearer in Authorization. Used only with ws_path.
            loop: The event loop the callbacks run on. None means the loop
                running at construction, else the loop set as current with
                asyncio.set_event_loop() at that time, else the same two at a
                connect() made while none is bound. A facade constructed and
                connected with no running or current loop must be given one.
            records: The account's devices, objects with an id attribute; the
                topics of each are subscribed. With None or an empty list the
                topics are subscribed with a wildcard in place of the device.
            keepalive_seconds: The MQTT keepalive; at least 30 is used. The
                default is 60 because idle links die after about ten minutes,
                and a ping a minute keeps them alive and finds a dead one
                within about two minutes (NavimowMQTT says more).
            reconnect_min_delay: Seconds paho waits before its first reconnect
                attempt; a negative value is read as 0.
            reconnect_max_delay: The most seconds paho waits between reconnect
                attempts; at least reconnect_min_delay is used.
            allow_experimental_mqtt_commands: True lets start_mowing, pause,
                return_to_base and set_blade_height publish; otherwise they
                raise MowerUnsupportedOperationError.
            subscribe_location: True subscribes each device's location topic
                as well as its state, event and attributes topics.
            extra_topics: Further topic filters, subscribed as given on every
                connect; the on_raw callbacks see their messages.
            reject_late_state: True keeps a state message whose timestamp is
                implausible, or older than the device's newest accepted one,
                from being applied (see on_rejected).

        Raises:
            ValueError: The loop given, or the current loop found in its
                place, is closed; or an extra topic is not a string that is a
                valid MQTT topic filter.
        """
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
        """Build a facade for the broker a credential reply names, set up as the cloud expects.

        The facade is set up for the one connection the cloud has been seen to
        serve: TLS over WebSocket on the reply's port, with the bearer token at
        the upgrade. The broker, port, WebSocket path, username and password
        come from info. A reply without a WebSocket path is refused rather
        than guessed at; a consumer that wants plain TCP or another address
        uses the constructor. The facade is returned unconnected: call
        connect(). After an OAuth token refresh, pass the new header with
        update_mqtt_credentials(auth_headers=...) as usual.

        Args:
            info: MowerAPI.async_get_mqtt_connection_info()'s result.
            access_token: The OAuth access token. The bearer header
                Authorization: Bearer <access_token> is merged into
                auth_headers, replacing an Authorization header given there in
                any spelling. With None no Authorization header is added and
                the headers of auth_headers are passed as given.
            records: The account's devices.
            auth_headers: Further headers for the WebSocket upgrade; None for
                none.
            **options: The constructor's other arguments (loop,
                keepalive_seconds, subscribe_location, reject_late_state,
                ...), passed to it unchanged.

        Returns:
            The new facade, not yet connected.

        Raises:
            TypeError: options names an argument info supplies (broker, port,
                ws_path, username or password).
            ValueError: info has no WebSocket path (ws_path is ""); or the
                constructor refuses an argument, as its docstring says.
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
        """The event loop the callbacks run on, or None while none is bound.

        This reads the MQTT client's binding, which is made at construction
        or, failing that, by a connect() or an
        async_refresh_broker_credentials() made while none is bound. A
        callback that arrives while no loop is bound is dropped with a
        warning.
        """
        return self._mqtt.loop

    @property
    def mqtt(self) -> NavimowMQTT:
        """The MQTT client: its connection hooks, counters, reasons and message times."""
        return self._mqtt

    def connect(self) -> None:
        """Start connecting to the MQTT broker and consuming its messages.

        The connection is made on paho's network thread, so this returns
        before the broker has answered; the topics are subscribed on every
        connect. A call made while that thread runs (connected, connecting or
        retrying after a failure) does nothing. With no loop bound yet, the
        running loop, else the loop set as current, is bound first. The call
        waits while a rebuild of the client runs on another thread.

        Raises:
            RuntimeError: Called from a running event loop other than the
                bound one.
        """
        self._mqtt.connect_async()

    def disconnect(self) -> None:
        """Stop paho's network thread and disconnect from the MQTT broker.

        A call made while a rebuild of the client runs on another thread waits
        for it and then disconnects the new client, so the rebuild cannot
        reconnect afterwards.
        """
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

        Used after an OAuth token refresh to update the MQTT WebSocket auth
        header, and to update the MQTT username and password issued by the
        server.

        Unchanged values are ignored and None means "keep", so a password-only
        or headers-only update is merged with the stored values. While
        connected, the merged values are set on the live paho client and the
        connection is kept on purpose, so hourly OAuth token rotation does not
        force a disconnect; paho uses them at its next connect, automatic
        reconnects included. While disconnected, changed values rebuild the
        paho client and start an asynchronous reconnect
        (NavimowMQTT.update_credentials and NavimowMQTT.rebuild say more).

        The rebuilding paths block and must be called off the event loop.
        Every path waits while a rebuild runs on another thread, as connect()
        and disconnect() do.

        Args:
            username: The new MQTT user name; None keeps the stored one.
            password: The new MQTT password; None keeps the stored one.
            auth_headers: The new WebSocket upgrade headers, which replace
                the stored ones whole; None keeps them.
            broker: The broker's new host name or URL; None keeps it. A
                broker, port or ws_path that differs from the client's
                rebuilds the client on the new address, connected or not,
                dropping a live connection.
            port: The broker's new port; None keeps it.
            ws_path: The new WebSocket path; None keeps it.
            force_reconnect: True rebuilds the client and reconnects in any
                case: whether or not a value changed, and whether or not the
                client is connected.
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
        with from_connection_info instead); and after on_connect_fail, where
        paho's thread is still retrying and uses the applied values at its
        next attempt, or at once with force_reconnect=True. Never on a timer,
        and never on an OAuth token refresh, which is
        update_mqtt_credentials(auth_headers=...) alone. It does not refresh
        the OAuth token: do that first, and pass the new bearer header as
        auth_headers.

        The endpoint allows about one call a minute, so a call within cooldown
        seconds of the last attempt (a failed one included, since the cloud
        counted it) makes no request; concurrent calls run one at a time.
        Otherwise userName and pwdInfo from the reply are applied, as strings,
        through update_mqtt_credentials(..., force_reconnect=...), run in the
        default executor because its rebuilding paths block; when the reply
        carries only one of the two, the stored value of the other is kept.
        When the reply names a broker host, port or WebSocket path (read as
        MqttConnectionInfo reads them) that differs from the client's, they
        are applied too, and the client is rebuilt on the new address whether
        or not it is connected; a value the reply does not name is kept, and a
        broker the reply names but that cannot be read is logged and kept
        while the credentials are still applied.

        It does not start a connection of its own: unchanged values without
        force_reconnect leave the client alone. Changed values on a client
        that is not connected go through a rebuild, which connects, as with
        update_mqtt_credentials; a connect() after it is then a no-op.

        The loop the call is made from is bound to the MQTT client if none is,
        before any executor work, so the callbacks of a facade constructed
        outside a loop go to the caller's loop.

        Args:
            api: The REST client the credentials are fetched with, through its
                async_get_mqtt_user_info.
            auth_headers: The WebSocket upgrade headers to apply with the
                credentials, such as the new bearer header; None keeps the
                stored ones.
            force_reconnect: True rebuilds the client and reconnects at once,
                whether or not a value changed.
            cooldown: Seconds after the last attempt in which a call makes no
                request.

        Returns:
            True when the credentials were fetched and applied; False when the
            call fell within the cooldown and made no request.

        Raises:
            RuntimeError: The client is bound to an event loop other than the
                one the call is made from.
            MowerAPIError: The request failed (MowerRateLimitedError: too
                early), or the reply carried neither a userName nor a pwdInfo.
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
        """Call callback with each state message that is applied.

        The message is cached first, so get_cached_state returns it while the
        callback runs. A state message that is not applied (see on_rejected)
        is not delivered.

        Args:
            callback: A synchronous function taking the DeviceStateMessage.
        """
        self._state_callbacks.append(callback)

    def on_event(self, callback: Callable[[DeviceEventMessage], None]) -> None:
        """Call callback with each event message.

        Event messages are not cached.

        Args:
            callback: A synchronous function taking the DeviceEventMessage.
        """
        self._event_callbacks.append(callback)

    def on_attributes(self, callback: Callable[[DeviceAttributesMessage], None]) -> None:
        """Call callback with each attributes message.

        The message is cached first, so get_cached_attributes returns it
        while the callback runs.

        Args:
            callback: A synchronous function taking the
                DeviceAttributesMessage.
        """
        self._attributes_callbacks.append(callback)

    def on_location(self, callback: Callable[[DeviceLocationMessage], None]) -> None:
        """Call callback with each applied entry of a location message, in the order applied.

        Each message carries the merged record as it stood after its entry. The
        channel is subscribed only with subscribe_location=True.

        Args:
            callback: A synchronous function taking the DeviceLocationMessage.
        """
        self._location_callbacks.append(callback)

    def on_rejected(self, callback: Callable[[RejectedMessage], None]) -> None:
        """Call callback once for each message that was not applied cleanly.

        That is a message that was not applied, or was applied with something
        unknown in it: a state, event or attributes payload that is not a JSON
        object (unparsable); a state payload whose fields cannot be read into
        a DeviceStateMessage (unparsable); a state payload with a field
        outside STATE_KNOWN_FIELDS (unknown_field, still applied); and the
        location channel's reasons, with each location entry that was not
        applied in RejectedMessage.skipped. With reject_late_state=True, a
        state message whose timestamp is implausible (implausible_time) or
        older than the device's newest accepted one (stale) is not applied;
        one without a readable timestamp is. The callback runs after whatever
        was applied has been delivered.

        Args:
            callback: A synchronous function taking the RejectedMessage.
        """
        self._rejected_callbacks.append(callback)

    def on_raw(self, callback: Callable[[str, bytes], None]) -> None:
        """Call callback(topic, payload) for every MQTT message, on any topic.

        The payload is the bytes as received. Nothing extra runs per message
        until the first raw callback is registered; registering one sets the
        MQTT client's on_raw hook to the facade's own.

        Args:
            callback: A synchronous function taking the topic and the payload
                bytes.
        """
        self._raw_callbacks.append(callback)
        self._mqtt.on_raw = self._on_mqtt_raw

    def on_message_seen(self, callback: Callable[[str, str, datetime], None]) -> None:
        """Call callback(device_id, channel, received_at) for every message on a device's topic.

        Every message whose topic names a device and a channel counts, whatever
        its payload and whether or not it is applied, with the UTC receipt time
        mqtt.last_message_at() records for it. Nothing extra runs per message
        until the first such callback is registered; registering one sets the
        MQTT client's on_message_seen hook to the facade's own.

        Args:
            callback: A synchronous function taking the device id, the
                channel and the UTC receipt time.
        """
        self._seen_callbacks.append(callback)
        self._mqtt.on_message_seen = self._on_mqtt_message_seen

    def get_cached_location(self, device_id: str) -> DeviceLocation | None:
        """The merged location record for device_id.

        Args:
            device_id: The device.

        Returns:
            The DeviceLocation as it stands, or None before any location entry
            or restore.
        """
        return self._location.get(device_id)

    def restore_location(self, device_id: str, location: DeviceLocation) -> None:
        """Install a location record persisted earlier (DeviceLocation.to_dict / from_dict).

        Call it before connecting, so a late entry older than what was applied
        before a restart is rejected as stale rather than applied. The record
        replaces the one held for the device, if any.

        Args:
            device_id: The device the record is installed for.
            location: The record, rebuilt with DeviceLocation.from_dict.
        """
        self._location.restore(device_id, location)

    def get_cached_state(self, device_id: str) -> DeviceStateMessage | None:
        """The last state message applied for device_id.

        Args:
            device_id: The device.

        Returns:
            The DeviceStateMessage, or None before any was applied. A state
            message that was not applied (see on_rejected) is not cached.
        """
        return self._state_cache.get(device_id)

    def get_cached_attributes(self, device_id: str) -> DeviceAttributesMessage | None:
        """The last attributes message seen for device_id.

        Args:
            device_id: The device.

        Returns:
            The DeviceAttributesMessage, or None before any arrived.
        """
        return self._attributes_cache.get(device_id)

    def get_cached_state_age(self, device_id: str) -> float | None:
        """Seconds since the cached state message for device_id arrived.

        With it a consumer can tell a stale cache from a fresh one.

        Args:
            device_id: The device.

        Returns:
            The age in seconds, measured on time.monotonic(), or None without
            a cached state message.
        """
        updated_at = self._state_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_attributes_age(self, device_id: str) -> float | None:
        """Seconds since the cached attributes message for device_id arrived.

        Args:
            device_id: The device.

        Returns:
            The age in seconds, measured on time.monotonic(), or None without
            a cached attributes message.
        """
        updated_at = self._attributes_cache_updated_at.get(device_id)
        return None if updated_at is None else time.monotonic() - updated_at

    def get_cached_state_received_at(self, device_id: str) -> datetime | None:
        """The UTC time the cached state message for device_id arrived.

        For a consumer that compares readings across sources: the cloud's REST
        status lags the mower by one to two minutes, so observation times, not
        receipt order, decide which reading is newer.

        Args:
            device_id: The device.

        Returns:
            The message's received_at, or None without a cached state message.
        """
        return self._state_cache_received_at.get(device_id)

    async def _on_mqtt_raw(self, topic: str, payload: bytes) -> None:
        """Call each raw callback with the topic and bytes.

        One that raises is logged and the rest still run. This is the MQTT
        client's on_raw hook once a raw callback is registered.

        Args:
            topic: The topic the message arrived on.
            payload: The payload bytes as received.
        """
        for callback in list(self._raw_callbacks):
            try:
                callback(topic, payload)
            except Exception:
                _LOGGER.exception("Navimow raw callback %r failed for topic %s", callback, topic)

    async def _on_mqtt_message_seen(
        self, device_id: str, channel: str, received_at: datetime
    ) -> None:
        """Call each message-seen callback; one that raises is logged and the rest still run.

        This is the MQTT client's on_message_seen hook once such a callback is
        registered.

        Args:
            device_id: The device id the topic names.
            channel: The channel the topic names.
            received_at: The UTC receipt time the MQTT client recorded.
        """
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
        """Handle one message on a device's topic: decode, cache and deliver it.

        This is the MQTT client's on_message hook. A location message goes to
        _on_location_message, and a message on a channel other than state,
        event, attributes or location is ignored. A payload that is not a JSON
        object is reported to the rejected callbacks as unparsable and nothing
        else is done with it. A payload without a device_id gets the topic's.
        A state payload then goes to _on_state_payload; an event message is
        delivered to the event callbacks; an attributes message is cached and
        delivered to the attributes callbacks. An event or attributes message
        gets received_at, the UTC time read here, and original, the bytes as
        received.

        Args:
            topic: The topic the message arrived on.
            payload: The payload bytes. For a JSON object the MQTT client
                passes its re-encoded form with device_id added, a
                ReceivedPayload that keeps the bytes as received.
            device_id: The device id the topic names.
        """
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
        its reason. A message without a timestamp, or with one mower_time_ms
        cannot read as a positive time, is applied. A payload whose fields
        cannot be read into a DeviceStateMessage (metrics sent as a number, say) is
        unparsable: reported, and neither applied nor cached.

        An applied message gets received_at and original, is cached with its
        receipt times, and is delivered to the state callbacks; with
        reject_late_state its timestamp becomes the device's newest accepted
        one. The rejected callbacks are called after that, once, when the
        message earned any reason.

        Args:
            topic: The topic the message arrived on.
            payload: The payload bytes as the facade received them.
            payload_dict: The payload decoded, a JSON object with device_id in
                it.
            received_at: The UTC receipt time.
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
        """Merge a location message into the device's record and deliver what it did.

        The payload is decoded as JSON; one that is not JSON reaches the
        decoder as None, which it reports as unparsable. A device_id equal to
        the topic's is removed from an object payload first, since the MQTT
        client added it and the mower did not send it. Each applied entry is
        delivered to the location callbacks in the order applied. If the
        decoder gave any reason, one RejectedMessage with the entries that
        were not applied is delivered to the rejected callbacks after them.

        Args:
            topic: The topic the message arrived on.
            payload: The payload bytes as the facade received them.
            device_id: The device id the topic names.
        """
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
        """Build a RejectedMessage and deliver it to the rejected callbacks.

        Args:
            channel: The channel the message arrived on.
            topic: The topic the message arrived on.
            device_id: The device the message is about.
            reason: The deciding reason; None takes the first of reasons.
            reasons: Every reason the message earned; not empty.
            payload: The payload bytes as the facade received them. The bytes
                as the MQTT client received them, kept on a re-encoded
                payload, become the message's original.
            received_at: The UTC receipt time.
            skipped: For a location message, the entries that were not
                applied.
        """
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

        The exception is logged with its traceback. Left to escape, it would
        end the task NavimowMQTT._schedule created and skip the later
        callbacks, and its only trace would be asyncio's "Task exception was
        never retrieved" at loop shutdown or garbage collection.

        Args:
            callbacks: The callbacks to call, in order; the list is copied
                first.
            message: The message to pass; its device_id goes in the log line
                of a failure.
            channel: The channel's name, for the log line of a failure.
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

        The message, with a new id of the form cmd-<uuid4>, is published to
        navimow/{device_id}/command, a topic the broker accepts and no mower
        has been seen to act on. The result of the publish is not checked.

        Args:
            device_id: The device the command is for.
            command: The command's name, as the message carries it.
            params: The command's parameters, as the message carries them.

        Raises:
            MowerUnsupportedOperationError: Unless the facade was constructed
                with allow_experimental_mqtt_commands=True. Raised before the
                MQTT client is touched; the message names the supported
                alternative, when one exists.
            RuntimeError: The MQTT client is not connected. A connect is
                started first, so a later call can succeed; when the call is
                made inside a running event loop other than the bound one,
                that connect itself raises RuntimeError and nothing is
                started.
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
        """Whether the MQTT client is connected to the broker."""
        return self._mqtt.is_connected

    def start_mowing(self, device_id: str) -> None:
        """Publish the experimental MQTT command start_mowing for a device.

        It goes to the MQTT command topic, which the broker accepts and no
        mower has been seen to act on. The supported way to start mowing is
        MowerAPI.async_send_command(device_id, MowerCommand.START), over REST.

        Args:
            device_id: The device.

        Raises:
            MowerUnsupportedOperationError: The facade was not constructed
                with allow_experimental_mqtt_commands=True; nothing is
                published.
            RuntimeError: The MQTT client is not connected. A connect is
                started first, so a later call can succeed; when the call is
                made inside a running event loop other than the bound one,
                that connect itself raises RuntimeError and nothing is
                started.
        """
        self._send_mqtt_command(device_id, "start_mowing", {})

    def pause(self, device_id: str) -> None:
        """Publish the experimental MQTT command pause for a device.

        It goes to the MQTT command topic, which the broker accepts and no
        mower has been seen to act on. The supported way to pause is
        MowerAPI.async_send_command(device_id, MowerCommand.PAUSE), over REST.

        Args:
            device_id: The device.

        Raises:
            MowerUnsupportedOperationError: The facade was not constructed
                with allow_experimental_mqtt_commands=True; nothing is
                published.
            RuntimeError: The MQTT client is not connected. A connect is
                started first, so a later call can succeed; when the call is
                made inside a running event loop other than the bound one,
                that connect itself raises RuntimeError and nothing is
                started.
        """
        self._send_mqtt_command(device_id, "pause", {})

    def return_to_base(self, device_id: str) -> None:
        """Publish the experimental MQTT command return_to_base for a device.

        It goes to the MQTT command topic, which the broker accepts and no
        mower has been seen to act on. The supported way to send a mower to
        its dock is MowerAPI.async_send_command(device_id, MowerCommand.DOCK),
        over REST.

        Args:
            device_id: The device.

        Raises:
            MowerUnsupportedOperationError: The facade was not constructed
                with allow_experimental_mqtt_commands=True; nothing is
                published.
            RuntimeError: The MQTT client is not connected. A connect is
                started first, so a later call can succeed; when the call is
                made inside a running event loop other than the bound one,
                that connect itself raises RuntimeError and nothing is
                started.
        """
        self._send_mqtt_command(device_id, "return_to_base", {})

    def set_blade_height(self, device_id: str, height: int) -> None:
        """Publish the experimental MQTT command set_blade_height for a device.

        It goes to the MQTT command topic, which the broker accepts and no
        mower has been seen to act on. Nothing supported sets the blade
        height: the REST API has no such command.

        Args:
            device_id: The device.
            height: The height, published as given as the command's height
                parameter.

        Raises:
            MowerUnsupportedOperationError: The facade was not constructed
                with allow_experimental_mqtt_commands=True; nothing is
                published.
            RuntimeError: The MQTT client is not connected. A connect is
                started first, so a later call can succeed; when the call is
                made inside a running event loop other than the bound one,
                that connect itself raises RuntimeError and nothing is
                started.
        """
        self._send_mqtt_command(device_id, "set_blade_height", {"height": height})

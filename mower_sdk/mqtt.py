"""MQTT client module.

Provides MQTT connection, subscription and device status updates.
"""

import asyncio
import importlib
import json
import logging
import sys
import threading
import time
import uuid
from urllib.parse import urlparse
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
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
# MowerMQTT and are kept as inventory aliases; MowerMQTT and
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


def _current_event_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop set as current with asyncio.set_event_loop() for this thread, else None.

    The warning filters are never touched, and on the default policy and uvloop no
    loop is created (on 3.14 a custom policy that creates one when asked answers for
    itself). asyncio.get_event_loop() returns a set loop on every version, but with
    none set it creates one on 3.11 (silently, in the main thread) and on 3.12 and
    3.13 (with a DeprecationWarning), so on those versions the policy's thread-local
    slot is read instead of asking it. That is where the default policy and uvloop
    keep the current loop; a custom policy that keeps it elsewhere is not seen, and
    loop= is the way to hand its loop over. On 3.14 the default policy no longer
    creates a loop and get_event_loop() raises instead.
    """
    if sys.version_info >= (3, 14):
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            return None
    local = getattr(asyncio.get_event_loop_policy(), "_local", None)
    loop = getattr(local, "_loop", None)
    return loop if isinstance(loop, asyncio.AbstractEventLoop) else None


def _resolve_event_loop(
    loop: asyncio.AbstractEventLoop | None,
) -> asyncio.AbstractEventLoop | None:
    """Return the explicit loop, else the running loop, else the current loop, else None.

    The current loop is one set with asyncio.set_event_loop() and not yet running:
    what a program that connects first and calls run_forever() afterwards has at
    that point. On the default policy and uvloop no loop is created;
    _current_event_loop says how the current one is found.
    """
    if loop is not None:
        return loop
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return _current_event_loop()


def _build_web_client_id(username: str | None) -> str:
    base = username or "unknown"
    rand = uuid.uuid4().hex[:10]
    return f"web_{base}_{rand}"


_MAX_TOPIC_BYTES = 65_535


def _parse_topic(topic: str) -> tuple[str | None, str | None]:
    """(device id, channel) of a /downlink/vehicle/{id}/realtimeDate/{channel} topic, else (None, None)."""
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


def _decode_json(payload: bytes) -> Any:
    """A message payload decoded as UTF-8 JSON, or None when it is not."""
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError both are
        return None


def _valid_topic(topic: Any) -> str:
    """An extra topic as given, if MQTT can carry it and it is a valid filter; ValueError otherwise.

    paho would refuse the same topic later, inside the connect callback on its
    own thread, where the error reaches nobody.
    """
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"extra topic must be a non-empty string: {topic!r}")
    if "\x00" in topic:
        raise ValueError(f"extra topic must not contain NUL: {topic!r}")
    try:
        encoded = topic.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"extra topic is not encodable as UTF-8: {topic!r}") from exc
    if len(encoded) > _MAX_TOPIC_BYTES:
        raise ValueError(f"extra topic is {len(encoded)} bytes encoded, more than MQTT's {_MAX_TOPIC_BYTES}")
    levels = topic.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise ValueError(f"extra topic uses # other than as the whole last level: {topic!r}")
        if "+" in level and level != "+":
            raise ValueError(f"extra topic uses + other than as a whole level: {topic!r}")
    return topic


def _redact_client_id(client_id: str) -> str:
    """The client id for a log line: web_…_<random suffix>, without the account id in the middle."""
    prefix, _, rest = client_id.partition("_")
    _, _, suffix = rest.rpartition("_")
    return f"{prefix}_…_{suffix}" if prefix and suffix else "…"


def _redact_ws_path(path: str | None) -> str | None:
    """The WebSocket path for a log line: its first segment only (/mqtt/{userId} logs as /mqtt/…)."""
    if not path:
        return path
    first, sep, _ = path.lstrip("/").partition("/")
    return f"/{first}/…" if sep else path


def _configured(value: str | None) -> str:
    """Whether a value is set, for a log line that must not show it."""
    return "configured" if value is not None else "not configured"


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
    """Navimow MQTT client for cloud topics.

    Callbacks are scheduled on ``loop``: the loop passed in, else the loop
    running when the client is constructed, else the loop set as current with
    ``asyncio.set_event_loop()`` at that time, else the same two at the first
    ``connect_async()``. A client constructed and connected with no running or
    current loop must be given ``loop=``; a callback that arrives while no loop
    is bound is dropped with a warning. A closed ``loop=`` is refused with
    ValueError, and ``connect_async()`` called from inside a running loop other
    than the bound one raises RuntimeError: the callbacks would go to a loop the
    caller is not running.

    The connection is observable without wrapping paho's callbacks:
    ``on_connect_fail`` is called with a reason when a connect is refused or
    fails before the broker answers; ``last_connect_fail_reason``,
    ``last_disconnect_reason`` and ``last_connected_at`` keep the latest of
    each; ``connects``, ``disconnects`` and ``connect_failures`` count them
    since construction; ``client_id`` is the id the wire client was built
    with; and ``last_message_at()`` and ``last_message_age()`` say when a
    message last arrived for a device, per channel or across channels. The
    bookkeeping happens whether or not a hook is set.

    ``rebuild()`` replaces the paho client, for example when a watchdog finds
    the link silently dead; ``rebuilds`` and ``last_rebuild_reason`` record
    it. The SDK's own paho callbacks ignore a client that has been replaced;
    callbacks a consumer set directly on the paho object are not guarded.
    ``connect_async()`` starts paho once: while the current client's network
    thread runs (connected, connecting or retrying after a failure) a repeated
    call does nothing.

    ``subscribe_location=True`` subscribes each device's location topic as
    well (off by default: several models never publish on it, and its payload
    is a movement trace). ``extra_topics`` are subscribed as given on every
    connect, for trying topics the protocol reference does not list (such as
    the subTopics names the credential reply advertises); an extra topic that
    overlaps a built-in one can make the broker deliver a message more than
    once (MQTT allows a copy per matching subscription), and a device-scoped wildcard was refused by the
    broker on an X430 in September 2026.

    ``subscription_results`` says what the broker answered for each topic
    subscribed since the latest connect: ``"pending"`` until its
    acknowledgement arrives, then ``"granted"`` or ``"refused: <reason>"``, or
    ``"not sent: <error>"`` when paho could not send the request. A refused
    topic is logged as a warning, and ``on_subscribe(topic, granted, codes)``
    is called for each acknowledged topic with the broker's reason code
    values. Without this a refused subscription is invisible: its data simply
    never arrives. ``on_raw(topic, payload)`` is called
    for every message on any topic with the bytes as received, before
    anything is decoded or added.

    ``keepalive_seconds`` defaults to 60 (at least 30 is used): the cloud's
    idle links die after about ten minutes without a FIN or DISCONNECT, and a
    ping a minute keeps them alive and detects a dead one within about two
    minutes. Pass 2400 for the previous default.
    """

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
        keepalive_seconds: int = 60,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
        subscribe_location: bool = False,
        extra_topics: list[str] | None = None,
    ) -> None:
        parsed = urlparse(broker)
        self.broker = parsed.hostname or broker
        self.port = parsed.port or port
        self.username = username
        self.password = password
        self.records = records
        if loop is not None and loop.is_closed():
            raise ValueError("NavimowMQTT: the loop= given is closed")
        self.loop = _resolve_event_loop(loop)
        self.ws_path = ws_path
        self.auth_headers = auth_headers
        self._use_tls = bool(ws_path) or parsed.scheme == "wss"
        self._client_id = _build_web_client_id(self.username)
        self.keepalive_seconds = max(30, int(keepalive_seconds))
        self.reconnect_min_delay = max(0, int(reconnect_min_delay))
        self.reconnect_max_delay = max(self.reconnect_min_delay, int(reconnect_max_delay))
        self.subscribe_location = subscribe_location
        self.extra_topics = [_valid_topic(topic) for topic in extra_topics or []]

        self.on_connected: Callable[[], Awaitable[None]] | None = None
        self.on_ready: Callable[[], Awaitable[None]] | None = None
        self.on_message: Callable[[str, bytes, str], Awaitable[None]] | None = None
        self.on_disconnected: Callable[[], Awaitable[None]] | None = None
        self.on_connect_fail: Callable[[str], Awaitable[None]] | None = None
        self.on_raw: Callable[[str, bytes], Awaitable[None]] | None = None
        self.on_subscribe: Callable[[str, bool, tuple[int, ...]], Awaitable[None]] | None = None

        self.last_connect_fail_reason: str | None = None
        self.last_disconnect_reason: str | None = None
        self.last_connected_at: datetime | None = None
        self.connects = 0
        self.disconnects = 0
        self.connect_failures = 0
        self.rebuilds = 0
        self.last_rebuild_reason: str | None = None
        self.subscription_results: dict[str, str] = {}
        # message id of a SUBSCRIBE sent -> its topic, until the broker acknowledges it.
        # subscribe_all may run on a caller's thread while paho's thread handles an
        # acknowledgement, so both sides hold the lock.
        self._pending_subscribes: dict[int, str] = {}
        self._subscribe_lock = threading.Lock()
        # True from the paho connect_async/loop_start pair until disconnect() or a
        # rebuild: paho's network thread keeps retrying after a failed connect, so
        # a failure does not clear it.
        self._loop_started = False
        # rebuild(), disconnect(), connect_async() and update_credentials() run one at
        # a time: two rebuilds from different threads would each tear down the same
        # old client and leave one of their new clients running, a disconnect() made
        # during a rebuild would be undone by the rebuild's connect, a connect during
        # one would start the new client while the old is still connected, and a
        # credential update during one would miss the new client. Re-entrant: the
        # update and the rebuild call the others.
        self._lifecycle_lock = threading.RLock()
        # device id -> channel -> (UTC receipt time, time.monotonic() at receipt)
        self._last_message: dict[str, dict[str, tuple[datetime, float]]] = {}

        # self.client is assigned before it is configured: the callback lookups
        # in _configure_client may read it, and a subclass can rely on that.
        self.client = self._new_paho_client()
        self._configure_client(self.client)
        _LOGGER.info(
            "NavimowMQTT init: broker=%s port=%s ws_path=%s tls=%s client_id=%s",
            self.broker,
            self.port,
            _redact_ws_path(self.ws_path),
            self._use_tls,
            _redact_client_id(self._client_id),
        )

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected()

    @property
    def client_id(self) -> str:
        """The MQTT client id the paho client was built with."""
        return self._client_id

    def _last_message_stamp(self, device_id: str, channel: str | None) -> tuple[datetime, float] | None:
        channels = self._last_message.get(device_id, {})
        if channel is not None:
            return channels.get(channel)
        # paho's thread adds channels while this runs on the loop: list() copies the
        # values in one step, and max() then walks the copy, not the live dict.
        return max(list(channels.values()), key=lambda stamp: stamp[1], default=None)

    def last_message_at(self, device_id: str, channel: str | None = None) -> datetime | None:
        """The UTC time the last message for device_id arrived, or None if none has.

        channel names one topic channel ("state", "event", "attributes", ...);
        None gives the newest across all channels. Every message on a topic that
        parses counts, whether or not on_message is set.
        """
        stamp = self._last_message_stamp(device_id, channel)
        return None if stamp is None else stamp[0]

    def last_message_age(self, device_id: str, channel: str | None = None) -> float | None:
        """Seconds (monotonic) since the last message for device_id arrived, or None if none has.

        channel as in last_message_at.
        """
        stamp = self._last_message_stamp(device_id, channel)
        return None if stamp is None else time.monotonic() - stamp[1]

    def _new_paho_client(self) -> mqtt_client.Client:
        """An unconfigured paho client on callback API version 2, with the current client id and transport."""
        return mqtt_client.Client(
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            transport="websockets" if self.ws_path else "tcp",
        )

    def _apply_credentials(self, client: mqtt_client.Client) -> None:
        """Set the current username and password and WebSocket options on client, where they apply."""
        if self._credentials_set():
            client.username_pw_set(self.username, self.password)
        if self.ws_path:
            client.ws_set_options(path=self.ws_path, headers=self.auth_headers or {})

    def _configure_client(self, client: mqtt_client.Client) -> None:
        """Apply the current credentials, WebSocket options, TLS, reconnect delays and callbacks."""
        self._apply_credentials(client)
        if self._use_tls:
            client.tls_set()
        client.reconnect_delay_set(
            min_delay=self.reconnect_min_delay, max_delay=self.reconnect_max_delay
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        client.on_subscribe = self._on_subscribe

    def _credentials_set(self) -> bool:
        """Whether username_pw_set applies: both values given. An empty string is a value."""
        return self.username is not None and self.password is not None

    def _build_new_client(self) -> mqtt_client.Client:
        """Build a new paho MQTT client with the latest credentials and configuration.

        Not called by __init__, which configures self.client in place, so an
        override here takes effect on the next rebuild, not at construction.
        """
        client = self._new_paho_client()
        self._configure_client(client)
        return client

    def update_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
        *,
        force_reconnect: bool = False,
    ) -> None:
        """Update the MQTT credentials.

        Unchanged values are ignored and None means "keep", so a password-only or
        headers-only update, which is what a token refresh sends, is merged with the
        stored values and the current username is kept. While connected, the merged
        values are set on the live paho client with username_pw_set and ws_set_options
        and the connection is kept, so hourly OAuth token rotation does not force a
        disconnect. paho reads the username, password, WebSocket path and headers from
        the client object at every connect, automatic reconnects included, so the next
        reconnect uses the new values without a rebuild. If paho's thread is in the
        middle of a reconnect when they are set, that attempt may use the old values
        and be refused once; the next one uses the new values. While disconnected,
        changed values go through rebuild().

        With force_reconnect=True the merged values go through rebuild() whether or
        not anything changed and whether or not the client is connected, dropping a
        healthy connection on purpose.

        The rebuilding paths block (see rebuild()) and must be called off the event
        loop. The connected, non-forced path does no blocking work of its own, but
        like connect_async() it waits while a rebuild runs on another thread.
        """
        with self._lifecycle_lock:
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

            if force_reconnect:
                self.rebuild(reason="credentials updated, reconnect forced")
                return

            if not changed:
                return

            if self.client.is_connected():
                # The connection is healthy and is kept: hourly token rotation would otherwise
                # cause needless reconnects and "device unavailable". The setters change what
                # paho uses at its next connect, automatic reconnects included, so the merged
                # values (not the arguments: a partial update keeps the current username) apply
                # then without a rebuild.
                self._apply_credentials(self.client)
                _LOGGER.info(
                    "NavimowMQTT credentials updated while connected: set on the client, used at the "
                    "next reconnect: broker=%s port=%s",
                    self.broker,
                    self.port,
                )
                return

            _LOGGER.info(
                "NavimowMQTT credentials updated while disconnected, rebuilding and reconnecting: broker=%s port=%s",
                self.broker,
                self.port,
            )
            self.rebuild(reason="credentials updated while disconnected")

    def rebuild(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        """Replace the paho client with a new one and connect it.

        The given values are merged into the stored ones (None means keep, as in
        update_credentials). The new client is built through _build_new_client with
        a fresh random suffix in its client id and installed as self.client before
        the old one is torn down, so the SDK's own callbacks (on_connect,
        on_disconnect, on_connect_fail, on_message) from the old client, including
        the disconnect paho reports while disconnect() runs, are ignored; one
        already scheduled on the loop came from a live client and is delivered. The old client is then disconnected and its network thread
        stopped; an OSError, RuntimeError or ValueError from either is logged at
        debug level, since the client is being discarded anyway. Building a paho
        client does not connect, so two client objects exist during the teardown
        and never two connections. rebuilds is incremented and reason recorded as
        last_rebuild_reason.

        Callbacks a consumer set directly on the old paho object (on_subscribe,
        on_log and the like) are not guarded: they may still fire from the old
        client during its teardown. Nor are they carried to the new one; set them
        again on self.client.

        This blocks: paho's loop_stop() joins the old network thread and tls_set()
        loads certificates. Call it off the event loop, for example in an executor.
        Rebuilds, disconnect(), connect_async() and update_credentials() run one
        at a time, so rebuilds from two threads leave exactly one running client
        and a call made during a rebuild waits for it.
        """
        with self._lifecycle_lock:
            if username is not None:
                self.username = username
            if password is not None:
                self.password = password
            if auth_headers is not None:
                self.auth_headers = auth_headers

            old = self.client
            self._client_id = _build_web_client_id(self.username)
            self.client = self._build_new_client()
            self._loop_started = False
            _LOGGER.info(
                "NavimowMQTT rebuilding the client: reason=%s broker=%s port=%s client_id=%s",
                reason,
                self.broker,
                self.port,
                _redact_client_id(self._client_id),
            )
            for teardown in (old.disconnect, old.loop_stop):
                try:
                    teardown()
                except (OSError, RuntimeError, ValueError) as exc:
                    _LOGGER.debug("NavimowMQTT old client %s failed: %r", teardown.__name__, exc)
            self.rebuilds += 1
            self.last_rebuild_reason = reason
            self.connect_async()

    def connect_async(self) -> None:
        """Start connecting on paho's network thread; a no-op while that thread runs.

        Waits while a rebuild runs on another thread (see rebuild()).
        """
        with self._lifecycle_lock:
            if self.loop is None:
                # Constructed with no running or current loop: bind the loop this connect
                # is made from, running or set as current, so the usual patterns (construct
                # anywhere, connect from inside the loop; or set the loop, connect, then
                # run_forever) deliver the callbacks there.
                self.loop = _resolve_event_loop(None)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not None and self.loop is not None and running is not self.loop:
                raise RuntimeError(
                    f"NavimowMQTT is bound to event loop {self.loop!r}; connect_async() was called "
                    f"from another running loop, {running!r}. Connect from the bound loop, or "
                    "construct the client in (or with loop=) the loop that will run it."
                )
            if self._loop_started:
                # paho's thread is running for this client: connected, connecting, or
                # waiting to retry. Calling paho's connect_async again would reset the
                # attempt in progress.
                _LOGGER.debug("NavimowMQTT connect already started: broker=%s port=%s", self.broker, self.port)
                return
            if not self.is_connected:
                _LOGGER.info(
                    "NavimowMQTT connect details: transport=%s broker=%s port=%s ws_path=%s tls=%s username=%s auth_headers=%s",
                    "websockets" if self.ws_path else "tcp",
                    self.broker,
                    self.port,
                    _redact_ws_path(self.ws_path),
                    self._use_tls,
                    _configured(self.username),
                    _format_auth_headers(self.auth_headers),
                )
                _LOGGER.info(
                    "NavimowMQTT connecting: broker=%s port=%s ws_path=%s client_id=%s",
                    self.broker,
                    self.port,
                    _redact_ws_path(self.ws_path),
                    _redact_client_id(self._client_id),
                )
                self.client.connect_async(self.broker, self.port, self.keepalive_seconds)
                self.client.loop_start()
                self._loop_started = True

    def disconnect(self) -> None:
        """Stop the network thread and disconnect.

        A disconnect() made while rebuild() runs on another thread waits for it
        and then disconnects the new client, so the rebuild cannot reconnect
        afterwards.
        """
        with self._lifecycle_lock:
            self._loop_started = False
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

    def _topics(self) -> tuple[list[str], list[str]]:
        """The topics subscribe_all subscribes, and the device ids they were built from (none: the wildcard)."""
        channels = ["state", "event", "attributes"]
        if self.subscribe_location:
            channels.append("location")
        device_ids = self._get_device_ids()
        topics = [
            f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
            for device_id in (device_ids or ["+"])
            for channel in channels
        ]
        return topics + self.extra_topics, device_ids

    def subscribe_all(self, product_key: str = "", device_name: str = "") -> None:  # noqa: ARG002
        """Subscribe to the state, event and attributes topics of every known device.

        With subscribe_location, the location topic too; then every extra topic,
        as given. With no device ids known, the device segment is the + wildcard.
        Called on every connect, so the subscriptions survive a reconnect.
        Each topic is recorded in subscription_results as pending until the
        broker answers. product_key and device_name are ignored; they are kept,
        optional, so callers and overrides written against the original
        signature keep working.
        """
        topics, device_ids = self._topics()
        if not device_ids:
            _LOGGER.warning(
                "NavimowMQTT subscribing cloud topics with wildcard: no device ids available"
            )
        else:
            _LOGGER.info(
                "NavimowMQTT subscribing cloud topics for %d device(s)", len(device_ids)
            )
        for topic in topics:
            with self._subscribe_lock:
                result, mid = self.client.subscribe(topic)
                if result == mqtt_client.MQTT_ERR_SUCCESS and mid is not None:
                    self._pending_subscribes[mid] = topic
                    self.subscription_results[topic] = "pending"
                else:
                    self.subscription_results[topic] = f"not sent: {mqtt_client.error_string(result)}"

    def unsubscribe_all(self, product_key: str = "", device_name: str = "") -> None:  # noqa: ARG002
        """Unsubscribe from the topics subscribe_all subscribed to.

        product_key and device_name are ignored, as in subscribe_all.
        """
        topics, device_ids = self._topics()
        if not device_ids:
            _LOGGER.info("NavimowMQTT unsubscribing cloud topics (wildcard)")
        else:
            _LOGGER.info(
                "NavimowMQTT unsubscribing cloud topics for %d device(s)", len(device_ids)
            )
        for topic in topics:
            self.client.unsubscribe(topic)

    def _schedule(self, coro: Awaitable[None]) -> None:
        """Run the callback coroutine on the bound loop, else drop it, closed.

        A dropped coroutine is closed so it does not raise asyncio's "coroutine was
        never awaited" RuntimeWarning at garbage collection. No loop bound at all is
        logged as a warning: nothing will be delivered until loop= is passed or a
        connect is made from inside a loop. A bound loop that is not running (stopped
        at shutdown, say) keeps the debug line, and so does a loop that closes
        between the running check and the hand-over, which paho's thread would
        otherwise die of.
        """
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(asyncio.create_task, coro)
                return
            except RuntimeError:  # "Event loop is closed": it closed under the call
                pass
        if loop is None:
            _LOGGER.warning(
                "NavimowMQTT has no event loop bound, MQTT callback dropped; pass loop= or "
                "connect from inside the loop: broker=%s port=%s",
                self.broker,
                self.port,
            )
        else:
            _LOGGER.debug("Event loop not running, skip scheduling MQTT callback")
        close = getattr(coro, "close", None)
        if close is not None:
            close()

    def _connect_failed(self, reason: str) -> None:
        self.connect_failures += 1
        self.last_connect_fail_reason = reason
        if self.on_connect_fail is not None:
            self._schedule(self.on_connect_fail(reason))

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        """paho's on_connect, callback API version 2: reason_code is a paho ReasonCode."""
        if client is not self.client:
            return  # a client replaced by rebuild()
        if reason_code.is_failure:
            _LOGGER.error("MQTT connection failed: %s (%s)", reason_code, reason_code.value)
            self._connect_failed(f"refused: {reason_code} ({reason_code.value})")
            return
        self.connects += 1
        self.last_connected_at = datetime.now(UTC)
        with self._subscribe_lock:
            # A new session: the broker keeps no subscription of the last one (clean
            # session), and acknowledgements still owed for it will not come.
            self.subscription_results = {}
            self._pending_subscribes.clear()
        _LOGGER.info(
            "NavimowMQTT connected: broker=%s port=%s client_id=%s",
            self.broker,
            self.port,
            _redact_client_id(self._client_id),
        )
        # Called with both arguments so an override with the original two-argument
        # signature keeps working.
        self.subscribe_all("", "")

        if self.on_connected is not None:
            self._schedule(self.on_connected())
        if self.on_ready is not None:
            self._schedule(self.on_ready())

    def _on_connect_fail(self, client, _userdata) -> None:
        """paho's on_connect_fail: no CONNACK at all.

        A network failure, or a bearer token refused at the WebSocket upgrade,
        shows this way. paho keeps retrying with the reconnect delays.
        """
        if client is not self.client:
            return
        _LOGGER.warning(
            "NavimowMQTT connection failed before CONNACK: broker=%s port=%s",
            self.broker,
            self.port,
        )
        self._connect_failed("connection failed before CONNACK")

    def _on_disconnect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        """paho's on_disconnect, callback API version 2."""
        if client is not self.client:
            return
        self.disconnects += 1
        self.last_disconnect_reason = "requested" if not reason_code.is_failure else str(reason_code)
        _LOGGER.debug(
            "NavimowMQTT disconnected: broker=%s port=%s client_id=%s rc=%s",
            self.broker,
            self.port,
            _redact_client_id(self._client_id),
            reason_code,
        )
        if self.on_disconnected is not None:
            self._schedule(self.on_disconnected())

    def _on_subscribe(self, client, _userdata, mid, reason_code_list, _properties=None) -> None:
        """paho's on_subscribe, callback API version 2: one reason code per topic sent.

        subscribe_all sends one topic per request, so the first code decides. A
        code of 0x80 or more is a refusal.
        """
        if client is not self.client:
            return
        with self._subscribe_lock:
            topic = self._pending_subscribes.pop(mid, None)
            if topic is None:
                return  # not sent by subscribe_all, or from before the latest connect
            codes = tuple(int(code.value) for code in reason_code_list)
            refused = next((code for code in reason_code_list if code.is_failure), None)
            granted = bool(reason_code_list) and refused is None
            if granted:
                self.subscription_results[topic] = "granted"
            else:
                reason = f"{refused} ({refused.value})" if refused is not None else "no reason code"
                self.subscription_results[topic] = f"refused: {reason}"
        if not granted:
            _LOGGER.warning(
                "NavimowMQTT subscription refused by the broker: topic=%s reason=%s", topic, reason
            )
        if self.on_subscribe is not None:
            self._schedule(self.on_subscribe(topic, granted, codes))

    _parse_topic = staticmethod(_parse_topic)

    def _on_message(self, client, _userdata, msg) -> None:
        """paho's on_message.

        The payload debug line logs every payload whole. With the location
        channel subscribed that is a movement trace of the mower: keep this
        logger above DEBUG outside troubleshooting.
        """
        if client is not self.client:
            return
        topic = msg.topic
        device_id, channel = self._parse_topic(topic)
        if device_id and channel:
            self._last_message.setdefault(device_id, {})[channel] = (datetime.now(UTC), time.monotonic())

        payload_bytes = msg.payload
        if self.on_raw is not None:
            # Every message, on any topic, as it came off the wire: before decoding and
            # before device_id is added, so an extra topic or an unknown one is seen too.
            self._schedule(self.on_raw(topic, payload_bytes))
        if _LOGGER.isEnabledFor(logging.DEBUG):
            # Decoding the whole payload for the line is the cost: skipped when it would not be logged.
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
        if self.on_message is None or not device_id:
            return

        payload = _decode_json(payload_bytes)
        if isinstance(payload, dict):
            # Re-encoded on purpose: on_message(topic, bytes, device_id) is a public
            # contract, consumers decode the bytes themselves, and integrations wrap
            # this slot expecting the device_id to be present in the payload.
            payload.setdefault("device_id", device_id)
            payload_bytes = json.dumps(payload).encode("utf-8")

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

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
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from paho.mqtt import client as mqtt_client
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from mower_sdk._deprecation import warn_legacy
from mower_sdk.errors import ERROR_MESSAGES, MowerMQTTError
from mower_sdk.models import Device, DeviceStatus

if TYPE_CHECKING:
    from mower_sdk.legacy.mqtt_v1 import MowerMQTT as MowerMQTT
    from mower_sdk.legacy.utils import parse_json as parse_json

# The public surface upstream published from this module: the classes it
# defined plus the names it imported and callers picked up as aliases.
# MowerMQTTError, ERROR_MESSAGES and DeviceStatus were used only by
# MowerMQTT and are kept as inventory aliases; MowerMQTT and
# parse_json now live in mower_sdk.legacy and are served by __getattr__.
__all__ = [
    "ConnectionEvent",
    "MowerMQTT",
    "NavimowMQTT",
    "parse_topic",
    "Device",
    "DeviceStatus",
    "ERROR_MESSAGES",
    "MowerMQTTError",
    "ReceivedPayload",
    "parse_json",
]

_LOGGER = logging.getLogger(__name__)

# What a hook returns: the coroutine of an async function, which the client
# runs as a task on the bound loop.
_HookResult = Coroutine[Any, Any, None]


@dataclass(frozen=True)
class ConnectionEvent:
    """One change in NavimowMQTT's connection, with the context it happened in.

    All of it is fixed when the event happens, not when the callback runs, and
    belongs to the client the event came from, even while a rebuild is building
    its successor.

    Attributes:
        kind: "connected", "disconnected" or "connect_failed".
        client_id: The MQTT client id of the paho client the event came from.
            A rebuild gives the next client a new one, so an event delivered
            after a rebuild still names the client it is about.
        reason: None for connected; for disconnected, "requested" or paho's
            reason, as last_disconnect_reason holds it; for connect_failed,
            the text last_connect_fail_reason holds.
        at: The UTC time the event was recorded, in paho's thread.
        rebuilds: The rebuild count the client started at: 0 for the first
            client, n for the one the nth rebuild built.
    """

    kind: str
    client_id: str
    reason: str | None
    at: datetime
    rebuilds: int


class ReceivedPayload(bytes):
    """The bytes on_message receives for a payload NavimowMQTT re-encoded, with the original kept.

    A JSON object payload gets device_id added and is re-encoded, so the bytes
    are no longer the mower's. It is still bytes, equal to the re-encoded form,
    so a consumer that treats it as bytes sees no change. A payload that was not
    re-encoded (an array, or anything that is not a JSON object) is passed as
    plain bytes, and those are the original.

    Attributes:
        original: The bytes exactly as they came off the wire.
    """

    original: bytes

    def __new__(cls, payload: bytes, original: bytes) -> "ReceivedPayload":
        """Create the bytes from the re-encoded payload and attach the original.

        Args:
            payload: The re-encoded bytes, which become the value.
            original: The bytes as received, kept as the original attribute.

        Returns:
            The new instance.
        """
        instance = super().__new__(cls, payload)
        instance.original = original
        return instance

    def __reduce_ex__(
        self, protocol: object
    ) -> tuple[type["ReceivedPayload"], tuple[bytes, bytes]]:
        """Rebuild from both byte forms, so copy, deepcopy and pickle keep original.

        Args:
            protocol: The pickle protocol; unused.

        Returns:
            The class and the two arguments to call it with: the re-encoded
            bytes as plain bytes, and original.
        """
        return type(self), (bytes(self), self.original)


def _original_payload(payload: bytes) -> bytes:
    """Give the bytes of a payload as they were received.

    Args:
        payload: A payload as on_message receives it: plain bytes or a
            ReceivedPayload.

    Returns:
        payload.original for a ReceivedPayload, else payload itself.
    """
    return payload.original if isinstance(payload, ReceivedPayload) else payload


def _current_event_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop set as current for this thread, without creating one.

    The warning filters are never touched, and on the default policy and uvloop no
    loop is created (on 3.14 a custom policy that creates one when asked answers for
    itself). asyncio.get_event_loop() returns a set loop on every version, but with
    none set it creates one on 3.11 (silently, in the main thread) and on 3.12 and
    3.13 (with a DeprecationWarning), so on those versions the policy's thread-local
    slot is read instead of asking it. That is where the default policy and uvloop
    keep the current loop; a custom policy that keeps it elsewhere is not seen, and
    loop= is the way to hand its loop over. On 3.14 the default policy no longer
    creates a loop and get_event_loop() raises instead.

    Returns:
        The loop set with asyncio.set_event_loop() for this thread, else None.
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

    Args:
        loop: The loop the caller chose, or None to look for one.

    Returns:
        loop when it is given; else the loop running in this thread; else the
        loop set as current for this thread; else None.
    """
    if loop is not None:
        return loop
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return _current_event_loop()


def _build_web_client_id(username: str | None) -> str:
    """Build an MQTT client id of the form web_{username}_{random suffix}.

    Args:
        username: The MQTT username; None or an empty string gives "unknown".

    Returns:
        The client id. The suffix is ten random hexadecimal characters, new
        on every call.
    """
    base = username or "unknown"
    rand = uuid.uuid4().hex[:10]
    return f"web_{base}_{rand}"


_MAX_TOPIC_BYTES = 65_535


def parse_topic(topic: str) -> tuple[str | None, str | None]:
    """The device id and channel of a cloud topic, else (None, None).

    A cloud topic is /downlink/vehicle/{device id}/realtimeDate/{channel}, the
    leading slash optional; the channel is "state", "event", "attributes",
    "location" or whatever else the cloud publishes there.

    Args:
        topic: The topic a message arrived on.

    Returns:
        (device id, channel) for a cloud topic. Either part may come back
        empty for a topic with an empty level; NavimowMQTT keeps message times
        only when both are non-empty. (None, None) for any other topic (an
        extra topic, say).
    """
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


# The name the SDK used before parse_topic was public; kept for code that imported it.
_parse_topic = parse_topic


def _decode_json(payload: bytes) -> Any:
    """Decode a message payload as UTF-8 JSON.

    Args:
        payload: The payload bytes.

    Returns:
        The decoded value, or None when the bytes are not UTF-8 or not JSON.
        A JSON null decodes to None as well.
    """
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError both are
        return None


def _valid_topic(topic: Any) -> str:
    """Check that an extra topic is one MQTT can carry and a valid topic filter.

    paho would refuse the same topic later, inside the connect callback on its
    own thread, where the error reaches nobody.

    Args:
        topic: The extra topic to check.

    Returns:
        The topic as given.

    Raises:
        ValueError: The topic is not a non-empty string, contains NUL, cannot
            be encoded as UTF-8, is longer than 65,535 bytes encoded, uses #
            other than as the whole last level, or uses + other than as a
            whole level.
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
        raise ValueError(
            f"extra topic is {len(encoded)} bytes encoded, more than MQTT's {_MAX_TOPIC_BYTES}"
        )
    levels = topic.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise ValueError(f"extra topic uses # other than as the whole last level: {topic!r}")
        if "+" in level and level != "+":
            raise ValueError(f"extra topic uses + other than as a whole level: {topic!r}")
    return topic


def _redact_client_id(client_id: str) -> str:
    """The client id for a log line, without the account id in the middle.

    Args:
        client_id: An MQTT client id, as _build_web_client_id builds it.

    Returns:
        web_…_<random suffix>: the text before the first underscore and the
        text after the last, around an ellipsis. The ellipsis alone when
        either of the two is empty.
    """
    prefix, _, rest = client_id.partition("_")
    _, _, suffix = rest.rpartition("_")
    return f"{prefix}_…_{suffix}" if prefix and suffix else "…"


def _redact_ws_path(path: str | None) -> str | None:
    """The WebSocket path for a log line: its first segment only.

    A query is never shown: a path read from a full URL in the credential
    reply may carry one.

    Args:
        path: The WebSocket path, or None.

    Returns:
        None or an empty path as given. A path of more than one segment as its
        first segment and an ellipsis (/mqtt/{userId} gives /mqtt/…), any
        query dropped. A path of one segment as given, its query replaced by
        an ellipsis (/mqtt?token=… gives /mqtt?…).
    """
    if not path:
        return path
    path_only, query, _ = path.partition("?")
    first, sep, _ = path_only.lstrip("/").partition("/")
    if sep:
        return f"/{first}/…"
    return f"{path_only}?…" if query else path


def _configured(value: str | None) -> str:
    """Whether a value is set, for a log line that must not show it.

    Args:
        value: The value, or None.

    Returns:
        "configured" for any value other than None, an empty string included;
        "not configured" for None.
    """
    return "configured" if value is not None else "not configured"


def _mask_secret(value: str | None) -> str:
    """Mask a secret for a log line.

    Args:
        value: The secret, or None.

    Returns:
        "<empty>" for None or an empty string; one asterisk per character for
        a value of up to four characters; otherwise the first two and the last
        two characters around three asterisks.
    """
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def _format_auth_headers(headers: dict[str, str] | None) -> str:
    """Format the WebSocket headers for a log line, the Authorization value masked.

    Args:
        headers: The headers, or None.

    Returns:
        "<none>" for None or no headers; otherwise the text of a dict of the
        headers in which the value of a header named Authorization, in any
        letter case, is masked by _mask_secret and every other value is shown.
    """
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

    One paho-mqtt client, over TCP or WebSockets, that subscribes the cloud
    topics of the known devices, records what happens to the connection and
    passes messages and connection changes to the hooks a consumer sets. Set
    the hooks (the on_ attributes below), then call connect_async().

    Threads: connect_async() starts paho's network thread. That thread
    connects, retries after a failed connect and reconnects after a lost
    connection, waiting between reconnect_min_delay and reconnect_max_delay
    seconds, and runs this class's paho callbacks. They subscribe again on
    every connect and keep the counters, timestamps and reasons below, whether
    or not a hook is set. rebuild(), disconnect(), connect_async() and
    update_credentials() run one at a time, whichever threads call them; the
    ones that rebuild block, and must be called off the event loop.

    Event loop: a hook is an async function, and the coroutine it returns is
    run as a task on loop: the loop passed in, else the loop running when the
    client is constructed, else the loop set as current with
    asyncio.set_event_loop() at that time, else the same two at a
    connect_async() made while none is bound. A client constructed and
    connected with no running or current loop must be given loop=; a callback
    that arrives while no loop is bound is dropped with a warning.

    The connection is observable without wrapping paho's callbacks: through
    the hooks, the attributes below, and last_message_at() and
    last_message_age() for when a message last arrived for a device. rebuild()
    replaces the paho client. The SDK's own paho callbacks ignore a client
    that has been replaced; callbacks a consumer set directly on the paho
    object are not guarded.

    Attributes:
        broker: The broker's host name.
        port: The broker's port.
        username: The MQTT username, or None.
        password: The MQTT password, or None.
        records: The devices whose ids build the subscribed topics: the list
            the constructor was given, read at every subscribe.
        loop: The event loop the hooks run on; None while none is bound.
        ws_path: The WebSocket path; None or empty for the TCP transport.
        auth_headers: The headers sent with the WebSocket upgrade, or None.
        keepalive_seconds: The MQTT keepalive in seconds, at least 30.
        reconnect_min_delay: The shortest wait before a reconnect, in seconds.
        reconnect_max_delay: The longest wait before a reconnect, in seconds.
        subscribe_location: Whether each device's location topic is subscribed
            as well.
        extra_topics: The topics subscribed as given on every connect.
        on_connected: Hook called with no argument after each successful
            connect, once the subscriptions are requested. None, as every
            hook is at first, means no call.
        on_ready: Hook called with no argument after each successful connect,
            scheduled right after on_connected.
        on_message: Hook called as on_message(topic, payload, device_id) for
            each message on a cloud topic that names a device. A payload that
            is a JSON object has device_id added, unless it carries one, and
            arrives re-encoded as a ReceivedPayload; any other arrives as the
            bytes received.
        on_disconnected: Hook called with no argument on each disconnect.
        on_connect_fail: Hook called as on_connect_fail(reason) when a connect
            is refused or fails before the broker answers, with the text
            last_connect_fail_reason holds.
        on_raw: Hook called as on_raw(topic, payload) for every message on any
            topic with the bytes as received, before anything is decoded or
            added.
        on_subscribe: Hook called as on_subscribe(topic, granted, codes) for
            each topic the broker acknowledges, with whether it was granted
            and the broker's reason code values.
        on_connection_event: Hook called as on_connection_event(event) with a
            ConnectionEvent for each connect, disconnect and connect failure,
            carrying the client id and reason as they were at that moment.
            The zero-argument on_connected and on_disconnected carry neither,
            and the attributes they would read may have changed by a rebuild
            before they run.
        on_message_seen: Hook called as on_message_seen(device_id, channel,
            received_at) for every message whose topic names both a device and
            a channel (see parse_topic), with the UTC time last_message_at()
            records for it. It serves a consumer that only needs to know that
            a message arrived and would otherwise parse the topic in on_raw
            again.
        last_connect_fail_reason: Why the latest connect failed: "refused: "
            with the broker's reason code and its value, or "connection failed
            before CONNACK" when no answer came. None before any failure.
        last_disconnect_reason: The reason of the latest disconnect:
            "requested" when paho's reason code is not a failure, else that
            reason code as text. None before any disconnect.
        last_connected_at: The UTC time of the latest successful connect, or
            None before the first.
        last_connected_monotonic: last_connected_at on time.monotonic(), the
            clock last_message_age() uses, for measuring how long the client
            has been connected without a wall-clock jump in between.
        last_connect_failed_at: The UTC time of the failure
            last_connect_fail_reason belongs to.
        last_disconnected_at: The UTC time of the disconnect
            last_disconnect_reason belongs to.
        connects: Successful connects since construction.
        disconnects: Disconnects since construction.
        connect_failures: Connect failures since construction.
        rebuilds: Rebuilds since construction.
        last_rebuild_reason: The reason given for the latest rebuild; None
            before any, or when none was given.
        subscription_results: What the broker answered for each topic
            subscribed since the latest connect: "pending" until its
            acknowledgement arrives, then "granted" or "refused: <reason>", or
            "not sent: <error>" when paho could not send the request. Without
            this a refused subscription is invisible: its data simply never
            arrives.
        client: The current paho client. rebuild() replaces it.
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
        """Create the client and its paho client; nothing connects until connect_async().

        Args:
            broker: The broker's host name, or a URL, of which the host is used
                and, when it has one, the port, which wins over port. A wss://
                URL asks for TLS even without a WebSocket path.
            port: The broker's port, used when broker carries none.
            username: The MQTT username. It is also the middle of the client
                id, web_{username}_{random suffix}, where None gives "unknown".
            password: The MQTT password. Username and password are set on the
                paho client only when both are given; an empty string is a
                value, None is not.
            records: The devices whose ids build the subscribed topics. The
                list is kept, not copied, and read at every subscribe.
            ws_path: The WebSocket path. With one, the transport is WebSockets
                and TLS is used; None or empty means TCP.
            auth_headers: Headers sent with the WebSocket upgrade; used only
                with ws_path.
            loop: The event loop the hooks run on. None takes the running
                loop, else the loop set as current, else leaves it to
                connect_async().
            keepalive_seconds: The MQTT keepalive in seconds; at least 30 is
                used. The cloud's idle links die after about ten minutes
                without a FIN or DISCONNECT, and a ping a minute keeps them
                alive and detects a dead one within about two minutes.
            reconnect_min_delay: The shortest wait before a reconnect, in
                seconds; at least 0 is used.
            reconnect_max_delay: The longest wait before a reconnect, in
                seconds; at least reconnect_min_delay is used.
            subscribe_location: True subscribes each device's location topic
                as well. Off by default: several models never publish on it,
                and its payload is a movement trace.
            extra_topics: Topics subscribed as given on every connect, for
                trying topics the protocol reference does not list (such as
                the subTopics names the credential reply advertises). An extra
                topic that overlaps a built-in one can make the broker deliver
                a message more than once (MQTT allows a copy per matching
                subscription), and a device-scoped wildcard was refused by the
                broker on an X430 in September 2026.

        Raises:
            ValueError: loop is closed, or an extra topic is not one MQTT can
                carry or not a valid topic filter.
        """
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
        # A wss:// broker asks for TLS even without a WebSocket path; kept so a later
        # change of path or of a scheme-less host does not lose it.
        self._wss_scheme = parsed.scheme == "wss"
        self._use_tls = bool(ws_path) or self._wss_scheme
        self._client_id = _build_web_client_id(self.username)
        self.keepalive_seconds = max(30, int(keepalive_seconds))
        self.reconnect_min_delay = max(0, int(reconnect_min_delay))
        self.reconnect_max_delay = max(self.reconnect_min_delay, int(reconnect_max_delay))
        self.subscribe_location = subscribe_location
        self.extra_topics = [_valid_topic(topic) for topic in extra_topics or []]

        self.on_connected: Callable[[], _HookResult] | None = None
        self.on_ready: Callable[[], _HookResult] | None = None
        self.on_message: Callable[[str, bytes, str], _HookResult] | None = None
        self.on_disconnected: Callable[[], _HookResult] | None = None
        self.on_connect_fail: Callable[[str], _HookResult] | None = None
        self.on_raw: Callable[[str, bytes], _HookResult] | None = None
        self.on_subscribe: Callable[[str, bool, tuple[int, ...]], _HookResult] | None = None
        self.on_connection_event: Callable[[ConnectionEvent], _HookResult] | None = None
        self.on_message_seen: Callable[[str, str, datetime], _HookResult] | None = None

        self.last_connect_fail_reason: str | None = None
        self.last_disconnect_reason: str | None = None
        self.last_connected_at: datetime | None = None
        self.last_connected_monotonic: float | None = None
        self.last_connect_failed_at: datetime | None = None
        self.last_disconnected_at: datetime | None = None
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

        # Each paho client built -> (its client id, the rebuild count it starts at),
        # recorded when it is installed, so a connection event is attributed to the
        # client it came from even while rebuild() is building the next one (the
        # client id changes first, self.client last).
        self._client_context: weakref.WeakKeyDictionary[Any, tuple[str, int]] = (
            weakref.WeakKeyDictionary()
        )

        # self.client is assigned before it is configured: the callback lookups
        # in _configure_client may read it, and a subclass can rely on that.
        self.client = self._new_paho_client()
        self._client_context[self.client] = (self._client_id, 0)
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
        """Whether the current paho client reports that it is connected."""
        return self.client.is_connected()

    @property
    def client_id(self) -> str:
        """The MQTT client id the paho client was built with."""
        return self._client_id

    def _last_message_stamp(
        self, device_id: str, channel: str | None
    ) -> tuple[datetime, float] | None:
        """Find the receipt stamp of the last message for a device.

        Args:
            device_id: The device.
            channel: One topic channel, or None for the newest stamp across
                all channels.

        Returns:
            The UTC receipt time and the time.monotonic() reading at receipt,
            or None when no message has arrived for the device, or none on
            that channel.
        """
        channels = self._last_message.get(device_id, {})
        if channel is not None:
            return channels.get(channel)
        # paho's thread adds channels while this runs on the loop: list() copies the
        # values in one step, and max() then walks the copy, not the live dict.
        return max(list(channels.values()), key=lambda stamp: stamp[1], default=None)

    def last_message_at(self, device_id: str, channel: str | None = None) -> datetime | None:
        """The UTC time the last message for a device arrived.

        Every message on a cloud topic with a device id and a channel counts,
        whether or not on_message is set.

        Args:
            device_id: The device.
            channel: One topic channel ("state", "event", "attributes", ...);
                None gives the newest across all channels.

        Returns:
            The UTC time of receipt, or None if no such message has arrived.
        """
        stamp = self._last_message_stamp(device_id, channel)
        return None if stamp is None else stamp[0]

    def last_message_age(self, device_id: str, channel: str | None = None) -> float | None:
        """Seconds since the last message for a device arrived, on time.monotonic().

        Args:
            device_id: The device.
            channel: One topic channel, or None for the newest across all
                channels, as in last_message_at.

        Returns:
            The age in seconds, or None if no such message has arrived.
        """
        stamp = self._last_message_stamp(device_id, channel)
        return None if stamp is None else time.monotonic() - stamp[1]

    def _new_paho_client(self) -> mqtt_client.Client:
        """Make an unconfigured paho client on callback API version 2.

        Returns:
            A paho client with the current client id and the transport the
            WebSocket path decides: WebSockets with one, TCP without.
        """
        return mqtt_client.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            transport="websockets" if self.ws_path else "tcp",
        )

    def _apply_credentials(self, client: mqtt_client.Client) -> None:
        """Set the current credentials and WebSocket options on a paho client.

        Each is set only where it applies: the username and password when both
        are given, the WebSocket path and headers when there is a path.

        Args:
            client: The paho client to set them on.
        """
        if self._credentials_set():
            client.username_pw_set(self.username, self.password)
        if self.ws_path:
            client.ws_set_options(path=self.ws_path, headers=self.auth_headers or {})

    def _configure_client(self, client: mqtt_client.Client) -> None:
        """Apply the current credentials, WebSocket options, TLS, reconnect delays and callbacks.

        TLS is set, with paho's defaults, for a client with a WebSocket path
        or a wss:// broker. The callbacks are this object's _on_connect,
        _on_disconnect, _on_connect_fail, _on_message and _on_subscribe.

        Args:
            client: The paho client to configure.
        """
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
        """Say whether username_pw_set applies: both values are given.

        Returns:
            True when neither the username nor the password is None. An empty
            string is a value.
        """
        return self.username is not None and self.password is not None

    def _build_new_client(self) -> mqtt_client.Client:
        """Build a new paho MQTT client with the latest credentials and configuration.

        Not called by __init__, which configures self.client in place, so an
        override here takes effect on the next rebuild, not at construction.

        Returns:
            The configured paho client, not yet connected.
        """
        client = self._new_paho_client()
        self._configure_client(client)
        return client

    def _endpoint_after(
        self, broker: str | None, port: int | None, ws_path: str | None
    ) -> tuple[str, int, str | None, bool]:
        """Work out the address once the given values are merged into the current one.

        Nothing is stored.

        Args:
            broker: A new broker, read as the constructor reads it: a URL's
                host and, when it has one, its port, which wins over port. A
                URL with a scheme also decides the wss flag; a bare host keeps
                the current one. None keeps the current broker.
            port: A new port; None keeps the current one.
            ws_path: A new WebSocket path; None keeps the current one.

        Returns:
            The merged (broker, port, ws_path, wss scheme), the last being
            whether the broker was given as a wss:// URL.
        """
        new_broker, new_port, new_path, wss = self.broker, self.port, self.ws_path, self._wss_scheme
        if broker is not None:
            parsed = urlparse(broker)
            new_broker = parsed.hostname or broker
            if parsed.scheme:
                wss = parsed.scheme == "wss"
            if parsed.port:
                port = parsed.port
        if port is not None:
            new_port = int(port)
        if ws_path is not None:
            new_path = ws_path
        return new_broker, new_port, new_path, wss

    def _endpoint_differs(self, broker: str | None, port: int | None, ws_path: str | None) -> bool:
        """Say whether merging the given values changes where the client connects.

        Args:
            broker: A new broker host or URL, or None to keep the current one.
            port: A new port, or None to keep the current one.
            ws_path: A new WebSocket path, or None to keep the current one.

        Returns:
            True when the merged broker, port, WebSocket path or wss scheme
            (see _endpoint_after) differs from the current one. Host names are
            compared without regard to case.
        """
        new_broker, new_port, new_path, wss = self._endpoint_after(broker, port, ws_path)
        return (new_broker.lower(), new_port, new_path, wss) != (
            self.broker.lower(),
            self.port,
            self.ws_path,
            self._wss_scheme,
        )

    def update_credentials(
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
        """Update the MQTT credentials and, when given, the broker address.

        None means keep and a value equal to the stored one is ignored, so a
        password-only or headers-only update, which is what a token refresh
        sends, is merged with the stored values and the current username is
        kept. An update that changes nothing does nothing, unless it is forced.

        While connected, the merged values are set on the live paho client with
        username_pw_set and ws_set_options and the connection is kept, so hourly
        OAuth token rotation does not force a disconnect. paho reads the
        username, password, WebSocket path and headers from the client object at
        every connect, automatic reconnects included, so the next reconnect uses
        the new values without a rebuild. If paho's thread is in the middle of a
        reconnect when they are set, that attempt may use the old values and be
        refused once; the next one uses the new values. While disconnected,
        changed values go through rebuild(), with the reason "credentials
        updated while disconnected".

        broker, port and ws_path move the client to another address. A live
        client cannot change address, so when any of them differs from the
        current value the update goes through rebuild(), with the reason "broker
        changed", whether or not the client is connected, dropping a live
        connection.

        The rebuilding paths block (see rebuild()) and must be called off the
        event loop. The connected, non-forced path does no blocking work of its
        own, but like connect_async() it waits while a rebuild runs on another
        thread.

        Args:
            username: The new MQTT username; None keeps the current one.
            password: The new MQTT password; None keeps the current one.
            auth_headers: The new WebSocket headers; None keeps the current
                ones.
            broker: The new broker host or URL, read as the constructor reads
                it; None keeps the current one.
            port: The new port; None keeps the current one.
            ws_path: The new WebSocket path; None keeps the current one.
            force_reconnect: True sends the merged values through rebuild(),
                with the reason "credentials updated, reconnect forced",
                whether or not anything changed and whether or not the client
                is connected, dropping a healthy connection on purpose. A
                changed broker address comes first: the update then rebuilds
                with the reason "broker changed", forced or not.

        Raises:
            RuntimeError: A rebuilding path was taken from inside a running
                event loop other than the bound one; connect_async() raises it
                at the end of the rebuild.
        """
        with self._lifecycle_lock:
            if self._endpoint_differs(broker, port, ws_path):
                # Decision: a new address always rebuilds, connected or not. paho keeps
                # the host, port and path of the client it connected with, so setting
                # them on a live client would change nothing until a rebuild, and a
                # client left on a host the cloud no longer names can never reconnect.
                # A live connection to the old address is dropped.
                self.rebuild(
                    username,
                    password,
                    auth_headers,
                    broker=broker,
                    port=port,
                    ws_path=ws_path,
                    reason="broker changed",
                )
                return
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
                    "NavimowMQTT credentials updated while connected: set on the client, "
                    "used at the next reconnect: broker=%s port=%s",
                    self.broker,
                    self.port,
                )
                return

            _LOGGER.info(
                "NavimowMQTT credentials updated while disconnected, "
                "rebuilding and reconnecting: broker=%s port=%s",
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
        broker: str | None = None,
        port: int | None = None,
        ws_path: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Replace the paho client with a new one and connect it.

        For a connection that has to be made anew, for example when a watchdog
        finds the link silently dead. The given values are merged into the
        stored ones; a change of broker, port or ws_path is logged with the old
        and the new address, the path redacted, and TLS follows the new values
        as at construction. The new client is built through _build_new_client
        with a fresh random suffix in its client id and installed as self.client
        before the old one is torn down, so the SDK's own callbacks (on_connect,
        on_disconnect, on_connect_fail, on_message, on_subscribe) from the old
        client, including the disconnect paho reports while disconnect() runs,
        are ignored; one already scheduled on the loop came from a live client
        and is delivered. The old client is then disconnected and its network
        thread stopped; an OSError, RuntimeError or ValueError from either is
        logged at debug level, since the client is being discarded anyway.
        Building a paho client does not connect, so two client objects exist
        during the teardown and never two connections. rebuilds is incremented,
        reason recorded as last_rebuild_reason, and connect_async() starts the
        new client.

        Callbacks a consumer set directly on the old paho object (on_log,
        on_publish and the like) are not guarded: they may still fire from the
        old client during its teardown. Nor are they carried to the new one; set
        them again on self.client.

        This blocks: paho's loop_stop() joins the old network thread and
        tls_set() loads certificates. Call it off the event loop, for example in
        an executor. Rebuilds, disconnect(), connect_async() and
        update_credentials() run one at a time, so rebuilds from two threads
        leave exactly one running client and a call made during a rebuild waits
        for it.

        Args:
            username: A new MQTT username; None keeps the current one.
            password: A new MQTT password; None keeps the current one.
            auth_headers: New WebSocket headers; None keeps the current ones.
            broker: A new broker, read as the constructor reads it: a URL's
                host, and its port when it has one. None keeps the current
                one.
            port: A new port; None keeps the current one.
            ws_path: A new WebSocket path; None keeps the current one.
            reason: Why the client is rebuilt, for the log line and
                last_rebuild_reason; None records None.

        Raises:
            RuntimeError: Called from inside a running event loop other than
                the bound one. connect_async() raises it after the old client
                is torn down and the new one installed, so the new client is
                left unstarted.
        """
        with self._lifecycle_lock:
            if username is not None:
                self.username = username
            if password is not None:
                self.password = password
            if auth_headers is not None:
                self.auth_headers = auth_headers
            if self._endpoint_differs(broker, port, ws_path):
                old_address = (self.broker, self.port, _redact_ws_path(self.ws_path))
                self.broker, self.port, self.ws_path, self._wss_scheme = self._endpoint_after(
                    broker, port, ws_path
                )
                self._use_tls = bool(self.ws_path) or self._wss_scheme
                _LOGGER.info(
                    "NavimowMQTT broker changed: from broker=%s port=%s ws_path=%s "
                    "to broker=%s port=%s ws_path=%s tls=%s",
                    *old_address,
                    self.broker,
                    self.port,
                    _redact_ws_path(self.ws_path),
                    self._use_tls,
                )

            old = self.client
            self._client_id = _build_web_client_id(self.username)
            self.client = self._build_new_client()
            self._client_context[self.client] = (self._client_id, self.rebuilds + 1)
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

        paho is started once: while the current client's network thread runs
        (connected, connecting or retrying after a failure) a repeated call
        does nothing, and neither does a call made while the paho client
        reports that it is connected. How the connect ends is reported through
        the hooks and the attributes, not here. A client with no event loop
        bound binds one first: the running loop, else the loop set as current.
        Waits while a rebuild runs on another thread (see rebuild()).

        Raises:
            RuntimeError: Called from inside a running event loop other than
                the bound one: the callbacks would go to a loop the caller is
                not running.
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
                _LOGGER.debug(
                    "NavimowMQTT connect already started: broker=%s port=%s", self.broker, self.port
                )
                return
            if not self.is_connected:
                _LOGGER.info(
                    "NavimowMQTT connect details: transport=%s broker=%s port=%s ws_path=%s "
                    "tls=%s username=%s auth_headers=%s",
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

        paho's network thread is stopped first, then the current paho client
        is disconnected. A disconnect() made while rebuild() runs on another
        thread waits for it and then disconnects the new client, so the rebuild
        cannot reconnect afterwards.
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
        """List the ids of the devices in records.

        Returns:
            The id of each record, in order, leaving out a record with no id
            attribute or an empty one.
        """
        device_ids: list[str] = []
        for device in self.records:
            device_id = getattr(device, "id", None)
            if device_id:
                device_ids.append(device_id)
        return device_ids

    def _topics(self) -> tuple[list[str], list[str]]:
        """The topics subscribe_all subscribes, and the device ids they were built from.

        Returns:
            The topics, then the device ids. The topics are, for each device
            id, its state, event and attributes topics, and its location topic
            with subscribe_location; then the extra topics. With no device id
            the device level of the topics is the + wildcard and the list of
            ids is empty.
        """
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
        Each topic is sent in a request of its own and recorded in
        subscription_results: as "pending" until the broker answers, or as
        "not sent: <error>" when paho could not send the request.

        Args:
            product_key: Ignored. It and device_name are kept, optional, so
                callers and overrides written against the original signature
                keep working.
            device_name: Ignored.
        """
        topics, device_ids = self._topics()
        if not device_ids:
            _LOGGER.warning(
                "NavimowMQTT subscribing cloud topics with wildcard: no device ids available"
            )
        else:
            _LOGGER.info("NavimowMQTT subscribing cloud topics for %d device(s)", len(device_ids))
        for topic in topics:
            with self._subscribe_lock:
                result, mid = self.client.subscribe(topic)
                if result == mqtt_client.MQTT_ERR_SUCCESS and mid is not None:
                    self._pending_subscribes[mid] = topic
                    self.subscription_results[topic] = "pending"
                else:
                    self.subscription_results[topic] = (
                        f"not sent: {mqtt_client.error_string(result)}"
                    )

    def unsubscribe_all(self, product_key: str = "", device_name: str = "") -> None:  # noqa: ARG002
        """Unsubscribe from the topics subscribe_all subscribes.

        The topics are worked out again from the current records and settings.
        subscription_results is left as it is.

        Args:
            product_key: Ignored, as in subscribe_all.
            device_name: Ignored, as in subscribe_all.
        """
        topics, device_ids = self._topics()
        if not device_ids:
            _LOGGER.info("NavimowMQTT unsubscribing cloud topics (wildcard)")
        else:
            _LOGGER.info("NavimowMQTT unsubscribing cloud topics for %d device(s)", len(device_ids))
        for topic in topics:
            self.client.unsubscribe(topic)

    def _schedule(self, coro: _HookResult) -> None:
        """Run the callback coroutine on the bound loop, else drop it, closed.

        On a bound, running loop the coroutine is handed over with
        call_soon_threadsafe and runs there as a task. Otherwise it is dropped,
        and closed so it does not raise asyncio's "coroutine was never awaited"
        RuntimeWarning at garbage collection. No loop bound at all is logged as
        a warning: nothing will be delivered until loop= is passed or a connect
        is made from inside a loop. A bound loop that is not running (stopped
        at shutdown, say) is logged at debug level, and so is a loop that
        closes between the running check and the hand-over, which paho's thread
        would otherwise die of.

        Args:
            coro: The coroutine a hook returned.
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

    def _connection_event(self, client: Any, kind: str, reason: str | None, at: datetime) -> None:
        """Schedule on_connection_event, if set, with the context of the client the event came from.

        Args:
            client: The paho client the event came from. Its client id and
                rebuild count go into the event; a client this object did not
                install (a consumer replaced self.client) is described by the
                current client id and rebuild count.
            kind: "connected", "disconnected" or "connect_failed".
            reason: The event's reason, as ConnectionEvent describes it.
            at: The stamp the matching attribute holds (last_connected_at,
                last_disconnected_at or last_connect_failed_at), so the event
                and the attribute agree.
        """
        if self.on_connection_event is not None:
            client_id, rebuilds = self._client_context.get(client, (self._client_id, self.rebuilds))
            event = ConnectionEvent(
                kind=kind, client_id=client_id, reason=reason, at=at, rebuilds=rebuilds
            )
            self._schedule(self.on_connection_event(event))

    def _connect_failed(self, client: Any, reason: str) -> None:
        """Record a failed connect and tell the hooks.

        Counts it in connect_failures, sets last_connect_fail_reason and
        last_connect_failed_at, and schedules on_connect_fail with the reason
        and a "connect_failed" connection event, each if its hook is set.

        Args:
            client: The paho client the failure came from.
            reason: Why the connect failed, as last_connect_fail_reason holds
                it.
        """
        self.connect_failures += 1
        self.last_connect_fail_reason = reason
        self.last_connect_failed_at = datetime.now(UTC)
        if self.on_connect_fail is not None:
            self._schedule(self.on_connect_fail(reason))
        self._connection_event(client, "connect_failed", reason, self.last_connect_failed_at)

    def _on_connect(
        self,
        client: mqtt_client.Client,
        _userdata: Any,
        _flags: mqtt_client.ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None = None,
    ) -> None:
        """Handle paho's on_connect, callback API version 2: the broker answered a connect.

        A refusal is logged as an error and recorded as a connect failure with
        the reason "refused: <reason code> (<its value>)". A success is counted
        in connects and stamped in last_connected_at and
        last_connected_monotonic; subscription_results and the acknowledgements
        still awaited are cleared, since the broker keeps no subscription of
        the last session and will not answer for it; subscribe_all is called;
        and on_connected, on_ready and a "connected" connection event are
        scheduled, each if its hook is set.

        Args:
            client: The paho client the callback came from. A client other
                than self.client, one replaced by rebuild(), is ignored.
            _userdata: paho's user data; unused.
            _flags: paho's connect flags; unused.
            reason_code: paho's ReasonCode for the broker's answer; a failure
                means the connect was refused.
            _properties: The MQTT 5 properties paho passes; unused.
        """
        if client is not self.client:
            return  # a client replaced by rebuild()
        if reason_code.is_failure:
            _LOGGER.error("MQTT connection failed: %s (%s)", reason_code, reason_code.value)
            self._connect_failed(client, f"refused: {reason_code} ({reason_code.value})")
            return
        self.connects += 1
        self.last_connected_at = datetime.now(UTC)
        self.last_connected_monotonic = time.monotonic()
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
        self._connection_event(client, "connected", None, self.last_connected_at)

    def _on_connect_fail(self, client: mqtt_client.Client, _userdata: Any) -> None:
        """Handle paho's on_connect_fail: no CONNACK at all.

        A network failure, or a bearer token refused at the WebSocket upgrade,
        shows this way. paho keeps retrying with the reconnect delays. The
        failure is logged as a warning and recorded as a connect failure with
        the reason "connection failed before CONNACK".

        Args:
            client: The paho client the callback came from. A client other
                than self.client, one replaced by rebuild(), is ignored.
            _userdata: paho's user data; unused.
        """
        if client is not self.client:
            return
        _LOGGER.warning(
            "NavimowMQTT connection failed before CONNACK: broker=%s port=%s",
            self.broker,
            self.port,
        )
        self._connect_failed(client, "connection failed before CONNACK")

    def _on_disconnect(
        self,
        client: mqtt_client.Client,
        _userdata: Any,
        _flags: mqtt_client.DisconnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None = None,
    ) -> None:
        """Handle paho's on_disconnect, callback API version 2.

        The disconnect is counted in disconnects and its reason and time kept
        in last_disconnect_reason and last_disconnected_at: "requested" when
        the reason code is not a failure, else the reason code as text. Then
        on_disconnected and a "disconnected" connection event are scheduled,
        each if its hook is set.

        Args:
            client: The paho client the callback came from. A client other
                than self.client, one replaced by rebuild(), is ignored.
            _userdata: paho's user data; unused.
            _flags: paho's disconnect flags; unused.
            reason_code: paho's ReasonCode for the disconnect.
            _properties: The MQTT 5 properties paho passes; unused.
        """
        if client is not self.client:
            return
        self.disconnects += 1
        self.last_disconnect_reason = (
            "requested" if not reason_code.is_failure else str(reason_code)
        )
        self.last_disconnected_at = datetime.now(UTC)
        _LOGGER.debug(
            "NavimowMQTT disconnected: broker=%s port=%s client_id=%s rc=%s",
            self.broker,
            self.port,
            _redact_client_id(self._client_id),
            reason_code,
        )
        if self.on_disconnected is not None:
            self._schedule(self.on_disconnected())
        self._connection_event(
            client, "disconnected", self.last_disconnect_reason, self.last_disconnected_at
        )

    def _on_subscribe(
        self,
        client: mqtt_client.Client,
        _userdata: Any,
        mid: int,
        reason_code_list: list[ReasonCode],
        _properties: Properties | None = None,
    ) -> None:
        """Handle paho's on_subscribe, callback API version 2: one reason code per topic sent.

        subscribe_all sends one topic per request, so one code is expected. A
        code of 0x80 or more is a refusal, and so is an acknowledgement with no
        code at all. The topic's entry in subscription_results becomes
        "granted" or "refused: <reason>", a refusal is logged as a warning, and
        on_subscribe is scheduled, if set, with the topic, whether it was
        granted and the value of every code. An acknowledgement for a request
        subscribe_all did not send, or one sent before the latest connect, is
        ignored.

        Args:
            client: The paho client the callback came from. A client other
                than self.client, one replaced by rebuild(), is ignored.
            _userdata: paho's user data; unused.
            mid: The message id of the SUBSCRIBE the broker acknowledges,
                which names the topic.
            reason_code_list: paho's ReasonCode for each topic of the request.
            _properties: The MQTT 5 properties paho passes; unused.
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

    # Kept for subclasses and callers that used the method before parse_topic was public.
    _parse_topic = staticmethod(parse_topic)

    def _on_message(
        self, client: mqtt_client.Client, _userdata: Any, msg: mqtt_client.MQTTMessage
    ) -> None:
        """Handle paho's on_message: record the message and pass it to the hooks.

        A message whose topic names both a device and a channel has its receipt
        time recorded for last_message_at() and last_message_age(), and
        on_message_seen is scheduled with that same time. on_raw is scheduled
        for every message, and on_message for a cloud topic that names a
        device, with the payload its entry under Attributes describes; each
        only if its hook is set.

        The payload debug line logs every payload whole. With the location
        channel subscribed that is a movement trace of the mower: keep this
        logger above DEBUG outside troubleshooting.

        Args:
            client: The paho client the callback came from. A client other
                than self.client, one replaced by rebuild(), is ignored.
            _userdata: paho's user data; unused.
            msg: paho's message; its topic and payload are read.
        """
        if client is not self.client:
            return
        topic = msg.topic
        device_id, channel = self._parse_topic(topic)
        if device_id and channel:
            received_at = datetime.now(UTC)
            self._last_message.setdefault(device_id, {})[channel] = (received_at, time.monotonic())
            if self.on_message_seen is not None:
                # The same condition as the message times, both parts non-empty, so a
                # consumer's own record of "a message arrived" agrees with
                # last_message_at(); and the stamp stored there, not a second reading
                # of the clock. Scheduled like on_raw, with no order promised between
                # the two or with on_message.
                self._schedule(self.on_message_seen(device_id, channel, received_at))

        payload_bytes = msg.payload
        if self.on_raw is not None:
            # Every message, on any topic, as it came off the wire: before decoding and
            # before device_id is added, so an extra topic or an unknown one is seen too.
            self._schedule(self.on_raw(topic, payload_bytes))
        if _LOGGER.isEnabledFor(logging.DEBUG):
            # Decoding the whole payload for the line is the cost: skipped when it
            # would not be logged.
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
            # this slot expecting the device_id to be present in the payload. The
            # bytes the mower sent ride along as .original.
            payload.setdefault("device_id", device_id)
            payload_bytes = ReceivedPayload(json.dumps(payload).encode("utf-8"), payload_bytes)

        self._schedule(self.on_message(topic, payload_bytes, device_id))

    def publish_command(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish a command for a device on navimow/{device_id}/command.

        The payload is sent as JSON through the current paho client. Whether
        the client is connected is not checked, and what paho returns for the
        publish is not looked at.

        Args:
            device_id: The device the command is for.
            payload: The command, a dict that json.dumps can encode.
        """
        topic = f"navimow/{device_id}/command"
        self.client.publish(topic, json.dumps(payload))


# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
_LEGACY_NAMES = {
    "MowerMQTT": ("mqtt_v1", "MowerMQTT"),
    "parse_json": ("utils", "parse_json"),
}


def __getattr__(name: str) -> Any:
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module.

    Args:
        name: The module attribute that was not found: "MowerMQTT" or
            "parse_json" for the names served.

    Returns:
        The object of that name from its module under mower_sdk.legacy. It is
        also stored in this module's globals, so the next access finds it
        there.

    Raises:
        AttributeError: name is not one of the names that moved.
        DeprecationWarning: A warnings filter turns the warning into an error
            (see warn_legacy); nothing is imported or stored then.
    """
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value

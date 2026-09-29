"""Characterisation tests for NavimowMQTT's client setup and message handling.

A recording fake replaces ``paho.mqtt.client.Client``, so nothing connects.
Most tests run inside ``asyncio.run``, so the client binds the running loop at
construction and the callbacks it schedules can be drained; the loop-binding
tests at the end construct outside a loop on purpose.

They pin what the MQTT setup refactor must keep: the setup calls ``__init__``
makes are the ones ``_build_new_client`` makes; the topics ``subscribe_all``
subscribes with and without device ids; ``_on_connect`` calls
``subscribe_all`` with two arguments; ``_on_message`` injects ``device_id``
and re-encodes the payload; two things a subclass can observe about
construction: ``self.client`` is assigned before the callback attributes are
read, and an override of ``_build_new_client`` is not called; and how the
event loop is bound: at construction when one is running or set as current,
else at the first ``connect_async()``, with an explicit ``loop=`` winning over
both, and a callback with no loop to run on dropped, closed, with a warning.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from mower_sdk import mqtt as mqtt_module
from mower_sdk.models import Device
from mower_sdk.mqtt import NavimowMQTT
from mower_sdk.sdk import NavimowSDK

Call = tuple[str, tuple[Any, ...], dict[str, Any]]
VERSION2 = mqtt_module.mqtt_client.CallbackAPIVersion.VERSION2


class FakeClient:
    """Records every paho call made on it; connects to nothing."""

    instances: list[FakeClient] = []
    # Every call on every instance, in order, for tests about the order across clients.
    events: list[tuple[FakeClient, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[Call] = [("__init__", args, kwargs)]
        self.connected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        FakeClient.instances.append(self)
        FakeClient.events.append((self, "__init__"))

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        FakeClient.events.append((self, name))

    def username_pw_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("username_pw_set", *args, **kwargs)

    def ws_set_options(self, *args: Any, **kwargs: Any) -> None:
        self._record("ws_set_options", *args, **kwargs)

    def tls_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("tls_set", *args, **kwargs)

    def reconnect_delay_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("reconnect_delay_set", *args, **kwargs)

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        self._record("subscribe", *args, **kwargs)

    def unsubscribe(self, *args: Any, **kwargs: Any) -> None:
        self._record("unsubscribe", *args, **kwargs)

    def connect_async(self, *args: Any, **kwargs: Any) -> None:
        self._record("connect_async", *args, **kwargs)

    def loop_start(self) -> None:
        self._record("loop_start")

    def loop_stop(self) -> None:
        self._record("loop_stop")

    def disconnect(self) -> None:
        self._record("disconnect")
        self.connected = False

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self._record("publish", *args, **kwargs)

    def is_connected(self) -> bool:
        return self.connected

    def named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call[0] == name]

    @property
    def callbacks(self) -> tuple[Any, Any, Any, Any]:
        return (self.on_connect, self.on_disconnect, self.on_message, getattr(self, "on_connect_fail", None))


class FakeReasonCode:
    """The parts of paho's ReasonCode the client reads."""

    def __init__(self, value: int, name: str) -> None:
        self.value = value
        self.is_failure = value >= 0x80
        self._name = name

    def __str__(self) -> str:
        return self._name


SUCCESS = FakeReasonCode(0, "Success")
NOT_AUTHORIZED = FakeReasonCode(135, "Not authorized")
UNSPECIFIED = FakeReasonCode(128, "Unspecified error")


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances = []
    FakeClient.events = []
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakeClient)
    return FakeClient


def run(test: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(test())


async def drain() -> None:
    """Let call_soon_threadsafe callbacks and the tasks they create run."""
    for _ in range(3):
        await asyncio.sleep(0)


def device(device_id: str) -> Device:
    return Device(id=device_id, name="n", model="m", firmware_version="f", serial_number="s")


WS_KWARGS: dict[str, Any] = {
    "broker": "wss://broker.example.invalid:8884",
    "port": 443,
    "username": "user",
    "password": "secret",
    "records": [],
    "ws_path": "/mqtt",
    "auth_headers": {"Authorization": "Bearer tok"},
}
TCP_KWARGS: dict[str, Any] = {
    "broker": "broker.example.invalid",
    "port": 1883,
    "username": None,
    "password": None,
    "records": [],
}
WSS_NO_PATH_KWARGS: dict[str, Any] = {
    "broker": "wss://broker.example.invalid",
    "port": 443,
    "username": "user",
    "password": None,
    "records": [],
}


def make(kwargs: dict[str, Any], **overrides: Any) -> NavimowMQTT:
    return NavimowMQTT(**{**kwargs, **overrides})


def test_init_setup_calls_for_websockets(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        assert mqtt.broker == "broker.example.invalid"
        assert mqtt.port == 8884
        assert mqtt._use_tls is True
        assert mqtt._client_id.startswith("web_user_")
        assert fake_paho.instances == [mqtt.client]
        assert mqtt.client.calls == [
            ("__init__", (), {"callback_api_version": VERSION2, "client_id": mqtt._client_id, "transport": "websockets"}),
            ("username_pw_set", ("user", "secret"), {}),
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer tok"}}),
            ("tls_set", (), {}),
            ("reconnect_delay_set", (), {"min_delay": 1, "max_delay": 60}),
        ]
        assert mqtt.client.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message, mqtt._on_connect_fail)

    run(test)


def test_init_setup_calls_for_plain_tcp(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert mqtt.broker == "broker.example.invalid"
        assert mqtt.port == 1883
        assert mqtt._use_tls is False
        assert mqtt._client_id.startswith("web_unknown_")
        assert fake_paho.instances == [mqtt.client]
        assert mqtt.client.calls == [
            ("__init__", (), {"callback_api_version": VERSION2, "client_id": mqtt._client_id, "transport": "tcp"}),
            ("reconnect_delay_set", (), {"min_delay": 1, "max_delay": 60}),
        ]
        assert mqtt.client.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message, mqtt._on_connect_fail)

    run(test)


def test_init_setup_calls_for_wss_scheme_without_ws_path(fake_paho: type[FakeClient]) -> None:
    """A wss:// broker turns TLS on, but without ws_path the transport stays tcp."""

    async def test() -> None:
        mqtt = make(WSS_NO_PATH_KWARGS)
        assert mqtt.port == 443
        assert mqtt._use_tls is True
        assert [name for name, _, _ in mqtt.client.calls] == ["__init__", "tls_set", "reconnect_delay_set"]
        assert mqtt.client.calls[0][2] == {
            "callback_api_version": VERSION2,
            "client_id": mqtt._client_id,
            "transport": "tcp",
        }
        assert fake_paho.instances == [mqtt.client]

    run(test)


@pytest.mark.parametrize("kwargs", [WS_KWARGS, TCP_KWARGS, WSS_NO_PATH_KWARGS], ids=["ws", "tcp", "wss"])
def test_build_new_client_makes_the_same_setup_calls_as_init(
    fake_paho: type[FakeClient], kwargs: dict[str, Any]
) -> None:
    async def test() -> None:
        mqtt = make(kwargs)
        built = mqtt._build_new_client()
        assert built is not mqtt.client
        assert fake_paho.instances == [mqtt.client, built]
        assert built.calls == mqtt.client.calls
        assert built.callbacks == mqtt.client.callbacks
        assert mqtt.client is fake_paho.instances[0]  # the live client is not replaced

    run(test)


def test_keepalive_and_reconnect_delays_are_bounded(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS, keepalive_seconds=10, reconnect_min_delay=-5, reconnect_max_delay=-9)
        assert mqtt.keepalive_seconds == 30
        assert mqtt.reconnect_min_delay == 0
        assert mqtt.reconnect_max_delay == 0
        assert mqtt.client.named("reconnect_delay_set") == [
            ("reconnect_delay_set", (), {"min_delay": 0, "max_delay": 0})
        ]
        mqtt = make(TCP_KWARGS, reconnect_min_delay=30, reconnect_max_delay=5)
        assert (mqtt.reconnect_min_delay, mqtt.reconnect_max_delay) == (30, 30)
        assert fake_paho.instances[-1] is mqtt.client

    run(test)


DEVICE_TOPICS = [
    f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
    for device_id in ("dev-1", "dev-2")
    for channel in ("state", "event", "attributes")
]
WILDCARD_TOPICS = [f"/downlink/vehicle/+/realtimeDate/{c}" for c in ("state", "event", "attributes")]


def test_subscribe_all_uses_the_device_ids_and_skips_empty_ones(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS, records=[device("dev-1"), device(""), device("dev-2")])
        mqtt.subscribe_all("", "")
        assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in DEVICE_TOPICS]
        mqtt.unsubscribe_all("", "")
        assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in DEVICE_TOPICS]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_subscribe_all_falls_back_to_wildcards_without_device_ids(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS, records=[device("")])
        mqtt.subscribe_all("", "")
        assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in WILDCARD_TOPICS]
        mqtt.unsubscribe_all("", "")
        assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in WILDCARD_TOPICS]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_on_connect_calls_subscribe_all_with_two_arguments(fake_paho: type[FakeClient]) -> None:
    """An override with upstream's two-argument signature keeps working."""

    class Recording(NavimowMQTT):
        def __init__(self, **kwargs: Any) -> None:
            self.subscribe_calls: list[tuple[str, str]] = []
            super().__init__(**kwargs)

        def subscribe_all(self, product_key: str, device_name: str) -> None:
            self.subscribe_calls.append((product_key, device_name))

    async def test() -> None:
        connected: list[str] = []

        async def on_connected() -> None:
            connected.append("connected")

        async def on_ready() -> None:
            connected.append("ready")

        mqtt = Recording(**TCP_KWARGS)
        mqtt.on_connected = on_connected
        mqtt.on_ready = on_ready

        mqtt._on_connect(mqtt.client, None, {}, UNSPECIFIED, None)
        await drain()
        assert mqtt.subscribe_calls == []
        assert connected == []

        mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
        await drain()
        assert mqtt.subscribe_calls == [("", "")]
        assert connected == ["connected", "ready"]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_on_disconnect_schedules_the_callback(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        seen: list[str] = []

        async def on_disconnected() -> None:
            seen.append("disconnected")

        mqtt = make(TCP_KWARGS)
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        await drain()
        assert seen == []
        mqtt.on_disconnected = on_disconnected
        mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
        await drain()
        assert seen == ["disconnected"]
        assert fake_paho.instances == [mqtt.client]

    run(test)


STATE_TOPIC = "/downlink/vehicle/dev-1/realtimeDate/state"


def recording_handler() -> tuple[list[tuple[str, bytes, str]], Callable[..., Awaitable[None]]]:
    received: list[tuple[str, bytes, str]] = []

    async def on_message(topic: str, payload: bytes, device_id: str) -> None:
        received.append((topic, payload, device_id))

    return received, on_message


def test_on_message_injects_device_id_and_reencodes_the_payload(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_message = handler
        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"state":"isDocked","battery":50}'))
        await drain()
        assert received == [
            (STATE_TOPIC, b'{"state": "isDocked", "battery": 50, "device_id": "dev-1"}', "dev-1")
        ]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_on_message_keeps_a_device_id_already_in_the_payload(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_message = handler
        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"device_id":"other","state":"isDocked"}'))
        await drain()
        assert received == [(STATE_TOPIC, b'{"device_id": "other", "state": "isDocked"}', "dev-1")]
        assert fake_paho.instances == [mqtt.client]

    run(test)


@pytest.mark.parametrize("payload", [b"[1, 2]", b"not json", b"", b'"text"'], ids=["list", "invalid", "empty", "string"])
def test_on_message_passes_non_object_payloads_through_unchanged(
    fake_paho: type[FakeClient], payload: bytes
) -> None:
    async def test() -> None:
        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_message = handler
        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, payload))
        await drain()
        assert received == [(STATE_TOPIC, payload, "dev-1")]
        assert received[0][1] is payload
        assert fake_paho.instances == [mqtt.client]

    run(test)


LOCATION_TOPIC = "/downlink/vehicle/dev-1/realtimeDate/location"


def test_a_location_message_is_passed_on_like_any_other_channel(fake_paho: type[FakeClient]) -> None:
    """An array arrives as the original bytes; an object is re-encoded with device_id."""

    async def test() -> None:
        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_message = handler
        array = b'[{"type":"1","time":"1790000000000"}]'
        mqtt._on_message(mqtt.client, None, FakeMessage(LOCATION_TOPIC, array))
        mqtt._on_message(mqtt.client, None, FakeMessage(LOCATION_TOPIC, b'{"type":"1"}'))
        await drain()
        assert received == [
            (LOCATION_TOPIC, array, "dev-1"),
            (LOCATION_TOPIC, b'{"type": "1", "device_id": "dev-1"}', "dev-1"),
        ]
        assert received[0][1] is array
        assert fake_paho.instances == [mqtt.client]

    run(test)


@pytest.mark.parametrize(
    "topic",
    [
        "navimow/dev-1/state",
        "/downlink/vehicle/dev-1/other/state",
        "/downlink/vehicle//realtimeDate/state",
        "/downlink/vehicle/dev-1/realtimeDate/state/extra",
        "/uplink/vehicle/dev-1/realtimeDate/state",
    ],
    ids=["legacy_shape", "wrong_segment", "empty_device_id", "too_long", "wrong_direction"],
)
def test_on_message_ignores_topics_it_cannot_parse(fake_paho: type[FakeClient], topic: str) -> None:
    async def test() -> None:
        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_message = handler
        mqtt._on_message(mqtt.client, None, FakeMessage(topic, b'{"state":"isDocked"}'))
        await drain()
        assert received == []
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_on_message_without_a_handler_is_a_no_op(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert mqtt.on_message is None
        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"state":"isDocked"}'))
        await drain()
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_parse_topic_accepts_a_missing_leading_slash(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert mqtt._parse_topic("downlink/vehicle/dev-1/realtimeDate/event") == ("dev-1", "event")
        assert mqtt._parse_topic(STATE_TOPIC) == ("dev-1", "state")
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_connect_async_disconnect_and_publish(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS, keepalive_seconds=600)
        mqtt.connect_async()
        assert mqtt.client.named("connect_async") == [
            ("connect_async", ("broker.example.invalid", 8884, 600), {})
        ]
        assert mqtt.client.named("loop_start") == [("loop_start", (), {})]

        mqtt.client.connected = True
        assert mqtt.is_connected is True
        mqtt.connect_async()  # already connected: nothing more
        assert len(mqtt.client.named("connect_async")) == 1

        mqtt.publish_command("dev-1", {"command": "pause", "params": {}})
        assert mqtt.client.named("publish") == [
            ("publish", ("navimow/dev-1/command", '{"command": "pause", "params": {}}'), {})
        ]

        mqtt.disconnect()
        assert mqtt.client.named("loop_stop") == [("loop_stop", (), {})]
        assert mqtt.client.named("disconnect") == [("disconnect", (), {})]
        assert mqtt.is_connected is False
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_a_second_connect_before_the_first_completes_is_a_no_op(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        mqtt.connect_async()
        mqtt.connect_async()  # not connected yet, but paho's thread is running: nothing more
        assert mqtt.client.named("connect_async") == [
            ("connect_async", ("broker.example.invalid", 1883, 60), {}),
        ]
        assert mqtt.client.named("loop_start") == [("loop_start", (), {})]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_the_init_and_connect_logs_carry_the_client_id_and_the_websocket_path(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    async def test() -> None:
        with caplog.at_level(logging.INFO, logger="mower_sdk.mqtt"):
            mqtt = make(WS_KWARGS, username="account-1234")
            mqtt.connect_async()
        assert [r.getMessage() for r in caplog.records] == [
            "NavimowMQTT init: broker=broker.example.invalid port=8884 ws_path=/mqtt tls=True "
            f"client_id={mqtt._client_id}",
            "NavimowMQTT connect details: transport=websockets broker=broker.example.invalid port=8884 "
            "ws_path=/mqtt tls=True username=ac***34 auth_headers={'Authorization': 'Be***ok'}",
            "NavimowMQTT connecting: broker=broker.example.invalid port=8884 ws_path=/mqtt",
        ]
        assert mqtt._client_id.startswith("web_account-1234_")
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_a_refused_connect_logs_an_error_and_calls_the_connect_failure_hook(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    async def test() -> None:
        seen: list[str] = []

        async def on_connected() -> None:
            seen.append("connected")

        async def on_connect_fail(reason: str) -> None:
            seen.append(reason)

        mqtt = make(TCP_KWARGS)
        mqtt.on_connected = on_connected
        mqtt.on_connect_fail = on_connect_fail
        assert mqtt.client.on_connect_fail == mqtt._on_connect_fail
        with caplog.at_level(logging.INFO, logger="mower_sdk.mqtt"):
            mqtt._on_connect(mqtt.client, None, {}, NOT_AUTHORIZED, None)
        await drain()
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.ERROR, "MQTT connection failed: Not authorized (135)")
        ]
        assert seen == ["refused: Not authorized (135)"]
        assert mqtt.client.named("subscribe") == []
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_update_credentials(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        first = mqtt.client

        # Unchanged values, and None for "keep": nothing happens.
        mqtt.update_credentials(username="user", password="secret", auth_headers={"Authorization": "Bearer tok"})
        mqtt.update_credentials()
        assert mqtt.client is first
        assert fake_paho.instances == [first]
        assert first.named("loop_stop") == []

        # Changed while connected: merged into the stored values and set on the live
        # client, which is kept; paho uses them at its next reconnect.
        first.connected = True
        mqtt.update_credentials(password="rotated", auth_headers={"Authorization": "Bearer new"})
        assert (mqtt.username, mqtt.password) == ("user", "rotated")
        assert mqtt.auth_headers == {"Authorization": "Bearer new"}
        assert first.named("username_pw_set") == [
            ("username_pw_set", ("user", "secret"), {}),
            ("username_pw_set", ("user", "rotated"), {}),
        ]
        assert first.named("ws_set_options") == [
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer tok"}}),
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer new"}}),
        ]
        assert mqtt.client is first
        assert fake_paho.instances == [first]
        assert first.named("loop_stop") == []
        assert first.named("disconnect") == []
        assert first.connected is True

        # Changed while disconnected: the old client is stopped, a new one is built and connected.
        first.connected = False
        mqtt.update_credentials(username="user2")
        assert mqtt.username == "user2"
        assert first.named("loop_stop") == [("loop_stop", (), {})]
        assert first.named("disconnect") == [("disconnect", (), {})]
        second = mqtt.client
        assert second is not first
        assert fake_paho.instances == [first, second]
        assert second.named("username_pw_set") == [("username_pw_set", ("user2", "rotated"), {})]
        assert second.named("ws_set_options") == [
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer new"}})
        ]
        assert second.named("connect_async") == [
            ("connect_async", ("broker.example.invalid", 8884, 60), {})
        ]
        assert second.named("loop_start") == [("loop_start", (), {})]
        assert second.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message, mqtt._on_connect_fail)

    run(test)


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"password": "rotated"}, ("user", "rotated", {"Authorization": "Bearer tok"})),
        ({"username": "user2"}, ("user2", "secret", {"Authorization": "Bearer tok"})),
        (
            {"auth_headers": {"Authorization": "Bearer new"}},
            ("user", "secret", {"Authorization": "Bearer new"}),
        ),
    ],
    ids=["password_only", "username_only", "headers_only"],
)
def test_update_credentials_partial_update_while_connected(
    fake_paho: type[FakeClient], update: dict[str, Any], expected: tuple[Any, Any, Any]
) -> None:
    """A partial update while connected merges into the stored values, which are set on the live client.

    A token refresh typically sends a password-only or headers-only update, so the
    untouched values must survive the merge and the setters must receive the
    merged pair, not the arguments.
    """

    async def test() -> None:
        mqtt = make(WS_KWARGS)
        client = mqtt.client
        client.connected = True
        calls_before = list(client.calls)

        mqtt.update_credentials(**update)

        assert (mqtt.username, mqtt.password, mqtt.auth_headers) == expected
        username, password, headers = expected
        assert client.calls == [
            *calls_before,
            ("username_pw_set", (username, password), {}),
            ("ws_set_options", (), {"path": "/mqtt", "headers": headers}),
        ]
        assert client.connected is True  # no disconnect, no new client
        assert mqtt.client is client
        assert fake_paho.instances == [client]

    run(test)


@pytest.mark.parametrize(
    ("kwargs", "update", "new_calls"),
    [
        (WSS_NO_PATH_KWARGS, {"password": "p"}, [("username_pw_set", ("user", "p"), {})]),
        (TCP_KWARGS, {"password": "p"}, []),
        (TCP_KWARGS, {"auth_headers": {"Authorization": "Bearer t"}}, []),
    ],
    ids=["no_ws_path", "no_username", "headers_without_ws_path"],
)
def test_update_credentials_while_connected_calls_only_the_setters_that_apply(
    fake_paho: type[FakeClient], kwargs: dict[str, Any], update: dict[str, Any], new_calls: list[Call]
) -> None:
    """username_pw_set needs both a username and a password; ws_set_options needs a WebSocket path."""

    async def test() -> None:
        mqtt = make(kwargs)
        client = mqtt.client
        client.connected = True
        calls_before = list(client.calls)

        mqtt.update_credentials(**update)

        for name, value in update.items():
            assert getattr(mqtt, name) == value
        assert client.calls == [*calls_before, *new_calls]
        assert client.connected is True
        assert fake_paho.instances == [client]

    run(test)


def test_update_credentials_while_connected_logs_what_happens(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        mqtt.client.connected = True
        with caplog.at_level(logging.INFO, logger="mower_sdk.mqtt"):
            mqtt.update_credentials(password="rotated")
        messages = [r.getMessage() for r in caplog.records if "credentials updated" in r.getMessage()]
        assert messages == [
            "NavimowMQTT credentials updated while connected: set on the client, used at the "
            "next reconnect: broker=broker.example.invalid port=8884"
        ]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_construction_does_not_call_an_overridden_build_new_client(fake_paho: type[FakeClient]) -> None:
    class Overriding(NavimowMQTT):
        build_calls = 0

        def _build_new_client(self) -> Any:
            type(self).build_calls += 1
            return super()._build_new_client()

    async def test() -> None:
        mqtt = Overriding(**TCP_KWARGS)
        assert Overriding.build_calls == 0
        assert fake_paho.instances == [mqtt.client]

        mqtt.update_credentials(username="u", password="p")  # disconnected: rebuilds through the override
        assert Overriding.build_calls == 1
        assert fake_paho.instances == [fake_paho.instances[0], mqtt.client]

    run(test)


def test_callback_properties_can_read_self_client_during_construction(fake_paho: type[FakeClient]) -> None:
    """self.client is assigned before the callback attributes are looked up."""

    class PropertyCallbacks(NavimowMQTT):
        @property
        def _on_connect(self) -> Any:  # type: ignore[override]
            self.client  # noqa: B018  # AttributeError if construction reads this first
            return self.connect_hook

        @property
        def _on_disconnect(self) -> Any:  # type: ignore[override]
            self.client  # noqa: B018
            return self.disconnect_hook

        @property
        def _on_message(self) -> Any:  # type: ignore[override]
            self.client  # noqa: B018
            return self.message_hook

        def connect_hook(self, *args: Any) -> None:
            pass

        def disconnect_hook(self, *args: Any) -> None:
            pass

        def message_hook(self, *args: Any) -> None:
            pass

    async def test() -> None:
        mqtt = PropertyCallbacks(**WS_KWARGS)
        assert mqtt.client.callbacks == (
            mqtt.connect_hook, mqtt.disconnect_hook, mqtt.message_hook, mqtt._on_connect_fail
        )
        built = mqtt._build_new_client()
        assert built.callbacks == mqtt.client.callbacks
        assert fake_paho.instances == [mqtt.client, built]

    run(test)


def test_construction_inside_a_running_loop_binds_it(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert mqtt.loop is asyncio.get_running_loop()
        mqtt.connect_async()
        assert mqtt.loop is asyncio.get_running_loop()
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_an_explicit_loop_wins_at_construction_and_a_connect_from_another_loop_raises(
    fake_paho: type[FakeClient],
) -> None:
    other = asyncio.new_event_loop()
    try:

        async def test() -> None:
            mqtt = make(TCP_KWARGS, loop=other)
            assert mqtt.loop is other
            with pytest.raises(RuntimeError, match="connect_async\\(\\) was called from another running loop"):
                mqtt.connect_async()
            assert mqtt.loop is other
            assert mqtt.client.named("connect_async") == []
            assert fake_paho.instances == [mqtt.client]

        run(test)
    finally:
        other.close()


def test_a_connect_from_the_explicit_loop_delivers_there(fake_paho: type[FakeClient]) -> None:
    seen: list[asyncio.AbstractEventLoop] = []
    other = asyncio.new_event_loop()

    async def on_disconnected() -> None:
        seen.append(asyncio.get_running_loop())

    mqtt = make(TCP_KWARGS, loop=other)
    mqtt.on_disconnected = on_disconnected

    async def connect_and_disconnect() -> None:
        mqtt.connect_async()
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        await drain()

    try:
        other.run_until_complete(connect_and_disconnect())
    finally:
        other.close()
    assert seen == [other]
    assert len(mqtt.client.named("connect_async")) == 1
    assert fake_paho.instances == [mqtt.client]


def test_construction_outside_a_running_loop_binds_no_loop_and_creates_none(
    fake_paho: type[FakeClient],
) -> None:
    """No asyncio warning either: with no current loop set, asyncio.get_event_loop() creates a loop
    on 3.11, warns and creates one on 3.12 and 3.13 and raises on 3.14, so on 3.11 to 3.13 the
    policy's current-loop slot is read instead and only 3.14 asks it. paho's own callback-API
    warning does not arise with the fake."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        mqtt = make(TCP_KWARGS)
    assert mqtt.loop is None
    assert fake_paho.instances == [mqtt.client]


def test_a_client_constructed_outside_a_loop_binds_the_loop_it_connects_from(
    fake_paho: type[FakeClient],
) -> None:
    seen: list[str] = []

    async def on_disconnected() -> None:
        seen.append("disconnected")

    mqtt = make(TCP_KWARGS)
    mqtt.on_disconnected = on_disconnected
    assert mqtt.loop is None

    async def test() -> None:
        mqtt.connect_async()
        assert mqtt.loop is asyncio.get_running_loop()
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        await drain()
        assert seen == ["disconnected"]

    run(test)
    assert mqtt.client.named("connect_async") == [("connect_async", ("broker.example.invalid", 1883, 60), {})]
    assert fake_paho.instances == [mqtt.client]


def test_a_client_constructed_and_connected_outside_any_loop_drops_the_callback_with_a_warning(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Without loop= there is nothing to schedule on: the callback's coroutine is closed, so no
    "never awaited" RuntimeWarning follows, and the drop is logged as a warning naming the cure."""
    seen: list[str] = []

    async def on_disconnected() -> None:
        seen.append("disconnected")

    mqtt = make(TCP_KWARGS)
    mqtt.on_disconnected = on_disconnected
    mqtt.connect_async()
    assert mqtt.loop is None
    with (
        warnings.catch_warnings(record=True) as caught,
        caplog.at_level(logging.WARNING, logger="mower_sdk.mqtt"),
    ):
        warnings.simplefilter("always")
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        gc.collect()
    assert [w.message for w in caught if issubclass(w.category, RuntimeWarning)] == []
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "NavimowMQTT has no event loop bound, MQTT callback dropped; pass loop= or connect from "
        "inside the loop: broker=broker.example.invalid port=1883"
    ]

    async def test() -> None:
        await drain()

    run(test)
    assert seen == []
    assert mqtt.loop is None
    assert fake_paho.instances == [mqtt.client]


def test_a_bound_loop_that_has_closed_drops_the_callback_closed_with_a_debug_line(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """The loop bound at construction is closed by the time the callback arrives: the coroutine is
    closed, so no "never awaited" RuntimeWarning follows, and nothing above DEBUG is logged, since
    a loop that has stopped is the shutdown case, not a missing loop."""
    seen: list[str] = []

    async def on_disconnected() -> None:
        seen.append("disconnected")

    async def construct() -> NavimowMQTT:
        return make(TCP_KWARGS)

    mqtt = asyncio.run(construct())
    assert mqtt.loop is not None
    assert mqtt.loop.is_closed()
    mqtt.on_disconnected = on_disconnected
    with (
        warnings.catch_warnings(record=True) as caught,
        caplog.at_level(logging.DEBUG, logger="mower_sdk.mqtt"),
    ):
        warnings.simplefilter("always")
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        gc.collect()
    assert [w.message for w in caught if issubclass(w.category, RuntimeWarning)] == []
    assert [r.levelno for r in caplog.records if "callback" in r.getMessage()] == [logging.DEBUG]
    assert seen == []
    assert fake_paho.instances == [mqtt.client]


def test_a_loop_set_as_current_but_not_running_is_bound_and_receives_the_callbacks(
    fake_paho: type[FakeClient],
) -> None:
    """The run_forever pattern: set the loop, construct, connect, then run the loop. No loop is
    created and no asyncio warning escapes on the way."""
    seen: list[str] = []

    async def on_disconnected() -> None:
        seen.append("disconnected")

    current = asyncio.new_event_loop()
    asyncio.set_event_loop(current)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            mqtt = make(TCP_KWARGS)
        assert mqtt.loop is current
        mqtt.on_disconnected = on_disconnected
        mqtt.connect_async()
        assert mqtt.loop is current
        current.call_soon(mqtt._on_disconnect, mqtt.client, None, {}, SUCCESS, None)
        current.run_until_complete(drain())
    finally:
        asyncio.set_event_loop(None)
        current.close()
    assert seen == ["disconnected"]
    assert mqtt.client.named("connect_async") == [
        ("connect_async", ("broker.example.invalid", 1883, 60), {})
    ]
    assert fake_paho.instances == [mqtt.client]


def test_a_client_constructed_outside_any_loop_binds_the_loop_set_as_current_at_connect(
    fake_paho: type[FakeClient],
) -> None:
    mqtt = make(TCP_KWARGS)
    assert mqtt.loop is None
    current = asyncio.new_event_loop()
    asyncio.set_event_loop(current)
    try:
        mqtt.connect_async()
        assert mqtt.loop is current
    finally:
        asyncio.set_event_loop(None)
        current.close()
    assert fake_paho.instances == [mqtt.client]


def test_a_refusal_from_paho_is_logged_with_its_reason_text_and_value(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """paho's own ReasonCode, as a CONNACK refusal carries it."""

    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        with caplog.at_level(logging.ERROR, logger="mower_sdk.mqtt"):
            mqtt._on_connect(mqtt.client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=135), None)
        assert [r.getMessage() for r in caplog.records] == ["MQTT connection failed: Not authorized (135)"]
        assert mqtt.client.named("subscribe") == []
        assert fake_paho.instances == [mqtt.client]

    run(test)


# ---- connection bookkeeping: hooks, reasons, counters, client id, message times -------------------

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeClock:
    """``monotonic()`` and ``now(tz)`` read from settable values."""

    def __init__(self) -> None:
        self.monotonic_now = 100.0
        self.wall_now = T0

    def monotonic(self) -> float:
        return self.monotonic_now

    def now(self, tz: Any) -> datetime:
        assert tz is UTC
        return self.wall_now

    def advance(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(mqtt_module, "time", fake)
    monkeypatch.setattr(mqtt_module, "datetime", fake)
    return fake


def test_the_counters_and_reasons_are_kept_with_no_hook_registered(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert (mqtt.connects, mqtt.disconnects, mqtt.connect_failures) == (0, 0, 0)
        assert (mqtt.last_connect_fail_reason, mqtt.last_disconnect_reason, mqtt.last_connected_at) == (
            None,
            None,
            None,
        )

        mqtt._on_connect(mqtt.client, None, {}, NOT_AUTHORIZED, None)
        assert (mqtt.connects, mqtt.connect_failures) == (0, 1)
        assert mqtt.last_connect_fail_reason == "refused: Not authorized (135)"

        mqtt._on_connect_fail(mqtt.client, None)
        assert mqtt.connect_failures == 2
        assert mqtt.last_connect_fail_reason == "connection failed before CONNACK"

        clock.advance(5)
        mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
        assert mqtt.connects == 1
        assert mqtt.last_connected_at == T0 + timedelta(seconds=5)
        assert mqtt.last_connect_fail_reason == "connection failed before CONNACK"  # the latest failure stays

        mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
        assert (mqtt.disconnects, mqtt.last_disconnect_reason) == (1, "Unspecified error")
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
        assert (mqtt.disconnects, mqtt.last_disconnect_reason) == (2, "requested")
        await drain()
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_the_connect_failure_hook_gets_the_reason_after_it_is_recorded(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    async def test() -> None:
        seen: list[tuple[str, int, str | None]] = []
        mqtt = make(TCP_KWARGS)

        async def on_connect_fail(reason: str) -> None:
            seen.append((reason, mqtt.connect_failures, mqtt.last_connect_fail_reason))

        mqtt.on_connect_fail = on_connect_fail
        with caplog.at_level(logging.WARNING, logger="mower_sdk.mqtt"):
            mqtt._on_connect_fail(mqtt.client, None)
        mqtt._on_connect(mqtt.client, None, {}, NOT_AUTHORIZED, None)
        await drain()
        assert seen == [
            ("connection failed before CONNACK", 2, "refused: Not authorized (135)"),
            ("refused: Not authorized (135)", 2, "refused: Not authorized (135)"),
        ]
        assert caplog.records[0].getMessage() == (
            "NavimowMQTT connection failed before CONNACK: broker=broker.example.invalid port=1883"
        )
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_the_disconnect_reason_is_set_before_on_disconnected_runs(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        seen: list[tuple[int, str | None]] = []
        mqtt = make(TCP_KWARGS)

        async def on_disconnected() -> None:
            seen.append((mqtt.disconnects, mqtt.last_disconnect_reason))

        mqtt.on_disconnected = on_disconnected
        mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
        await drain()
        assert seen == [(1, "Unspecified error")]
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_client_id_is_the_id_the_paho_client_was_built_with(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        assert mqtt.client_id == mqtt.client.calls[0][2]["client_id"]
        assert mqtt.client_id.startswith("web_user_")
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_last_message_times_per_channel_and_across_channels(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)  # no on_message: the times are kept anyway
        assert mqtt.last_message_at("dev-1") is None
        assert mqtt.last_message_age("dev-1", "state") is None

        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"state":"isDocked"}'))
        clock.advance(30)
        mqtt._on_message(mqtt.client, None, FakeMessage(LOCATION_TOPIC, b"[]"))
        clock.advance(10)

        assert mqtt.last_message_at("dev-1", "state") == T0
        assert mqtt.last_message_at("dev-1", "location") == T0 + timedelta(seconds=30)
        assert mqtt.last_message_at("dev-1") == T0 + timedelta(seconds=30)
        assert mqtt.last_message_age("dev-1", "state") == 40.0
        assert mqtt.last_message_age("dev-1", "location") == 10.0
        assert mqtt.last_message_age("dev-1") == 10.0
        assert mqtt.last_message_at("dev-1", "event") is None
        assert mqtt.last_message_at("dev-2") is None

        # A topic that does not parse records nothing.
        mqtt._on_message(mqtt.client, None, FakeMessage("/downlink/vehicle//realtimeDate/state", b"{}"))
        mqtt._on_message(mqtt.client, None, FakeMessage("navimow/dev-2/state", b"{}"))
        assert mqtt._last_message.keys() == {"dev-1"}
        await drain()
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_the_newest_message_time_is_read_safely_while_paho_adds_a_channel(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    """A message on a new channel arriving from paho's thread while the newest time is computed.

    The comparison of the first two times delivers an event message, the way paho's
    thread could between two steps of the read; the read must not walk the live map.
    """

    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b"{}"))
        clock.advance(1)
        mqtt._on_message(mqtt.client, None, FakeMessage(LOCATION_TOPIC, b"[]"))

        class Intruding(float):
            delivered = False

            def _intrude(self) -> None:
                if not Intruding.delivered:
                    Intruding.delivered = True
                    event = "/downlink/vehicle/dev-1/realtimeDate/event"
                    mqtt._on_message(mqtt.client, None, FakeMessage(event, b"{}"))

            def __gt__(self, other: object) -> bool:
                self._intrude()
                return float(self) > other

            def __lt__(self, other: object) -> bool:
                self._intrude()
                return float(self) < other

        wall, monotonic = mqtt._last_message["dev-1"]["state"]
        mqtt._last_message["dev-1"]["state"] = (wall, Intruding(monotonic))
        assert mqtt.last_message_at("dev-1") == T0 + timedelta(seconds=1)
        assert Intruding.delivered
        assert mqtt.last_message_at("dev-1", "event") == T0 + timedelta(seconds=1)
        assert fake_paho.instances == [mqtt.client]

    run(test)


# ---- rebuild(), the retired-client guard, the connect guard and the credential rule ---------------


def test_rebuild_installs_the_new_client_before_tearing_the_old_one_down(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        old, old_id = mqtt.client, mqtt.client_id
        mqtt.connect_async()
        fake_paho.events.clear()

        mqtt.rebuild(password="rotated", auth_headers={"Authorization": "Bearer new"}, reason="watchdog")

        new = mqtt.client
        assert new is not old
        assert fake_paho.instances == [old, new]
        assert [(client is new, name) for client, name in fake_paho.events] == [
            (True, "__init__"),
            (True, "username_pw_set"),
            (True, "ws_set_options"),
            (True, "tls_set"),
            (True, "reconnect_delay_set"),
            (False, "disconnect"),
            (False, "loop_stop"),
            (True, "connect_async"),
            (True, "loop_start"),
        ]
        assert new.named("username_pw_set") == [("username_pw_set", ("user", "rotated"), {})]
        assert new.named("ws_set_options") == [
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer new"}})
        ]
        assert mqtt.client_id != old_id
        assert mqtt.client_id.startswith("web_user_") and len(mqtt.client_id) == len(old_id)
        assert new.calls[0][2]["client_id"] == mqtt.client_id
        assert new.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message, mqtt._on_connect_fail)
        assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "watchdog")

    run(test)


class FiringClient(FakeClient):
    """Reports its own disconnect through on_disconnect while disconnect() runs, as paho does."""

    def disconnect(self) -> None:
        super().disconnect()
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, SUCCESS, None)


def test_callbacks_from_a_retired_client_are_ignored(
    fake_paho: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FiringClient)

    async def test() -> None:
        seen: list[str] = []

        async def hook(*_args: Any) -> None:
            seen.append("hook")

        received, handler = recording_handler()
        mqtt = make(TCP_KWARGS)
        mqtt.on_connected = mqtt.on_disconnected = hook
        mqtt.on_connect_fail = hook
        mqtt.on_message = handler
        old = mqtt.client

        mqtt.rebuild(reason="test")  # the old client fires on_disconnect during its teardown
        old.on_connect(old, None, {}, SUCCESS, None)
        old.on_connect(old, None, {}, NOT_AUTHORIZED, None)
        old.on_connect_fail(old, None)
        old.on_message(old, None, FakeMessage(STATE_TOPIC, b"{}"))
        await drain()

        assert seen == []
        assert received == []
        assert (mqtt.connects, mqtt.disconnects, mqtt.connect_failures) == (0, 0, 0)
        assert (mqtt.last_disconnect_reason, mqtt.last_connect_fail_reason) == (None, None)
        assert mqtt.last_message_at("dev-1") is None
        assert old.named("subscribe") == []

        # The new client's callbacks are delivered.
        new = mqtt.client
        new.on_connect(new, None, {}, SUCCESS, None)
        await drain()
        assert seen == ["hook"]
        assert mqtt.connects == 1
        assert fake_paho.instances == [old, new]

    run(test)


def test_an_error_tearing_the_old_client_down_is_logged_and_the_rebuild_goes_on(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        old = mqtt.client

        def refuse() -> None:
            raise OSError("socket already closed")

        old.disconnect = refuse  # type: ignore[method-assign]
        with caplog.at_level(logging.DEBUG, logger="mower_sdk.mqtt"):
            mqtt.rebuild(reason="test")
        assert old.named("loop_stop") == [("loop_stop", (), {})]
        assert mqtt.client is not old
        assert mqtt.client.named("connect_async") == [("connect_async", ("broker.example.invalid", 1883, 60), {})]
        assert "NavimowMQTT old client refuse failed: OSError('socket already closed')" in [
            r.getMessage() for r in caplog.records
        ]
        assert fake_paho.instances == [old, mqtt.client]

    run(test)


def test_force_reconnect_rebuilds_a_healthy_connection(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        first = mqtt.client
        first.connected = True
        mqtt.update_credentials(force_reconnect=True)  # nothing changed, connected: rebuilt anyway
        second = mqtt.client
        assert second is not first
        assert first.named("disconnect") == [("disconnect", (), {})]
        assert second.named("username_pw_set") == [("username_pw_set", ("user", "secret"), {})]
        assert second.named("connect_async") == [("connect_async", ("broker.example.invalid", 8884, 60), {})]
        assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "credentials updated, reconnect forced")

        mqtt.update_credentials(password="rotated", force_reconnect=True)
        assert mqtt.client.named("username_pw_set") == [("username_pw_set", ("user", "rotated"), {})]
        assert mqtt.rebuilds == 2
        assert fake_paho.instances == [first, second, mqtt.client]

    run(test)


def test_a_credential_update_while_disconnected_is_a_rebuild(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        mqtt.update_credentials(username="user2")
        assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "credentials updated while disconnected")
        assert mqtt.client_id.startswith("web_user2_")
        assert len(fake_paho.instances) == 2

    run(test)


def test_the_connect_guard_holds_through_a_failure_and_clears_on_disconnect_and_rebuild(
    fake_paho: type[FakeClient],
) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        first = mqtt.client
        mqtt.connect_async()
        mqtt._on_connect_fail(first, None)  # paho's thread goes on retrying
        mqtt.connect_async()
        assert len(first.named("connect_async")) == 1

        mqtt.disconnect()
        mqtt.connect_async()
        assert len(first.named("connect_async")) == 2
        assert len(first.named("loop_start")) == 2

        mqtt.rebuild(reason="test")
        assert len(mqtt.client.named("connect_async")) == 1
        mqtt.connect_async()
        assert len(mqtt.client.named("connect_async")) == 1
        assert fake_paho.instances == [first, mqtt.client]

    run(test)


def test_a_repeated_sdk_connect_after_a_failure_does_not_restart_paho(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        sdk = NavimowSDK(broker="broker.example.invalid", port=1883, allow_experimental_mqtt_commands=True)
        sdk.connect()
        client = sdk.mqtt.client
        client.on_connect_fail(client, None)
        sdk.connect()
        with pytest.raises(RuntimeError, match="MQTT not connected"):
            sdk.pause("dev-1")  # asks the client to connect first
        assert client.named("connect_async") == [("connect_async", ("broker.example.invalid", 1883, 60), {})]
        assert client.named("loop_start") == [("loop_start", (), {})]
        assert fake_paho.instances == [client]

    run(test)


@pytest.mark.parametrize("connected", [True, False], ids=["connected", "disconnected"])
def test_an_empty_username_and_password_are_values(fake_paho: type[FakeClient], connected: bool) -> None:
    async def test() -> None:
        mqtt = make(WS_KWARGS)
        mqtt.client.connected = connected
        mqtt.update_credentials(username="", password="")
        assert (mqtt.username, mqtt.password) == ("", "")
        assert mqtt.client.named("username_pw_set")[-1] == ("username_pw_set", ("", ""), {})
        assert len(fake_paho.instances) == (1 if connected else 2)

    run(test)


def test_a_username_without_a_password_is_still_not_applied(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(WSS_NO_PATH_KWARGS)
        mqtt.rebuild(reason="test")
        assert mqtt.client.named("username_pw_set") == []
        assert len(fake_paho.instances) == 2

    run(test)


@pytest.mark.parametrize(
    ("username", "password", "applied"),
    [("", "", [("username_pw_set", ("", ""), {})]), ("user", "", [("username_pw_set", ("user", ""), {})]), ("", None, [])],
    ids=["both_empty", "empty_password", "empty_username_no_password"],
)
def test_empty_credentials_at_construction(
    fake_paho: type[FakeClient], username: str, password: str | None, applied: list[Call]
) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS, username=username, password=password)
        assert mqtt.client.named("username_pw_set") == applied
        assert fake_paho.instances == [mqtt.client]

    run(test)


def test_rebuilds_from_two_threads_leave_exactly_one_running_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    original_build = mqtt._build_new_client
    second: list[threading.Thread] = []

    def slow_build() -> Any:
        client = original_build()
        if not second:  # the first rebuild: start another one while this one is mid-way
            thread = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "second"})
            second.append(thread)
            thread.start()
            time.sleep(0.2)
        return client

    mqtt._build_new_client = slow_build  # type: ignore[method-assign]
    first = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "first"})
    first.start()
    first.join(5)
    second[0].join(5)

    original, *replacements = fake_paho.instances
    assert len(replacements) == 2 and mqtt.client is replacements[-1]
    for retired in (original, replacements[0]):
        assert (len(retired.named("disconnect")), len(retired.named("loop_stop"))) == (1, 1)
    assert mqtt.client.named("disconnect") == []
    assert mqtt.rebuilds == 2


def test_a_disconnect_during_a_rebuild_waits_and_then_disconnects_the_new_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    old = mqtt.client
    entered, release = threading.Event(), threading.Event()

    def slow_loop_stop() -> None:
        entered.set()
        release.wait(5)

    old.loop_stop = slow_loop_stop  # type: ignore[method-assign]
    rebuilding = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "watchdog"})
    rebuilding.start()
    assert entered.wait(5)
    disconnecting = threading.Thread(target=mqtt.disconnect)
    disconnecting.start()
    time.sleep(0.1)
    release.set()
    rebuilding.join(5)
    disconnecting.join(5)

    new = mqtt.client
    assert new is not old
    assert [name for name, _, _ in new.calls if name in ("connect_async", "loop_start", "loop_stop", "disconnect")] == [
        "connect_async", "loop_start", "loop_stop", "disconnect"
    ]
    assert mqtt._loop_started is False
    assert fake_paho.instances == [old, new]


def test_a_connect_during_a_rebuild_waits_until_the_old_client_is_disconnected(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    old = mqtt.client
    entered, release = threading.Event(), threading.Event()
    original_disconnect = old.disconnect

    def slow_disconnect() -> None:
        entered.set()
        release.wait(5)
        original_disconnect()

    old.disconnect = slow_disconnect  # type: ignore[method-assign]
    rebuilding = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "watchdog"})
    rebuilding.start()
    assert entered.wait(5)
    connecting = threading.Thread(target=mqtt.connect_async)
    connecting.start()
    time.sleep(0.1)
    release.set()
    rebuilding.join(5)
    connecting.join(5)

    events = [(client is old, name) for client, name in fake_paho.events if name in ("disconnect", "loop_stop", "connect_async")]
    assert events.index((True, "disconnect")) < events.index((False, "connect_async"))
    assert len(mqtt.client.named("connect_async")) == 1  # the waiting connect found it started
    assert fake_paho.instances == [old, mqtt.client]


def test_a_credential_update_during_a_rebuild_reaches_the_new_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WS_KWARGS)
    mqtt.client.connected = True
    original_build = mqtt._build_new_client
    built, release = threading.Event(), threading.Event()

    def paused_build() -> Any:
        client = original_build()
        built.set()
        release.wait(5)
        return client

    mqtt._build_new_client = paused_build  # type: ignore[method-assign]
    rebuilding = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "watchdog"})
    rebuilding.start()
    assert built.wait(5)
    updating = threading.Thread(target=mqtt.update_credentials, kwargs={"password": "rotated"})
    updating.start()
    time.sleep(0.1)
    release.set()
    rebuilding.join(5)
    updating.join(5)

    assert mqtt.password == "rotated"
    # The update waited for the rebuild, found the new client not yet connected, and
    # rebuilt once more with the rotated password.
    assert mqtt.client.named("username_pw_set")[-1] == ("username_pw_set", ("user", "rotated"), {})
    assert len(fake_paho.instances) == 3
    assert mqtt.last_rebuild_reason == "credentials updated while disconnected"


# ---- loop affinity: a closed loop, a second loop, a loop closing under a callback -----------------


def test_a_closed_loop_is_refused_at_construction(fake_paho: type[FakeClient]) -> None:
    closed = asyncio.new_event_loop()
    closed.close()
    with pytest.raises(ValueError, match="closed"):
        make(TCP_KWARGS, loop=closed)
    with pytest.raises(ValueError, match="closed"):
        NavimowSDK(broker="broker.example.invalid", port=1883, loop=closed)
    assert fake_paho.instances == []


def test_a_connect_from_a_second_loop_in_another_thread_raises(fake_paho: type[FakeClient]) -> None:
    errors: list[BaseException] = []

    async def test() -> None:
        mqtt = make(TCP_KWARGS)  # binds this loop

        def from_another_thread() -> None:
            async def connect() -> None:
                mqtt.connect_async()

            try:
                asyncio.run(connect())
            except RuntimeError as exc:
                errors.append(exc)

        await asyncio.to_thread(from_another_thread)
        assert mqtt.client.named("connect_async") == []
        assert fake_paho.instances == [mqtt.client]

    run(test)
    assert len(errors) == 1 and "another running loop" in str(errors[0])


class ClosingLoop:
    """A loop that reports itself running and then closes under call_soon_threadsafe."""

    def is_running(self) -> bool:
        return True

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, *_args: Any) -> None:
        raise RuntimeError("Event loop is closed")


def test_a_loop_closing_under_the_hand_over_drops_the_callback_closed_with_a_debug_line(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []

    async def on_disconnected() -> None:
        seen.append("disconnected")

    mqtt = make(TCP_KWARGS, loop=ClosingLoop())
    mqtt.on_disconnected = on_disconnected
    with (
        warnings.catch_warnings(record=True) as caught,
        caplog.at_level(logging.DEBUG, logger="mower_sdk.mqtt"),
    ):
        warnings.simplefilter("always")
        mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)  # raises nothing on paho's thread
        gc.collect()
    assert [w.message for w in caught if issubclass(w.category, RuntimeWarning)] == []
    assert [r.levelno for r in caplog.records if "callback" in r.getMessage()] == [logging.DEBUG]
    assert seen == []
    assert fake_paho.instances == [mqtt.client]


def test_the_facades_loop_follows_the_clients_binding(fake_paho: type[FakeClient]) -> None:
    sdk = NavimowSDK(broker="broker.example.invalid", port=1883)
    assert sdk.loop is None
    assert not hasattr(sdk, "_loop")

    async def test() -> None:
        sdk.connect()
        assert sdk.loop is asyncio.get_running_loop()
        assert sdk.loop is sdk.mqtt.loop

    run(test)
    assert fake_paho.instances == [sdk.mqtt.client]


def test_the_keepalive_defaults_to_60_seconds_and_2400_can_still_be_passed(fake_paho: type[FakeClient]) -> None:
    async def test() -> None:
        mqtt = make(TCP_KWARGS)
        assert mqtt.keepalive_seconds == 60
        upstream = make(TCP_KWARGS, keepalive_seconds=2400)
        upstream.connect_async()
        assert upstream.client.named("connect_async") == [("connect_async", ("broker.example.invalid", 1883, 2400), {})]
        assert fake_paho.instances == [mqtt.client, upstream.client]

    run(test)

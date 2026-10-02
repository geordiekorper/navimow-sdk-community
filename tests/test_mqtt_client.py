"""Characterisation tests for NavimowMQTT's client setup and message handling.

A recording fake replaces ``paho.mqtt.client.Client``, so nothing connects.
Most tests are async tests, so the client binds the running loop at
construction and the callbacks it schedules can be drained; the loop-binding
tests construct outside a loop on purpose and drive a loop by hand.

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
import copy
import dataclasses
import gc
import json
import logging
import pickle
import threading
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

import mower_sdk
from mower_sdk import mqtt as mqtt_module
from mower_sdk.models import Device, RejectedMessage
from mower_sdk.mqtt import NavimowMQTT
from mower_sdk.sdk import NavimowSDK

from .fakes import SUCCESS, T0, Call, FakeClient, FakeClock, FakeMessage, FakeReasonCode, drain

VERSION2 = mqtt_module.mqtt_client.CallbackAPIVersion.VERSION2


NOT_AUTHORIZED = FakeReasonCode(135, "Not authorized")
UNSPECIFIED = FakeReasonCode(128, "Unspecified error")


def run(test: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(test())


class WatchedLock:
    """Stands in for the lifecycle lock; ``contended`` is set when a thread has to wait for it.

    A test that holds one call inside the lock waits for ``contended`` to know
    that the other thread's call has arrived, whatever the machine's speed.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contended = threading.Event()

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


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


@pytest.mark.asyncio
async def test_init_setup_calls_for_websockets(fake_paho: type[FakeClient]) -> None:
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


@pytest.mark.asyncio
async def test_init_setup_calls_for_plain_tcp(fake_paho: type[FakeClient]) -> None:
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


@pytest.mark.asyncio
async def test_init_setup_calls_for_wss_scheme_without_ws_path(fake_paho: type[FakeClient]) -> None:
    """A wss:// broker turns TLS on, but without ws_path the transport stays tcp."""

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


@pytest.mark.parametrize("kwargs", [WS_KWARGS, TCP_KWARGS, WSS_NO_PATH_KWARGS], ids=["ws", "tcp", "wss"])
@pytest.mark.asyncio
async def test_build_new_client_makes_the_same_setup_calls_as_init(
    fake_paho: type[FakeClient], kwargs: dict[str, Any]
) -> None:
    mqtt = make(kwargs)
    built = mqtt._build_new_client()
    assert built is not mqtt.client
    assert fake_paho.instances == [mqtt.client, built]
    assert built.calls == mqtt.client.calls
    assert built.callbacks == mqtt.client.callbacks
    assert mqtt.client is fake_paho.instances[0]  # the live client is not replaced


@pytest.mark.asyncio
async def test_keepalive_and_reconnect_delays_are_bounded(fake_paho: type[FakeClient]) -> None:
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


DEVICE_TOPICS = [
    f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
    for device_id in ("dev-1", "dev-2")
    for channel in ("state", "event", "attributes")
]
WILDCARD_TOPICS = [f"/downlink/vehicle/+/realtimeDate/{c}" for c in ("state", "event", "attributes")]


@pytest.mark.asyncio
async def test_subscribe_all_uses_the_device_ids_and_skips_empty_ones(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1"), device(""), device("dev-2")])
    mqtt.subscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in DEVICE_TOPICS]
    mqtt.unsubscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in DEVICE_TOPICS]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_subscribe_all_falls_back_to_wildcards_without_device_ids(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, records=[device("")])
    mqtt.subscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in WILDCARD_TOPICS]
    mqtt.unsubscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in WILDCARD_TOPICS]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_connect_calls_subscribe_all_with_two_arguments(fake_paho: type[FakeClient]) -> None:
    """An override with upstream's two-argument signature keeps working."""

    class Recording(NavimowMQTT):
        def __init__(self, **kwargs: Any) -> None:
            self.subscribe_calls: list[tuple[str, str]] = []
            super().__init__(**kwargs)

        def subscribe_all(self, product_key: str, device_name: str) -> None:
            self.subscribe_calls.append((product_key, device_name))

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


@pytest.mark.asyncio
async def test_on_disconnect_schedules_the_callback(fake_paho: type[FakeClient]) -> None:
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


STATE_TOPIC = "/downlink/vehicle/dev-1/realtimeDate/state"


def recording_handler() -> tuple[list[tuple[str, bytes, str]], Callable[..., Awaitable[None]]]:
    received: list[tuple[str, bytes, str]] = []

    async def on_message(topic: str, payload: bytes, device_id: str) -> None:
        received.append((topic, payload, device_id))

    return received, on_message


@pytest.mark.asyncio
async def test_on_message_injects_device_id_and_reencodes_the_payload(fake_paho: type[FakeClient]) -> None:
    received, handler = recording_handler()
    mqtt = make(TCP_KWARGS)
    mqtt.on_message = handler
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"state":"isDocked","battery":50}'))
    await drain()
    assert received == [
        (STATE_TOPIC, b'{"state": "isDocked", "battery": 50, "device_id": "dev-1"}', "dev-1")
    ]
    assert isinstance(received[0][1], mqtt_module.ReceivedPayload)
    assert received[0][1].original == b'{"state":"isDocked","battery":50}'
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_message_keeps_a_device_id_already_in_the_payload(fake_paho: type[FakeClient]) -> None:
    received, handler = recording_handler()
    mqtt = make(TCP_KWARGS)
    mqtt.on_message = handler
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"device_id":"other","state":"isDocked"}'))
    await drain()
    assert received == [(STATE_TOPIC, b'{"device_id": "other", "state": "isDocked"}', "dev-1")]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.parametrize("payload", [b"[1, 2]", b"not json", b"", b'"text"'], ids=["list", "invalid", "empty", "string"])
@pytest.mark.asyncio
async def test_on_message_passes_non_object_payloads_through_unchanged(
    fake_paho: type[FakeClient], payload: bytes
) -> None:
    received, handler = recording_handler()
    mqtt = make(TCP_KWARGS)
    mqtt.on_message = handler
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, payload))
    await drain()
    assert received == [(STATE_TOPIC, payload, "dev-1")]
    assert received[0][1] is payload
    assert not isinstance(received[0][1], mqtt_module.ReceivedPayload)
    assert fake_paho.instances == [mqtt.client]


LOCATION_TOPIC = "/downlink/vehicle/dev-1/realtimeDate/location"


@pytest.mark.asyncio
async def test_a_location_message_is_passed_on_like_any_other_channel(fake_paho: type[FakeClient]) -> None:
    """An array arrives as the original bytes; an object is re-encoded with device_id."""

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
@pytest.mark.asyncio
async def test_on_message_ignores_topics_it_cannot_parse(fake_paho: type[FakeClient], topic: str) -> None:
    received, handler = recording_handler()
    mqtt = make(TCP_KWARGS)
    mqtt.on_message = handler
    mqtt._on_message(mqtt.client, None, FakeMessage(topic, b'{"state":"isDocked"}'))
    await drain()
    assert received == []
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_message_without_a_handler_is_a_no_op(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    assert mqtt.on_message is None
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b'{"state":"isDocked"}'))
    await drain()
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_parse_topic_accepts_a_missing_leading_slash(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    assert mqtt._parse_topic("downlink/vehicle/dev-1/realtimeDate/event") == ("dev-1", "event")
    assert mqtt._parse_topic(STATE_TOPIC) == ("dev-1", "state")
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.parametrize(
    ("topic", "parsed"),
    [
        ("/downlink/vehicle/dev-1/realtimeDate/state", ("dev-1", "state")),
        ("downlink/vehicle/dev-1/realtimeDate/location", ("dev-1", "location")),
        ("/downlink/vehicle/dev-1/realtimeDate/newChannel", ("dev-1", "newChannel")),
        ("/downlink/vehicle//realtimeDate/state", ("", "state")),
        ("/downlink/vehicle/dev-1/realtimeDate/", ("dev-1", "")),
        ("navimow/dev-1/state", (None, None)),
        ("/downlink/vehicle/dev-1/other/state", (None, None)),
        ("/downlink/vehicle/dev-1/realtimeDate/state/extra", (None, None)),
        ("/uplink/vehicle/dev-1/realtimeDate/state", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_topic_is_public_and_the_old_names_are_the_same_function(
    topic: str, parsed: tuple[str | None, str | None]
) -> None:
    assert mqtt_module.parse_topic(topic) == parsed
    assert mqtt_module._parse_topic is mqtt_module.parse_topic
    assert NavimowMQTT._parse_topic is mqtt_module.parse_topic
    assert mower_sdk.parse_topic is mqtt_module.parse_topic
    assert "parse_topic" in mower_sdk.__all__


@pytest.mark.asyncio
async def test_connect_async_disconnect_and_publish(fake_paho: type[FakeClient]) -> None:
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


@pytest.mark.asyncio
async def test_a_second_connect_before_the_first_completes_is_a_no_op(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    mqtt.connect_async()
    mqtt.connect_async()  # not connected yet, but paho's thread is running: nothing more
    assert mqtt.client.named("connect_async") == [
        ("connect_async", ("broker.example.invalid", 1883, 60), {}),
    ]
    assert mqtt.client.named("loop_start") == [("loop_start", (), {})]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_the_connection_logs_redact_the_client_id_the_account_id_and_the_websocket_path(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="mower_sdk.mqtt"):
        mqtt = make(WS_KWARGS, username="12345678", ws_path="/mqtt/12345678")
        mqtt.connect_async()
        suffix = mqtt.client_id.rsplit("_", 1)[1]
        mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
        mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
        mqtt.rebuild(reason="watchdog")
        new_suffix = mqtt.client_id.rsplit("_", 1)[1]
    lines = [r.getMessage() for r in caplog.records if "subscribing" not in r.getMessage()]
    assert lines[:4] == [
        f"NavimowMQTT init: broker=broker.example.invalid port=8884 ws_path=/mqtt/… tls=True client_id=web_…_{suffix}",
        "NavimowMQTT connect details: transport=websockets broker=broker.example.invalid port=8884 "
        "ws_path=/mqtt/… tls=True username=configured auth_headers={'Authorization': 'Be***ok'}",
        f"NavimowMQTT connecting: broker=broker.example.invalid port=8884 ws_path=/mqtt/… client_id=web_…_{suffix}",
        f"NavimowMQTT connected: broker=broker.example.invalid port=8884 client_id=web_…_{suffix}",
    ]
    assert f"NavimowMQTT disconnected: broker=broker.example.invalid port=8884 client_id=web_…_{suffix} rc=Unspecified error" in lines
    assert (
        f"NavimowMQTT rebuilding the client: reason=watchdog broker=broker.example.invalid port=8884 "
        f"client_id=web_…_{new_suffix}"
    ) in lines
    assert not any("12345678" in line for line in lines)
    # What goes to the broker is untouched.
    assert mqtt.client_id == f"web_12345678_{new_suffix}"
    assert mqtt.client.named("ws_set_options")[0][2]["path"] == "/mqtt/12345678"
    assert len(fake_paho.instances) == 2


@pytest.mark.parametrize(
    ("value", "redacted"),
    [
        ("web_12345678_a1b2c3d4e5", "web_…_a1b2c3d4e5"),
        ("web_unknown_a1b2c3d4e5", "web_…_a1b2c3d4e5"),
        ("web_user_name_a1b2c3d4e5", "web_…_a1b2c3d4e5"),
        ("opaque", "…"),
    ],
)
def test_redact_client_id(value: str, redacted: str) -> None:
    assert mqtt_module._redact_client_id(value) == redacted


@pytest.mark.parametrize(
    ("path", "redacted"),
    [
        ("/mqtt/12345678", "/mqtt/…"),
        ("/mqtt/1/2", "/mqtt/…"),
        ("/mqtt", "/mqtt"),
        ("/mqtt?token=secret", "/mqtt?…"),
        ("/mqtt/1?token=secret", "/mqtt/…"),
        ("/?token=secret", "/?…"),
        ("", ""),
        (None, None),
    ],
)
def test_redact_ws_path(path: str | None, redacted: str | None) -> None:
    assert mqtt_module._redact_ws_path(path) == redacted


@pytest.mark.asyncio
async def test_the_username_is_logged_as_configured_or_not(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="mower_sdk.mqtt"):
        make(TCP_KWARGS).connect_async()
    (details,) = [r.getMessage() for r in caplog.records if "connect details" in r.getMessage()]
    assert "username=not configured" in details
    assert len(fake_paho.instances) == 1


@pytest.mark.asyncio
async def test_a_refused_connect_logs_an_error_and_calls_the_connect_failure_hook(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
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


@pytest.mark.asyncio
async def test_update_credentials(fake_paho: type[FakeClient]) -> None:
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
@pytest.mark.asyncio
async def test_update_credentials_partial_update_while_connected(
    fake_paho: type[FakeClient], update: dict[str, Any], expected: tuple[Any, Any, Any]
) -> None:
    """A partial update while connected merges into the stored values, which are set on the live client.

    A token refresh typically sends a password-only or headers-only update, so the
    untouched values must survive the merge and the setters must receive the
    merged pair, not the arguments.
    """

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


@pytest.mark.parametrize(
    ("kwargs", "update", "new_calls"),
    [
        (WSS_NO_PATH_KWARGS, {"password": "p"}, [("username_pw_set", ("user", "p"), {})]),
        (TCP_KWARGS, {"password": "p"}, []),
        (TCP_KWARGS, {"auth_headers": {"Authorization": "Bearer t"}}, []),
    ],
    ids=["no_ws_path", "no_username", "headers_without_ws_path"],
)
@pytest.mark.asyncio
async def test_update_credentials_while_connected_calls_only_the_setters_that_apply(
    fake_paho: type[FakeClient], kwargs: dict[str, Any], update: dict[str, Any], new_calls: list[Call]
) -> None:
    """username_pw_set needs both a username and a password; ws_set_options needs a WebSocket path."""

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


@pytest.mark.asyncio
async def test_update_credentials_while_connected_logs_what_happens(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
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


@pytest.mark.asyncio
async def test_construction_does_not_call_an_overridden_build_new_client(fake_paho: type[FakeClient]) -> None:
    class Overriding(NavimowMQTT):
        build_calls = 0

        def _build_new_client(self) -> Any:
            type(self).build_calls += 1
            return super()._build_new_client()

    mqtt = Overriding(**TCP_KWARGS)
    assert Overriding.build_calls == 0
    assert fake_paho.instances == [mqtt.client]

    mqtt.update_credentials(username="u", password="p")  # disconnected: rebuilds through the override
    assert Overriding.build_calls == 1
    assert fake_paho.instances == [fake_paho.instances[0], mqtt.client]


@pytest.mark.asyncio
async def test_callback_properties_can_read_self_client_during_construction(fake_paho: type[FakeClient]) -> None:
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

    mqtt = PropertyCallbacks(**WS_KWARGS)
    assert mqtt.client.callbacks == (
        mqtt.connect_hook, mqtt.disconnect_hook, mqtt.message_hook, mqtt._on_connect_fail
    )
    built = mqtt._build_new_client()
    assert built.callbacks == mqtt.client.callbacks
    assert fake_paho.instances == [mqtt.client, built]


@pytest.mark.asyncio
async def test_construction_inside_a_running_loop_binds_it(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    assert mqtt.loop is asyncio.get_running_loop()
    mqtt.connect_async()
    assert mqtt.loop is asyncio.get_running_loop()
    assert fake_paho.instances == [mqtt.client]


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


@pytest.mark.asyncio
async def test_a_refusal_from_paho_is_logged_with_its_reason_text_and_value(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """paho's own ReasonCode, as a CONNACK refusal carries it."""

    mqtt = make(TCP_KWARGS)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.mqtt"):
        mqtt._on_connect(mqtt.client, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=135), None)
    assert [r.getMessage() for r in caplog.records] == ["MQTT connection failed: Not authorized (135)"]
    assert mqtt.client.named("subscribe") == []
    assert fake_paho.instances == [mqtt.client]


# ---- connection bookkeeping: hooks, reasons, counters, client id, message times -------------------

@pytest.mark.asyncio
async def test_the_counters_and_reasons_are_kept_with_no_hook_registered(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    mqtt = make(TCP_KWARGS)
    assert (mqtt.connects, mqtt.disconnects, mqtt.connect_failures) == (0, 0, 0)
    assert (mqtt.last_connect_fail_reason, mqtt.last_disconnect_reason, mqtt.last_connected_at) == (
        None,
        None,
        None,
    )
    assert (mqtt.last_connect_failed_at, mqtt.last_disconnected_at, mqtt.last_connected_monotonic) == (
        None,
        None,
        None,
    )

    mqtt._on_connect(mqtt.client, None, {}, NOT_AUTHORIZED, None)
    assert (mqtt.connects, mqtt.connect_failures) == (0, 1)
    assert mqtt.last_connect_fail_reason == "refused: Not authorized (135)"
    assert mqtt.last_connect_failed_at == T0
    assert (mqtt.last_connected_at, mqtt.last_connected_monotonic) == (None, None)  # a refusal is no connect

    clock.advance(2)
    mqtt._on_connect_fail(mqtt.client, None)
    assert mqtt.connect_failures == 2
    assert mqtt.last_connect_fail_reason == "connection failed before CONNACK"
    assert mqtt.last_connect_failed_at == T0 + timedelta(seconds=2)

    clock.advance(3)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    assert mqtt.connects == 1
    assert mqtt.last_connected_at == T0 + timedelta(seconds=5)
    assert mqtt.last_connected_monotonic == 105.0
    assert mqtt.last_connect_fail_reason == "connection failed before CONNACK"  # the latest failure stays
    assert mqtt.last_connect_failed_at == T0 + timedelta(seconds=2)  # and its time
    assert mqtt.last_disconnected_at is None

    clock.advance(10)
    mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
    assert (mqtt.disconnects, mqtt.last_disconnect_reason) == (1, "Unspecified error")
    assert mqtt.last_disconnected_at == T0 + timedelta(seconds=15)
    assert mqtt.last_connected_monotonic == 105.0  # the connect's, kept after the disconnect
    clock.advance(1)
    mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
    assert (mqtt.disconnects, mqtt.last_disconnect_reason) == (2, "requested")
    assert mqtt.last_disconnected_at == T0 + timedelta(seconds=16)
    await drain()
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_the_connect_failure_hook_gets_the_reason_after_it_is_recorded(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
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


@pytest.mark.asyncio
async def test_the_disconnect_reason_is_set_before_on_disconnected_runs(fake_paho: type[FakeClient]) -> None:
    seen: list[tuple[int, str | None]] = []
    mqtt = make(TCP_KWARGS)

    async def on_disconnected() -> None:
        seen.append((mqtt.disconnects, mqtt.last_disconnect_reason))

    mqtt.on_disconnected = on_disconnected
    mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
    await drain()
    assert seen == [(1, "Unspecified error")]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_client_id_is_the_id_the_paho_client_was_built_with(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WS_KWARGS)
    assert mqtt.client_id == mqtt.client.calls[0][2]["client_id"]
    assert mqtt.client_id.startswith("web_user_")
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_last_message_times_per_channel_and_across_channels(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
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


@pytest.mark.asyncio
async def test_the_newest_message_time_is_read_safely_while_paho_adds_a_channel(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    """A message on a new channel arriving from paho's thread while the newest time is computed.

    The comparison of the first two times delivers an event message, the way paho's
    thread could between two steps of the read; the read must not walk the live map.
    """

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


# ---- rebuild(), the retired-client guard, the connect guard and the credential rule ---------------


@pytest.mark.asyncio
async def test_rebuild_installs_the_new_client_before_tearing_the_old_one_down(fake_paho: type[FakeClient]) -> None:
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


class FiringClient(FakeClient):
    """Reports its own disconnect through on_disconnect while disconnect() runs, as paho does."""

    def disconnect(self) -> None:
        super().disconnect()
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, SUCCESS, None)


@pytest.mark.asyncio
async def test_callbacks_from_a_retired_client_are_ignored(
    fake_paho: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FiringClient)

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
    assert (mqtt.last_disconnected_at, mqtt.last_connect_failed_at) == (None, None)
    assert (mqtt.last_connected_at, mqtt.last_connected_monotonic) == (None, None)
    assert mqtt.last_message_at("dev-1") is None
    assert old.named("subscribe") == []

    # The new client's callbacks are delivered.
    new = mqtt.client
    new.on_connect(new, None, {}, SUCCESS, None)
    await drain()
    assert seen == ["hook"]
    assert mqtt.connects == 1
    assert fake_paho.instances == [old, new]


@pytest.mark.asyncio
async def test_an_error_tearing_the_old_client_down_is_logged_and_the_rebuild_goes_on(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
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


@pytest.mark.asyncio
async def test_force_reconnect_rebuilds_a_healthy_connection(fake_paho: type[FakeClient]) -> None:
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


@pytest.mark.asyncio
async def test_a_new_broker_address_rebuilds_a_live_connection_on_it(
    fake_paho: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    mqtt = make(WS_KWARGS)
    first = mqtt.client
    first.connected = True
    with caplog.at_level(logging.INFO, logger="mower_sdk.mqtt"):
        mqtt.update_credentials(password="new", broker="moved.example.invalid", ws_path="/mqtt/12345?t=secret")
    second = mqtt.client
    assert second is not first
    assert first.named("disconnect") == [("disconnect", (), {})]
    assert (mqtt.broker, mqtt.port, mqtt.ws_path, mqtt._use_tls) == (
        "moved.example.invalid",
        8884,  # not named: kept
        "/mqtt/12345?t=secret",
        True,
    )
    assert second.named("connect_async") == [("connect_async", ("moved.example.invalid", 8884, 60), {})]
    assert second.named("ws_set_options")[0][2]["path"] == "/mqtt/12345?t=secret"
    assert second.named("username_pw_set") == [("username_pw_set", ("user", "new"), {})]
    assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "broker changed")
    lines = [record.getMessage() for record in caplog.records]
    assert (
        "NavimowMQTT broker changed: from broker=broker.example.invalid port=8884 ws_path=/mqtt "
        "to broker=moved.example.invalid port=8884 ws_path=/mqtt/… tls=True"
    ) in lines
    assert not any("secret" in line or "12345" in line for line in lines)
    assert fake_paho.instances == [first, second]


@pytest.mark.parametrize(
    ("change", "address"),
    [
        ({"port": 9443}, ("broker.example.invalid", 9443, "/mqtt")),
        ({"broker": "wss://moved.example.invalid:9443"}, ("moved.example.invalid", 9443, "/mqtt")),
        ({"broker": "wss://moved.example.invalid:9443", "port": 1}, ("moved.example.invalid", 9443, "/mqtt")),
        ({"broker": "moved.example.invalid"}, ("moved.example.invalid", 8884, "/mqtt")),
        ({"ws_path": "/other"}, ("broker.example.invalid", 8884, "/other")),
    ],
    ids=["port", "url_with_port", "url_port_wins", "host", "path"],
)
@pytest.mark.asyncio
async def test_each_part_of_the_address_is_merged_and_rebuilds(
    fake_paho: type[FakeClient], change: dict[str, Any], address: tuple[str, int, str]
) -> None:
    mqtt = make(WS_KWARGS)
    mqtt.update_credentials(**change)
    assert (mqtt.broker, mqtt.port, mqtt.ws_path) == address
    assert mqtt.client.named("connect_async") == [("connect_async", (address[0], address[1], 60), {})]
    assert mqtt.last_rebuild_reason == "broker changed"
    assert len(fake_paho.instances) == 2


@pytest.mark.parametrize(
    "same",
    [
        {"broker": "BROKER.example.invalid"},
        {"broker": "wss://broker.example.invalid:8884", "port": 8884, "ws_path": "/mqtt"},
        {"port": 8884},
        {"broker": None, "port": None, "ws_path": None},
    ],
    ids=["case", "all_equal", "port_equal", "none"],
)
@pytest.mark.asyncio
async def test_the_same_address_does_not_rebuild_a_live_client(fake_paho: type[FakeClient], same: dict[str, Any]) -> None:
    mqtt = make(WS_KWARGS)
    mqtt.client.connected = True
    mqtt.update_credentials(password="rotated", **same)
    assert mqtt.rebuilds == 0
    assert mqtt.client.named("username_pw_set")[-1] == ("username_pw_set", ("user", "rotated"), {})
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_a_tcp_client_moved_to_another_host_stays_on_tcp(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    mqtt.rebuild(broker="moved.example.invalid", reason="test")
    assert (mqtt.broker, mqtt.port, mqtt.ws_path, mqtt._use_tls) == ("moved.example.invalid", 1883, None, False)
    assert mqtt.client.calls[0][2]["transport"] == "tcp"
    assert mqtt.client.named("tls_set") == []
    assert len(fake_paho.instances) == 2


@pytest.mark.asyncio
async def test_a_wss_broker_keeps_tls_when_moved_to_a_host_without_a_scheme(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WSS_NO_PATH_KWARGS)
    assert mqtt._use_tls is True
    mqtt.rebuild(broker="moved.example.invalid", reason="test")
    assert mqtt._use_tls is True
    assert mqtt.client.named("tls_set") == [("tls_set", (), {})]
    assert len(fake_paho.instances) == 2


@pytest.mark.parametrize(
    ("start", "change", "transport", "tls"),
    [
        # A scheme names TLS: wss:// turns it on, another scheme off, on the same host and port.
        (TCP_KWARGS, {"broker": "wss://broker.example.invalid:1883"}, "tcp", True),
        (WSS_NO_PATH_KWARGS, {"broker": "tcp://broker.example.invalid:443"}, "tcp", False),
        # A WebSocket path turns on the WebSocket transport and TLS; an empty one turns both off.
        (TCP_KWARGS, {"ws_path": "/mqtt"}, "websockets", True),
        ({**TCP_KWARGS, "ws_path": "/mqtt"}, {"ws_path": ""}, "tcp", False),
        # An empty path with a wss:// broker keeps TLS on TCP.
        (WS_KWARGS, {"ws_path": ""}, "tcp", True),
    ],
    ids=["scheme_on", "scheme_off", "path_added", "path_cleared", "path_cleared_wss"],
)
@pytest.mark.asyncio
async def test_the_transport_and_tls_follow_a_new_scheme_or_path(
    fake_paho: type[FakeClient], start: dict[str, Any], change: dict[str, Any], transport: str, tls: bool
) -> None:
    mqtt = make(start)
    old = mqtt.client
    mqtt.update_credentials(**change)
    new = mqtt.client
    assert new is not old
    assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "broker changed")
    assert new.calls[0][2]["transport"] == transport
    assert mqtt._use_tls is tls
    assert new.named("tls_set") == ([("tls_set", (), {})] if tls else [])
    assert bool(new.named("ws_set_options")) is (transport == "websockets")
    assert fake_paho.instances == [old, new]


@pytest.mark.asyncio
async def test_a_credential_update_while_disconnected_is_a_rebuild(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WS_KWARGS)
    mqtt.update_credentials(username="user2")
    assert (mqtt.rebuilds, mqtt.last_rebuild_reason) == (1, "credentials updated while disconnected")
    assert mqtt.client_id.startswith("web_user2_")
    assert len(fake_paho.instances) == 2


@pytest.mark.asyncio
async def test_the_connect_guard_holds_through_a_failure_and_clears_on_disconnect_and_rebuild(
    fake_paho: type[FakeClient],
) -> None:
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


@pytest.mark.asyncio
async def test_a_repeated_sdk_connect_after_a_failure_does_not_restart_paho(fake_paho: type[FakeClient]) -> None:
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


@pytest.mark.parametrize("connected", [True, False], ids=["connected", "disconnected"])
@pytest.mark.asyncio
async def test_an_empty_username_and_password_are_values(fake_paho: type[FakeClient], connected: bool) -> None:
    mqtt = make(WS_KWARGS)
    mqtt.client.connected = connected
    mqtt.update_credentials(username="", password="")
    assert (mqtt.username, mqtt.password) == ("", "")
    assert mqtt.client.named("username_pw_set")[-1] == ("username_pw_set", ("", ""), {})
    assert len(fake_paho.instances) == (1 if connected else 2)


@pytest.mark.asyncio
async def test_a_username_without_a_password_is_still_not_applied(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WSS_NO_PATH_KWARGS)
    mqtt.rebuild(reason="test")
    assert mqtt.client.named("username_pw_set") == []
    assert len(fake_paho.instances) == 2


@pytest.mark.parametrize(
    ("username", "password", "applied"),
    [("", "", [("username_pw_set", ("", ""), {})]), ("user", "", [("username_pw_set", ("user", ""), {})]), ("", None, [])],
    ids=["both_empty", "empty_password", "empty_username_no_password"],
)
@pytest.mark.asyncio
async def test_empty_credentials_at_construction(
    fake_paho: type[FakeClient], username: str, password: str | None, applied: list[Call]
) -> None:
    mqtt = make(TCP_KWARGS, username=username, password=password)
    assert mqtt.client.named("username_pw_set") == applied
    assert fake_paho.instances == [mqtt.client]


def test_rebuilds_from_two_threads_leave_exactly_one_running_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    mqtt._lifecycle_lock = lock = WatchedLock()  # type: ignore[assignment]
    original_build = mqtt._build_new_client
    second: list[threading.Thread] = []
    arrived: list[bool] = []

    def slow_build() -> Any:
        client = original_build()
        if not second:  # the first rebuild: start another one while this one is mid-way
            thread = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "second"})
            second.append(thread)
            thread.start()
            arrived.append(lock.contended.wait(5))
        return client

    mqtt._build_new_client = slow_build  # type: ignore[method-assign]
    first = threading.Thread(target=mqtt.rebuild, kwargs={"reason": "first"})
    first.start()
    first.join(5)
    second[0].join(5)

    assert arrived == [True]  # the second rebuild asked for the lock while the first held it
    original, *replacements = fake_paho.instances
    assert len(replacements) == 2 and mqtt.client is replacements[-1]
    for retired in (original, replacements[0]):
        assert (len(retired.named("disconnect")), len(retired.named("loop_stop"))) == (1, 1)
    assert mqtt.client.named("disconnect") == []
    assert mqtt.rebuilds == 2


def test_a_disconnect_during_a_rebuild_waits_and_then_disconnects_the_new_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    mqtt._lifecycle_lock = lock = WatchedLock()  # type: ignore[assignment]
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
    assert lock.contended.wait(5)
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
    mqtt._lifecycle_lock = lock = WatchedLock()  # type: ignore[assignment]
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
    assert lock.contended.wait(5)
    release.set()
    rebuilding.join(5)
    connecting.join(5)

    events = [(client is old, name) for client, name in fake_paho.events if name in ("disconnect", "loop_stop", "connect_async")]
    assert events.index((True, "disconnect")) < events.index((False, "connect_async"))
    assert len(mqtt.client.named("connect_async")) == 1  # the waiting connect found it started
    assert fake_paho.instances == [old, mqtt.client]


def test_a_credential_update_during_a_rebuild_reaches_the_new_client(fake_paho: type[FakeClient]) -> None:
    mqtt = make(WS_KWARGS)
    mqtt._lifecycle_lock = lock = WatchedLock()  # type: ignore[assignment]
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
    assert lock.contended.wait(5)
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


@pytest.mark.asyncio
async def test_the_keepalive_defaults_to_60_seconds_and_2400_can_still_be_passed(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS)
    assert mqtt.keepalive_seconds == 60
    upstream = make(TCP_KWARGS, keepalive_seconds=2400)
    upstream.connect_async()
    assert upstream.client.named("connect_async") == [("connect_async", ("broker.example.invalid", 1883, 2400), {})]
    assert fake_paho.instances == [mqtt.client, upstream.client]


# ---- subscribe_location, extra_topics and on_raw ------------------------------------------------

LOCATION_TOPICS = [f"/downlink/vehicle/{d}/realtimeDate/{c}" for d in ("dev-1", "dev-2") for c in ("state", "event", "attributes", "location")]
EXTRA = ["/downlink/vehicle/dev-1/realtimeDate/other", "custom/+/topic"]


@pytest.mark.asyncio
async def test_subscribe_location_adds_the_location_topic_per_device(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1"), device("dev-2")], subscribe_location=True)
    mqtt.subscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in LOCATION_TOPICS]
    mqtt.unsubscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in LOCATION_TOPICS]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_subscribe_location_and_extra_topics_in_the_wildcard_fallback(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, subscribe_location=True, extra_topics=EXTRA)
    mqtt.subscribe_all("", "")
    expected = [*WILDCARD_TOPICS, "/downlink/vehicle/+/realtimeDate/location", *EXTRA]
    assert [args for _, args, _ in mqtt.client.named("subscribe")] == [(t,) for t in expected]
    mqtt.unsubscribe_all("", "")
    assert [args for _, args, _ in mqtt.client.named("unsubscribe")] == [(t,) for t in expected]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_extra_topics_are_subscribed_verbatim_on_every_connect(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")], extra_topics=EXTRA)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    subscribed = [args[0] for _, args, _ in mqtt.client.named("subscribe")]
    assert subscribed == 2 * [*DEVICE_TOPICS[:3], *EXTRA]
    assert mqtt.extra_topics == EXTRA and mqtt.extra_topics is not EXTRA
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.parametrize(
    "topic",
    [
        "", None, 5, "a\x00b", "bad\udc80surrogate", "t" * 65_536, "é" * 32_768,
        "/downlink/vehicle/#/realtimeDate/location", "a/b#", "a/+b/c", "a+/b",
    ],
    ids=[
        "empty", "none", "not_a_string", "nul", "lone_surrogate", "too_long", "too_long_encoded",
        "hash_not_last", "hash_in_level", "plus_in_level", "plus_suffix",
    ],
)
def test_an_extra_topic_mqtt_cannot_carry_is_refused_at_construction(fake_paho: type[FakeClient], topic: Any) -> None:
    with pytest.raises(ValueError, match="extra topic"):
        make(TCP_KWARGS, extra_topics=["ok/topic", topic])
    assert fake_paho.instances == []


def test_wildcard_extra_topics_are_accepted_when_well_formed(fake_paho: type[FakeClient]) -> None:
    topics = ["#", "a/#", "/downlink/vehicle/+/realtimeDate/+", "+/+", "a/b/c"]
    mqtt = make(TCP_KWARGS, extra_topics=topics)
    assert mqtt.extra_topics == topics
    for topic in topics:  # and paho accepts them as subscription filters
        mqtt_module.mqtt_client.topic_matches_sub(topic, "a/b/c")
    assert fake_paho.instances == [mqtt.client]


def test_the_longest_extra_topic_is_accepted(fake_paho: type[FakeClient]) -> None:
    mqtt = make(TCP_KWARGS, extra_topics=["t" * 65_535])
    assert mqtt.extra_topics == ["t" * 65_535]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_raw_receives_the_wire_bytes_on_every_topic(fake_paho: type[FakeClient]) -> None:
    raw: list[tuple[str, bytes]] = []
    received, handler = recording_handler()

    async def on_raw(topic: str, payload: bytes) -> None:
        raw.append((topic, payload))

    mqtt = make(TCP_KWARGS, extra_topics=["custom/topic"])
    mqtt.on_raw = on_raw
    mqtt.on_message = handler
    state = b'{"state":"isDocked"}'
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, state))
    mqtt._on_message(mqtt.client, None, FakeMessage("custom/topic", b"\x01\x02"))
    mqtt._on_message(mqtt.client, None, FakeMessage("navimow/dev-1/state", b"{}"))
    await drain()
    assert raw == [(STATE_TOPIC, state), ("custom/topic", b"\x01\x02"), ("navimow/dev-1/state", b"{}")]
    assert raw[0][1] is state  # before device_id is added
    assert received == [(STATE_TOPIC, b'{"state": "isDocked", "device_id": "dev-1"}', "dev-1")]
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_message_seen_gets_the_device_channel_and_the_recorded_time(
    fake_paho: type[FakeClient], clock: FakeClock
) -> None:
    seen: list[tuple[str, str, datetime]] = []

    async def on_message_seen(device_id: str, channel: str, received_at: datetime) -> None:
        seen.append((device_id, channel, received_at))

    mqtt = make(TCP_KWARGS, extra_topics=["custom/topic"])
    mqtt.on_message_seen = on_message_seen
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, b"not json"))
    clock.advance(2)
    mqtt._on_message(mqtt.client, None, FakeMessage(LOCATION_TOPIC, b"[]"))
    # Neither of these names a device and a channel: nothing seen, nothing recorded.
    mqtt._on_message(mqtt.client, None, FakeMessage("custom/topic", b"{}"))
    mqtt._on_message(mqtt.client, None, FakeMessage("/downlink/vehicle//realtimeDate/state", b"{}"))
    mqtt._on_message(mqtt.client, None, FakeMessage("/downlink/vehicle/dev-1/realtimeDate/", b"{}"))
    await drain()
    assert seen == [("dev-1", "state", T0), ("dev-1", "location", T0 + timedelta(seconds=2))]
    assert seen[0][2] is mqtt.last_message_at("dev-1", "state")  # the stamp stored, not a second reading
    assert mqtt._last_message.keys() == {"dev-1"}
    assert fake_paho.instances == [mqtt.client]


@pytest.mark.asyncio
async def test_on_message_seen_is_not_called_for_a_retired_client(
    fake_paho: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FiringClient)

    seen: list[tuple[str, str]] = []

    async def on_message_seen(device_id: str, channel: str, _received_at: datetime) -> None:
        seen.append((device_id, channel))

    mqtt = make(TCP_KWARGS)
    mqtt.on_message_seen = on_message_seen
    old = mqtt.client
    mqtt.rebuild(reason="test")
    old.on_message(old, None, FakeMessage(STATE_TOPIC, b"{}"))
    new = mqtt.client
    new.on_message(new, None, FakeMessage(STATE_TOPIC, b"{}"))
    await drain()
    assert seen == [("dev-1", "state")]
    assert fake_paho.instances == [old, new]


@pytest.mark.asyncio
async def test_without_on_raw_nothing_extra_is_scheduled(
    fake_paho: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    mqtt = make(TCP_KWARGS)
    scheduled: list[Any] = []
    monkeypatch.setattr(mqtt, "_schedule", scheduled.append)
    mqtt._on_message(mqtt.client, None, FakeMessage("custom/topic", b"x"))
    assert scheduled == []
    assert fake_paho.instances == [mqtt.client]


# ---- subscription acknowledgements -----------------------------------------------------------------


def acknowledge(mqtt: NavimowMQTT, topic: str, *codes: FakeReasonCode, client: Any = None) -> None:
    """Deliver the broker's acknowledgement for topic's latest SUBSCRIBE, as paho's thread would."""
    client = client or mqtt.client
    mids = [mid for mid, pending in mqtt._pending_subscribes.items() if pending == topic]
    mqtt._on_subscribe(client, None, mids[-1] if mids else 999, list(codes), None)


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_each_topic_is_pending_then_granted_or_refused(caplog: pytest.LogCaptureFixture) -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")], extra_topics=["custom/+/topic"])
    seen: list[tuple[str, bool, tuple[int, ...]]] = []

    async def on_subscribe(topic: str, granted: bool, codes: tuple[int, ...]) -> None:
        seen.append((topic, granted, codes))

    mqtt.on_subscribe = on_subscribe
    assert mqtt.client.on_subscribe == mqtt._on_subscribe
    assert mqtt.subscription_results == {}
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    topics = [*DEVICE_TOPICS[:3], "custom/+/topic"]
    assert mqtt.subscription_results == dict.fromkeys(topics, "pending")
    with caplog.at_level(logging.WARNING, logger="mower_sdk.mqtt"):
        for topic in topics[:3]:
            acknowledge(mqtt, topic, SUCCESS)
        acknowledge(mqtt, "custom/+/topic", UNSPECIFIED)
    await drain()
    assert mqtt.subscription_results == {
        **dict.fromkeys(topics[:3], "granted"),
        "custom/+/topic": "refused: Unspecified error (128)",
    }
    assert seen == [*[(t, True, (0,)) for t in topics[:3]], ("custom/+/topic", False, (128,))]
    assert [r.getMessage() for r in caplog.records] == [
        "NavimowMQTT subscription refused by the broker: topic=custom/+/topic reason=Unspecified error (128)"
    ]
    assert mqtt._pending_subscribes == {}


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_a_granted_quality_of_service_below_0x80_is_granted() -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    acknowledge(mqtt, DEVICE_TOPICS[0], FakeReasonCode(1, "Granted QoS 1"))
    acknowledge(mqtt, DEVICE_TOPICS[1])  # no reason code at all
    assert mqtt.subscription_results[DEVICE_TOPICS[0]] == "granted"
    assert mqtt.subscription_results[DEVICE_TOPICS[1]] == "refused: no reason code"


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_a_reconnect_starts_the_results_afresh_and_a_late_acknowledgement_is_ignored() -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    old_mids = dict(mqtt._pending_subscribes)
    acknowledge(mqtt, DEVICE_TOPICS[0], UNSPECIFIED)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    assert mqtt.subscription_results == dict.fromkeys(DEVICE_TOPICS[:3], "pending")
    for mid in old_mids:
        if mid not in mqtt._pending_subscribes:
            mqtt._on_subscribe(mqtt.client, None, mid, [UNSPECIFIED], None)
    assert set(mqtt.subscription_results.values()) == {"pending"}


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_an_acknowledgement_from_a_replaced_client_is_ignored_and_the_new_client_reports() -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    mqtt.connect_async()
    old = mqtt.client
    mqtt._on_connect(old, None, {}, SUCCESS, None)
    mqtt.rebuild(reason="test")
    new = mqtt.client
    assert new is not old and new.on_subscribe == mqtt._on_subscribe
    acknowledge(mqtt, DEVICE_TOPICS[0], UNSPECIFIED, client=old)
    assert mqtt.subscription_results[DEVICE_TOPICS[0]] == "pending"
    mqtt._on_connect(new, None, {}, SUCCESS, None)
    acknowledge(mqtt, DEVICE_TOPICS[0], SUCCESS)
    assert mqtt.subscription_results[DEVICE_TOPICS[0]] == "granted"


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_a_subscribe_paho_could_not_send_is_recorded_as_not_sent() -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    mqtt.client.subscribe_result = mqtt_module.mqtt_client.MQTT_ERR_NO_CONN
    mqtt.subscribe_all()
    assert mqtt.subscription_results == dict.fromkeys(
        DEVICE_TOPICS[:3], "not sent: The client is not currently connected."
    )
    assert mqtt._pending_subscribes == {}


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_an_acknowledgement_for_an_unknown_message_id_changes_nothing() -> None:
    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    called: list[Any] = []

    async def on_subscribe(*args: Any) -> None:
        called.append(args)

    mqtt.on_subscribe = on_subscribe
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    before = dict(mqtt.subscription_results)
    mqtt._on_subscribe(mqtt.client, None, 12345, [UNSPECIFIED], None)
    await drain()
    assert (mqtt.subscription_results, called) == (before, [])


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_an_acknowledgement_racing_the_subscribe_call_is_not_lost() -> None:
    """paho's thread may handle the acknowledgement before subscribe() has returned the id.

    The fake's subscribe starts that thread and gives it time to run before
    returning; the acknowledgement must wait for the id to be recorded rather
    than find nothing pending and be dropped.
    """

    mqtt = make(TCP_KWARGS, records=[device("dev-1")])
    seen: list[tuple[str, bool, tuple[int, ...]]] = []

    async def on_subscribe(topic: str, granted: bool, codes: tuple[int, ...]) -> None:
        seen.append((topic, granted, codes))

    mqtt.on_subscribe = on_subscribe
    client = mqtt.client
    plain_subscribe = client.subscribe
    acknowledgers: list[threading.Thread] = []

    def racing_subscribe(topic: str) -> tuple[int, int | None]:
        result, mid = plain_subscribe(topic)
        thread = threading.Thread(target=mqtt._on_subscribe, args=(client, None, mid, [SUCCESS], None))
        thread.start()
        thread.join(timeout=0.05)  # still waiting for the lock, if subscribe_all holds it
        acknowledgers.append(thread)
        return result, mid

    client.subscribe = racing_subscribe
    mqtt.subscribe_all()
    for thread in acknowledgers:
        thread.join(timeout=5)
        assert not thread.is_alive()
    await drain()
    assert mqtt.subscription_results == dict.fromkeys(DEVICE_TOPICS[:3], "granted")
    assert mqtt._pending_subscribes == {}
    # The acknowledging threads take the lock in whatever order they get it.
    assert sorted(seen) == sorted((t, True, (0,)) for t in DEVICE_TOPICS[:3])


# ---- the original bytes of a re-encoded payload ------------------------------------------------------


def test_a_received_payload_is_bytes_equal_to_the_re_encoded_form() -> None:
    payload = mqtt_module.ReceivedPayload(b'{"a": 1, "device_id": "d"}', b'{ "a" : 1 }')
    assert isinstance(payload, bytes)
    assert payload == b'{"a": 1, "device_id": "d"}'
    assert payload.decode() == '{"a": 1, "device_id": "d"}'
    assert payload.original == b'{ "a" : 1 }'
    assert mqtt_module._original_payload(payload) == b'{ "a" : 1 }'
    assert mqtt_module._original_payload(b"[1]") == b"[1]"
    assert "ReceivedPayload" in mqtt_module.__all__
    assert mower_sdk.ReceivedPayload is mqtt_module.ReceivedPayload
    assert "ReceivedPayload" in mower_sdk.__all__


@pytest.mark.parametrize(
    "clone",
    [copy.copy, copy.deepcopy, *(lambda value, p=p: pickle.loads(pickle.dumps(value, protocol=p)) for p in range(6))],
    ids=["copy", "deepcopy", *(f"pickle_{p}" for p in range(6))],
)
def test_a_received_payload_copies_and_pickles_with_both_forms(clone: Callable[[Any], Any]) -> None:
    payload = mqtt_module.ReceivedPayload(b'{"a": 1, "device_id": "d"}', b'{ "a" : 1 }')
    cloned = clone(payload)
    assert type(cloned) is mqtt_module.ReceivedPayload
    assert (bytes(cloned), cloned.original) == (bytes(payload), payload.original)


def test_a_rejection_holding_a_received_payload_converts_and_pickles() -> None:
    payload = mqtt_module.ReceivedPayload(b'{"a": 1, "device_id": "d"}', b'{ "a" : 1 }')
    rejected = RejectedMessage("state", "t", "d", "unknown_field", ("unknown_field",), payload, datetime.now(UTC),
                               original=payload.original)
    assert dataclasses.asdict(rejected)["payload"] == payload
    assert dataclasses.asdict(rejected)["payload"].original == b'{ "a" : 1 }'
    assert pickle.loads(pickle.dumps(rejected)).payload.original == b'{ "a" : 1 }'


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_the_mowers_bytes_survive_the_re_encoding_exactly() -> None:
    """Spacing, key order, number spelling and escapes are the mower's, not json.dumps's."""
    wire = b'{ "battery":50.0,\n "state" : "isDocked", "name": "L\\u00e9a" }'

    received, handler = recording_handler()
    mqtt = make(TCP_KWARGS)
    mqtt.on_message = handler
    mqtt._on_message(mqtt.client, None, FakeMessage(STATE_TOPIC, wire))
    await drain()
    ((_, payload, _),) = received
    assert payload.original is wire
    assert payload != wire
    assert json.loads(payload) == {**json.loads(wire), "device_id": "dev-1"}


# ---- connection events with their context ---------------------------------------------------------


def recording_events(mqtt: NavimowMQTT) -> list[mqtt_module.ConnectionEvent]:
    events: list[mqtt_module.ConnectionEvent] = []

    async def on_connection_event(event: mqtt_module.ConnectionEvent) -> None:
        events.append(event)

    mqtt.on_connection_event = on_connection_event
    return events


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_each_connection_change_is_an_event_with_its_client_id_and_reason() -> None:
    mqtt = make(TCP_KWARGS)
    events = recording_events(mqtt)
    client_id = mqtt.client_id
    before = datetime.now(UTC)
    mqtt._on_connect(mqtt.client, None, {}, NOT_AUTHORIZED, None)
    mqtt._on_connect_fail(mqtt.client, None)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)
    mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
    await drain()
    assert [(e.kind, e.client_id, e.reason, e.rebuilds) for e in events] == [
        ("connect_failed", client_id, "refused: Not authorized (135)", 0),
        ("connect_failed", client_id, "connection failed before CONNACK", 0),
        ("connected", client_id, None, 0),
        ("disconnected", client_id, "Unspecified error", 0),
        ("disconnected", client_id, "requested", 0),
    ]
    assert all(before <= e.at <= datetime.now(UTC) and e.at.tzinfo is UTC for e in events)
    # Each event carries the stamp its attribute keeps, not a second reading of the clock.
    assert events[1].at is mqtt.last_connect_failed_at
    assert events[2].at is mqtt.last_connected_at
    assert events[4].at is mqtt.last_disconnected_at
    with pytest.raises(dataclasses.FrozenInstanceError):
        events[0].reason = "changed"  # type: ignore[misc]
    assert mower_sdk.ConnectionEvent is mqtt_module.ConnectionEvent
    assert "ConnectionEvent" in mower_sdk.__all__


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_an_event_delivered_after_a_rebuild_names_the_client_it_came_from() -> None:
    """The zero-argument hook can only read the attributes, which the rebuild has changed by then."""

    mqtt = make(TCP_KWARGS)
    events = recording_events(mqtt)
    read_by_plain_hook: list[tuple[str, str | None]] = []

    async def on_disconnected() -> None:
        read_by_plain_hook.append((mqtt.client_id, mqtt.last_disconnect_reason))

    mqtt.on_disconnected = on_disconnected
    mqtt.connect_async()
    old_id = mqtt.client_id
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt._on_disconnect(mqtt.client, None, {}, UNSPECIFIED, None)  # scheduled, not yet run
    mqtt.rebuild(reason="watchdog")
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
    await drain()
    new_id = mqtt.client_id
    assert new_id != old_id
    assert [(e.kind, e.client_id, e.reason, e.rebuilds) for e in events] == [
        ("connected", old_id, None, 0),
        ("disconnected", old_id, "Unspecified error", 0),
        ("connected", new_id, None, 1),
        ("disconnected", new_id, "requested", 1),
    ]
    # What the plain hook reads when it runs: the new client's id and latest reason, twice.
    assert read_by_plain_hook == [(new_id, "requested"), (new_id, "requested")]


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_the_plain_hooks_still_run_beside_the_event_hook() -> None:
    mqtt = make(TCP_KWARGS)
    events = recording_events(mqtt)
    plain: list[Any] = []

    async def on_connected() -> None:
        plain.append("connected")

    async def on_disconnected() -> None:
        plain.append("disconnected")

    async def on_connect_fail(reason: str) -> None:
        plain.append(("connect_fail", reason))

    mqtt.on_connected, mqtt.on_disconnected, mqtt.on_connect_fail = on_connected, on_disconnected, on_connect_fail
    mqtt._on_connect_fail(mqtt.client, None)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt._on_disconnect(mqtt.client, None, {}, SUCCESS, None)
    await drain()
    assert plain == [("connect_fail", "connection failed before CONNACK"), "connected", "disconnected"]
    assert [e.kind for e in events] == ["connect_failed", "connected", "disconnected"]


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_no_event_for_a_replaced_client_or_without_the_hook() -> None:
    mqtt = make(TCP_KWARGS)
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)  # no hook: nothing scheduled, nothing raised
    events = recording_events(mqtt)
    mqtt.connect_async()
    old = mqtt.client
    mqtt.rebuild(reason="test")
    mqtt._on_disconnect(old, None, {}, UNSPECIFIED, None)
    mqtt._on_connect_fail(old, None)
    await drain()
    assert events == []


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_an_event_from_the_old_client_while_its_successor_is_built_names_the_old_client() -> None:
    """rebuild() sets the new client id first and self.client last; the old client's
    callbacks still pass the replaced-client guard in between, as paho's thread
    may deliver them while TLS is set up on the new client."""

    class PausingBuild(NavimowMQTT):
        def _build_new_client(self) -> Any:
            # The old client is still self.client here, and the id is already the new one.
            self._on_disconnect(self.client, None, {}, UNSPECIFIED, None)
            self._on_connect_fail(self.client, None)
            return super()._build_new_client()

    mqtt = PausingBuild(**TCP_KWARGS)
    events = recording_events(mqtt)
    old_id = mqtt.client_id
    mqtt.connect_async()
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt.rebuild(reason="watchdog")
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    await drain()
    assert mqtt.client_id != old_id
    assert [(e.kind, e.client_id, e.rebuilds) for e in events] == [
        ("connected", old_id, 0),
        ("disconnected", old_id, 0),
        ("connect_failed", old_id, 0),
        ("connected", mqtt.client_id, 1),
    ]


@pytest.mark.usefixtures("fake_paho")
@pytest.mark.asyncio
async def test_a_client_the_object_did_not_install_is_described_by_the_current_context() -> None:
    mqtt = make(TCP_KWARGS)
    events = recording_events(mqtt)
    mqtt.client = FakeClient()
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    await drain()
    assert [(e.kind, e.client_id, e.rebuilds) for e in events] == [("connected", mqtt.client_id, 0)]

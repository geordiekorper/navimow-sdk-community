"""Characterisation tests for NavimowMQTT's client setup and message handling.

A recording fake replaces ``paho.mqtt.client.Client``, so nothing connects.
Each test runs inside ``asyncio.run`` because NavimowMQTT reads the event loop
in ``__init__`` and, on Python 3.14, ``asyncio.get_event_loop()`` raises
outside a running loop.

They pin what the MQTT setup refactor must keep: the setup calls ``__init__``
makes are the ones ``_build_new_client`` makes; the topics ``subscribe_all``
subscribes with and without device ids; ``_on_connect`` calls
``subscribe_all`` with two arguments; ``_on_message`` injects ``device_id``
and re-encodes the payload; and two things a subclass can observe about
construction: ``self.client`` is assigned before the callback attributes are
read, and an override of ``_build_new_client`` is not called.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mower_sdk import mqtt as mqtt_module
from mower_sdk.models import Device
from mower_sdk.mqtt import NavimowMQTT

Call = tuple[str, tuple[Any, ...], dict[str, Any]]


class FakeClient:
    """Records every paho call made on it; connects to nothing."""

    instances: list[FakeClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[Call] = [("__init__", args, kwargs)]
        self.connected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        FakeClient.instances.append(self)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

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
    def callbacks(self) -> tuple[Any, Any, Any]:
        return (self.on_connect, self.on_disconnect, self.on_message)


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances = []
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
            ("__init__", (), {"client_id": mqtt._client_id, "transport": "websockets"}),
            ("username_pw_set", ("user", "secret"), {}),
            ("ws_set_options", (), {"path": "/mqtt", "headers": {"Authorization": "Bearer tok"}}),
            ("tls_set", (), {}),
            ("reconnect_delay_set", (), {"min_delay": 1, "max_delay": 60}),
        ]
        assert mqtt.client.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message)

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
            ("__init__", (), {"client_id": mqtt._client_id, "transport": "tcp"}),
            ("reconnect_delay_set", (), {"min_delay": 1, "max_delay": 60}),
        ]
        assert mqtt.client.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message)

    run(test)


def test_init_setup_calls_for_wss_scheme_without_ws_path(fake_paho: type[FakeClient]) -> None:
    """A wss:// broker turns TLS on, but without ws_path the transport stays tcp."""

    async def test() -> None:
        mqtt = make(WSS_NO_PATH_KWARGS)
        assert mqtt.port == 443
        assert mqtt._use_tls is True
        assert [name for name, _, _ in mqtt.client.calls] == ["__init__", "tls_set", "reconnect_delay_set"]
        assert mqtt.client.calls[0][2]["transport"] == "tcp"
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

        mqtt._on_connect(mqtt.client, None, {}, 1)
        await drain()
        assert mqtt.subscribe_calls == []
        assert connected == []

        mqtt._on_connect(mqtt.client, None, {}, 0)
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
        mqtt._on_disconnect(mqtt.client, None, 0)
        await drain()
        assert seen == []
        mqtt.on_disconnected = on_disconnected
        mqtt._on_disconnect(mqtt.client, None, 7)
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
            ("connect_async", ("broker.example.invalid", 8884, 2400), {})
        ]
        assert second.named("loop_start") == [("loop_start", (), {})]
        assert second.callbacks == (mqtt._on_connect, mqtt._on_disconnect, mqtt._on_message)

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
        assert mqtt.client.callbacks == (mqtt.connect_hook, mqtt.disconnect_hook, mqtt.message_hook)
        built = mqtt._build_new_client()
        assert built.callbacks == mqtt.client.callbacks
        assert fake_paho.instances == [mqtt.client, built]

    run(test)

"""Characterisation tests for NavimowSDK's command dispatch and consumer callbacks.

A recording fake stands in for ``NavimowMQTT``, so nothing connects; the topic
``publish_command`` uses is pinned in the MQTT client tests. The tests run
inside ``asyncio.run``, where the facade binds the running loop and hands it to
the MQTT client; one constructs outside a loop to show that None is handed over
instead, and one under a loop set as current to show that it is handed over.

Pinned at today's values: each of the four command methods publishes a
``DeviceCommandMessage`` while connected and, while not, asks the client to
connect and raises RuntimeError; consumer callbacks run in registration order;
and a callback that raises stops ``_on_mqtt_message`` there, so the later
callbacks for the same message do not run. Both are pinned so the changes that
gate the dispatch and isolate the callbacks show exactly what moved.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mower_sdk import sdk as sdk_module
from mower_sdk.models import DeviceAttributesMessage, DeviceEventMessage, DeviceStateMessage
from mower_sdk.sdk import NavimowSDK

DEVICE_ID = "dev-1"


class FakeMQTT:
    """Records what NavimowSDK asks of its MQTT client; connects to nothing."""

    instances: list[FakeMQTT] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.on_message: Any = None
        self.is_connected = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        FakeMQTT.instances.append(self)

    def connect_async(self) -> None:
        self.calls.append(("connect_async", ()))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    def publish_command(self, device_id: str, payload: dict[str, Any]) -> None:
        self.calls.append(("publish_command", (device_id, payload)))


@pytest.fixture
def fake_mqtt(monkeypatch: pytest.MonkeyPatch) -> type[FakeMQTT]:
    FakeMQTT.instances = []
    monkeypatch.setattr(sdk_module, "NavimowMQTT", FakeMQTT)
    return FakeMQTT


def run(test: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(test())


def make(**overrides: Any) -> tuple[NavimowSDK, FakeMQTT]:
    sdk = NavimowSDK(broker="broker.example.invalid", port=443, **overrides)
    (mqtt,) = FakeMQTT.instances
    return sdk, mqtt


def topic(channel: str) -> str:
    return f"/downlink/vehicle/{DEVICE_ID}/realtimeDate/{channel}"


def test_construction_passes_the_parameters_through_and_wires_on_message(
    fake_mqtt: type[FakeMQTT],
) -> None:
    async def test() -> None:
        sdk, mqtt = make(username="u", password="p", ws_path="/mqtt", auth_headers={"Authorization": "Bearer t"})
        assert mqtt.kwargs == {
            "broker": "broker.example.invalid",
            "port": 443,
            "username": "u",
            "password": "p",
            "records": [],
            "ws_path": "/mqtt",
            "auth_headers": {"Authorization": "Bearer t"},
            "loop": asyncio.get_running_loop(),
            "keepalive_seconds": 2400,
            "reconnect_min_delay": 1,
            "reconnect_max_delay": 60,
        }
        assert mqtt.on_message == sdk._on_mqtt_message
        assert sdk.is_connected is False
        sdk.connect()
        sdk.disconnect()
        assert mqtt.calls == [("connect_async", ()), ("disconnect", ())]
        assert fake_mqtt.instances == [mqtt]

    run(test)


def test_construction_outside_a_running_loop_hands_over_no_loop_and_creates_none(
    fake_mqtt: type[FakeMQTT],
) -> None:
    """The MQTT client binds the loop at its first connect instead. No loop is created and there
    is no asyncio warning: with no current loop set, asyncio.get_event_loop() creates a loop on
    3.11, warns and creates one on 3.12 and 3.13 and raises on 3.14, so on 3.11 to 3.13 the
    policy's current-loop slot is read instead and only 3.14 asks it."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        sdk = NavimowSDK(broker="broker.example.invalid", port=443)
    (mqtt,) = fake_mqtt.instances
    assert mqtt.kwargs["loop"] is None
    assert mqtt.on_message == sdk._on_mqtt_message


def test_construction_under_a_loop_set_as_current_hands_it_over(fake_mqtt: type[FakeMQTT]) -> None:
    current = asyncio.new_event_loop()
    asyncio.set_event_loop(current)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            sdk = NavimowSDK(broker="broker.example.invalid", port=443)
    finally:
        asyncio.set_event_loop(None)
        current.close()
    (mqtt,) = fake_mqtt.instances
    assert mqtt.kwargs["loop"] is current
    assert mqtt.on_message == sdk._on_mqtt_message


COMMANDS = [
    pytest.param("start_mowing", (), "start_mowing", {}, id="start_mowing"),
    pytest.param("pause", (), "pause", {}, id="pause"),
    pytest.param("return_to_base", (), "return_to_base", {}, id="return_to_base"),
    pytest.param("set_blade_height", (30,), "set_blade_height", {"height": 30}, id="set_blade_height"),
]


@pytest.mark.parametrize(("method", "args", "command", "params"), COMMANDS)
def test_command_publishes_a_command_message_while_connected(
    fake_mqtt: type[FakeMQTT], method: str, args: tuple, command: str, params: dict[str, Any]
) -> None:
    async def test() -> None:
        sdk, mqtt = make()
        mqtt.is_connected = True
        getattr(sdk, method)(DEVICE_ID, *args)
        ((name, (device_id, payload)),) = mqtt.calls
        assert name == "publish_command"
        assert device_id == DEVICE_ID
        assert payload == {"id": payload["id"], "device_id": DEVICE_ID, "command": command, "params": params}
        assert payload["id"].startswith("cmd-")
        uuid.UUID(payload["id"][len("cmd-") :])  # a fresh id per command
        assert fake_mqtt.instances == [mqtt]

    run(test)


@pytest.mark.parametrize(("method", "args", "command", "params"), COMMANDS)
def test_command_asks_to_connect_and_raises_while_disconnected(
    fake_mqtt: type[FakeMQTT], method: str, args: tuple, command: str, params: dict[str, Any]  # noqa: ARG001
) -> None:
    async def test() -> None:
        sdk, mqtt = make()
        with pytest.raises(RuntimeError, match="^MQTT not connected$"):
            getattr(sdk, method)(DEVICE_ID, *args)
        assert mqtt.calls == [("connect_async", ())]
        assert fake_mqtt.instances == [mqtt]

    run(test)


CHANNELS = [
    pytest.param(
        "state",
        "on_state",
        b'{"state": "isRunning", "battery": 80}',
        DeviceStateMessage(
            device_id=DEVICE_ID, timestamp=None, state="mowing", battery=80, metrics={"raw_state": "isRunning"}
        ),
        id="state",
    ),
    pytest.param(
        "event",
        "on_event",
        b'{"type": "system", "event": "started"}',
        DeviceEventMessage(device_id=DEVICE_ID, timestamp=None, type="system", event="started"),
        id="event",
    ),
    pytest.param(
        "attributes",
        "on_attributes",
        b'{"attributes": {"a": 1}}',
        DeviceAttributesMessage(device_id=DEVICE_ID, attributes={"a": 1}),
        id="attributes",
    ),
]


@pytest.mark.parametrize(("channel", "register", "payload", "message"), CHANNELS)
def test_callbacks_run_in_registration_order(
    fake_mqtt: type[FakeMQTT], channel: str, register: str, payload: bytes, message: Any
) -> None:
    async def test() -> None:
        sdk, mqtt = make()
        seen: list[tuple[str, Any]] = []
        getattr(sdk, register)(lambda msg: seen.append(("first", msg)))
        getattr(sdk, register)(lambda msg: seen.append(("second", msg)))
        await sdk._on_mqtt_message(topic(channel), payload, DEVICE_ID)
        assert seen == [("first", message), ("second", message)]
        assert mqtt.calls == []
        assert fake_mqtt.instances == [mqtt]

    run(test)


@pytest.mark.parametrize(("channel", "register", "payload", "message"), CHANNELS)
def test_a_raising_callback_stops_the_later_callbacks(
    fake_mqtt: type[FakeMQTT], channel: str, register: str, payload: bytes, message: Any
) -> None:
    async def test() -> None:
        sdk, mqtt = make()
        seen: list[Any] = []

        def failing(_message: Any) -> None:
            raise RuntimeError("consumer failed")

        getattr(sdk, register)(failing)
        getattr(sdk, register)(seen.append)
        with pytest.raises(RuntimeError, match="^consumer failed$"):
            await sdk._on_mqtt_message(topic(channel), payload, DEVICE_ID)
        assert seen == []
        # The cache is written before the callbacks run, so it holds the message anyway.
        cached = {"state": sdk.get_cached_state, "attributes": sdk.get_cached_attributes}.get(channel)
        if cached is not None:
            assert cached(DEVICE_ID) == message
        assert fake_mqtt.instances == [mqtt]

    run(test)

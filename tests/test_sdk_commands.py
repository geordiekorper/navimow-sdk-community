"""Characterisation tests for NavimowSDK's command dispatch and consumer callbacks.

A recording fake stands in for ``NavimowMQTT``, so nothing connects; the topic
``publish_command`` uses is pinned in the MQTT client tests. Most tests are
async tests, where the facade binds the running loop and hands it to the MQTT
client; one constructs outside a loop to show that None is handed over
instead, and one under a loop set as current to show that it is handed over.

The four command methods raise ``MowerUnsupportedOperationError`` before
touching the client unless the facade was constructed with
``allow_experimental_mqtt_commands=True``; with the flag, each publishes a
``DeviceCommandMessage`` while connected and, while not, asks the client to
connect and raises RuntimeError. Consumer callbacks run in registration order,
and a callback that raises is logged with its traceback while the later
callbacks for the same message still run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import warnings
from typing import Any

import pytest

import mower_sdk
from mower_sdk.errors import MowerUnsupportedOperationError
from mower_sdk.models import DeviceAttributesMessage, DeviceEventMessage, DeviceStateMessage
from mower_sdk.sdk import NavimowSDK

from .fakes import DEVICE_ID, FakeMQTT, topic


def make(**overrides: Any) -> tuple[NavimowSDK, FakeMQTT]:
    sdk = NavimowSDK(broker="broker.example.invalid", port=443, **overrides)
    (mqtt,) = FakeMQTT.instances
    return sdk, mqtt


@pytest.mark.asyncio
async def test_construction_passes_the_parameters_through_and_wires_on_message(
    fake_mqtt: type[FakeMQTT],
) -> None:
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
        "keepalive_seconds": 60,
        "reconnect_min_delay": 1,
        "reconnect_max_delay": 60,
        "subscribe_location": False,
        "extra_topics": None,
    }
    assert mqtt.on_message == sdk._on_mqtt_message
    assert sdk.mqtt is mqtt
    assert sdk.is_connected is False
    sdk.connect()
    sdk.disconnect()
    assert mqtt.calls == [("connect_async", ()), ("disconnect", ())]
    assert fake_mqtt.instances == [mqtt]


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
# What the refusal names as the supported alternative; blade height has none.
REST_ALTERNATIVE = {
    "start_mowing": "MowerAPI.async_send_command(device_id, MowerCommand.START)",
    "pause": "MowerAPI.async_send_command(device_id, MowerCommand.PAUSE)",
    "return_to_base": "MowerAPI.async_send_command(device_id, MowerCommand.DOCK)",
    "set_blade_height": "No supported call sets the blade height",
}


def test_the_gate_error_is_exported_from_the_package() -> None:
    assert mower_sdk.MowerUnsupportedOperationError is MowerUnsupportedOperationError
    assert "MowerUnsupportedOperationError" in mower_sdk.__all__
    assert issubclass(MowerUnsupportedOperationError, Exception)


@pytest.mark.parametrize("connected", [False, True], ids=["disconnected", "connected"])
@pytest.mark.parametrize(("method", "args", "command", "params"), COMMANDS)
@pytest.mark.asyncio
async def test_command_is_refused_by_default_before_the_client_is_touched(
    fake_mqtt: type[FakeMQTT],
    method: str,
    args: tuple,
    command: str,
    params: dict[str, Any],  # noqa: ARG001
    connected: bool,
) -> None:
    sdk, mqtt = make()
    mqtt.is_connected = connected
    with pytest.raises(MowerUnsupportedOperationError) as info:
        getattr(sdk, method)(DEVICE_ID, *args)
    text = str(info.value)
    assert text.startswith(f"MQTT command {command!r} not sent: ")
    assert f"navimow/{DEVICE_ID}/command" in text
    assert REST_ALTERNATIVE[command] in text
    assert "allow_experimental_mqtt_commands=True" in text
    assert mqtt.calls == []
    assert fake_mqtt.instances == [mqtt]


@pytest.mark.parametrize(("method", "args", "command", "params"), COMMANDS)
@pytest.mark.asyncio
async def test_command_publishes_a_command_message_while_connected(
    fake_mqtt: type[FakeMQTT], method: str, args: tuple, command: str, params: dict[str, Any]
) -> None:
    sdk, mqtt = make(allow_experimental_mqtt_commands=True)
    assert "allow_experimental_mqtt_commands" not in mqtt.kwargs  # the gate has one layer
    mqtt.is_connected = True
    getattr(sdk, method)(DEVICE_ID, *args)
    ((name, (device_id, payload)),) = mqtt.calls
    assert name == "publish_command"
    assert device_id == DEVICE_ID
    assert payload == {"id": payload["id"], "device_id": DEVICE_ID, "command": command, "params": params}
    assert payload["id"].startswith("cmd-")
    uuid.UUID(payload["id"][len("cmd-") :])  # a fresh id per command
    assert fake_mqtt.instances == [mqtt]


@pytest.mark.parametrize(("method", "args", "command", "params"), COMMANDS)
@pytest.mark.asyncio
async def test_command_asks_to_connect_and_raises_while_disconnected(
    fake_mqtt: type[FakeMQTT],
    method: str,
    args: tuple,
    command: str,  # noqa: ARG001
    params: dict[str, Any],  # noqa: ARG001
) -> None:
    sdk, mqtt = make(allow_experimental_mqtt_commands=True)
    with pytest.raises(RuntimeError, match="^MQTT not connected$"):
        getattr(sdk, method)(DEVICE_ID, *args)
    assert mqtt.calls == [("connect_async", ())]
    assert fake_mqtt.instances == [mqtt]


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
@pytest.mark.asyncio
async def test_callbacks_run_in_registration_order(
    fake_mqtt: type[FakeMQTT], channel: str, register: str, payload: bytes, message: Any
) -> None:
    sdk, mqtt = make()
    seen: list[tuple[str, Any]] = []
    getattr(sdk, register)(lambda msg: seen.append(("first", msg)))
    getattr(sdk, register)(lambda msg: seen.append(("second", msg)))
    await sdk._on_mqtt_message(topic(channel), payload, DEVICE_ID)
    assert seen == [("first", message), ("second", message)]
    assert mqtt.calls == []
    assert fake_mqtt.instances == [mqtt]


@pytest.mark.parametrize(("channel", "register", "payload", "message"), CHANNELS)
@pytest.mark.asyncio
async def test_a_raising_callback_is_logged_and_the_later_callbacks_still_run(
    fake_mqtt: type[FakeMQTT],
    caplog: pytest.LogCaptureFixture,
    channel: str,
    register: str,
    payload: bytes,
    message: Any,
) -> None:
    sdk, mqtt = make()
    seen: list[tuple[str, Any]] = []

    def failing(_message: Any) -> None:
        raise RuntimeError("consumer failed")

    def failing_too(_message: Any) -> None:
        raise ValueError("another consumer failed")

    getattr(sdk, register)(failing)
    getattr(sdk, register)(lambda msg: seen.append(("second", msg)))
    getattr(sdk, register)(failing_too)
    getattr(sdk, register)(lambda msg: seen.append(("fourth", msg)))
    with caplog.at_level(logging.ERROR, logger="mower_sdk.sdk"):
        await sdk._on_mqtt_message(topic(channel), payload, DEVICE_ID)
    assert seen == [("second", message), ("fourth", message)]

    records = [r for r in caplog.records if r.name == "mower_sdk.sdk"]
    assert [(r.levelno, type(r.exc_info[1])) for r in records] == [
        (logging.ERROR, RuntimeError),
        (logging.ERROR, ValueError),
    ]
    for record in records:
        assert record.getMessage().startswith(f"Navimow {channel} callback ")
        assert record.getMessage().endswith(f" failed for device {DEVICE_ID}")
    cached = {"state": sdk.get_cached_state, "attributes": sdk.get_cached_attributes}.get(channel)
    if cached is not None:
        assert cached(DEVICE_ID) == message
    assert fake_mqtt.instances == [mqtt]


@pytest.mark.asyncio
async def test_update_mqtt_credentials_passes_everything_through(fake_mqtt: type[FakeMQTT]) -> None:
    sdk, mqtt = make()
    sdk.update_mqtt_credentials(password="p")
    sdk.update_mqtt_credentials("u", "p", {"Authorization": "Bearer t"}, force_reconnect=True)
    sdk.update_mqtt_credentials(broker="moved.example.invalid", port=8443, ws_path="/mqtt/1")
    keep = {"broker": None, "port": None, "ws_path": None}
    assert mqtt.calls == [
        (
            "update_credentials",
            ((), {"username": None, "password": "p", "auth_headers": None, **keep, "force_reconnect": False}),
        ),
        (
            "update_credentials",
            (
                (),
                {
                    "username": "u",
                    "password": "p",
                    "auth_headers": {"Authorization": "Bearer t"},
                    **keep,
                    "force_reconnect": True,
                },
            ),
        ),
        (
            "update_credentials",
            (
                (),
                {
                    "username": None,
                    "password": None,
                    "auth_headers": None,
                    "broker": "moved.example.invalid",
                    "port": 8443,
                    "ws_path": "/mqtt/1",
                    "force_reconnect": False,
                },
            ),
        ),
    ]
    assert fake_mqtt.instances == [mqtt]


@pytest.mark.asyncio
async def test_a_command_without_a_known_alternative_gets_a_neutral_refusal(fake_mqtt: type[FakeMQTT]) -> None:
    sdk, mqtt = make()
    with pytest.raises(MowerUnsupportedOperationError) as info:
        sdk._send_mqtt_command(DEVICE_ID, "set_cutting_pattern", {})
    text = str(info.value)
    assert "No supported alternative is known;" in text
    assert "blade height" not in text
    assert mqtt.calls == []
    assert fake_mqtt.instances == [mqtt]

"""MowerState and StateSource (mower_sdk.navimow_client): one state for a mower, either transport.

Both builders are pure functions of their inputs, so the tests build a state
message or a REST status the way the SDK does (``from_dict``, then the receipt
time the facade would set) and read every field back.
"""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import pytest

import mower_sdk
from mower_sdk import navimow_client
from mower_sdk.location import TargetZone, target_zone
from mower_sdk.models import (
    DeviceLocation,
    DeviceStateMessage,
    DeviceStatus,
    MowerError,
    MowerStatus,
)
from mower_sdk.navimow_client import MowerState, StateSource

DEVICE = "dev-1"
RECEIVED = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MONOTONIC = 1000.0
LOCATION = DeviceLocation(device_id=DEVICE, x=1.5, y=-2.0, vehicle_state=3, partition_ids=(7, 3))


def message(payload: dict[str, Any], received_at: datetime | None = RECEIVED) -> DeviceStateMessage:
    """A state message as the facade hands it on: read from the payload, then stamped."""
    result = DeviceStateMessage.from_dict({"device_id": DEVICE, **payload})
    result.received_at = received_at
    return result


def from_message(
    payload: dict[str, Any],
    *,
    location: DeviceLocation | None = None,
    received_monotonic: float = MONOTONIC,
) -> MowerState:
    return MowerState.from_state_message(
        message(payload), location=location, received_monotonic=received_monotonic
    )


def from_rest(
    entry: dict[str, Any],
    *,
    location: DeviceLocation | None = None,
    received_monotonic: float = MONOTONIC,
) -> MowerState:
    return MowerState.from_status(
        DeviceStatus.from_dict({"device_id": DEVICE, **entry}),
        location=location,
        received_at=RECEIVED,
        received_monotonic=received_monotonic,
    )


def test_every_field_from_a_state_message() -> None:
    source = message(
        {
            "state": "isRunning",
            "battery": 55,
            "timestamp": 1790000000,
            "error": {"code": "lifted", "message": "up"},
        }
    )
    state = MowerState.from_state_message(source, location=LOCATION, received_monotonic=MONOTONIC)
    assert state.device_id == DEVICE
    assert state.status is MowerStatus.MOWING
    assert state.raw_state == "isRunning"
    assert state.battery == 55
    assert state.error_code is MowerError.LIFTED
    assert state.error_message == "up"
    assert state.source is StateSource.MQTT
    assert state.observed_at == 1790000000000
    assert state.received_at == RECEIVED
    assert state.location is LOCATION
    assert state.received_monotonic == MONOTONIC
    assert state.message is source
    assert state.rest_status is None


@pytest.mark.parametrize(
    ("timestamp", "observed_at"),
    [
        (1790000000, 1790000000000),  # seconds
        (1790000000123, 1790000000123),  # milliseconds
        ("1790000000", 1790000000000),  # a number sent as text
        (None, None),  # no time
    ],
)
def test_observed_at_is_the_message_time_in_milliseconds(
    timestamp: Any, observed_at: int | None
) -> None:
    payload: dict[str, Any] = {"state": "isRunning"}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    assert from_message(payload).observed_at == observed_at


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        ({"code": "lifted", "message": "up"}, MowerError.LIFTED, "up"),
        ({"error_code": "stuck", "message": "wheel"}, MowerError.STUCK, "wheel"),
        ({"code": "lifted", "error_code": "stuck"}, MowerError.LIFTED, None),  # code first
        ({"code": "somethingNew"}, MowerError.UNKNOWN, None),
        ({"message": "no code"}, MowerError.NONE, "no code"),
        ({}, MowerError.NONE, None),
        ("not a dict", MowerError.NONE, None),
        (None, MowerError.NONE, None),
    ],
)
def test_the_error_dict_is_read_both_ways(error: Any, code: MowerError, text: str | None) -> None:
    payload: dict[str, Any] = {"state": "error"}
    if error is not None:
        payload["error"] = error
    state = from_message(payload)
    assert (state.error_code, state.error_message) == (code, text)


def test_every_field_from_a_rest_status() -> None:
    source = DeviceStatus.from_dict(
        {
            "device_id": DEVICE,
            "vehicleState": "isDocked",
            "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": "80"}],
            "error_code": "stuck",
            "error_message": "wheel",
        }
    )
    state = MowerState.from_status(
        source, location=LOCATION, received_at=RECEIVED, received_monotonic=MONOTONIC
    )
    assert state.device_id == DEVICE
    assert state.status is MowerStatus.DOCKED
    assert state.raw_state == "isDocked"
    assert state.battery == 80
    assert state.error_code is MowerError.STUCK
    assert state.error_message == "wheel"
    assert state.source is StateSource.REST
    assert state.observed_at is None  # the cloud's status entries carry no timestamp
    assert state.received_at == RECEIVED
    assert state.location is LOCATION
    assert state.received_monotonic == MONOTONIC
    assert state.message is None
    assert state.rest_status is source


@pytest.mark.parametrize(
    ("timestamp", "observed_at"),
    [(1790000000, 1790000000000), (1790000000123, 1790000000123), (None, None)],
)
def test_observed_at_from_a_status_with_a_timestamp(
    timestamp: Any, observed_at: int | None
) -> None:
    state = from_rest({"vehicleState": "isDocked", "timestamp": timestamp})
    assert state.observed_at == observed_at


def test_a_sparse_rest_status_gives_the_none_fields() -> None:
    state = from_rest({})
    assert state.status is MowerStatus.UNKNOWN
    assert (state.raw_state, state.battery) == (None, None)
    assert (state.error_code, state.error_message) == (MowerError.NONE, None)
    assert state.observed_at is None


def test_the_location_is_carried_as_given_from_either_builder() -> None:
    assert from_message({"state": "idle"}, location=LOCATION).location is LOCATION
    assert from_rest({"vehicleState": "idle"}, location=LOCATION).location is LOCATION
    assert from_message({"state": "idle"}).location is None
    assert from_rest({"vehicleState": "idle"}).location is None


@pytest.mark.parametrize(
    ("location", "raw", "zone"),
    [
        (DeviceLocation(device_id=DEVICE, partition_ids=(7, 3)), "idle", 7),
        (DeviceLocation(device_id=DEVICE, partition_ids=(7, 3)), "isRunning", 7),
        (DeviceLocation(device_id=DEVICE, partition_ids=()), "isRunning", TargetZone.ALL),
        (DeviceLocation(device_id=DEVICE, partition_ids=()), "isPaused", TargetZone.ALL),
        (DeviceLocation(device_id=DEVICE, partition_ids=()), "idle", TargetZone.NONE),
        (DeviceLocation(device_id=DEVICE, partition_ids=()), "isDocked", TargetZone.NONE),
        (DeviceLocation(device_id=DEVICE), "isRunning", None),  # no target report yet
        (None, "isRunning", None),
    ],
)
def test_target_zone_is_the_helper_read_with_the_record_and_the_status(
    location: DeviceLocation | None, raw: str, zone: int | TargetZone | None
) -> None:
    for state in (
        from_message({"state": raw}, location=location),
        from_rest({"vehicleState": raw}, location=location),
    ):
        assert state.target_zone == zone
        assert state.target_zone == target_zone(location, state.status)


def test_an_unknown_raw_state_is_unknown_and_kept_on_both_sides() -> None:
    mqtt = from_message({"state": "somethingNew"})
    assert (mqtt.status, mqtt.raw_state) == (MowerStatus.UNKNOWN, "somethingNew")
    rest = from_rest({"vehicleState": "somethingNew"})
    assert (rest.status, rest.raw_state) == (MowerStatus.UNKNOWN, "somethingNew")


def test_raw_state_from_a_message() -> None:
    # The canonical state equals the raw one: metrics has no raw_state, the state is the raw state.
    canonical = from_message({"state": "mowing"})
    assert canonical.message is not None and canonical.message.metrics is None
    assert canonical.raw_state == "mowing"
    # An explicit unknown in the payload is a state the payload named.
    assert from_message({"state": "unknown"}).raw_state == "unknown"
    # The payload named a state under another key.
    assert from_message({"vehicleState": "isPaused"}).raw_state == "isPaused"
    # A raw value that is not text is kept as text.
    assert from_message({"vehicleState": 1}).raw_state == "1"
    # No state named: a payload without a state key, or with a null one.
    assert from_message({"battery": 5}).raw_state is None
    assert from_message({"state": None}).raw_state is None
    # A message built by hand, without a raw payload, names a state unless it is "unknown".
    by_hand = DeviceStateMessage(
        device_id=DEVICE, timestamp=None, state="unknown", received_at=RECEIVED
    )
    unknown = MowerState.from_state_message(by_hand, location=None, received_monotonic=MONOTONIC)
    assert (unknown.status, unknown.raw_state) == (MowerStatus.UNKNOWN, None)
    named = replace(by_hand, state="docked")
    assert (
        MowerState.from_state_message(named, location=None, received_monotonic=MONOTONIC).raw_state
        == "docked"
    )


def test_raw_state_from_a_rest_status() -> None:
    assert from_rest({"vehicleState": "isDocked"}).raw_state == "isDocked"
    assert from_rest({"vehicleState": 1}).raw_state == "1"  # an int becomes its text
    under_status = from_rest({"status": "docked"})
    assert (under_status.status, under_status.raw_state) == (MowerStatus.DOCKED, None)
    under_state = from_rest({"state": "isDocked"})
    assert (under_state.status, under_state.raw_state) == (MowerStatus.DOCKED, None)
    no_extra = MowerState.from_status(
        DeviceStatus(device_id=DEVICE, status=MowerStatus.IDLE, battery=None),
        location=None,
        received_at=RECEIVED,
        received_monotonic=MONOTONIC,
    )
    assert no_extra.raw_state is None


def test_a_message_without_a_receipt_time_is_refused() -> None:
    with pytest.raises(ValueError, match="received_at"):
        MowerState.from_state_message(
            message({"state": "idle"}, received_at=None), location=None, received_monotonic=0.0
        )
    with pytest.raises(ValueError, match="received_at"):
        MowerState.from_state_message(
            DeviceStateMessage.from_dict({"device_id": DEVICE, "state": "idle"}),
            location=None,
            received_monotonic=0.0,
        )


def test_age_is_measured_from_received_monotonic() -> None:
    state = from_message({"state": "idle"}, received_monotonic=MONOTONIC)
    assert state.age(now=MONOTONIC + 12.5) == 12.5
    assert state.age(now=MONOTONIC) == 0.0
    before = time.monotonic()
    live = from_message({"state": "idle"}, received_monotonic=before)
    age = live.age()
    assert 0.0 <= age <= time.monotonic() - before


def test_equality_compares_the_status_half_and_the_location_only() -> None:
    payload = {"state": "isRunning", "battery": 55, "timestamp": 1790000000}
    first = from_message({**payload, "signal_strength": -70}, location=LOCATION)
    second = from_message(
        {**payload, "signal_strength": -40}, location=LOCATION, received_monotonic=MONOTONIC + 60
    )
    assert first == second
    assert first.message != second.message
    assert first.received_monotonic != second.received_monotonic
    assert first != from_message({**payload, "battery": 54}, location=LOCATION)
    assert first != replace(first, location=None)
    assert first != replace(first, location=DeviceLocation(device_id=DEVICE, partition_ids=(7,)))


def test_equality_ignores_the_rest_source_object_and_sees_the_source() -> None:
    entry = {"vehicleState": "isRunning", "battery": 55}
    first = from_rest({**entry, "mowing_time": 600}, location=LOCATION)
    second = from_rest({**entry, "mowing_time": 900}, location=LOCATION, received_monotonic=1.0)
    assert first == second
    assert first.rest_status != second.rest_status
    # The same status half from the other transport is another state: source is compared.
    mqtt = from_message({"state": "isRunning", "battery": 55}, location=LOCATION)
    assert replace(first, observed_at=mqtt.observed_at) != mqtt


def test_repr_hides_the_three_uncompared_fields() -> None:
    state = from_message(
        {"state": "isRunning", "error": {"code": "lifted", "message": "up"}}, location=LOCATION
    )
    text = repr(state)
    assert text.startswith("MowerState(device_id='dev-1', ")
    assert "error_message='up'" in text and "source=<StateSource.MQTT: 'mqtt'>" in text
    assert "received_monotonic" not in text
    assert ", message=" not in text
    assert "rest_status" not in text


def test_the_state_is_frozen() -> None:
    state = from_message({"state": "idle"})
    with pytest.raises(FrozenInstanceError):
        state.battery = 1  # type: ignore[misc]


def test_state_source_values() -> None:
    assert issubclass(StateSource, StrEnum)
    assert (StateSource.MQTT, StateSource.REST) == ("mqtt", "rest")
    assert [member.value for member in StateSource] == ["mqtt", "rest"]


def test_both_names_are_exported_from_the_module_and_the_package_root() -> None:
    assert {"MowerState", "StateSource"} <= set(navimow_client.__all__)
    assert mower_sdk.MowerState is MowerState
    assert mower_sdk.StateSource is StateSource
    for name in ("MowerState", "StateSource"):
        assert name in mower_sdk.__all__

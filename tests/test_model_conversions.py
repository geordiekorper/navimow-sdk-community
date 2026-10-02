"""DeviceStateMessage.from_status() and DeviceStatus.from_state_message(), in both directions.

Both build new objects and leave their source untouched. A REST status and an
MQTT state message share device_id, status, battery, position, the error,
signal_strength and the timestamp; mowing_time, total_mowing_time and extra have
no field on a state message.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from mower_sdk.models import DeviceStateMessage, DeviceStatus, MowerError, MowerStatus

RECEIVED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
FULL_STATUS = DeviceStatus(
    device_id="dev-1",
    status=MowerStatus.DOCKED,
    battery=80,
    position={"lat": 1.0, "lng": 2.0},
    error_code=MowerError.STUCK,
    error_message="wheel stuck",
    mowing_time=600,
    total_mowing_time=36000,
    signal_strength=-60,
    timestamp=1790000000,
    extra={"vehicleState": "isDocked"},
)
SHARED = ("device_id", "status", "battery", "position", "error_code", "error_message", "signal_strength", "timestamp")
NOT_CARRIED = ("mowing_time", "total_mowing_time", "extra")


def test_from_status_carries_the_shared_fields_and_normalises_the_time() -> None:
    before = copy.deepcopy(FULL_STATUS)
    message = DeviceStateMessage.from_status(FULL_STATUS, received_at=RECEIVED)
    assert message == DeviceStateMessage(
        device_id="dev-1",
        timestamp=1790000000000,
        state="docked",
        battery=80,
        signal_strength=-60,
        position={"lat": 1.0, "lng": 2.0},
        error={"code": "stuck", "message": "wheel stuck"},
    )
    assert (message.received_at, message.raw, message.metrics) == (RECEIVED, None, None)
    assert before == FULL_STATUS


def test_from_status_from_a_sparse_status() -> None:
    message = DeviceStateMessage.from_status(DeviceStatus(device_id="d", status=MowerStatus.UNKNOWN, battery=None))
    assert message == DeviceStateMessage(device_id="d", timestamp=None, state="unknown")
    assert (message.error, message.received_at) == (None, None)
    assert DeviceStateMessage.from_status(replace(FULL_STATUS, timestamp=1790000000123)).timestamp == 1790000000123


def test_from_state_message_from_a_full_message() -> None:
    payload = {
        "device_id": "dev-1", "state": "isRunning", "battery": 55, "timestamp": 1790000000,
        "signal_strength": -70, "position": {"lat": 3.0, "lng": 4.0}, "error": {"code": "lifted", "message": "up"},
    }
    message = DeviceStateMessage.from_dict(payload)
    before = copy.deepcopy(message)
    status = DeviceStatus.from_state_message(message, fallback_status=MowerStatus.DOCKED, fallback_battery=10)
    assert status == DeviceStatus(
        device_id="dev-1",
        status=MowerStatus.MOWING,
        battery=55,
        position={"lat": 3.0, "lng": 4.0},
        error_code=MowerError.LIFTED,
        error_message="up",
        signal_strength=-70,
        timestamp=1790000000000,
        extra={"vehicleState": "isRunning"},
    )
    assert message == before and message.raw == before.raw


def test_from_state_message_from_a_sparse_message() -> None:
    status = DeviceStatus.from_state_message(DeviceStateMessage.from_dict({"device_id": "d"}))
    assert status == DeviceStatus(device_id="d", status=MowerStatus.UNKNOWN, battery=None)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"device_id": "d", "battery": 5}, MowerStatus.DOCKED),  # no state at all: the fallback
        ({"device_id": "d", "state": None}, MowerStatus.DOCKED),  # a null state is no state
        ({"device_id": "d", "state": "unknown"}, MowerStatus.UNKNOWN),  # an explicit unknown is kept
        ({"device_id": "d", "vehicleState": "isPaused"}, MowerStatus.PAUSED),
        ({"device_id": "d", "state": "somethingNew"}, MowerStatus.UNKNOWN),  # a value the enum lacks
        ({"device_id": "d", "state": "Offline"}, MowerStatus.OFFLINE),
    ],
    ids=["absent", "null", "explicit_unknown", "vehicle_state", "unrecognised", "offline"],
)
def test_the_state_fallback_applies_only_when_the_payload_carries_no_state(
    payload: dict[str, object], expected: MowerStatus
) -> None:
    message = DeviceStateMessage.from_dict(payload)
    assert DeviceStatus.from_state_message(message, fallback_status=MowerStatus.DOCKED).status is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("unknown", MowerStatus.DOCKED), ("mowing", MowerStatus.MOWING)],
    ids=["unknown", "known"],
)
def test_a_message_built_by_hand_falls_back_on_unknown(state: str, expected: MowerStatus) -> None:
    message = DeviceStateMessage(device_id="d", timestamp=None, state=state)
    assert DeviceStatus.from_state_message(message, fallback_status=MowerStatus.DOCKED).status is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"device_id": "d"}, 40), ({"device_id": "d", "battery": "n/a"}, 40), ({"device_id": "d", "battery": 0}, 0)],
    ids=["absent", "unreadable", "zero"],
)
def test_the_battery_fallback_applies_only_without_a_readable_battery(payload: dict[str, object], expected: int) -> None:
    message = DeviceStateMessage.from_dict(payload)
    assert DeviceStatus.from_state_message(message, fallback_battery=40).battery == expected


def test_without_fallbacks_nothing_is_invented() -> None:
    status = DeviceStatus.from_state_message(DeviceStateMessage.from_dict({"device_id": "d"}))
    assert (status.status, status.battery) == (MowerStatus.UNKNOWN, None)


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        ({"code": "rain"}, MowerError.RAIN, None),
        ({"error_code": "stuck", "message": "m"}, MowerError.STUCK, "m"),
        ({"code": "E1234"}, MowerError.UNKNOWN, None),
        ({"code": ""}, MowerError.NONE, None),
        ("not a dict", MowerError.NONE, None),
    ],
    ids=["code", "error_code_key", "unrecognised", "empty", "not_a_dict"],
)
def test_the_error_dict_becomes_the_error_code_and_message(error: object, code: MowerError, message: str | None) -> None:
    state = DeviceStateMessage(device_id="d", timestamp=None, state="error", error=error)  # type: ignore[arg-type]
    status = DeviceStatus.from_state_message(state)
    assert (status.error_code, status.error_message) == (code, message)


@pytest.mark.parametrize(("timestamp", "expected"), [(1790000000, 1790000000000), (1790000000000, 1790000000000), (None, None), (0, None)])
def test_times_are_normalised_to_milliseconds_in_both_directions(timestamp: int | None, expected: int | None) -> None:
    assert DeviceStateMessage.from_status(replace(FULL_STATUS, timestamp=timestamp)).timestamp == expected
    message = DeviceStateMessage(device_id="d", timestamp=timestamp, state="docked")
    assert DeviceStatus.from_state_message(message).timestamp == expected


def test_a_round_trip_keeps_every_shared_field() -> None:
    back = DeviceStatus.from_state_message(DeviceStateMessage.from_status(FULL_STATUS))
    for name in SHARED:
        expected = getattr(FULL_STATUS, name)
        if name == "timestamp":
            expected = 1790000000000
        assert getattr(back, name) == expected, name
    assert [getattr(back, name) for name in NOT_CARRIED] == [None, None, None]

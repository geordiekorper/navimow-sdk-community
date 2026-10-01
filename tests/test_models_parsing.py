"""Characterisation tests for the state and battery readers in mower_sdk.models.

``DeviceStatus.from_dict`` and ``DeviceStateMessage.from_dict`` share the
normaliser but read the raw state with different key precedence, and both use
the same battery reader: ``capacityRemaining``'s PERCENTAGE entry, then any
entry whose ``rawValue`` parses, then ``battery``, and None when nothing
parses (a missing, unparsable, bool or non-finite value); out-of-range numbers
pass through. The state values are pinned, falsy fallbacks included: isMapping,
inSoftwareUpdate and both spellings of offline have MowerStatus members of
their own.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import mower_sdk
from mower_sdk import models
from mower_sdk.models import (
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceStateMessage,
    DeviceStatus,
    MowerError,
    MowerStatus,
)

# Every raw-state spelling the table maps today, with its canonical value.
RAW_STATE_TABLE = [
    ("isDocked", "docked"),
    ("isIdel", "idle"),
    ("isIdle", "idle"),
    ("isMapping", "mapping"),
    ("isRunning", "mowing"),
    ("isPaused", "paused"),
    ("isDocking", "returning"),
    ("Error", "error"),
    ("error", "error"),
    ("isLifted", "error"),
    ("inSoftwareUpdate", "updating"),
    ("Self-Checking", "idle"),
    ("Self-checking", "idle"),
    ("Offline", "offline"),
    ("offline", "offline"),
]


def test_raw_state_table_is_exactly_this() -> None:
    assert dict(RAW_STATE_TABLE) == models._RAW_STATE_TO_CANONICAL


def test_the_public_raw_state_table_is_the_models_table_read_only() -> None:
    assert mower_sdk.RAW_STATE_TO_CANONICAL is models.RAW_STATE_TO_CANONICAL
    assert dict(models.RAW_STATE_TO_CANONICAL) == dict(RAW_STATE_TABLE)
    with pytest.raises(TypeError):
        models.RAW_STATE_TO_CANONICAL["isNew"] = "idle"  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [*RAW_STATE_TABLE, ("isSomethingNew", "isSomethingNew"), (MowerStatus.DOCKED, "docked"), (None, "unknown"), (3, "unknown")],
)
def test_canonical_state_is_what_a_state_message_holds(raw: Any, expected: str) -> None:
    assert mower_sdk.canonical_state(raw) == expected
    if raw is not None and not isinstance(raw, MowerStatus):
        assert DeviceStateMessage.from_dict({"device_id": "d", "state": raw}).state == expected


@pytest.mark.parametrize(
    "raw", [*(raw for raw, _ in RAW_STATE_TABLE), "isSomethingNew", MowerStatus.PAUSED, None, 3, ""]
)
def test_mower_status_from_raw_is_what_a_rest_status_holds(raw: Any) -> None:
    expected = DeviceStatus.from_dict({"id": "d", "vehicleState": raw}).status
    assert mower_sdk.mower_status_from_raw(raw) is expected
    if raw == "isSomethingNew":
        assert expected is MowerStatus.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        {"capacityRemaining": [{"rawValue": "40", "unit": "MAH"}, {"rawValue": "88", "unit": "percentage"}]},
        {"capacityRemaining": [{"rawValue": "x"}, {"rawValue": 55}], "battery": 10},
        {"battery": "73"},
        {"battery": True},
        {"battery": 140},
        {},
    ],
    ids=["percentage_first", "any_raw_value", "plain_battery", "bool", "out_of_range", "none"],
)
def test_battery_from_payload_is_what_both_models_read(payload: dict[str, Any]) -> None:
    value = mower_sdk.battery_from_payload(payload)
    assert value == DeviceStatus.from_dict({"id": "d", **payload}).battery
    assert value == DeviceStateMessage.from_dict({"device_id": "d", **payload}).battery


@pytest.mark.parametrize("data", [None, [], "88", 88], ids=["none", "list", "string", "number"])
def test_battery_from_payload_reads_nothing_from_what_is_not_a_dict(data: Any) -> None:
    assert mower_sdk.battery_from_payload(data) is None


def test_the_private_reader_names_still_resolve() -> None:
    assert models._normalize_state_value is models.canonical_state
    assert models._extract_battery_value is models.battery_from_payload


def test_mower_status_members() -> None:
    assert [status.value for status in MowerStatus] == [
        "idle", "mowing", "paused", "docked", "charging", "error", "returning",
        "mapping", "updating", "offline", "unknown",
    ]


@pytest.mark.parametrize(("raw", "canonical"), RAW_STATE_TABLE)
@pytest.mark.parametrize("key", ["status", "state", "vehicleState"])
def test_device_status_maps_every_spelling_from_every_key(
    key: str, raw: str, canonical: str
) -> None:
    assert DeviceStatus.from_dict({key: raw}).status is MowerStatus(canonical)


@pytest.mark.parametrize(("raw", "canonical"), RAW_STATE_TABLE)
@pytest.mark.parametrize("key", ["state", "status", "vehicleState"])
def test_state_message_maps_every_spelling_and_records_the_raw_value(
    key: str, raw: str, canonical: str
) -> None:
    message = DeviceStateMessage.from_dict({key: raw})
    assert message.state == canonical
    # raw_state is recorded only when normalisation changed the value.
    assert message.metrics == ({"raw_state": raw} if raw != canonical else None)


def test_competing_keys_resolve_differently_per_reader() -> None:
    payload = {"status": "isDocked", "state": "isRunning"}
    assert DeviceStatus.from_dict(payload).status is MowerStatus.DOCKED
    assert DeviceStateMessage.from_dict(payload).state == "mowing"


def test_vehicle_state_is_the_last_fallback_for_both_readers() -> None:
    payload = {"status": "isDocked", "vehicleState": "isPaused"}
    assert DeviceStatus.from_dict(payload).status is MowerStatus.DOCKED
    assert DeviceStateMessage.from_dict(payload).state == "docked"
    assert DeviceStatus.from_dict({"vehicleState": "isPaused"}).status is MowerStatus.PAUSED
    assert DeviceStateMessage.from_dict({"vehicleState": "isPaused"}).state == "paused"


def test_missing_keys() -> None:
    assert DeviceStatus.from_dict({}) == DeviceStatus(
        device_id="", status=MowerStatus.UNKNOWN, battery=None
    )
    assert DeviceStateMessage.from_dict({}) == DeviceStateMessage(
        device_id="", timestamp=None, state="unknown", battery=None
    )


def test_none_values() -> None:
    payload = {"status": None, "state": None, "vehicleState": None}
    status = DeviceStatus.from_dict(payload)
    assert status.status is MowerStatus.UNKNOWN
    assert status.extra == {"vehicleState": None}
    message = DeviceStateMessage.from_dict(payload)
    assert message.state == "unknown"
    assert message.metrics is None


@pytest.mark.parametrize(
    ("payload", "status", "extra"),
    [
        ({"status": "", "state": "isRunning"}, MowerStatus.MOWING, None),
        ({"status": 0, "state": "", "vehicleState": "isDocked"}, MowerStatus.DOCKED, {"vehicleState": "isDocked"}),
        ({"status": "", "state": "", "vehicleState": ""}, MowerStatus.UNKNOWN, {"vehicleState": ""}),
        ({"status": False}, MowerStatus.UNKNOWN, None),
        ({"vehicleState": False}, MowerStatus.UNKNOWN, {"vehicleState": False}),
        ({"status": 0}, MowerStatus.UNKNOWN, None),
    ],
    ids=["empty_status", "zero_then_empty", "all_empty", "false_status", "false_vehicle_state", "zero_status"],
)
def test_device_status_falsy_fallbacks(
    payload: dict[str, Any], status: MowerStatus, extra: dict[str, Any] | None
) -> None:
    result = DeviceStatus.from_dict(payload)
    assert result.status is status
    assert result.extra == extra


@pytest.mark.parametrize(
    ("payload", "state", "metrics"),
    [
        ({"state": "", "status": "isDocked"}, "docked", {"raw_state": "isDocked"}),
        ({"state": 0, "status": None, "vehicleState": "isRunning"}, "mowing", {"raw_state": "isRunning"}),
        ({"state": "", "status": "", "vehicleState": ""}, "", None),
        ({"vehicleState": False}, "unknown", {"raw_state": False}),
        ({"state": 0}, "unknown", None),
        ({"state": 0, "vehicleState": 0}, "unknown", {"raw_state": 0}),
        ({"state": False, "status": 0, "vehicleState": None}, "unknown", None),
    ],
    ids=["empty_state", "zero_then_none", "all_empty", "false_vehicle_state", "zero_state", "zero_last", "none_last"],
)
def test_state_message_falsy_fallbacks(
    payload: dict[str, Any], state: str, metrics: dict[str, Any] | None
) -> None:
    message = DeviceStateMessage.from_dict(payload)
    assert message.state == state
    assert message.metrics == metrics


def test_unknown_spelling_passes_through_the_message_but_not_the_status() -> None:
    assert DeviceStatus.from_dict({"status": "isFlying"}).status is MowerStatus.UNKNOWN
    message = DeviceStateMessage.from_dict({"state": "isFlying"})
    assert message.state == "isFlying"
    assert message.metrics is None


def test_mower_status_instances_are_accepted() -> None:
    assert DeviceStatus.from_dict({"status": MowerStatus.CHARGING}).status is MowerStatus.CHARGING
    message = DeviceStateMessage.from_dict({"state": MowerStatus.CHARGING})
    assert message.state == "charging"
    assert message.metrics == {"raw_state": MowerStatus.CHARGING}


# Each case names its value; the ones whose value changed when the reader was
# replaced say what they gave before.
BATTERY_CASES = [
    pytest.param({"battery": 57}, 57, id="int"),
    pytest.param({"battery": 0}, 0, id="zero_is_zero"),
    pytest.param({"battery": "57"}, 57, id="numeric_string"),
    pytest.param({"battery": 57.9}, 57, id="float_truncated"),
    pytest.param({"battery": True}, None, id="bool_is_none"),  # was 1
    pytest.param({"battery": 150}, 150, id="above_100_passes"),
    pytest.param({"battery": -1}, -1, id="negative_passes"),
    pytest.param({}, None, id="missing"),  # was 0
    pytest.param({"battery": None}, None, id="none"),  # was 0
    pytest.param({"battery": "n/a"}, None, id="unparsable"),  # was 0
    pytest.param({"battery": [50]}, None, id="list_battery"),  # was 0
    pytest.param({"battery": float("inf")}, None, id="infinity"),  # raised OverflowError
    pytest.param({"battery": float("nan")}, None, id="nan"),  # was 0
    pytest.param({"battery": 1e15}, 10**15, id="large_finite_float_passes"),
    pytest.param(
        {"battery": None, "capacityRemaining": [{"unit": "percentage", "rawValue": "73"}]},
        73,
        id="capacity_percentage_case_insensitive",
    ),
    pytest.param(
        {"capacityRemaining": [{"unit": "WH", "rawValue": 500}, {"unit": "PERCENTAGE", "rawValue": 64}]},
        64,
        id="capacity_percentage_after_other_unit",
    ),
    pytest.param({"capacityRemaining": [{"unit": "WH", "rawValue": 500}]}, 500, id="capacity_first_item_fallback"),
    pytest.param(
        {"capacityRemaining": [{"unit": "WH", "rawValue": "x"}, {"unit": "MINUTES", "rawValue": 45}]},
        45,
        id="capacity_fallback_scans_past_the_first_entry",  # was 0: only the first entry was tried
    ),
    pytest.param(
        {"capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": True}, {"unit": "WH", "rawValue": 7}]},
        7,
        id="capacity_bool_skipped",  # was 1
    ),
    pytest.param(
        {"capacityRemaining": [{"rawValue": "x"}, {"unit": "PERCENTAGE", "rawValue": 12}]},
        12,
        id="capacity_unparsable_first_item",
    ),
    pytest.param(
        {"capacityRemaining": ["nope", {"unit": "PERCENTAGE", "rawValue": 12}]},
        12,
        id="capacity_non_dict_skipped",
    ),
    pytest.param({"capacityRemaining": ["nope"]}, None, id="capacity_only_non_dict"),  # was 0
    pytest.param({"capacityRemaining": []}, None, id="capacity_empty"),  # was 0
    pytest.param({"capacityRemaining": {"unit": "PERCENTAGE", "rawValue": 12}}, None, id="capacity_not_a_list"),  # was 0
    pytest.param(
        {"battery": "abc", "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 5}]},
        5,
        id="unparsable_battery_falls_back",
    ),
    pytest.param(
        {"battery": 0, "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 5}]},
        5,
        id="capacity_wins_over_battery",  # was 0: battery came first
    ),
    pytest.param(
        {"battery": 9, "capacityRemaining": [{"unit": "WH", "rawValue": "x"}]},
        9,
        id="battery_when_no_capacity_entry_parses",
    ),
]


@pytest.mark.parametrize(("payload", "battery"), BATTERY_CASES)
def test_battery_extraction(payload: dict[str, Any], battery: int | None) -> None:
    assert DeviceStatus.from_dict(payload).battery == battery
    assert DeviceStateMessage.from_dict(payload).battery == battery


def test_to_dict_emits_a_none_battery() -> None:
    assert DeviceStatus.from_dict({"id": "d"}).to_dict()["battery"] is None
    assert DeviceStateMessage.from_dict({"device_id": "d"}).to_dict()["battery"] is None


def test_device_status_extra_merges_the_raw_status_keys() -> None:
    capacity = [{"unit": "PERCENTAGE", "rawValue": 40}]
    status = DeviceStatus.from_dict(
        {
            "extra": {"k": 1},
            "vehicleState": "isDocked",
            "capacityRemaining": capacity,
            "descriptiveCapacityRemaining": "HIGH",
        }
    )
    assert status.extra == {
        "k": 1,
        "vehicleState": "isDocked",
        "capacityRemaining": capacity,
        "descriptiveCapacityRemaining": "HIGH",
    }
    assert DeviceStatus.from_dict({"extra": {}}).extra is None
    assert DeviceStatus.from_dict({"extra": None, "vehicleState": "x"}).extra == {"vehicleState": "x"}
    assert DeviceStatus.from_dict({"vehicleState": "x", "zone": 2}).extra == {"zone": 2, "vehicleState": "x"}


def test_device_status_never_writes_to_the_callers_extra_dict() -> None:
    extra = {"k": 1}
    status = DeviceStatus.from_dict({"extra": extra, "vehicleState": "isDocked"})
    assert status.extra is not extra
    assert status.extra == {"k": 1, "vehicleState": "isDocked"}
    assert extra == {"k": 1}


def test_device_status_keeps_a_rest_key_the_model_does_not_read_in_extra() -> None:
    status = DeviceStatus.from_dict({"id": "d", "vehicleState": "isDocked", "mowingZone": "front", "deviceId": "d"})
    assert status.extra == {"mowingZone": "front", "deviceId": "d", "vehicleState": "isDocked"}
    assert status.to_dict()["extra"] == status.extra


def test_the_known_field_sets() -> None:
    assert {
        "state", "vehicleState", "status", "battery", "capacityRemaining", "timestamp", "device_id",
    } == models.STATE_KNOWN_FIELDS
    assert {
        "id", "device_id", "deviceId", "vehicleState", "capacityRemaining", "descriptiveCapacityRemaining", "battery",
    } == models.REST_STATUS_KNOWN_FIELDS


STATE_PAYLOAD = {"device_id": "d", "state": "isDocked", "battery": 50, "speed": 1, "metrics": {"rpm": 3}}
EVENT_PAYLOAD = {"device_id": "d", "type": "system", "event": "started", "extra": True}
ATTRIBUTES_PAYLOAD = {"device_id": "d", "attributes": {"a": 1}, "other": 2}


@pytest.mark.parametrize(
    ("cls", "payload", "by_hand"),
    [
        (
            DeviceStateMessage,
            STATE_PAYLOAD,
            DeviceStateMessage(
                device_id="d", timestamp=None, state="docked", battery=50, metrics={"rpm": 3, "raw_state": "isDocked"}
            ),
        ),
        (DeviceEventMessage, EVENT_PAYLOAD, DeviceEventMessage(device_id="d", timestamp=None, type="system", event="started")),
        (DeviceAttributesMessage, ATTRIBUTES_PAYLOAD, DeviceAttributesMessage(device_id="d", attributes={"a": 1})),
    ],
    ids=["state", "event", "attributes"],
)
def test_the_message_dataclasses_keep_the_payload_as_raw_outside_equality_repr_and_to_dict(
    cls: Any, payload: dict[str, Any], by_hand: Any
) -> None:
    decoded = json.loads(json.dumps(payload))
    message = cls.from_dict(decoded)
    assert message.raw == payload  # as decoded: the metrics raw_state the reader adds is not in it
    assert message.raw is not decoded
    assert message == by_hand
    assert by_hand.raw is None
    assert "raw" not in message.to_dict()
    assert "raw=" not in repr(message)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, MowerError.NONE),
        ({"error_code": "stuck"}, MowerError.STUCK),
        ({"error_code": "bogus"}, MowerError.UNKNOWN),
        ({"error_code": None}, MowerError.UNKNOWN),
    ],
    ids=["missing", "known", "unknown", "none"],
)
def test_device_status_error_code(payload: dict[str, Any], error: MowerError) -> None:
    assert DeviceStatus.from_dict(payload).error_code is error


@pytest.mark.parametrize(
    ("payload", "device_id"),
    [
        ({"device_id": "a", "id": "b"}, "a"),
        ({"id": "b"}, "b"),
        ({"device_id": "", "id": "b"}, "b"),
        ({}, ""),
    ],
    ids=["device_id_wins", "id_fallback", "empty_device_id", "missing"],
)
def test_device_status_device_id(payload: dict[str, Any], device_id: str) -> None:
    assert DeviceStatus.from_dict(payload).device_id == device_id


def test_device_status_to_dict_omits_unset_fields() -> None:
    status = DeviceStatus.from_dict({"id": "d", "status": "isDocked", "battery": 9, "timestamp": 5})
    assert status.to_dict() == {
        "device_id": "d",
        "status": "docked",
        "battery": 9,
        "error_code": "none",
        "timestamp": 5,
    }


def test_state_message_metrics_handling() -> None:
    metrics = {"speed": 1}
    payload = {"metrics": metrics, "state": "isDocked"}
    message = DeviceStateMessage.from_dict(payload)
    assert message.metrics == {"speed": 1, "raw_state": "isDocked"}
    assert message.metrics is not metrics
    assert metrics == {"speed": 1}  # the caller's dict is left as it was
    assert payload == {"metrics": {"speed": 1}, "state": "isDocked"}
    assert message.raw == {"metrics": {"speed": 1}, "state": "isDocked"}
    assert DeviceStateMessage.from_dict({"metrics": [("a", 1)], "state": "docked"}).metrics == {"a": 1}
    assert DeviceStateMessage.from_dict({"metrics": None, "state": "docked"}).metrics is None


def test_state_message_passes_the_other_fields_through() -> None:
    payload = {
        "device_id": "d",
        "timestamp": 1700000000,
        "state": "isRunning",
        "battery": 42,
        "signal_strength": -60,
        "position": {"lat": 1.0, "lng": 2.0},
        "error": {"code": 7},
    }
    message = DeviceStateMessage.from_dict(payload)
    assert message == DeviceStateMessage(
        device_id="d",
        timestamp=1700000000,
        state="mowing",
        battery=42,
        signal_strength=-60,
        position={"lat": 1.0, "lng": 2.0},
        error={"code": 7},
        metrics={"raw_state": "isRunning"},
    )
    assert message.to_dict() == {
        "device_id": "d",
        "timestamp": 1700000000,
        "state": "mowing",
        "battery": 42,
        "signal_strength": -60,
        "position": {"lat": 1.0, "lng": 2.0},
        "error": {"code": 7},
        "metrics": {"raw_state": "isRunning"},
    }

"""Tests for the location channel's decoder and records (mower_sdk.location, mower_sdk.models).

The entries are synthetic, shaped as the mower sends them: an array of entries
with an integer type and numbers as strings. Times are mower milliseconds around
T (2026-09-28 12:00 UTC); the plausibility window is measured from the receipt
time, RECEIVED.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import mower_sdk
from mower_sdk.location import (
    LOCATION_ENTRY_TYPES,
    LOCATION_KNOWN_FIELDS,
    PLAUSIBLE_MIN_MS,
    LocationDecoder,
    ParsedLocation,
)
from mower_sdk.models import (
    VEHICLE_STATE_TO_STATUS,
    DeviceLocation,
    DeviceLocationMessage,
    MowerStatus,
    SkippedLocationEntry,
    mower_time_ms,
)

DEVICE = "dev-1"
RECEIVED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
T = int(RECEIVED.timestamp() * 1000)


def pose(
    t: int | None,
    x: str = "1.50",
    y: str = "2.50",
    theta: str | None = "0.25",
    state: str | None = "4",
) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": 1, "postureX": x, "postureY": y}
    if theta is not None:
        entry["postureTheta"] = theta
    if state is not None:
        entry["vehicleState"] = state
    if t is not None:
        entry["time"] = str(t)
    return entry


def task(t: int | None, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": 2, **fields}
    if t is not None:
        entry["time"] = str(t)
    return entry


def target(t: int, ids: list[int] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": 3, "time": str(t)}
    if ids is not None:
        entry["partitionIds"] = ids
    return entry


def decode(
    decoder: LocationDecoder, payload: Any, received_at: datetime = RECEIVED
) -> ParsedLocation:
    return decoder.decode(DEVICE, payload, received_at)


# ---- the helpers and the tables -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1790000000000, 1790000000000),
        ("1790000000000", 1790000000000),
        (1790000000, 1790000000000),
        ("1790000000.5", 1790000000000),
        (0, None),
        (-5, None),
        (None, None),
        ("soon", None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_mower_time_ms_reads_seconds_or_milliseconds(value: Any, expected: int | None) -> None:
    assert mower_time_ms(value) == expected


def test_the_pose_code_table() -> None:
    assert VEHICLE_STATE_TO_STATUS == {
        1: MowerStatus.DOCKED,
        2: MowerStatus.CHARGING,
        3: MowerStatus.PAUSED,
        4: MowerStatus.MOWING,
        5: MowerStatus.RETURNING,
        6: MowerStatus.MAPPING,
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("1", MowerStatus.DOCKED),
        ("3", MowerStatus.PAUSED),
        ("6", MowerStatus.MAPPING),
        ("9", MowerStatus.UNKNOWN),
        (None, None),
    ],
    ids=["docked", "paused", "mapping", "unknown_code", "lifted_no_code"],
)
def test_a_pose_status_comes_from_its_code(code: str | None, status: MowerStatus | None) -> None:
    decoder = LocationDecoder()
    (message,) = decode(decoder, [pose(T, state=code)]).messages
    assert message.status is status
    assert message.location.status is status


def test_the_known_fields_and_types() -> None:
    assert {
        "type",
        "time",
        "postureX",
        "partitionIds",
        "taskDelay",
        "mapWorkPosition",
    } <= LOCATION_KNOWN_FIELDS
    assert len(LOCATION_KNOWN_FIELDS) == 17
    assert sorted(LOCATION_ENTRY_TYPES) == [1, 2, 3, 4]


def test_the_package_exports_the_location_models() -> None:
    assert mower_sdk.DeviceLocation is DeviceLocation
    assert mower_sdk.DeviceLocationMessage is DeviceLocationMessage
    assert mower_sdk.VEHICLE_STATE_TO_STATUS is VEHICLE_STATE_TO_STATUS
    assert mower_sdk.mower_time_ms is mower_time_ms


# ---- one message ---------------------------------------------------------------------------------


def test_a_pose_is_decoded_with_numbers_sent_as_strings() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [pose(T - 1000)])
    assert parsed.reasons == [] and parsed.reason is None
    (message,) = parsed.messages
    assert (message.device_id, message.entry_type, message.timestamp, message.received_at) == (
        DEVICE,
        1,
        T - 1000,
        RECEIVED,
    )
    assert (message.x, message.y, message.theta, message.vehicle_state) == (1.5, 2.5, 0.25, 4)
    assert message.raw == pose(T - 1000)
    assert message.route_progress is None and message.partition_ids is None
    record = decoder.get(DEVICE)
    assert record == message.location
    assert (record.x, record.y, record.pose_at, record.pose_received_at) == (
        1.5,
        2.5,
        T - 1000,
        RECEIVED,
    )
    assert record.marks == {1: T - 1000}


def test_a_lone_entry_object_is_one_entry() -> None:
    decoder = LocationDecoder()
    (message,) = decode(decoder, pose(T)).messages
    assert message.x == 1.5


@pytest.mark.parametrize(
    "payload", [None, "text", 5, True], ids=["null", "string", "number", "bool"]
)
def test_a_payload_that_is_neither_a_list_nor_an_object_is_unparsable(payload: Any) -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, payload)
    assert (parsed.messages, parsed.reasons, parsed.reason) == ([], ["unparsable"], "unparsable")
    assert decoder.get(DEVICE) is None


def test_an_empty_array_changes_nothing_and_earns_no_reason() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [])
    assert (parsed.messages, parsed.reasons) == ([], [])
    assert decoder.get(DEVICE) is None


def test_the_reconnect_delay_shape_without_task_delay_is_skipped_silently() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [{"type": 4, "time": str(T), "vehicleState": "1"}])
    assert (parsed.messages, parsed.reasons) == ([], [])


def test_a_delay_entry_has_no_time_and_is_never_stale() -> None:
    decoder = LocationDecoder()
    first = decode(decoder, [{"type": 4, "taskDelay": True}])
    later = decode(decoder, [{"type": 4, "taskDelay": False}], RECEIVED + timedelta(seconds=5))
    assert [m.task_delay for m in first.messages + later.messages] == [True, False]
    assert first.messages[0].timestamp is None
    record = decoder.get(DEVICE)
    assert (record.task_delay, record.delay_received_at) == (False, RECEIVED + timedelta(seconds=5))
    assert record.marks == {}


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ([{"type": 7, "time": str(T)}], "unknown_type"),
        ([{"type": "1", "time": str(T)}], "unknown_type"),
        ([{**pose(T), "type": 1.0}], "unknown_type"),
        ([{**pose(T), "type": []}], "unknown_type"),
        ([{**pose(T), "type": {}}], "unknown_type"),
        ([{**pose(T), "type": True}], "unknown_type"),
        ([{**pose(T), "speed": "1"}], "unknown_field"),
        ([pose(PLAUSIBLE_MIN_MS - 1)], "implausible_time"),
        ([pose(5000)], "implausible_time"),
        ([pose(T + 5 * 60 * 1000 + 1)], "implausible_time"),
        ([pose(0)], "implausible_time"),
        ([pose(-5)], "implausible_time"),
        ([{**pose(None), "time": "soon"}], "implausible_time"),
        ([task(0, mowingPercentage="10")], "implausible_time"),
        ([target(0, [1])], "implausible_time"),
        ([pose(T, x="far")], "unparsable"),
        ([pose(T, y="inf")], "unparsable"),
        ([{"type": 1, "postureY": "1", "time": str(T)}], "unparsable"),
        ([pose(T, x="0", y="0", theta="0")], "placeholder"),
        ([pose(T, x="0", y="0", theta=None)], "placeholder"),
    ],
    ids=[
        "unknown_type",
        "type_as_string",
        "type_as_float",
        "type_as_list",
        "type_as_object",
        "type_as_bool",
        "unknown_field",
        "before_2020",
        "1970",
        "ahead",
        "time_zero",
        "time_negative",
        "time_unreadable",
        "task_time_zero",
        "target_time_zero",
        "x_unreadable",
        "y_infinite",
        "x_missing",
        "all_zero",
        "zero_no_heading",
    ],
)
def test_each_reason(entries: list[dict[str, Any]], reason: str) -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, entries)
    assert parsed.reasons == [reason]
    if reason == "unknown_field":
        assert len(parsed.messages) == 1  # applied, and marked
        assert parsed.skipped == []
    else:
        assert parsed.messages == []
        assert decoder.get(DEVICE) is None
        (skipped,) = parsed.skipped
        assert (skipped.device_id, skipped.reason, skipped.received_at) == (
            DEVICE,
            reason,
            RECEIVED,
        )
        assert skipped.raw == entries[0]


def test_the_edge_of_the_plausibility_window_is_believed() -> None:
    decoder = LocationDecoder()
    assert decode(decoder, [pose(T + 5 * 60 * 1000)]).reasons == []


def test_a_time_at_or_below_the_mark_is_stale_per_type() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 10_000), task(T - 10_000, mowingPercentage="10")])
    parsed = decode(
        decoder,
        [pose(T - 10_000, x="9"), pose(T - 20_000, x="8"), task(T - 5000, mowingPercentage="11")],
    )
    assert parsed.reasons == ["stale"]
    assert [m.entry_type for m in parsed.messages] == [2]
    assert decoder.get(DEVICE).x == 1.5


def test_the_deciding_reason_follows_the_priority() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 10_000)])
    parsed = decode(
        decoder,
        [pose(T - 20_000), {**pose(T - 5000), "speed": "1"}, pose(T, x="0", y="0", theta="0")],
    )
    assert sorted(parsed.reasons) == ["placeholder", "stale", "unknown_field"]
    assert parsed.reason == "unknown_field"


def test_a_catch_up_message_is_applied_in_time_order_with_a_snapshot_per_entry() -> None:
    decoder = LocationDecoder()
    parsed = decode(
        decoder,
        [
            pose(T - 1000, x="3"),
            {"type": 4, "taskDelay": False},
            pose(T - 3000, x="1"),
            pose(T - 2000, x="2"),
        ],
    )
    assert parsed.reasons == []
    assert [(m.entry_type, m.x) for m in parsed.messages] == [
        (1, 1.0),
        (4, None),
        (1, 2.0),
        (1, 3.0),
    ]
    assert [m.location.x for m in parsed.messages] == [1.0, 1.0, 2.0, 3.0]
    assert [m.location.task_delay for m in parsed.messages] == [None, False, False, False]
    assert decoder.get(DEVICE).pose_at == T - 1000


def test_a_docked_x430_reports_a_real_pose_and_other_models_a_placeholder() -> None:
    decoder = LocationDecoder()
    (docked,) = decode(
        decoder, [pose(T - 300_000, x="0.12", y="-0.40", theta="3.1", state="1")]
    ).messages
    assert docked.location.status is MowerStatus.DOCKED
    parsed = decode(decoder, [pose(T, x="0", y="0", theta="0", state="2")])
    assert parsed.reasons == ["placeholder"]
    assert decoder.get(DEVICE) == docked.location  # the previous pose stays


def test_a_target_is_set_repeated_and_cleared() -> None:
    decoder = LocationDecoder()
    (first,) = decode(decoder, [target(T - 30_000, [3, 1])]).messages
    assert first.partition_ids == (3, 1)
    assert (first.location.target_at, first.location.target_last_at) == (T - 30_000, T - 30_000)
    (repeat,) = decode(decoder, [target(T - 20_000, [1, 3])]).messages  # the same set, reordered
    assert repeat.partition_ids == (1, 3)  # the entry as sent
    assert repeat.location.partition_ids == (3, 1)  # the record keeps the set as first reported
    assert (repeat.location.target_at, repeat.location.target_last_at) == (T - 30_000, T - 20_000)
    (cleared,) = decode(decoder, [target(T - 10_000, None)]).messages
    assert cleared.partition_ids == ()
    assert (cleared.location.partition_ids, cleared.location.target_at) == ((), T - 10_000)


def test_no_target_report_is_none_and_a_report_without_zones_is_empty() -> None:
    assert DeviceLocation(device_id=DEVICE).partition_ids is None
    decoder = LocationDecoder()
    decode(decoder, [target(T, [])])
    assert decoder.get(DEVICE).partition_ids == ()


def test_the_route_reading_is_kept_across_a_task_entry_without_one() -> None:
    decoder = LocationDecoder()
    location = DeviceLocation(device_id=DEVICE)
    assert (location.progress_percent, location.progress_source) == (None, "none")

    (only_percentage,) = decode(decoder, [task(T - 30_000, mowingPercentage="12.5")]).messages
    assert (
        only_percentage.location.progress_percent,
        only_percentage.location.progress_source,
    ) == (12.5, "percentage")

    (with_route,) = decode(
        decoder,
        [
            task(
                T - 20_000,
                currentMowProgress="4250",
                currentMowBoundary="2",
                mowingPercentage="40",
                subtotalArea="120.50",
            )
        ],
    ).messages
    assert (with_route.route_progress, with_route.current_zone) == (4250, 2)
    assert (with_route.location.progress_percent, with_route.location.progress_source) == (
        42.5,
        "route",
    )

    (without,) = decode(decoder, [task(T - 10_000, mowingPercentage="41", action="1")]).messages
    assert without.route_progress is None and without.area_m2 is None  # the entry's own fields
    record = without.location
    assert (record.route_progress, record.progress_at, record.current_zone, record.zone_at) == (
        4250,
        T - 20_000,
        2,
        T - 20_000,
    )
    assert (record.mowing_percentage, record.area_m2, record.action, record.task_at) == (
        41.0,
        None,
        1,
        T - 10_000,
    )
    assert (record.progress_percent, record.progress_source) == (42.5, "route")


def test_a_zero_route_reading_is_zero_not_unknown() -> None:
    decoder = LocationDecoder()
    (message,) = decode(decoder, [task(T, currentMowProgress="0")]).messages
    assert (message.location.progress_percent, message.location.progress_source) == (0.0, "route")


def test_the_task_fields_and_map_work_position_as_a_string() -> None:
    decoder = LocationDecoder()
    position = "0" * 120 + "0000abcd"
    (message,) = decode(
        decoder,
        [
            task(
                T,
                mowingWeekArea="300",
                subAction="2",
                mowStartType="1",
                mapWorkPosition=position,
                subtotalArea="true",
            )
        ],
    ).messages
    assert (message.week_area_m2, message.sub_action, message.mow_start_type) == (300.0, 2, 1)
    assert message.map_work_position == position
    assert message.area_m2 is None  # "true" is not a number
    assert json.dumps(message.location.to_dict())


def test_bools_and_non_finite_numbers_are_refused() -> None:
    decoder = LocationDecoder()
    (message,) = decode(
        decoder, [pose(T, theta="nan", state=None) | {"vehicleState": True}]
    ).messages
    assert (message.theta, message.vehicle_state) == (None, None)


def test_an_entry_that_is_not_an_object_is_skipped() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [1, "x", pose(T)])
    assert (len(parsed.messages), parsed.reasons) == (1, [])


def test_devices_are_kept_apart() -> None:
    decoder = LocationDecoder()
    decoder.decode("dev-2", [pose(T, x="7")], RECEIVED)
    decode(decoder, [pose(T - 1000)])
    assert (decoder.get("dev-2").x, decoder.get(DEVICE).x) == (7.0, 1.5)


# ---- persisting and restoring ------------------------------------------------------------------


def test_the_record_round_trips_through_a_dict_with_its_marks() -> None:
    decoder = LocationDecoder()
    decode(
        decoder,
        [
            pose(T - 30_000),
            task(T - 20_000, currentMowProgress="100", mapWorkPosition="ab"),
            target(T - 10_000, [2]),
            {"type": 4, "taskDelay": True},
        ],
    )
    record = decoder.get(DEVICE)
    data = json.loads(json.dumps(record.to_dict()))
    assert data["marks"] == {"1": T - 30_000, "2": T - 20_000, "3": T - 10_000}
    assert data["partition_ids"] == [2]
    assert data["pose_received_at"] == RECEIVED.isoformat()
    assert DeviceLocation.from_dict(data) == record
    assert DeviceLocation.from_dict(data).marks == record.marks


def test_from_dict_tolerates_missing_and_unreadable_values() -> None:
    location = DeviceLocation.from_dict(
        {
            "device_id": DEVICE,
            "x": "1.0",
            "y": "far",
            "pose_at": "yesterday",
            "route_progress": "bad",
            "task_delay": "yes",
            "map_work_position": 5,
            "partition_ids": "1",
            "pose_received_at": "yesterday",
            "marks": {"x": 1, "1": "soon"},
            "other": 1,
        }
    )
    assert location == DeviceLocation(device_id=DEVICE, x=1.0)
    assert (location.progress_percent, location.progress_source) == (None, "none")
    decoder = LocationDecoder()
    decoder.restore(DEVICE, location)
    assert len(decode(decoder, [pose(T)]).messages) == 1


def test_a_restored_record_rejects_an_older_pose_and_accepts_a_newer_one() -> None:
    decoder = LocationDecoder()
    decoder.restore(
        DEVICE,
        DeviceLocation(device_id=DEVICE, x=5.0, y=5.0, pose_at=T - 10_000, marks={1: T - 10_000}),
    )
    assert decode(decoder, [pose(T - 20_000)]).reasons == ["stale"]
    assert decoder.get(DEVICE).x == 5.0
    (message,) = decode(decoder, [pose(T - 5000)]).messages
    assert message.location.x == 1.5


def test_a_record_persisted_without_marks_takes_them_from_its_observation_times() -> None:
    decoder = LocationDecoder()
    persisted = DeviceLocation(
        device_id="old-id", pose_at=T - 10_000, task_at=T - 9000, target_last_at=T - 8000
    )
    decoder.restore(DEVICE, persisted)
    assert decoder.get(DEVICE).marks == {1: T - 10_000, 2: T - 9000, 3: T - 8000}
    assert decoder.get(DEVICE).device_id == DEVICE
    parsed = decode(decoder, [pose(T - 10_000), task(T - 9500), target(T - 8000, [1])])
    assert (parsed.messages, parsed.reasons) == ([], ["stale"])


def test_the_mark_survives_an_untimed_pose_and_a_persist_and_restore() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 10_000, x="4")])
    (untimed,) = decode(decoder, [pose(None, x="6")]).messages
    assert (untimed.location.pose_at, untimed.location.marks) == (None, {1: T - 10_000})

    restored = LocationDecoder()
    restored.restore(
        DEVICE, DeviceLocation.from_dict(json.loads(json.dumps(decoder.get(DEVICE).to_dict())))
    )
    parsed = decode(restored, [pose(T - 20_000, x="3")])
    assert parsed.reasons == ["stale"]
    assert restored.get(DEVICE).x == 6.0


def test_the_records_are_frozen_and_messages_compare_without_raw() -> None:
    decoder = LocationDecoder()
    (message,) = decode(decoder, [pose(T)]).messages
    with pytest.raises(AttributeError):
        message.location.x = 0.0  # type: ignore[misc]
    assert replace(message, raw={"other": 1}) == message
    assert hash(message.location) == hash(replace(message.location, marks={}))


def test_a_bad_time_cannot_overwrite_newer_data_but_a_missing_one_is_applied() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 10_000, x="4")])
    assert decode(decoder, [pose(0, x="9")]).reasons == ["implausible_time"]
    assert (decoder.get(DEVICE).x, decoder.get(DEVICE).pose_at) == (4.0, T - 10_000)
    (untimed,) = decode(decoder, [{**pose(None, x="6"), "time": None}]).messages
    assert (untimed.location.x, untimed.location.marks) == (6.0, {1: T - 10_000})


@pytest.mark.parametrize(
    "time_value", ["0", "1", str(T + 5 * 60 * 1000 + 1)], ids=["zero", "1970", "ahead"]
)
def test_a_delay_entry_is_never_time_guarded(time_value: str) -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [{"type": 4, "taskDelay": True, "time": time_value}])
    assert parsed.reasons == []
    (message,) = parsed.messages
    assert (message.task_delay, message.timestamp) == (True, None)
    assert decoder.get(DEVICE).marks == {}


def test_delay_entries_keep_their_message_order_whatever_time_they_carry() -> None:
    decoder = LocationDecoder()
    parsed = decode(
        decoder,
        [
            {"type": 4, "taskDelay": True, "time": str(T + 300_001)},
            pose(T - 2000),
            {"type": 4, "taskDelay": False, "time": "1"},
        ],
    )
    assert parsed.reasons == []
    assert [(m.entry_type, m.task_delay) for m in parsed.messages] == [
        (4, True),
        (1, None),
        (4, False),
    ]
    assert decoder.get(DEVICE).task_delay is False


# ---- pinned cases: each entry type's fields and marks stand on their own ------------------------


def test_a_pose_without_a_heading_does_not_keep_the_previous_one() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 1000, theta="0.25")])
    decode(decoder, [pose(T, theta=None)])
    assert decoder.get(DEVICE).theta is None


def test_an_unusable_pose_after_a_good_one_leaves_the_record_as_it_was() -> None:
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 1000)])
    before = decoder.get(DEVICE)
    parsed = decode(decoder, [pose(T, x="n/a", theta="3", state="1")])
    assert (parsed.messages, parsed.reasons) == ([], ["unparsable"])
    assert decoder.get(DEVICE) == before


def test_a_zero_position_with_a_heading_is_a_real_pose() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [pose(T, x="0", y="0", theta="1.2")])
    assert parsed.reasons == []
    assert (decoder.get(DEVICE).x, decoder.get(DEVICE).theta) == (0.0, 1.2)


def test_a_pose_leaves_the_task_fields_and_a_task_leaves_the_pose_receipt_time() -> None:
    decoder = LocationDecoder()
    decode(decoder, [task(T - 2000, mowingPercentage="40", subtotalArea="100.00")])
    decode(decoder, [pose(T - 1000)])
    later = RECEIVED + timedelta(seconds=5)
    decode(decoder, [task(T, mowingPercentage="41")], later)
    record = decoder.get(DEVICE)
    assert record.pose_received_at == RECEIVED
    decode(decoder, [pose(T + 1000)], later)
    record = decoder.get(DEVICE)
    assert (record.mowing_percentage, record.task_at) == (41.0, T)


def test_each_type_has_its_own_mark() -> None:
    decoder = LocationDecoder()
    decode(decoder, [task(T, mowingPercentage="10")])
    parsed = decode(decoder, [pose(T - 60_000), target(T - 60_000, [2])])
    assert parsed.reasons == []
    assert [m.entry_type for m in parsed.messages] == [1, 3]


def test_untimed_task_and_target_entries_apply_and_keep_their_marks() -> None:
    decoder = LocationDecoder()
    decode(decoder, [task(T - 10_000, mowingPercentage="10"), target(T - 10_000, [2])])
    untimed = decode(decoder, [task(None, mowingPercentage="20"), {"type": 3, "partitionIds": [3]}])
    assert untimed.reasons == []
    record = decoder.get(DEVICE)
    assert (record.mowing_percentage, record.task_at, record.partition_ids) == (20.0, None, (3,))
    assert (record.target_at, record.target_last_at) == (None, None)  # a new target, with no time
    assert record.marks == {2: T - 10_000, 3: T - 10_000}
    older = decode(decoder, [task(T - 20_000, mowingPercentage="5"), target(T - 20_000, [4])])
    assert (older.messages, older.reasons) == ([], ["stale"])


def test_an_untimed_first_target_report_has_no_times() -> None:
    decoder = LocationDecoder()
    (message,) = decode(decoder, [{"type": 3, "partitionIds": [2]}]).messages
    assert (message.location.target_at, message.location.target_last_at) == (None, None)
    assert 3 not in message.location.marks


def test_a_repeat_of_a_restored_target_keeps_its_first_time() -> None:
    decoder = LocationDecoder()
    decoder.restore(
        DEVICE,
        DeviceLocation(
            device_id=DEVICE,
            partition_ids=(2, 3),
            target_at=T - 60_000,
            target_last_at=T - 60_000,
        ),
    )
    (repeat,) = decode(decoder, [target(T, [3, 2])]).messages
    assert (repeat.location.target_at, repeat.location.target_last_at) == (T - 60_000, T)


def test_a_late_task_leaves_the_route_reading_and_the_task_as_they_were() -> None:
    decoder = LocationDecoder()
    decode(decoder, [task(T, currentMowProgress="4000", mowingPercentage="40")])
    parsed = decode(decoder, [task(T - 1000, currentMowProgress="3000", mowingPercentage="30")])
    assert parsed.reasons == ["stale"]
    record = decoder.get(DEVICE)
    assert (record.route_progress, record.mowing_percentage) == (4000, 40.0)


def test_the_reconnect_delay_shape_leaves_the_last_delay_alone() -> None:
    decoder = LocationDecoder()
    decode(decoder, [{"type": 4, "taskDelay": True}])
    parsed = decode(
        decoder,
        [{"type": 4, "time": str(T), "vehicleState": "1"}, pose(T)],
        RECEIVED + timedelta(seconds=5),
    )
    assert [m.entry_type for m in parsed.messages] == [1]
    record = decoder.get(DEVICE)
    assert (record.task_delay, record.delay_received_at) == (True, RECEIVED)


def test_the_reconnect_delay_shape_alone_leaves_no_record() -> None:
    decoder = LocationDecoder()
    decode(decoder, [{"type": 4, "time": str(T), "vehicleState": "1"}])
    assert decoder.get(DEVICE) is None


def test_the_reconnect_delay_shape_with_an_unknown_field_is_reported() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [{"type": 4, "time": str(T), "vehicleState": "1", "new": 1}])
    assert (parsed.messages, parsed.reasons) == ([], ["unknown_field"])


def test_the_lower_edge_of_the_plausibility_window_is_believed() -> None:
    decoder = LocationDecoder()
    assert decode(decoder, [pose(PLAUSIBLE_MIN_MS)]).reasons == []


def test_an_entry_without_a_type_is_an_unknown_type() -> None:
    decoder = LocationDecoder()
    parsed = decode(decoder, [{"postureX": "1", "postureY": "2", "time": str(T)}])
    assert (parsed.messages, parsed.reasons) == ([], ["unknown_type"])


def test_task_numbers_sent_as_strings_zero_and_negative_are_kept() -> None:
    decoder = LocationDecoder()
    (message,) = decode(
        decoder,
        [
            task(
                T,
                subtotalArea="100.00",
                mowingWeekArea="0.00",
                mowingPercentage=0,
                action=-1,
                subAction=-1,
                mapWorkPosition=7,
            )
        ],
    ).messages
    record = message.location
    assert (record.area_m2, record.week_area_m2, record.mowing_percentage) == (100.0, 0.0, 0.0)
    assert (record.action, record.sub_action, record.map_work_position) == (-1, -1, "7")


# ---- skipped entries -----------------------------------------------------------------------------


def test_a_skipped_entry_is_a_frozen_public_model() -> None:
    assert mower_sdk.SkippedLocationEntry is SkippedLocationEntry
    skipped = SkippedLocationEntry(
        device_id=DEVICE, entry_type=1, timestamp=T, reason="stale", received_at=RECEIVED
    )
    with pytest.raises(AttributeError):
        skipped.reason = "placeholder"  # type: ignore[misc]


def test_each_skipped_entry_is_named_in_a_mixed_message_and_the_applied_ones_are_unchanged() -> (
    None
):
    decoder = LocationDecoder()
    decode(decoder, [pose(T - 10_000), task(T - 10_000, mowingPercentage="10")])
    payload = [
        pose(T - 20_000, x="8"),
        pose(T - 5000, x="9"),
        task(
            T - 15_000,
            currentMowBoundary="3",
            currentMowProgress="4200",
            subtotalArea="120.5",
            mowingPercentage="42",
        ),
        {"type": 7, "time": str(T)},
        pose(T - 1000, x="0", y="0", theta="0"),
    ]
    parsed = decode(decoder, payload)
    assert [(m.entry_type, m.timestamp, m.x) for m in parsed.messages] == [(1, T - 5000, 9.0)]
    # In the order the decoder met them: the timed entries by ascending time, the
    # unknown type in its place.
    assert [(s.entry_type, s.timestamp, s.reason) for s in parsed.skipped] == [
        (1, T - 20_000, "stale"),
        (2, T - 15_000, "stale"),
        (7, T, "unknown_type"),
        (1, T - 1000, "placeholder"),
    ]
    late_pose, late_task, unknown, placeholder = parsed.skipped
    # The late task's readings, read as an applied task entry's would be.
    assert (
        late_task.current_zone,
        late_task.route_progress,
        late_task.area_m2,
        late_task.mowing_percentage,
    ) == (
        3,
        4200,
        120.5,
        42.0,
    )
    assert (late_task.x, late_task.partition_ids) == (None, None)
    assert (late_pose.x, late_pose.y, late_pose.theta, late_pose.vehicle_state) == (
        8.0,
        2.5,
        0.25,
        4,
    )
    assert late_pose.status is MowerStatus.MOWING
    assert unknown.x is None and unknown.raw == {"type": 7, "time": str(T)}
    assert (placeholder.x, placeholder.y, placeholder.theta) == (0.0, 0.0, 0.0)
    # Nothing skipped reached the record or its marks.
    record = decoder.get(DEVICE)
    assert (record.x, record.pose_at, record.task_at, record.mowing_percentage) == (
        9.0,
        T - 5000,
        T - 10_000,
        10.0,
    )
    assert record.marks == {1: T - 5000, 2: T - 10_000}


def test_a_skipped_task_leaves_out_the_zone_and_progress_it_did_not_send() -> None:
    decoder = LocationDecoder()
    decode(decoder, [task(T, currentMowBoundary="3")])
    (skipped,) = decode(decoder, [task(T - 1000, subtotalArea="5")]).skipped
    assert (skipped.reason, skipped.current_zone, skipped.route_progress, skipped.area_m2) == (
        "stale",
        None,
        None,
        5.0,
    )


@pytest.mark.parametrize(
    ("entry", "timestamp"),
    [
        (pose(0), 0),
        (pose(-5), -5),
        (pose(5000), 5000),
        (pose(T + 5 * 60 * 1000 + 1), T + 5 * 60 * 1000 + 1),
        ({**pose(None), "time": "soon"}, None),
        (target(0, [1, 2]), 0),
    ],
    ids=["zero", "negative", "1970", "ahead", "unreadable", "target_zero"],
)
def test_an_implausible_time_is_kept_as_read(entry: dict[str, Any], timestamp: int | None) -> None:
    (skipped,) = decode(LocationDecoder(), [entry]).skipped
    assert (skipped.reason, skipped.timestamp) == ("implausible_time", timestamp)
    if entry["type"] == 3:
        assert skipped.partition_ids == (1, 2)
    else:
        assert (skipped.x, skipped.y) == (1.5, 2.5)


def test_an_unparsable_pose_keeps_what_could_be_read() -> None:
    (skipped,) = decode(LocationDecoder(), [pose(T, x="far")]).skipped
    assert (skipped.reason, skipped.entry_type, skipped.timestamp) == ("unparsable", 1, T)
    assert (skipped.x, skipped.y, skipped.theta, skipped.vehicle_state) == (None, 2.5, 0.25, 4)


@pytest.mark.parametrize(
    ("entry", "entry_type"),
    [
        ({"type": "1", "time": str(T)}, None),
        ({"type": True}, None),
        ({"postureX": "1"}, None),
        ({"type": 9}, 9),
    ],
    ids=["type_as_string", "type_as_bool", "no_type", "unknown_int"],
)
def test_an_unknown_type_is_skipped_with_its_type_only_when_it_is_an_integer(
    entry: dict[str, Any], entry_type: int | None
) -> None:
    (skipped,) = decode(LocationDecoder(), [entry]).skipped
    assert (skipped.reason, skipped.entry_type) == ("unknown_type", entry_type)
    assert skipped.raw == entry


def test_what_is_not_an_entry_is_not_listed_as_skipped() -> None:
    decoder = LocationDecoder()
    assert decode(decoder, "text").skipped == []
    assert decode(decoder, [1, "x", {"type": 4, "time": str(T), "vehicleState": "1"}]).skipped == []


def test_a_message_with_nothing_skipped_lists_nothing() -> None:
    parsed = decode(LocationDecoder(), [pose(T), {**task(T), "speed": "1"}])
    assert (len(parsed.messages), parsed.reasons, parsed.skipped) == (2, ["unknown_field"], [])

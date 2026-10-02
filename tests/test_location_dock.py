"""The dock estimate the location decoder learns from docked poses (mower_sdk.location).

Poses are synthetic type 1 entries with the docked code (1) unless a test says
otherwise; times are mower milliseconds after T, the receipt time RECEIVED.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest

import mower_sdk
from mower_sdk import location as location_module
from mower_sdk.location import (
    DOCK_MAX_SAMPLES,
    DOCK_MOVE_DISTANCE_M,
    DOCK_MOVE_SAMPLES,
    DOCK_VEHICLE_STATES,
    LocationDecoder,
)
from mower_sdk.models import DeviceLocation

DEVICE = "dev-1"
RECEIVED = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
T = (
    int(RECEIVED.timestamp() * 1000) - 3_600_000
)  # an hour before receipt, so every step is plausible


def docked(
    step: int | None, x: float, y: float, theta: float | None = 0.5, code: Any = "1"
) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": 1, "postureX": str(x), "postureY": str(y)}
    if theta is not None:
        entry["postureTheta"] = str(theta)
    if code is not None:
        entry["vehicleState"] = code
    if step is not None:
        entry["time"] = str(T + step * 1000)
    return entry


def feed(decoder: LocationDecoder, *entries: dict[str, Any]) -> DeviceLocation:
    for entry in entries:
        decoder.decode(DEVICE, [entry], RECEIVED)
    record = decoder.get(DEVICE)
    assert record is not None
    return record


def dock(record: DeviceLocation) -> tuple[Any, ...]:
    return (
        None if record.dock_x is None else round(record.dock_x, 9),
        None if record.dock_y is None else round(record.dock_y, 9),
        record.dock_samples,
    )


def test_the_dock_constants_are_exported_from_the_package() -> None:
    for name in (
        "DOCK_MAX_SAMPLES",
        "DOCK_MOVE_DISTANCE_M",
        "DOCK_MOVE_SAMPLES",
        "DOCK_VEHICLE_STATES",
    ):
        assert getattr(mower_sdk, name) is getattr(location_module, name)
        assert name in mower_sdk.__all__


def test_the_defaults() -> None:
    assert (DOCK_MAX_SAMPLES, DOCK_MOVE_DISTANCE_M, DOCK_MOVE_SAMPLES) == (200, 1.0, 3)
    assert frozenset({1, 2}) == DOCK_VEHICLE_STATES
    decoder = LocationDecoder()
    assert (decoder.dock_max_samples, decoder.dock_move_distance_m, decoder.dock_move_samples) == (
        200,
        1.0,
        3,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dock_max_samples": 0},
        {"dock_max_samples": 1.5},
        {"dock_max_samples": True},
        {"dock_move_samples": 0},
        {"dock_move_samples": "3"},
        {"dock_move_distance_m": 0},
        {"dock_move_distance_m": -1.0},
        {"dock_move_distance_m": math.inf},
        {"dock_move_distance_m": math.nan},
        {"dock_move_distance_m": "1"},
        {"dock_move_distance_m": True},
    ],
)
def test_the_constructor_refuses_values_out_of_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=next(iter(kwargs))):
        LocationDecoder(**kwargs)


def test_the_smallest_allowed_values_are_accepted() -> None:
    decoder = LocationDecoder(dock_max_samples=1, dock_move_distance_m=0.01, dock_move_samples=1)
    assert (decoder.dock_max_samples, decoder.dock_move_distance_m, decoder.dock_move_samples) == (
        1,
        0.01,
        1,
    )


def test_no_estimate_until_a_docked_pose() -> None:
    decoder = LocationDecoder()
    record = feed(decoder, docked(1, 5.0, 5.0, code="4"))
    assert (
        record.dock_x,
        record.dock_y,
        record.dock_theta,
        record.dock_at,
        record.dock_samples,
    ) == (
        None,
        None,
        None,
        None,
        0,
    )


def test_the_first_docked_pose_is_the_estimate() -> None:
    record = feed(LocationDecoder(), docked(1, 1.0, 2.0, theta=0.25))
    assert (
        record.dock_x,
        record.dock_y,
        record.dock_theta,
        record.dock_at,
        record.dock_samples,
    ) == (
        1.0,
        2.0,
        0.25,
        T + 1000,
        1,
    )


def test_near_poses_are_averaged_and_charging_counts_too() -> None:
    record = feed(
        LocationDecoder(),
        docked(1, 1.00, 2.00),
        docked(2, 1.03, 2.00, code="2"),
        docked(3, 1.00, 2.06, theta=0.75),
    )
    assert dock(record) == (1.01, 2.02, 3)
    assert (record.dock_theta, record.dock_at) == (0.75, T + 3000)


def test_at_the_cap_each_pose_weighs_one_over_the_cap() -> None:
    decoder = LocationDecoder(dock_max_samples=4)
    record = feed(decoder, *(docked(step, 0.0, 1.0) for step in range(1, 5)))
    assert dock(record) == (0.0, 1.0, 4)
    record = feed(decoder, docked(5, 0.4, 1.0))
    assert dock(record) == (0.1, 1.0, 4)  # a quarter of the way
    record = feed(decoder, docked(6, 0.4, 1.0))
    assert dock(record) == (0.175, 1.0, 4)  # a quarter of the remaining 0.3


def test_the_default_cap_follows_a_small_move_about_63_percent_after_200_poses() -> None:
    decoder = LocationDecoder()
    feed(decoder, *(docked(step, 0.0, 0.0) for step in range(1, 201)))
    record = feed(decoder, *(docked(step, 0.5, 0.0) for step in range(201, 401)))
    assert record.dock_samples == 200
    assert 0.63 < record.dock_x / 0.5 < 0.64  # 1 - (1 - 1/200) ** 200


def test_a_pose_exactly_at_the_move_distance_is_folded_in() -> None:
    record = feed(LocationDecoder(), docked(1, 1.0, 1.0), docked(2, 2.0, 1.0))  # 1.0 m away
    assert dock(record) == (1.5, 1.0, 2)


def test_the_configured_move_distance_decides_what_is_far() -> None:
    near = feed(LocationDecoder(), docked(1, 1.0, 1.0), docked(2, 1.2, 1.0))
    assert dock(near) == (1.1, 1.0, 2)
    decoder = LocationDecoder(dock_move_distance_m=0.1)
    far = feed(decoder, docked(1, 1.0, 1.0), docked(2, 1.2, 1.0))
    assert dock(far) == (1.0, 1.0, 1)  # 0.2 m is far now: held as a candidate
    record = feed(
        decoder, docked(3, 1.25, 1.0), docked(4, 1.3, 1.0)
    )  # each within 0.1 m of the candidate's mean
    assert dock(record) == (1.25, 1.0, 3)


def test_a_far_pose_joins_the_candidate_by_its_running_mean() -> None:
    # 6.5 is 1.5 m from the first far pose but exactly 1 m from the mean of 5 and 6.
    record = feed(
        LocationDecoder(),
        docked(1, 1.0, 1.0),
        docked(2, 5.0, 1.0),
        docked(3, 6.0, 1.0),
        docked(4, 6.5, 1.0),
    )
    assert dock(record) == (round(17.5 / 3, 9), 1.0, 3)


def test_a_far_pose_close_to_the_last_but_not_to_the_candidates_mean_starts_again() -> None:
    decoder = LocationDecoder()
    # 6.8 is 0.9 m from 5.9 but 1.35 m from the mean of 5.0 and 5.9: a new candidate.
    record = feed(
        decoder, docked(1, 1.0, 1.0), docked(2, 5.0, 1.0), docked(3, 5.9, 1.0), docked(4, 6.8, 1.0)
    )
    assert dock(record) == (1.0, 1.0, 1)
    record = feed(decoder, docked(5, 6.8, 1.0), docked(6, 6.8, 1.0))
    assert dock(record) == (6.8, 1.0, 3)


def test_one_outlying_pose_never_moves_the_estimate() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 2.0, theta=0.25), docked(2, 1.0, 2.0))
    record = feed(decoder, docked(3, 9.0, 9.0, theta=2.0))
    assert dock(record) == (1.0, 2.0, 2)
    assert (record.dock_theta, record.dock_at) == (
        0.5,
        T + 2000,
    )  # the estimate's latest pose, not the outlier
    assert (record.x, record.y) == (9.0, 9.0)  # the pose itself is applied as usual


def test_agreeing_far_poses_replace_the_estimate_and_restart_the_count() -> None:
    decoder = LocationDecoder()
    feed(decoder, *(docked(step, 1.0, 2.0) for step in range(1, 11)))
    feed(decoder, docked(11, 5.0, 5.0), docked(12, 5.2, 5.0))
    assert dock(decoder.get(DEVICE)) == (1.0, 2.0, 10)  # two are not enough
    record = feed(decoder, docked(13, 5.1, 5.3, theta=1.5))
    assert dock(record) == (5.1, 5.1, 3)
    assert (record.dock_theta, record.dock_at) == (1.5, T + 13000)
    record = feed(decoder, docked(14, 5.1, 5.1))  # the next near pose folds into the new estimate
    assert record.dock_samples == 4


def test_a_move_takes_the_candidates_heading_not_the_old_docks() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 1.0, theta=0.25))
    record = feed(
        decoder,
        docked(2, 5.0, 1.0, theta=1.5),
        docked(3, 5.0, 1.0, theta=1.5),
        docked(4, 5.0, 1.0, theta=None),
    )
    assert dock(record) == (5.0, 1.0, 3)
    assert (record.dock_theta, record.dock_at) == (1.5, T + 4000)


def test_a_move_without_any_heading_has_none() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 1.0, theta=0.25))
    record = feed(decoder, *(docked(step, 5.0, 1.0, theta=None) for step in (2, 3, 4)))
    assert dock(record) == (5.0, 1.0, 3)
    assert record.dock_theta is None  # the old dock's heading does not carry over to the new place


def test_a_move_never_records_more_samples_than_the_cap() -> None:
    decoder = LocationDecoder(dock_max_samples=1)
    feed(decoder, docked(1, 1.0, 1.0))
    record = feed(decoder, *(docked(step, 5.0, 1.0) for step in (2, 3, 4)))
    assert dock(record) == (5.0, 1.0, 1)
    record = feed(decoder, docked(5, 5.2, 1.0))
    assert dock(record) == (5.2, 1.0, 1)  # at a cap of one, each near pose replaces the estimate


def test_far_poses_that_disagree_start_the_count_again() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 0.0, 0.0))
    feed(
        decoder,
        docked(2, 5.0, 0.0),
        docked(3, 5.0, 0.5),
        docked(4, 10.0, 0.0),
        docked(5, 10.0, 0.2),
    )
    assert dock(decoder.get(DEVICE)) == (0.0, 0.0, 1)  # the candidate moved to 10 m and holds two
    record = feed(decoder, docked(6, 10.0, 0.4))
    assert dock(record) == (10.0, 0.2, 3)


def test_a_near_pose_drops_the_candidate() -> None:
    decoder = LocationDecoder()
    feed(
        decoder, docked(1, 0.0, 0.0), docked(2, 5.0, 0.0), docked(3, 5.0, 0.0), docked(4, 0.1, 0.0)
    )
    feed(decoder, docked(5, 5.0, 0.0), docked(6, 5.0, 0.0))
    assert dock(decoder.get(DEVICE)) == (0.05, 0.0, 2)
    record = feed(decoder, docked(7, 5.0, 0.0))
    assert dock(record) == (5.0, 0.0, 3)


def test_poses_that_are_not_docked_leave_the_candidate_alone() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 0.0, 0.0), docked(2, 5.0, 0.0), docked(3, 5.0, 0.0))
    feed(decoder, docked(4, 3.0, 3.0, code="4"), docked(5, 4.0, 0.0, code="5"))  # out and back
    record = feed(decoder, docked(6, 5.0, 0.0))
    assert dock(record) == (5.0, 0.0, 3)


def test_one_move_sample_replaces_at_once() -> None:
    decoder = LocationDecoder(dock_move_samples=1)
    record = feed(decoder, docked(1, 0.0, 0.0), docked(2, 5.0, 0.0))
    assert dock(record) == (5.0, 0.0, 1)


@pytest.mark.parametrize("code", [None, "3", "4", "5", "6", "7", "x"])
def test_only_a_docked_or_charging_code_is_a_sample(code: Any) -> None:
    record = feed(LocationDecoder(), docked(1, 1.0, 2.0, code=code))
    assert dock(record) == (None, None, 0)


def test_other_entry_types_are_not_samples() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 2.0))
    record = feed(
        decoder,
        {"type": 2, "time": str(T + 2000), "mowingPercentage": "0"},
        {"type": 3, "time": str(T + 3000)},
    )
    assert record.dock_samples == 1


def test_a_pose_without_a_heading_keeps_the_estimates_heading() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 2.0, theta=0.25))
    record = feed(decoder, docked(2, 1.0, 2.0, theta=None))
    assert (record.theta, record.dock_theta, record.dock_at) == (None, 0.25, T + 2000)


def test_an_untimed_docked_pose_is_a_sample_without_a_time() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 1.0, 2.0))
    record = feed(decoder, docked(None, 1.2, 2.0))
    assert dock(record) == (1.1, 2.0, 2)
    assert record.dock_at is None


def test_rejected_poses_are_not_samples() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(5, 1.0, 2.0))
    record = feed(
        decoder,
        docked(4, 1.2, 2.0),  # stale
        docked(6, 0.0, 0.0, theta=None),  # a placeholder
        {
            "type": 1,
            "time": str(T + 7000),
            "postureX": "?",
            "postureY": "2",
            "vehicleState": "1",
        },  # unparsable
    )
    assert dock(record) == (1.0, 2.0, 1)


def test_each_message_carries_the_estimate_as_it_stood_after_its_entry() -> None:
    parsed = LocationDecoder().decode(DEVICE, [docked(1, 1.0, 2.0), docked(2, 1.2, 2.0)], RECEIVED)
    assert [dock(message.location) for message in parsed.messages] == [(1.0, 2.0, 1), (1.1, 2.0, 2)]


def test_devices_have_their_own_estimate_and_candidate() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 0.0, 0.0), docked(2, 5.0, 0.0), docked(3, 5.0, 0.0))
    decoder.decode("dev-2", [docked(1, 5.0, 0.0)], RECEIVED)
    assert dock(decoder.get("dev-2")) == (5.0, 0.0, 1)
    record = feed(decoder, docked(4, 5.0, 0.0))
    assert dock(record) == (5.0, 0.0, 3)


def test_a_restored_record_keeps_its_estimate_and_adds_no_samples() -> None:
    decoder = LocationDecoder()
    feed(decoder, *(docked(step, 1.0, 2.0) for step in range(1, 6)))
    saved = DeviceLocation.from_dict(json.loads(json.dumps(decoder.get(DEVICE).to_dict())))
    restored = LocationDecoder()
    restored.restore(DEVICE, saved)
    assert dock(restored.get(DEVICE)) == (1.0, 2.0, 5)
    record = feed(restored, docked(5, 1.5, 2.0))  # a replay of the last pose: stale, not a sample
    assert dock(record) == (1.0, 2.0, 5)
    record = feed(restored, docked(6, 1.6, 2.0))
    assert dock(record) == (1.1, 2.0, 6)


def test_a_restore_drops_the_move_candidate() -> None:
    decoder = LocationDecoder()
    feed(decoder, docked(1, 0.0, 0.0), docked(2, 5.0, 0.0), docked(3, 5.0, 0.0))
    decoder.restore(DEVICE, decoder.get(DEVICE))
    record = feed(decoder, docked(4, 5.0, 0.0))
    assert dock(record) == (0.0, 0.0, 1)  # the count starts again


def test_a_record_with_coordinates_but_no_samples_is_replaced_by_the_first_pose() -> None:
    decoder = LocationDecoder()
    decoder.restore(DEVICE, DeviceLocation(device_id=DEVICE, dock_x=9.0, dock_y=9.0))
    record = feed(decoder, docked(1, 1.0, 2.0))
    assert dock(record) == (1.0, 2.0, 1)


def test_the_dock_fields_round_trip_through_a_dict() -> None:
    decoder = LocationDecoder()
    record = feed(decoder, docked(1, 1.0, 2.0, theta=0.25), docked(2, 1.2, 2.0))
    data = json.loads(json.dumps(record.to_dict()))
    assert {
        key: data[key] for key in ("dock_x", "dock_y", "dock_theta", "dock_at", "dock_samples")
    } == {
        "dock_x": record.dock_x,
        "dock_y": 2.0,
        "dock_theta": 0.5,
        "dock_at": T + 2000,
        "dock_samples": 2,
    }
    assert DeviceLocation.from_dict(data) == record


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({}, (None, None, None, None, 0)),
        (
            {"dock_x": "1.5", "dock_y": 2, "dock_theta": "x", "dock_at": "12", "dock_samples": "7"},
            (1.5, 2.0, None, 12, 7),
        ),
        ({"dock_samples": -3}, (None, None, None, None, 0)),
        ({"dock_samples": "many"}, (None, None, None, None, 0)),
        ({"dock_samples": None}, (None, None, None, None, 0)),
    ],
)
def test_from_dict_reads_the_dock_fields_or_leaves_them_empty(
    data: dict[str, Any], fields: tuple[Any, ...]
) -> None:
    location = DeviceLocation.from_dict({"device_id": DEVICE, **data})
    assert (
        location.dock_x,
        location.dock_y,
        location.dock_theta,
        location.dock_at,
        location.dock_samples,
    ) == fields

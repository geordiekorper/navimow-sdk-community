"""target_zone(): the target report read with the mower's shown state (mower_sdk.location)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import mower_sdk
from mower_sdk import location as location_module
from mower_sdk.location import MOW_ALL_STATES, LocationDecoder, TargetZone, target_zone
from mower_sdk.models import DeviceLocation, MowerStatus

DEVICE = "dev-1"


def targeting(ids: tuple[int, ...] | None) -> DeviceLocation:
    return DeviceLocation(device_id=DEVICE, partition_ids=ids)


def test_the_values_and_the_mow_all_states() -> None:
    assert (TargetZone.ALL, TargetZone.NONE) == ("all", "none")
    assert frozenset({"mowing", "paused"}) == MOW_ALL_STATES


@pytest.mark.parametrize("status", [None, "mowing", MowerStatus.DOCKED])
def test_none_before_any_target_report(status: Any) -> None:
    assert target_zone(None, status) is None
    assert target_zone(targeting(None), status) is None


@pytest.mark.parametrize(
    "status", [None, "docked", "mowing", "returning", MowerStatus.IDLE, "not a state"]
)
def test_a_report_naming_zones_gives_its_first_zone_whatever_the_status(status: Any) -> None:
    assert target_zone(targeting((7, 3)), status) == 7


EMPTY_REPORT_CASES: list[tuple[Any, TargetZone]] = [
    ("mowing", TargetZone.ALL),
    ("paused", TargetZone.ALL),
    (MowerStatus.MOWING, TargetZone.ALL),
    (MowerStatus.PAUSED, TargetZone.ALL),
    ("returning", TargetZone.NONE),  # a dock command clears the target for the trip home
    ("charging", TargetZone.NONE),  # a charging break during a mow-all task
    ("docked", TargetZone.NONE),
    ("idle", TargetZone.NONE),
    ("mapping", TargetZone.NONE),
    (MowerStatus.ERROR, TargetZone.NONE),
    ("updating", TargetZone.NONE),
    ("offline", TargetZone.NONE),
    ("unknown", TargetZone.NONE),
    ("Mowing", TargetZone.NONE),  # canonical strings only
    (None, TargetZone.NONE),
]


@pytest.mark.parametrize(("status", "zone"), EMPTY_REPORT_CASES)
def test_an_empty_report_is_read_by_the_status(status: Any, zone: TargetZone) -> None:
    assert target_zone(targeting(()), status) is zone


def test_the_empty_report_cases_cover_every_status() -> None:
    covered = {
        status.value if isinstance(status, MowerStatus) else status
        for status, _ in EMPTY_REPORT_CASES
    }
    assert {status.value for status in MowerStatus} <= covered


def test_it_reads_the_decoders_record() -> None:
    decoder = LocationDecoder()
    received = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    t = int(received.timestamp() * 1000) - 60_000
    decoder.decode(DEVICE, [{"type": 3, "time": str(t)}], received)
    assert target_zone(decoder.get(DEVICE), "mowing") is TargetZone.ALL
    decoder.decode(DEVICE, [{"type": 3, "time": str(t + 1000), "partitionIds": [4]}], received)
    assert target_zone(decoder.get(DEVICE), "mowing") == 4


def test_target_zone_is_exported_from_the_package() -> None:
    for name in ("MOW_ALL_STATES", "TargetZone", "target_zone"):
        assert getattr(mower_sdk, name) is getattr(location_module, name)
        assert name in mower_sdk.__all__

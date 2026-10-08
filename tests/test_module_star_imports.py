"""Star imports from mower_sdk.mqtt, mower_sdk.models, mower_sdk.errors and the community's modules.

Before the legacy move only the package-level star import was tested. These three
modules now serve their moved names through ``__getattr__``, and names served
that way are not module globals, so each module needs an ``__all__`` for a
star import to carry them. Each ``__all__`` lists exactly the
inventory names, what upstream defined in the module plus the aliases callers
picked up, and the names the community edition has added to that module, which
``COMMUNITY_ADDITIONS`` records. The imported helpers that a bare star import
used to leak (``json``, ``asyncio``, ``dataclass`` and the like) no longer
arrive; plain attribute access to them is unchanged. A module the community
edition added (``mower_sdk.location``, ``mower_sdk.watchdog``,
``mower_sdk.navimow_client``) has no inventory
entry: its ``__all__`` is exactly what ``COMMUNITY_ADDITIONS`` records for it.
"""

from __future__ import annotations

import importlib
import json
import warnings
from pathlib import Path

import pytest

INVENTORY = json.loads(
    Path(__file__).with_name("upstream_exports.json").read_text(encoding="utf-8")
)
MODULES = [
    "mower_sdk.mqtt",
    "mower_sdk.models",
    "mower_sdk.errors",
    "mower_sdk.location",
    "mower_sdk.watchdog",
    "mower_sdk.navimow_client",
]

# Public names the community edition adds to a module's ``__all__`` beyond the
# inventory: module name -> the names added. Each addition is listed here in the
# commit that introduces it; ``upstream_exports.json`` itself is never edited.
COMMUNITY_ADDITIONS: dict[str, set[str]] = {
    "mower_sdk.mqtt": {"ConnectionEvent", "ReceivedPayload", "parse_topic"},
    "mower_sdk.errors": {
        "MowerAuthRequiredError",
        "MowerRateLimitedError",
        "MowerTransportError",
        "MowerUnsupportedOperationError",
    },
    "mower_sdk.models": {
        "CommandReceipt",
        "CommandVerdict",
        "DeviceLocation",
        "DeviceLocationMessage",
        "MqttConnectionInfo",
        "RAW_STATE_TO_CANONICAL",
        "REST_STATUS_KNOWN_FIELDS",
        "RejectedMessage",
        "STATE_KNOWN_FIELDS",
        "SkippedLocationEntry",
        "VEHICLE_STATE_TO_STATUS",
        "battery_from_payload",
        "canonical_state",
        "mower_status_from_raw",
        "mower_time_ms",
    },
    "mower_sdk.location": {
        "DOCK_MAX_SAMPLES",
        "DOCK_MOVE_DISTANCE_M",
        "DOCK_MOVE_SAMPLES",
        "DOCK_VEHICLE_STATES",
        "LOCATION_ENTRY_TYPES",
        "LOCATION_KNOWN_FIELDS",
        "LocationDecoder",
        "MOW_ALL_STATES",
        "PLAUSIBLE_MIN_MS",
        "ParsedLocation",
        "REASON_PRIORITY",
        "TIME_AHEAD_MAX_MS",
        "TargetZone",
        "target_zone",
    },
    "mower_sdk.watchdog": {
        "IGNORED_REST_STATES",
        "LOCATION_SILENCE_SECONDS",
        "MOVING_STATES",
        "MqttWatchdog",
        "REST_CACHE_LAG_SECONDS",
        "RebuildRequest",
        "WATCHDOG_DEBOUNCE_SECONDS",
        "WatchInput",
    },
    "mower_sdk.navimow_client": {
        "MQTT_STALE_SECONDS",
        "MowerState",
        "NavimowClient",
        "StateSource",
    },
}


def inventory_names(module: str) -> set[str]:
    """What upstream published from module; nothing for a module upstream never had."""
    entry = INVENTORY["modules"].get(module)
    return set() if entry is None else {*entry["defines"], *entry["aliases"]}


def expected_all(module: str) -> set[str]:
    return inventory_names(module) | COMMUNITY_ADDITIONS.get(module, set())


def star_import(module: str) -> dict[str, object]:
    namespace: dict[str, object] = {}
    with warnings.catch_warnings():
        # The lazy legacy names warn once per process; the deprecation policy
        # itself is tested in fresh subprocesses elsewhere.
        warnings.simplefilter("ignore", DeprecationWarning)
        exec(f"from {module} import *", namespace)  # the star import is the point of this test
    return {name: value for name, value in namespace.items() if not name.startswith("__")}


@pytest.mark.parametrize("module", MODULES)
def test_every_inventory_name_arrives_with_a_star_import(module: str) -> None:
    arrived = star_import(module)
    missing = sorted(inventory_names(module) - arrived.keys())
    assert not missing, f"names missing from a star import of {module}: {missing}"


@pytest.mark.parametrize("module", MODULES)
def test_star_import_carries_the_module_attributes(module: str) -> None:
    arrived = star_import(module)
    loaded = importlib.import_module(module)
    for name, value in arrived.items():
        assert value is getattr(loaded, name), (
            f"{module}.{name} differs from the star-imported object"
        )


@pytest.mark.parametrize("module", MODULES)
def test_all_lists_exactly_the_inventory_names(module: str) -> None:
    declared = importlib.import_module(module).__all__
    assert sorted(declared) == sorted(expected_all(module))
    assert len(declared) == len(set(declared))
    assert set(star_import(module)) == set(declared)

"""The location channel: decoding its messages into a per-device record.

The mower publishes its pose, its task progress, its target zones and delays on
``/downlink/vehicle/{device_id}/realtimeDate/location``. A message is a JSON
array of entries (occasionally a lone entry object), each with an integer
``type`` and numbers that usually arrive as strings:

- type 1, pose: ``postureX``, ``postureY`` (metres), ``postureTheta`` (radians),
  ``vehicleState`` (a code, see VEHICLE_STATE_TO_STATUS) and ``time``;
- type 2, task: ``currentMowBoundary`` (the partition being mowed now),
  ``currentMowProgress`` (route progress, 0 to 10000), ``mowingPercentage``,
  ``subtotalArea`` and ``mowingWeekArea`` (m²), ``action``, ``subAction``,
  ``mowStartType``, ``mapWorkPosition`` and ``time``;
- type 3, target: ``partitionIds`` (absent when no zone is targeted) and ``time``;
- type 4, delay: ``taskDelay``, with no time.

LocationDecoder merges each message into the device's DeviceLocation entry by
entry and says what it did in a ParsedLocation. Messages arrive late and out
of order, and a reconnect replays recent ones newest first, so the timed
entries are applied in time order and an entry at or below the newest applied
time of its type is stale.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any

from mower_sdk.models import DeviceLocation, DeviceLocationMessage, _number, _whole

__all__ = [
    "LOCATION_ENTRY_TYPES",
    "LOCATION_KNOWN_FIELDS",
    "LocationDecoder",
    "PLAUSIBLE_MIN_MS",
    "ParsedLocation",
    "REASON_PRIORITY",
    "TIME_AHEAD_MAX_MS",
]

# Every entry field the decoder knows. An entry with another field still applies
# what it knows, and the message is marked unknown_field, so a field the mower
# starts sending is noticed.
LOCATION_KNOWN_FIELDS = frozenset({
    "type", "time", "postureX", "postureY", "postureTheta", "vehicleState",
    "currentMowBoundary", "currentMowProgress", "mowingPercentage",
    "subtotalArea", "mowingWeekArea", "partitionIds", "taskDelay", "action",
    "subAction", "mowStartType", "mapWorkPosition",
})
LOCATION_ENTRY_TYPES = frozenset({1, 2, 3, 4})

# Entry times outside this window are not believed: before 2020, or more than five
# minutes after receipt (target reports stamped January 1970 have been seen).
PLAUSIBLE_MIN_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z
TIME_AHEAD_MAX_MS = 5 * 60 * 1000

# When a message earns several reasons, ParsedLocation.reason is the first of
# these present; all of them are in ParsedLocation.reasons.
REASON_PRIORITY = (
    "unparsable", "implausible_time", "unknown_type", "unknown_field",
    "stale", "placeholder",
)

# The entry types whose time is guarded, with the record field holding the
# latest observation time of that type.
_OBSERVED_AT = {1: "pose_at", 2: "task_at", 3: "target_last_at"}


@dataclass
class ParsedLocation:
    """What one location message did.

    ``messages`` holds one DeviceLocationMessage per applied entry, in the order
    they were applied (timed entries by ascending time, untimed entries in their
    place in the message); ``reasons`` why any part of the message was not
    applied, or was applied with something unknown in it.
    """

    messages: list[DeviceLocationMessage] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def _reject(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def reason(self) -> str | None:
        """The deciding reason, by REASON_PRIORITY, or None when there is none."""
        return next((reason for reason in REASON_PRIORITY if reason in self.reasons), None)


def _entry_time(item: dict[str, Any]) -> int | None:
    value = _whole(item.get("time"))
    return value if value is not None and value > 0 else None


def _in_time_order(entries: list[Any]) -> list[Any]:
    """The entries with the timed ones in ascending time order, the untimed ones in place.

    A reconnect's catch-up message lists poses newest first; applied in that
    order, the stale check would take the newest and reject the rest. The sort
    is stable, so equal times keep their order.
    """
    slots = [
        i for i, item in enumerate(entries)
        if isinstance(item, dict) and type(item.get("type")) is int and item["type"] in _OBSERVED_AT and _entry_time(item)
    ]
    ordered = sorted((entries[i] for i in slots), key=_entry_time)
    result = list(entries)
    for slot, item in zip(slots, ordered, strict=True):
        result[slot] = item
    return result


def _partition_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(pid for pid in (_whole(v) for v in value) if pid is not None)


def _task_delay(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


class LocationDecoder:
    """The per-device location records, and the merge of each message into them.

    decode() merges a message; get() returns a device's record; restore() installs
    a record persisted earlier, before the first message after a restart, so late
    entries older than what was already applied are still rejected. Holding
    messages back until the restore is done is the caller's.
    """

    def __init__(self) -> None:
        self._records: dict[str, DeviceLocation] = {}

    def get(self, device_id: str) -> DeviceLocation | None:
        """The device's merged record, or None before anything was applied or restored."""
        return self._records.get(device_id)

    def restore(self, device_id: str, location: DeviceLocation) -> None:
        """Install a persisted record for device_id, with its high-water marks.

        A record persisted without marks gets them from its observation times
        (pose_at, task_at, target_last_at).
        """
        marks = dict(location.marks)
        if not marks:
            for entry_type, name in _OBSERVED_AT.items():
                observed = getattr(location, name)
                if observed is not None:
                    marks[entry_type] = observed
        self._records[device_id] = replace(location, device_id=device_id, marks=marks)

    def decode(
        self,
        device_id: str,
        payload: Any,
        received_at: datetime,
        now_ms: int | None = None,
    ) -> ParsedLocation:
        """Merge one message into the device's record.

        payload is the decoded JSON value as the mower sent it: a list of entries,
        or a lone entry object (anything the caller added must be removed first).
        received_at is the UTC receipt time; now_ms, the time the plausibility
        window is measured from, defaults to it.

        The rules: anything but a list or an object is unparsable; an empty list
        changes nothing; an entry of an unknown type is skipped (unknown_type); a
        delay entry without taskDelay, which a reconnect sends, is skipped silently;
        an entry with a field outside LOCATION_KNOWN_FIELDS is applied and marked
        unknown_field; a time outside the plausibility window, or a pose, task or
        target time that is zero, negative or unreadable, is implausible_time; an
        entry sent without a time is applied and leaves the marks as they were;
        a time at or below the newest applied time of the entry's type is stale
        (delay entries carry no time and are never stale); a pose whose x or y is
        unreadable is unparsable, and an all-zero pose a placeholder, neither
        applied; a target entry without partitionIds clears the target; a repeat of
        the same target set advances only target_last_at.
        """
        result = ParsedLocation()
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            result._reject("unparsable")
            return result
        if now_ms is None:
            now_ms = round(received_at.timestamp() * 1000) if received_at else round(time.time() * 1000)

        current = self._records.get(device_id) or DeviceLocation(device_id=device_id)
        record = {item.name: getattr(current, item.name) for item in fields(DeviceLocation)}
        record["device_id"] = device_id
        record["marks"] = dict(current.marks)

        for item in _in_time_order(payload):
            if not isinstance(item, dict):
                continue
            entry_type = item.get("type")
            if not set(item) <= LOCATION_KNOWN_FIELDS:
                result._reject("unknown_field")
            if type(entry_type) is not int or entry_type not in LOCATION_ENTRY_TYPES:
                result._reject("unknown_type")
                continue
            if entry_type == 4 and "taskDelay" not in item:
                continue  # the reconnect-time shape: no delay in it, the pose has the state
            entry_time = _entry_time(item)
            if entry_time is None and item.get("time") is not None and entry_type in _OBSERVED_AT:
                # A time was sent but is zero, negative or unreadable: not believed, and
                # not taken as "no time", which would skip the stale check.
                result._reject("implausible_time")
                continue
            if entry_type not in _OBSERVED_AT:
                entry_time = None  # a delay entry carries no time of its own and is never guarded
            elif entry_time is not None and not PLAUSIBLE_MIN_MS <= entry_time <= now_ms + TIME_AHEAD_MAX_MS:
                result._reject("implausible_time")
                continue
            newest = self._newest(record, entry_type)
            if entry_time is not None and newest is not None and entry_time <= newest:
                result._reject("stale")
                continue

            own: dict[str, Any] = {}
            if entry_type == 1:
                x, y = _number(item.get("postureX")), _number(item.get("postureY"))
                if x is None or y is None:
                    result._reject("unparsable")
                    continue
                theta = _number(item.get("postureTheta"))
                if x == 0 and y == 0 and not theta:
                    result._reject("placeholder")
                    continue
                own = {"x": x, "y": y, "theta": theta, "vehicle_state": _whole(item.get("vehicleState"))}
                record.update(own, pose_at=entry_time, pose_received_at=received_at)
            elif entry_type == 2:
                if "currentMowBoundary" in item:
                    own["current_zone"] = _whole(item.get("currentMowBoundary"))
                    record.update(current_zone=own["current_zone"], zone_at=entry_time)
                if "currentMowProgress" in item:
                    own["route_progress"] = _whole(item.get("currentMowProgress"))
                    record.update(route_progress=own["route_progress"], progress_at=entry_time)
                task = {
                    "mowing_percentage": _number(item.get("mowingPercentage")),
                    "area_m2": _number(item.get("subtotalArea")),
                    "week_area_m2": _number(item.get("mowingWeekArea")),
                    "action": _whole(item.get("action")),
                    "sub_action": _whole(item.get("subAction")),
                    "mow_start_type": _whole(item.get("mowStartType")),
                    "map_work_position": None if item.get("mapWorkPosition") is None else str(item["mapWorkPosition"]),
                }
                own.update(task)
                record.update(task, task_at=entry_time)
            elif entry_type == 3:
                ids = _partition_ids(item.get("partitionIds"))
                own["partition_ids"] = ids
                if record["partition_ids"] is None or set(ids) != set(record["partition_ids"]):
                    record.update(partition_ids=ids, target_at=entry_time)
                record["target_last_at"] = entry_time  # a repeat of the same set, in any order, only advances this
            else:
                own["task_delay"] = _task_delay(item.get("taskDelay"))
                record.update(task_delay=own["task_delay"], delay_received_at=received_at)

            if entry_time is not None and entry_type in _OBSERVED_AT:
                record["marks"] = {**record["marks"], entry_type: entry_time}
            location = DeviceLocation(**{**record, "marks": dict(record["marks"])})
            result.messages.append(
                DeviceLocationMessage(
                    device_id=device_id,
                    entry_type=entry_type,
                    timestamp=entry_time if entry_type != 4 else None,
                    received_at=received_at,
                    location=location,
                    raw=dict(item),
                    **own,
                )
            )
        if result.messages:
            self._records[device_id] = result.messages[-1].location
        return result

    @staticmethod
    def _newest(record: dict[str, Any], entry_type: int) -> int | None:
        """The newest applied time of entry_type: its mark or its observation time, whichever is newer."""
        name = _OBSERVED_AT.get(entry_type)
        if name is None:
            return None
        times = [t for t in (record["marks"].get(entry_type), record[name]) if t is not None]
        return max(times) if times else None

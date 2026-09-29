"""Data models.

Defines every data model the SDK uses: enums and dataclasses.
"""

import importlib
import math
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from mower_sdk._deprecation import warn_legacy

if TYPE_CHECKING:
    from mower_sdk.legacy.thing_models import (
        ThingEventMessage as ThingEventMessage,
        ThingParams as ThingParams,
        ThingPropertiesMessage as ThingPropertiesMessage,
        ThingStatusMessage as ThingStatusMessage,
    )

# The public surface upstream published from this module, plus the community
# additions CommandReceipt and CommandVerdict and the location channel's
# DeviceLocation, DeviceLocationMessage, VEHICLE_STATE_TO_STATUS and
# mower_time_ms, RejectedMessage, and STATE_KNOWN_FIELDS and
# REST_STATUS_KNOWN_FIELDS. The four Thing* classes now live
# in mower_sdk.legacy.thing_models and are served by __getattr__.
__all__ = [
    "CommandReceipt",
    "CommandVerdict",
    "Device",
    "DeviceAttributesMessage",
    "DeviceCommandMessage",
    "DeviceEventMessage",
    "DeviceLocation",
    "DeviceLocationMessage",
    "DeviceStateMessage",
    "DeviceStatus",
    "MowerCommand",
    "MowerError",
    "MowerStatus",
    "REST_STATUS_KNOWN_FIELDS",
    "RejectedMessage",
    "STATE_KNOWN_FIELDS",
    "VEHICLE_STATE_TO_STATUS",
    "mower_time_ms",
    "ThingEventMessage",
    "ThingParams",
    "ThingPropertiesMessage",
    "ThingStatusMessage",
]


_RAW_STATE_TO_CANONICAL: dict[str, str] = {
    "isDocked": "docked",
    "isIdel": "idle",
    "isIdle": "idle",
    "isMapping": "mapping",
    "isRunning": "mowing",
    "isPaused": "paused",
    "isDocking": "returning",
    "Error": "error",
    "error": "error",
    "isLifted": "error",
    "inSoftwareUpdate": "updating",
    "Self-Checking": "idle",
    "Self-checking": "idle",
    "Offline": "offline",
    "offline": "offline",
}


def _raw_state(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the raw state under the first key with a truthy value, else the last key's value.

    Exactly what ``data.get(k1) or data.get(k2) or data.get(k3)`` returns: the
    first truthy value in key order, or, when none is truthy, whatever the last
    key holds, falsy values such as False or "" included. Each reader passes
    its own key order, because a payload carrying both keys resolves
    differently per reader and that precedence is kept.
    """
    value = None
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return value


def _first_present(data: dict[str, Any], keys: tuple[str, ...], default: Any) -> Any:
    """Return the value under the first key present in data, else the default.

    Presence, not truth, decides: an explicit empty or None value under an
    earlier key is returned as it is, and a later key is read only when the
    earlier ones are absent.
    """
    for key in keys:
        if key in data:
            return data[key]
    return default


def _normalize_state_value(raw_state: Any) -> str:
    """Normalize cloud/raw mower state to canonical internal state value."""
    if isinstance(raw_state, MowerStatus):
        return raw_state.value
    if not isinstance(raw_state, str):
        return "unknown"
    return _RAW_STATE_TO_CANONICAL.get(raw_state, raw_state)


def _int(value: Any) -> int | None:
    """int() that refuses bools and non-finite floats (JSON allows Infinity and NaN).

    None for anything int() cannot read, a bool, a non-finite float or a value
    too large to convert.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_battery_value(data: dict[str, Any]) -> int | None:
    """Read the battery percentage from a REST status or an MQTT state payload.

    The ``capacityRemaining`` entry whose ``unit`` is PERCENTAGE (compared
    upper-cased) comes first, then any ``capacityRemaining`` entry whose
    ``rawValue`` parses, then the plain ``battery`` field. Each value goes
    through ``_int``, so None is returned when the payload carries no readable
    value: both keys missing, an unparsable or non-numeric value, a bool, a
    non-finite float. Out-of-range numbers pass through unchanged. One reader
    for both payload shapes.
    """
    capacity = data.get("capacityRemaining")
    if isinstance(capacity, list):
        for entry in capacity:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("unit", "")).upper() != "PERCENTAGE":
                continue
            value = _int(entry.get("rawValue"))
            if value is not None:
                return value
        for entry in capacity:
            if isinstance(entry, dict):
                value = _int(entry.get("rawValue"))
                if value is not None:
                    return value
    return _int(data.get("battery"))


class MowerStatus(Enum):
    """Mower status.

    The state channel and the REST status never report CHARGING; the location
    channel's pose code does (a docked mower that is charging). MAPPING,
    UPDATING and OFFLINE are the raw states isMapping, inSoftwareUpdate and
    Offline/offline. A state message keeps a raw state that normalisation
    changed in metrics["raw_state"]; a DeviceStatus keeps a vehicleState key
    in extra["vehicleState"].
    """

    IDLE = "idle"  # Idle
    MOWING = "mowing"  # Mowing
    PAUSED = "paused"  # Paused
    DOCKED = "docked"  # Docked
    CHARGING = "charging"  # Charging
    ERROR = "error"  # Error
    RETURNING = "returning"  # Returning to the dock
    MAPPING = "mapping"  # Mapping the lawn
    UPDATING = "updating"  # Installing a software update
    OFFLINE = "offline"  # Not connected to the cloud
    UNKNOWN = "unknown"  # Unknown state


class MowerCommand(Enum):
    """Mower control commands."""

    START = "start"  # Start mowing
    PAUSE = "pause"  # Pause mowing
    DOCK = "dock"  # Return to the charging station
    RESUME = "resume"  # Resume mowing
    STOP = "stop"  # Stop


class MowerError(Enum):
    """Mower error types."""

    NONE = "none"  # No error
    STUCK = "stuck"  # Stuck
    LIFTED = "lifted"  # Lifted
    RAIN = "rain"  # Rain
    BATTERY_LOW = "battery_low"  # Battery low
    SENSOR_ERROR = "sensor_error"  # Sensor error
    MOTOR_ERROR = "motor_error"  # Motor error
    BLADE_ERROR = "blade_error"  # Blade error
    UNKNOWN = "unknown"  # Unknown error


class CommandVerdict(StrEnum):
    """The cloud's verdict on a submitted command, read from the reply's command results.

    ACCEPTED: a result said SUCCESS and none said alreadyInState. The cloud
    accepted the command; it does not mean the mower acted. Pause and resume
    settle within about 30 seconds, docking can take minutes; poll the status
    for the target state.
    ALREADY_IN_STATE: a result was an ERROR with errorCode alreadyInState, so
    the mower was already in the requested state. It wins over SUCCESS.
    UNKNOWN: no result said either, an empty result list included.
    """

    ACCEPTED = "accepted"
    ALREADY_IN_STATE = "already_in_state"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandReceipt:
    """What the cloud replied to one command, classified.

    Returned by MowerAPI.async_send_command_receipt. No receipt exists for a
    command the cloud refused (MowerAPIError) or that got no reply
    (MowerAPIError with the transport error or TimeoutError as its cause; the
    cloud may still have accepted it).

    Attributes:
        device_id: The device the command was sent to
        command: The command sent
        verdict: The cloud's verdict, a CommandVerdict
        command_number: The reply's command number, when it carried one under
            a recognised key (cmdNum and its spellings); None otherwise. No
            captured reply carries one, and where it would come from is
            undocumented.
        results: The per-command result dicts from the reply, as a tuple. Left
            out of the hash the frozen dataclass derives from its fields, since
            dicts are unhashable; equality still compares it.
    """

    device_id: str
    command: MowerCommand
    verdict: CommandVerdict
    command_number: str | None = None
    results: tuple[dict[str, Any], ...] = field(default=(), hash=False)

    @property
    def accepted(self) -> bool:
        return self.verdict is CommandVerdict.ACCEPTED

    @property
    def already_in_state(self) -> bool:
        return self.verdict is CommandVerdict.ALREADY_IN_STATE


@dataclass
class Device:
    """Device information.

    Attributes:
        id: Device ID
        name: Device name
        model: Device model
        firmware_version: Firmware version
        serial_number: Serial number
        mac_address: MAC address (optional)
        online: Whether the device is online
        extra: Extra information (optional)
    """

    id: str
    name: str
    model: str
    firmware_version: str
    serial_number: str
    mac_address: str | None = None
    online: bool = False
    extra: dict[str, Any] | None = None
    product_key: str | None = None
    device_name: str | None = None
    iot_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Device":
        """Create a Device from a dictionary.

        The snake_case keys the model defines are read first. When one is
        absent, and only then, its camelCase spelling is read instead:
        ``deviceModel``, ``firmwareVersion``, ``serialNumber``, ``macAddress``
        and ``isOnline``; an explicit empty or None snake_case value is kept.
        ``firmware_version`` has one more source: ``firmware_version`` if
        present, else ``firmware``, the key the device-list reply of an X430
        carries, else ``firmwareVersion``. ``product_key``, ``device_name``
        and ``iot_id`` take the first truthy value of their camelCase key,
        their snake_case key and, for the last two, ``name`` and ``id``.

        Args:
            data: Dictionary holding the device information

        Returns:
            A Device instance
        """
        product_key = data.get("productKey") or data.get("product_key")
        device_name = data.get("deviceName") or data.get("device_name") or data.get("name")
        iot_id = data.get("iotId") or data.get("iot_id") or data.get("id")

        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            model=_first_present(data, ("model", "deviceModel"), ""),
            firmware_version=_first_present(
                data, ("firmware_version", "firmware", "firmwareVersion"), ""
            ),
            serial_number=_first_present(data, ("serial_number", "serialNumber"), ""),
            mac_address=_first_present(data, ("mac_address", "macAddress"), None),
            online=_first_present(data, ("online", "isOnline"), False),
            extra=data.get("extra"),
            product_key=product_key,
            device_name=device_name,
            iot_id=iot_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary.

        Returns:
            Dictionary holding the device information
        """
        result = {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "serial_number": self.serial_number,
            "online": self.online,
        }
        if self.mac_address:
            result["mac_address"] = self.mac_address
        if self.extra:
            result["extra"] = self.extra
        if self.product_key:
            result["product_key"] = self.product_key
        if self.device_name:
            result["device_name"] = self.device_name
        if self.iot_id:
            result["iot_id"] = self.iot_id
        return result


# The fields each channel has been seen to carry (the facade adds device_id to a
# state payload). A payload with others is still read; the unknown keys are
# what a consumer may want to record. Deliberately the observed set, not the set
# the readers accept: DeviceStateMessage.from_dict also reads position, error,
# metrics and signal_strength, which no mower has been seen to send, so a state
# message carrying one of them is reported as a new field.
STATE_KNOWN_FIELDS = frozenset({
    "state", "vehicleState", "status", "battery", "capacityRemaining",
    "timestamp", "device_id",
})
REST_STATUS_KNOWN_FIELDS = frozenset({
    "id", "device_id", "deviceId", "vehicleState", "capacityRemaining",
    "descriptiveCapacityRemaining", "battery",
})

# The payload keys DeviceStatus.from_dict reads into a field; every other key is
# kept in extra.
_DEVICE_STATUS_READ_KEYS = frozenset({
    "status", "state", "vehicleState", "error_code", "capacityRemaining", "battery",
    "descriptiveCapacityRemaining", "extra", "device_id", "id", "position",
    "error_message", "mowing_time", "total_mowing_time", "signal_strength", "timestamp",
})


@dataclass
class DeviceStatus:
    """Device status.

    Attributes:
        device_id: Device ID
        status: Device status (a MowerStatus value)
        battery: Battery level in percent, or None when the payload carried no
            readable value; out-of-range numbers pass through. to_dict emits
            the None.
        position: Position (optional, as {"lat": float, "lng": float})
        error_code: Error code (a MowerError value)
        error_message: Error message (optional)
        mowing_time: Duration of the current mowing session in seconds (optional)
        total_mowing_time: Total mowing time in seconds (optional)
        signal_strength: Signal strength (optional)
        timestamp: Timestamp of the status update (optional)
        extra: Extra information (optional)
    """

    device_id: str
    status: MowerStatus
    battery: int | None
    position: dict[str, float] | None = None
    error_code: MowerError = MowerError.NONE
    error_message: str | None = None
    mowing_time: int | None = None
    total_mowing_time: int | None = None
    signal_strength: int | None = None
    timestamp: int | None = None
    extra: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceStatus":
        """Create a DeviceStatus from a dictionary.

        Args:
            data: Dictionary holding the device status

        Returns:
            A DeviceStatus instance
        """
        status_source = _raw_state(data, ("status", "state", "vehicleState"))
        normalized_state = _normalize_state_value(status_source)
        try:
            status = MowerStatus(normalized_state)
        except ValueError:
            status = MowerStatus.UNKNOWN

        error_str = data.get("error_code", "none")
        try:
            error_code = MowerError(error_str)
        except ValueError:
            error_code = MowerError.UNKNOWN

        battery = _extract_battery_value(data)

        # A new dict: the caller's extra, every payload key no field reads, and the
        # raw status keys. The caller's dict is never written to.
        caller_extra = data.get("extra")
        extra = dict(caller_extra) if isinstance(caller_extra, dict) else {}
        for key, value in data.items():
            if key not in _DEVICE_STATUS_READ_KEYS:
                extra[key] = value
        if "vehicleState" in data:
            extra["vehicleState"] = data.get("vehicleState")
        if "descriptiveCapacityRemaining" in data:
            extra["descriptiveCapacityRemaining"] = data.get(
                "descriptiveCapacityRemaining"
            )
        if "capacityRemaining" in data:
            extra["capacityRemaining"] = data.get("capacityRemaining")
        if not extra:
            extra = None

        return cls(
            device_id=data.get("device_id") or data.get("id", ""),
            status=status,
            battery=battery,
            position=data.get("position"),
            error_code=error_code,
            error_message=data.get("error_message"),
            mowing_time=data.get("mowing_time"),
            total_mowing_time=data.get("total_mowing_time"),
            signal_strength=data.get("signal_strength"),
            timestamp=data.get("timestamp"),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary.

        Returns:
            Dictionary holding the device status
        """
        result = {
            "device_id": self.device_id,
            "status": self.status.value,
            "battery": self.battery,
            "error_code": self.error_code.value,
        }
        if self.position:
            result["position"] = self.position
        if self.error_message:
            result["error_message"] = self.error_message
        if self.mowing_time is not None:
            result["mowing_time"] = self.mowing_time
        if self.total_mowing_time is not None:
            result["total_mowing_time"] = self.total_mowing_time
        if self.signal_strength is not None:
            result["signal_strength"] = self.signal_strength
        if self.timestamp is not None:
            result["timestamp"] = self.timestamp
        if self.extra:
            result["extra"] = self.extra
        return result


@dataclass
class DeviceStateMessage:
    """Unified state message from MQTT."""

    device_id: str
    timestamp: int | None
    state: str
    battery: int | None = None
    signal_strength: int | None = None
    position: dict[str, float] | None = None
    error: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    # The payload as decoded, for a message made by from_dict; None for one built by
    # hand. Not compared and not in to_dict().
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceStateMessage":
        raw_state = _raw_state(payload, ("state", "status", "vehicleState"))
        normalized_state = _normalize_state_value(raw_state)
        # Taken before metrics is extended below (in place, for a dict), so raw is the
        # payload as decoded.
        raw = dict(payload)
        if isinstance(payload.get("metrics"), dict):
            raw["metrics"] = dict(payload["metrics"])
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            metrics = dict(metrics or {})
        if raw_state is not None and normalized_state != raw_state:
            metrics["raw_state"] = raw_state

        return cls(
            device_id=payload.get("device_id", ""),
            timestamp=payload.get("timestamp"),
            state=normalized_state,
            battery=_extract_battery_value(payload),
            signal_strength=payload.get("signal_strength"),
            position=payload.get("position"),
            error=payload.get("error"),
            metrics=metrics or None,
            raw=raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "state": self.state,
            "battery": self.battery,
            "signal_strength": self.signal_strength,
            "position": self.position,
            "error": self.error,
            "metrics": self.metrics,
        }


@dataclass
class DeviceEventMessage:
    """Unified event message from MQTT."""

    device_id: str
    timestamp: int | None
    type: str
    event: str
    level: str | None = None
    message: str | None = None
    params: dict[str, Any] | None = None
    # The payload as decoded, as on DeviceStateMessage.
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceEventMessage":
        return cls(
            device_id=payload.get("device_id", ""),
            timestamp=payload.get("timestamp"),
            type=payload.get("type", "system"),
            event=payload.get("event", ""),
            level=payload.get("level"),
            message=payload.get("message"),
            params=payload.get("params"),
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "event": self.event,
            "level": self.level,
            "message": self.message,
            "params": self.params,
        }


@dataclass
class DeviceAttributesMessage:
    """Unified attributes message from MQTT."""

    device_id: str
    attributes: dict[str, Any]
    # The payload as decoded, as on DeviceStateMessage.
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceAttributesMessage":
        return cls(
            device_id=payload.get("device_id", ""),
            attributes=payload.get("attributes", {}) or {},
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "attributes": self.attributes,
        }


@dataclass
class DeviceCommandMessage:
    """Unified command message for MQTT publish."""

    id: str
    device_id: str
    command: str
    params: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceCommandMessage":
        return cls(
            id=payload.get("id", ""),
            device_id=payload.get("device_id", ""),
            command=payload.get("command", ""),
            params=payload.get("params"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "command": self.command,
            "params": self.params or {},
        }


# ---- the location channel ---------------------------------------------------------------------

# The pose entry's vehicleState code: 1 docked and charged, 2 docked and charging,
# 3 paused or stopped, 4 mowing, 5 returning, 6 mapping. A lifted mower sends no code.
VEHICLE_STATE_TO_STATUS: dict[int, MowerStatus] = {
    1: MowerStatus.DOCKED,
    2: MowerStatus.CHARGING,
    3: MowerStatus.PAUSED,
    4: MowerStatus.MOWING,
    5: MowerStatus.RETURNING,
    6: MowerStatus.MAPPING,
}

# A mower time above this is read as milliseconds, at or below it as seconds.
_MILLISECONDS_ABOVE = 100_000_000_000


def _number(value: Any) -> float | None:
    """A vendor number (often a string such as "100.00") as a float; None for a bool,
    a non-finite value or anything float() cannot read."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _whole(value: Any) -> int | None:
    """A vendor integer (an int, a float or a numeric string) as an int, else None."""
    number = _number(value)
    return None if number is None else int(number)


def mower_time_ms(value: Any) -> int | None:
    """A mower timestamp as epoch milliseconds, whether it was sent in seconds or milliseconds.

    Numbers may arrive as strings. None when the value is absent, unreadable, or
    not positive.
    """
    number = _whole(value)
    if number is None or number <= 0:
        return None
    return number if number > _MILLISECONDS_ABOVE else number * 1000


def _status_of(vehicle_state: int | None) -> MowerStatus | None:
    if vehicle_state is None:
        return None
    return VEHICLE_STATE_TO_STATUS.get(vehicle_state, MowerStatus.UNKNOWN)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _from_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_LOCATION_WHOLE_FIELDS = (
    "vehicle_state", "pose_at", "current_zone", "zone_at", "route_progress", "progress_at",
    "action", "sub_action", "mow_start_type", "task_at", "target_at", "target_last_at",
)
_LOCATION_NUMBER_FIELDS = ("x", "y", "theta", "mowing_percentage", "area_m2", "week_area_m2")


@dataclass(frozen=True)
class DeviceLocation:
    """The location channel's merged record for one device, as it stands.

    Built by merging the channel's entries one by one (mower_sdk.location): each
    entry type updates its own fields and leaves the others as they were. Times
    ending in ``_at`` without ``received`` are mower times in epoch milliseconds;
    ``pose_received_at`` and ``delay_received_at`` are the UTC receipt times.

    Pose (type 1 entries): ``x`` and ``y`` in metres on the lawn's local grid
    (origin near the dock or RTK reference, not latitude and longitude),
    ``theta`` in radians, ``vehicle_state`` (the pose code, see ``status``),
    ``pose_at`` and ``pose_received_at``. A pose is replaced whole, so a pose
    entry without a heading leaves ``theta`` None.

    Task (type 2 entries): ``current_zone`` is the partition the mower is in now
    (it changes once the mower has crossed into the next one), with ``zone_at``;
    ``route_progress`` is the last route reading seen (0 to 10000, 10000 at the
    end of the route), with ``progress_at``, kept when a newer task entry omits
    it. The rest of the latest task entry is replaced whole: ``mowing_percentage``,
    ``area_m2``, ``week_area_m2``, ``action``, ``sub_action``, ``mow_start_type``,
    ``map_work_position`` and ``task_at``. ``map_work_position`` is kept as the
    128-hex-digit string the mower sends: it holds sixteen 32-bit words whose
    order two readings of captures disagree on, so it is not decoded.

    Target (type 3 entries): ``partition_ids``, None until a target report has
    arrived and empty for a report with no active target (a mow-all task sends
    the same empty report as an idle mower); ``target_at``, when this set was
    first reported, and ``target_last_at``, its latest repeat.

    Delay (type 4 entries): ``task_delay`` (a rain or schedule delay) and
    ``delay_received_at``; the entry carries no time of its own.

    ``marks`` maps the entry types 1, 2 and 3 to the mower time of the newest
    entry of that type applied, the high-water mark below which a later entry is
    stale. It is kept apart from the observation times because an entry without
    a time leaves the time None while the mark must stand. to_dict() and
    from_dict() carry it, so a record persisted and handed back after a restart
    keeps rejecting late entries.
    """

    device_id: str
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    vehicle_state: int | None = None
    pose_at: int | None = None
    pose_received_at: datetime | None = None
    current_zone: int | None = None
    zone_at: int | None = None
    route_progress: int | None = None
    progress_at: int | None = None
    mowing_percentage: float | None = None
    area_m2: float | None = None
    week_area_m2: float | None = None
    action: int | None = None
    sub_action: int | None = None
    mow_start_type: int | None = None
    map_work_position: str | None = None
    task_at: int | None = None
    partition_ids: tuple[int, ...] | None = None
    target_at: int | None = None
    target_last_at: int | None = None
    task_delay: bool | None = None
    delay_received_at: datetime | None = None
    marks: dict[int, int] = field(default_factory=dict, hash=False)

    @property
    def status(self) -> MowerStatus | None:
        """The pose code as a MowerStatus; None without one, UNKNOWN for a code not in the table."""
        return _status_of(self.vehicle_state)

    @property
    def progress_percent(self) -> float | None:
        """The route reading as a percentage, else the latest task entry's mowingPercentage, else None."""
        if self.route_progress is not None:
            return self.route_progress / 100
        return self.mowing_percentage

    @property
    def progress_source(self) -> str:
        """Which reading progress_percent came from: "route", "percentage" or "none"."""
        if self.route_progress is not None:
            return "route"
        if self.mowing_percentage is not None:
            return "percentage"
        return "none"

    def to_dict(self) -> dict[str, Any]:
        """The record as JSON-ready values: times as ISO 8601, partition ids as a list, marks keyed by type."""
        data: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, datetime):
                value = _iso(value)
            elif isinstance(value, tuple):
                value = list(value)
            elif item.name == "marks":
                value = {str(entry_type): mark for entry_type, mark in value.items()}
            data[item.name] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceLocation":
        """The record from to_dict()'s output; absent or unreadable values are None, absent marks empty."""
        values: dict[str, Any] = {}
        for name in _LOCATION_WHOLE_FIELDS:
            values[name] = _whole(data.get(name))
        for name in _LOCATION_NUMBER_FIELDS:
            values[name] = _number(data.get(name))
        for name in ("pose_received_at", "delay_received_at"):
            values[name] = _from_iso(data.get(name))
        position = data.get("map_work_position")
        values["map_work_position"] = position if isinstance(position, str) else None
        delay = data.get("task_delay")
        values["task_delay"] = delay if isinstance(delay, bool) else None
        values["partition_ids"] = data.get("partition_ids")
        values["marks"] = data.get("marks")
        partition_ids = values.get("partition_ids")
        values["partition_ids"] = (
            tuple(pid for pid in (_whole(v) for v in partition_ids) if pid is not None)
            if isinstance(partition_ids, list | tuple)
            else None
        )
        marks = values.get("marks")
        values["marks"] = {
            entry_type: mark
            for entry_type, mark in (
                (_whole(key), _whole(value)) for key, value in (marks.items() if isinstance(marks, dict) else ())
            )
            if entry_type is not None and mark is not None
        }
        values["device_id"] = str(data.get("device_id") or "")
        return cls(**values)


@dataclass(frozen=True)
class DeviceLocationMessage:
    """One entry of a location message, decoded, with the merged record as it stood after it.

    ``entry_type`` is 1 (pose), 2 (task), 3 (target) or 4 (delay); ``timestamp``
    is the entry's mower time in epoch milliseconds (None for a delay entry, or an
    entry sent without a time); ``received_at`` is the UTC receipt time. The
    entry's own fields follow, None for those it does not carry (see
    DeviceLocation for their meaning). ``raw`` is the entry as decoded, and
    ``location`` the DeviceLocation after this entry was applied, so a consumer
    acting per entry sees the record at that point, not only after the message.
    """

    device_id: str
    entry_type: int
    timestamp: int | None
    received_at: datetime | None
    location: DeviceLocation
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    vehicle_state: int | None = None
    current_zone: int | None = None
    route_progress: int | None = None
    mowing_percentage: float | None = None
    area_m2: float | None = None
    week_area_m2: float | None = None
    action: int | None = None
    sub_action: int | None = None
    mow_start_type: int | None = None
    map_work_position: str | None = None
    partition_ids: tuple[int, ...] | None = None
    task_delay: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def status(self) -> MowerStatus | None:
        """The pose code as a MowerStatus; None without one, UNKNOWN for a code not in the table."""
        return _status_of(self.vehicle_state)


@dataclass(frozen=True)
class RejectedMessage:
    """A message NavimowSDK did not apply, or applied with something unknown in it.

    ``reason`` is the deciding one of ``reasons`` (unparsable, implausible_time,
    unknown_type, unknown_field, stale, placeholder). ``payload`` is the bytes the
    facade received: the wire bytes for an array, and for an object the MQTT
    client's re-encoded form with device_id added. Recording it is the
    consumer's.
    """

    channel: str
    topic: str
    device_id: str
    reason: str
    reasons: tuple[str, ...]
    payload: bytes
    received_at: datetime


# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
_LEGACY_NAMES = {
    "ThingParams": ("thing_models", "ThingParams"),
    "ThingStatusMessage": ("thing_models", "ThingStatusMessage"),
    "ThingPropertiesMessage": ("thing_models", "ThingPropertiesMessage"),
    "ThingEventMessage": ("thing_models", "ThingEventMessage"),
}


def __getattr__(name: str) -> Any:
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module."""
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value

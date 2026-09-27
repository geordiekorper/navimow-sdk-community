"""Data models.

Defines every data model the SDK uses: enums and dataclasses.
"""

import importlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from mower_sdk._deprecation import warn_legacy

if TYPE_CHECKING:
    from mower_sdk.legacy.thing_models import (
        ThingEventMessage as ThingEventMessage,
        ThingParams as ThingParams,
        ThingPropertiesMessage as ThingPropertiesMessage,
        ThingStatusMessage as ThingStatusMessage,
    )

# The public surface upstream published from this module. The four Thing*
# classes now live in mower_sdk.legacy.thing_models and are served by
# __getattr__.
__all__ = [
    "Device",
    "DeviceAttributesMessage",
    "DeviceCommandMessage",
    "DeviceEventMessage",
    "DeviceStateMessage",
    "DeviceStatus",
    "MowerCommand",
    "MowerError",
    "MowerStatus",
    "ThingEventMessage",
    "ThingParams",
    "ThingPropertiesMessage",
    "ThingStatusMessage",
]


_RAW_STATE_TO_CANONICAL: dict[str, str] = {
    "isDocked": "docked",
    "isIdel": "idle",
    "isIdle": "idle",
    "isMapping": "mowing",
    "isRunning": "mowing",
    "isPaused": "paused",
    "isDocking": "returning",
    "Error": "error",
    "error": "error",
    "isLifted": "error",
    "inSoftwareUpdate": "paused",
    "Self-Checking": "idle",
    "Self-checking": "idle",
    "Offline": "unknown",
    "offline": "unknown",
}


def _normalize_state_value(raw_state: Any) -> str:
    """Normalize cloud/raw mower state to canonical internal state value."""
    if isinstance(raw_state, MowerStatus):
        return raw_state.value
    if not isinstance(raw_state, str):
        return "unknown"
    return _RAW_STATE_TO_CANONICAL.get(raw_state, raw_state)


def _extract_battery_value(data: dict[str, Any]) -> int:
    """Extract battery percentage from multiple payload formats."""
    def _to_int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # MQTT state payload commonly carries direct battery field.
    battery = _to_int_or_none(data.get("battery"))
    if battery is not None:
        return battery

    # HTTP getVehicleStatus payload uses capacityRemaining[].rawValue.
    capacity_remaining = data.get("capacityRemaining")
    if isinstance(capacity_remaining, list):
        for item in capacity_remaining:
            if not isinstance(item, dict):
                continue
            unit = str(item.get("unit", "")).upper()
            if unit == "PERCENTAGE":
                raw_value = _to_int_or_none(item.get("rawValue"))
                if raw_value is not None:
                    return raw_value

        # Compatibility fallback: if PERCENTAGE unit missing, try first item.
        if capacity_remaining and isinstance(capacity_remaining[0], dict):
            raw_value = _to_int_or_none(capacity_remaining[0].get("rawValue"))
            if raw_value is not None:
                return raw_value

    return 0


class MowerStatus(Enum):
    """Mower status."""

    IDLE = "idle"  # Idle
    MOWING = "mowing"  # Mowing
    PAUSED = "paused"  # Paused
    DOCKED = "docked"  # Docked
    CHARGING = "charging"  # Charging
    ERROR = "error"  # Error
    RETURNING = "returning"  # Returning to the dock
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
            model=data.get("model", ""),
            firmware_version=data.get("firmware_version", ""),
            serial_number=data.get("serial_number", ""),
            mac_address=data.get("mac_address"),
            online=data.get("online", False),
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


@dataclass
class DeviceStatus:
    """Device status.

    Attributes:
        device_id: Device ID
        status: Device status (a MowerStatus value)
        battery: Battery level (0-100)
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
    battery: int
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
        status_source = data.get("status") or data.get("state") or data.get("vehicleState")
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

        extra = data.get("extra") or {}
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceStateMessage":
        raw_state = payload.get("state") or payload.get("status") or payload.get(
            "vehicleState"
        )
        normalized_state = _normalize_state_value(raw_state)
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceAttributesMessage":
        return cls(
            device_id=payload.get("device_id", ""),
            attributes=payload.get("attributes", {}) or {},
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

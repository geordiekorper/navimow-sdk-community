"""Data models.

Defines the enums and dataclasses that the REST API's replies and the MQTT
channels' messages are read into, and the readers of the cloud's raw states,
battery fields and mower times.
"""

import importlib
import math
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mower_sdk._deprecation import warn_legacy
from mower_sdk.errors import ERROR_MESSAGES, MowerAPIError

if TYPE_CHECKING:
    from mower_sdk.legacy.thing_models import (
        ThingEventMessage as ThingEventMessage,
    )
    from mower_sdk.legacy.thing_models import (
        ThingParams as ThingParams,
    )
    from mower_sdk.legacy.thing_models import (
        ThingPropertiesMessage as ThingPropertiesMessage,
    )
    from mower_sdk.legacy.thing_models import (
        ThingStatusMessage as ThingStatusMessage,
    )

# The public surface upstream published from this module, plus the community
# additions CommandReceipt and CommandVerdict and the location channel's
# DeviceLocation, DeviceLocationMessage, VEHICLE_STATE_TO_STATUS and
# mower_time_ms, RejectedMessage, SkippedLocationEntry, STATE_KNOWN_FIELDS and
# REST_STATUS_KNOWN_FIELDS, the payload readers RAW_STATE_TO_CANONICAL,
# canonical_state, mower_status_from_raw and battery_from_payload, and
# MqttConnectionInfo. The four Thing* classes now live
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
    "MqttConnectionInfo",
    "REST_STATUS_KNOWN_FIELDS",
    "RejectedMessage",
    "RAW_STATE_TO_CANONICAL",
    "SkippedLocationEntry",
    "STATE_KNOWN_FIELDS",
    "VEHICLE_STATE_TO_STATUS",
    "battery_from_payload",
    "canonical_state",
    "mower_status_from_raw",
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
# The raw states the cloud sends, as the REST status's status/state/vehicleState
# or the state message's state, mapped to MowerStatus values. Read-only: the
# models read the table behind it.
RAW_STATE_TO_CANONICAL = MappingProxyType(_RAW_STATE_TO_CANONICAL)


def _raw_state(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the raw state under the first key with a truthy value.

    Exactly what ``data.get(k1) or data.get(k2) or data.get(k3)`` returns.
    Each reader passes its own key order, because a payload carrying both keys
    resolves differently per reader and that precedence is kept.

    Args:
        data: The payload to read.
        keys: The keys to try, in order of precedence.

    Returns:
        The first truthy value in key order, or, when none is truthy, whatever
        the last key holds, falsy values such as False or "" included, and
        None when the last key is absent.
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

    Args:
        data: The payload to read.
        keys: The keys to try, in order of precedence.
        default: What to return when none of the keys is present.

    Returns:
        The value under the first key that is present, else default.
    """
    for key in keys:
        if key in data:
            return data[key]
    return default


def canonical_state(raw_state: Any) -> str:
    """Give the canonical state for a raw state, as DeviceStateMessage.state holds it.

    For the MowerStatus a DeviceStatus would hold, use mower_status_from_raw.

    Args:
        raw_state: A raw state as the cloud sent it, or a MowerStatus.

    Returns:
        A raw state in RAW_STATE_TO_CANONICAL gives its value there; a
        MowerStatus gives its value; any other string passes through
        unchanged, so a state the SDK does not know yet stays visible;
        anything else (None, a number) gives "unknown".
    """
    if isinstance(raw_state, MowerStatus):
        return raw_state.value
    if not isinstance(raw_state, str):
        return "unknown"
    return _RAW_STATE_TO_CANONICAL.get(raw_state, raw_state)


def mower_status_from_raw(raw_state: Any) -> "MowerStatus":
    """Give the MowerStatus for a raw state, as DeviceStatus.status holds it.

    Args:
        raw_state: A raw state as the cloud sent it, or a MowerStatus.

    Returns:
        The MowerStatus whose value is canonical_state(raw_state), UNKNOWN for
        a value MowerStatus lacks. So a string the SDK does not know gives
        UNKNOWN, as does anything that is neither a string nor a MowerStatus.
    """
    return _mower_status(canonical_state(raw_state))


# The names these readers had while private, kept for code that reached for them.
_normalize_state_value = canonical_state


def _int(value: Any) -> int | None:
    """Read a value with int(), refusing bools and non-finite floats.

    JSON allows Infinity and NaN. As with int(), a float loses its fraction
    and a string must hold a whole number: "87.5" is not read.

    Args:
        value: The value to read: a number or a numeric string.

    Returns:
        int(value), or None for anything int() cannot read, a bool, a
        non-finite float or a value too large to convert.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def battery_from_payload(data: Any) -> int | None:
    """Read the battery percentage from a REST status or an MQTT state payload.

    The ``capacityRemaining`` entry whose ``unit`` is PERCENTAGE (compared
    upper-cased) comes first, then any ``capacityRemaining`` entry whose
    ``rawValue`` parses, then the plain ``battery`` field. Each value goes
    through ``_int``. One reader for both payload shapes, the one
    DeviceStatus.battery and DeviceStateMessage.battery are read with.

    Args:
        data: The payload as decoded: a REST status entry or a state payload.

    Returns:
        The percentage; out-of-range numbers pass through unchanged. None when
        the payload carries no readable value (both keys missing, an
        unparsable or non-numeric value, a bool, a non-finite float) and for
        data that is not a dict.
    """
    if not isinstance(data, dict):
        return None
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


_extract_battery_value = battery_from_payload


class MowerStatus(Enum):
    """The state of a mower, in the SDK's canonical names.

    DeviceStatus.status holds a member, and DeviceStateMessage.state holds a
    member's value for every raw state in RAW_STATE_TO_CANONICAL. The state
    channel and the REST status never report CHARGING; the location channel's
    pose code does (a docked mower that is charging). MAPPING, UPDATING and
    OFFLINE are the raw states isMapping, inSoftwareUpdate and
    Offline/offline. ERROR is also what the raw state isLifted is read as, and
    IDLE what Self-Checking is. UNKNOWN stands for a state the SDK cannot
    name.

    The raw state stays on the model it was read into: see
    DeviceStateMessage.metrics and DeviceStatus.extra.
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
    """Mower control commands, sent over REST by MowerAPI.async_send_command.

    What the cloud does with each, as observed: START resumes the task the app
    created and cannot choose a zone; PAUSE and RESUME work and settle within
    about 30 seconds; STOP pauses the task rather than ending it; DOCK sends
    the mower home, which can take minutes. A SUCCESS result means the cloud
    accepted the command, not that the mower acted: poll the status for the
    state you want.
    """

    START = "start"  # Start (resume) the task the app created
    PAUSE = "pause"  # Pause mowing
    DOCK = "dock"  # Return to the charging station
    RESUME = "resume"  # Resume mowing
    STOP = "stop"  # Pauses the task; does not end it


class MowerError(Enum):
    """The kind of error a mower reports, as DeviceStatus.error_code holds it.

    NONE means no error. An error code that is none of the members' values is
    read as UNKNOWN.
    """

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

    ACCEPTED means a result said SUCCESS and none said alreadyInState. The
    cloud accepted the command; it does not mean the mower acted. Pause and
    resume settle within about 30 seconds, docking can take minutes; poll the
    status for the target state.

    ALREADY_IN_STATE means a result was an ERROR with errorCode
    alreadyInState, so the mower was already in the requested state. It wins
    over SUCCESS.

    UNKNOWN means no result said either, an empty result list included.
    """

    ACCEPTED = "accepted"
    ALREADY_IN_STATE = "already_in_state"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandReceipt:
    """What the cloud replied to one command, classified.

    Returned by MowerAPI.async_send_command_receipt. No receipt exists for a
    command the cloud refused (MowerAPIError) or that got no usable reply
    (MowerTransportError: a timeout, a connection error, an HTTP 5xx, or a body
    that is not a JSON object; the cloud may still have accepted it).

    Attributes:
        device_id: The device the command was sent to.
        command: The command sent.
        verdict: The cloud's verdict, a CommandVerdict.
        command_number: The reply's command number, when it carried one under
            a recognised key (cmdNum and its spellings); None otherwise. No
            captured reply carries one, and where it would come from is
            undocumented.
        results: The per-command result dicts from the reply, as a tuple;
            empty when the reply has none. Left out of the hash the frozen
            dataclass derives from its fields, since dicts are unhashable;
            equality still compares it.
    """

    device_id: str
    command: MowerCommand
    verdict: CommandVerdict
    command_number: str | None = None
    results: tuple[dict[str, Any], ...] = field(default=(), hash=False)

    @property
    def accepted(self) -> bool:
        """Whether the verdict is ACCEPTED: the cloud accepted the command."""
        return self.verdict is CommandVerdict.ACCEPTED

    @property
    def already_in_state(self) -> bool:
        """Whether the verdict is ALREADY_IN_STATE: the mower was in the requested state."""
        return self.verdict is CommandVerdict.ALREADY_IN_STATE


def _credential(value: Any) -> str | None:
    """Read a broker credential from the credential reply.

    Args:
        value: The reply's value for the credential; None when it has none.

    Returns:
        The value's text when present, None when absent. An empty string is a
        value, and a number (a user name sent as one) is read as its text.
    """
    return None if value is None else str(value)


def _endpoint(value: Any, key: str) -> tuple[str | None, int | None, str | None] | None:
    """Read the host, port and path that the reply's mqttHost or mqttUrl names.

    A value with a scheme is split as a URL; its path and query form the path,
    "/" standing in for an empty path before a query. Without a scheme, mqttHost
    is a host with an optional port, with no path read from it, and mqttUrl is
    a path taken as given, with a leading slash added.

    Args:
        value: The reply's value under key.
        key: "mqttHost" or "mqttUrl": it decides how a value without a scheme
            is read, and the error names it.

    Returns:
        The host, the port and the path, each None when the value does not
        give it; or None when the reply does not name an endpoint under the
        key (the value is not a string, or is blank).

    Raises:
        MowerAPIError: The value has a scheme other than wss, cannot be split
            as a URL, or has a port that is not a number from 0 to 65535. The
            message names the key but not the value, which may carry an
            account id or a token.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" not in text and key == "mqttUrl":
        return None, None, text if text.startswith("/") else f"/{text}"
    failed = f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: the credential reply's {key}"
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        # An unreadable port, or a bracketed host that is not an IP address; the
        # exception's own text would repeat the value.
        raise MowerAPIError(f"{failed} cannot be read as a host and port") from None
    if parsed.scheme.lower() not in ("", "wss"):
        raise MowerAPIError(f"{failed} uses the scheme {parsed.scheme!r}; only wss is supported")
    path = None
    if "://" in text and (parsed.path or parsed.query):
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    return hostname, port, path


def _broker_endpoint(data: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    """Read the broker host, port and WebSocket path a credential reply names.

    The host is mqttHost's, else a full mqttUrl's. The port is a full
    mqttUrl's, else mqttHost's: mqttUrl is the address the connection is made
    to, so its port wins. The path is mqttUrl's (see _endpoint); a path in
    mqttHost is ignored.

    Args:
        data: The credential reply.

    Returns:
        The host, the port and the path; each is None when the reply names
        none.

    Raises:
        MowerAPIError: mqttHost or mqttUrl cannot be read (see _endpoint).
    """
    host_host, host_port, _ = _endpoint(data.get("mqttHost"), "mqttHost") or (None, None, None)
    url_host, url_port, url_path = _endpoint(data.get("mqttUrl"), "mqttUrl") or (None, None, None)
    return host_host or url_host, url_port if url_port is not None else host_port, url_path


@dataclass(frozen=True)
class MqttConnectionInfo:
    """What the cloud's MQTT credential reply says about the broker, read once for every consumer.

    MowerAPI.async_get_mqtt_connection_info returns one, and
    NavimowSDK.from_connection_info builds the facade from it. The reply of
    /openapi/mqtt/userInfo/get/v2 has been seen with mqttHost as a wss:// URL
    and mqttUrl as a path (/mqtt/{userId}); from_dict also reads mqttHost
    without a scheme or with a port, and mqttUrl as a full wss:// URL.

    Attributes:
        broker: The broker's host name, never a URL: mqttHost's, else a full
            mqttUrl's.
        port: The port: a full mqttUrl's, else mqttHost's, else 443.
        ws_path: The WebSocket path, with the query a full mqttUrl carried; ""
            when the reply names none.
        username: userName, as text, or None when the reply has none.
        password: pwdInfo, as text, or None when the reply has none; left out
            of repr.
        raw: The reply as received (a copy), left out of repr and equality.
    """

    broker: str
    port: int
    ws_path: str
    username: str | None
    password: str | None = field(default=None, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MqttConnectionInfo":
        """Read a credential reply.

        Args:
            data: The reply's data, as MowerAPI.async_get_mqtt_user_info
                returns it.

        Returns:
            The connection info, with the port 443 and the path "" where the
            reply names none.

        Raises:
            MowerAPIError: The reply is not an object or names no broker (neither
                mqttHost nor a full mqttUrl), or names one that cannot be read: a
                scheme other than wss, a host and port that cannot be split, or a
                port that is not a number from 0 to 65535.
        """
        if not isinstance(data, dict):
            raise MowerAPIError(
                f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: the credential reply is not an object"
            )
        host, port, ws_path = _broker_endpoint(data)
        if host is None:
            raise MowerAPIError(
                f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: "
                "no broker (mqttHost) in the credential reply"
            )
        return cls(
            broker=host,
            port=443 if port is None else port,
            ws_path=ws_path or "",
            username=_credential(data.get("userName")),
            password=_credential(data.get("pwdInfo")),
            raw=dict(data),
        )


@dataclass
class Device:
    """Device information.

    MowerAPI.async_get_devices reads each entry of the cloud's device list
    into one with from_dict, which says which key fills which field.

    Attributes:
        id: Device ID.
        name: Device name.
        model: Device model.
        firmware_version: Firmware version.
        serial_number: Serial number.
        mac_address: MAC address; None without one.
        online: Whether the device is online.
        extra: Extra information; None without any. from_dict fills it from
            the entry's own extra key, not from the keys it does not read.
        product_key: The entry's productKey or product_key; None without one.
        device_name: The entry's deviceName or device_name, else its name;
            None without any of them.
        iot_id: The entry's iotId or iot_id, else its id; None without any of
            them.
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
        ``extra`` is the value under ``extra``; no other key is kept.

        Args:
            data: Dictionary holding the device information: an entry of the
                device list, or to_dict()'s output.

        Returns:
            A Device instance. Where the entry has no key for a field, id,
            name, model, firmware_version and serial_number are "", online is
            False and the other fields are None.
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
        """Convert to a dictionary keyed by the field names.

        Returns:
            Dictionary holding the device information: id, name, model,
            firmware_version, serial_number and online always; mac_address,
            extra, product_key, device_name and iot_id only when they hold a
            truthy value.
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
STATE_KNOWN_FIELDS = frozenset(
    {
        "state",
        "vehicleState",
        "status",
        "battery",
        "capacityRemaining",
        "timestamp",
        "device_id",
    }
)
REST_STATUS_KNOWN_FIELDS = frozenset(
    {
        "id",
        "device_id",
        "deviceId",
        "vehicleState",
        "capacityRemaining",
        "descriptiveCapacityRemaining",
        "battery",
    }
)

# The payload keys DeviceStatus.from_dict reads into a field; every other key is
# kept in extra.
_DEVICE_STATUS_READ_KEYS = frozenset(
    {
        "status",
        "state",
        "vehicleState",
        "error_code",
        "capacityRemaining",
        "battery",
        "descriptiveCapacityRemaining",
        "extra",
        "device_id",
        "id",
        "position",
        "error_message",
        "mowing_time",
        "total_mowing_time",
        "signal_strength",
        "timestamp",
    }
)


def _mower_status(value: Any) -> MowerStatus:
    """Read a canonical state as a MowerStatus.

    Args:
        value: A MowerStatus value such as "mowing", or anything else.

    Returns:
        MowerStatus(value), UNKNOWN for a value the enum lacks.
    """
    try:
        return MowerStatus(value)
    except ValueError:
        return MowerStatus.UNKNOWN


def _mower_error(value: Any) -> MowerError:
    """Read an error code as a MowerError.

    Args:
        value: A MowerError value such as "lifted", or anything else.

    Returns:
        MowerError(value), UNKNOWN for a value the enum lacks.
    """
    try:
        return MowerError(value)
    except ValueError:
        return MowerError.UNKNOWN


@dataclass
class DeviceStatus:
    """Device status.

    MowerAPI.async_get_device_statuses reads each entry of the cloud's REST
    status reply into one with from_dict, and from_state_message builds one
    from an MQTT state message. REST_STATUS_KNOWN_FIELDS names the keys a REST
    entry has been seen to carry.

    Attributes:
        device_id: Device ID.
        status: Device status, a MowerStatus; UNKNOWN for a raw state the SDK
            does not know.
        battery: Battery level in percent, or None when the payload carried no
            readable value; out-of-range numbers pass through.
        position: Position, as {"lat": float, "lng": float}; None without one.
        error_code: Error code, a MowerError; NONE without an error, UNKNOWN
            for a code the enum lacks.
        error_message: Error message; None without one.
        mowing_time: Duration of the current mowing session in seconds; None
            without one.
        total_mowing_time: Total mowing time in seconds; None without one.
        signal_strength: Signal strength; None without one.
        timestamp: Timestamp of the status update; None without one.
            from_dict keeps the payload's value as it is, and
            from_state_message stores epoch milliseconds; mower_time_ms reads
            either as epoch milliseconds.
        extra: Extra information; None without any. from_dict puts here what
            the payload carries beyond the fields above, its vehicleState key
            included, and from_state_message the message's raw state, also
            under "vehicleState".
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

        status is the first truthy value of ``status``, ``state`` and
        ``vehicleState``, read with mower_status_from_raw. battery is read
        with battery_from_payload. device_id is ``device_id``, else ``id``,
        else "". error_code is ``error_code`` read as a MowerError: NONE when
        the key is absent, UNKNOWN for a code the enum lacks. position,
        error_message, mowing_time, total_mowing_time, signal_strength and
        timestamp are the values under those keys as given, None when absent.

        extra is a new dict, or None when it would be empty: the entries of
        the payload's own ``extra`` dict, every payload key that no field
        reads, and the raw ``vehicleState``, ``capacityRemaining`` and
        ``descriptiveCapacityRemaining`` when the payload has those keys. The
        caller's dicts are never written to.

        Args:
            data: Dictionary holding the device status: an entry of the REST
                status reply, or to_dict()'s output.

        Returns:
            A DeviceStatus instance.
        """
        status_source = _raw_state(data, ("status", "state", "vehicleState"))
        status = mower_status_from_raw(status_source)
        error_code = _mower_error(data.get("error_code", "none"))

        battery = battery_from_payload(data)

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
            extra["descriptiveCapacityRemaining"] = data.get("descriptiveCapacityRemaining")
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
        """Convert to a dictionary keyed by the field names.

        Returns:
            Dictionary holding the device status: device_id, status and
            error_code (the enums as their values) and battery always, a None
            battery included; position, error_message and extra only when they
            hold a truthy value; mowing_time, total_mowing_time,
            signal_strength and timestamp only when they are not None.
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

    @classmethod
    def from_state_message(
        cls,
        message: "DeviceStateMessage",
        fallback_status: MowerStatus | None = None,
        fallback_battery: int | None = None,
    ) -> "DeviceStatus":
        """Build a DeviceStatus from an MQTT state message; the message is not changed.

        status is MowerStatus(message.state), UNKNOWN for a value the enum lacks.
        The state channel sends partial messages, so the fallbacks (a consumer's
        last known values, say) apply only to what the message does not carry.
        A message carries no state when its raw payload has no state, status or
        vehicleState value, or, for a message built by hand, when its state is
        "unknown". An explicit "unknown" in a payload is kept, and so is the
        UNKNOWN of a state the enum lacks.

        timestamp is the message's in milliseconds (mower_time_ms). The error
        dict's code (under ``code``, else ``error_code``) and message become
        error_code (UNKNOWN for a code the enum lacks, NONE without a code) and
        error_message. position and signal_strength are carried over, and the
        raw state kept in metrics["raw_state"] becomes extra["vehicleState"].
        mowing_time and total_mowing_time have no source in a state message
        and are None.

        Args:
            message: The state message.
            fallback_status: The status to use when the message carries no
                state; None for no fallback.
            fallback_battery: The battery level to use when the message carries
                no readable battery; None for no fallback, which leaves battery
                None.

        Returns:
            A new DeviceStatus.
        """
        status = _mower_status(message.state)
        if message.raw is not None:
            carries_state = any(
                message.raw.get(key) is not None for key in ("state", "status", "vehicleState")
            )
        else:
            carries_state = message.state != MowerStatus.UNKNOWN.value
        if not carries_state and fallback_status is not None:
            status = fallback_status
        battery = message.battery if message.battery is not None else fallback_battery

        error_code, error_message = MowerError.NONE, None
        if isinstance(message.error, dict):
            code = message.error.get("code") or message.error.get("error_code")
            error_message = message.error.get("message")
            if code:
                error_code = _mower_error(code)
        raw_state = (message.metrics or {}).get("raw_state")
        return cls(
            device_id=message.device_id,
            status=status,
            battery=battery,
            position=message.position,
            error_code=error_code,
            error_message=error_message,
            signal_strength=message.signal_strength,
            timestamp=mower_time_ms(message.timestamp),
            extra={"vehicleState": raw_state} if raw_state is not None else None,
        )


@dataclass
class DeviceStateMessage:
    """Unified state message from MQTT.

    NavimowSDK reads each payload of the state channel into one with from_dict
    and hands those it applies to the on_state callbacks; from_status builds
    one from a REST DeviceStatus. The state channel sends partial messages, so
    an optional field the payload does not carry is None; device_id, state,
    metrics and raw are filled as their entries under Attributes say.
    STATE_KNOWN_FIELDS names the
    keys a state payload has been seen to carry: signal_strength, position,
    error and metrics are read when present, and no mower has been seen to
    send them.

    Attributes:
        device_id: The device the message is from. NavimowSDK takes it from
            the topic when the payload names none; from_dict alone gives ""
            for a payload without one.
        timestamp: The mower's time for the state, as the payload gave it, in
            seconds or milliseconds (mower_time_ms gives epoch milliseconds);
            None without one.
        state: The canonical state (see canonical_state): a MowerStatus value
            for a raw state the SDK knows, any other string as it was sent,
            and "unknown" when the payload names no state.
        battery: Battery level in percent, read with battery_from_payload;
            None when the payload carried no readable value.
        signal_strength: The payload's signal_strength; None without one.
        position: The payload's position; None without one.
        error: The payload's error; None without one. from_status writes it
            as {"code", "message"}, and DeviceStatus.from_state_message reads
            a dict with those keys.
        metrics: A copy of the payload's metrics, with the raw state under
            "raw_state" when normalisation changed it; None when there is
            neither.
        raw: The payload as decoded, for a message made by from_dict; None for
            one built by hand. Not compared, not in repr and not in to_dict().
        received_at: When NavimowSDK received the message (UTC), set by
            NavimowSDK; None for a message built by hand or by from_dict
            directly, and as given for one built by from_status. Not compared
            and not in to_dict().
        original: The payload's bytes exactly as the mower sent them (raw is
            decoded, with device_id added), set by NavimowSDK; None for a
            message built by hand or by from_dict directly. Not compared, not
            in repr and not in to_dict().
    """

    device_id: str
    timestamp: int | None
    state: str
    battery: int | None = None
    signal_strength: int | None = None
    position: dict[str, float] | None = None
    error: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    received_at: datetime | None = field(default=None, compare=False)
    original: bytes | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceStateMessage":
        """Read a state payload.

        The raw state is the first truthy value of ``state``, ``status`` and
        ``vehicleState``, and state its canonical form (canonical_state).
        battery is read with battery_from_payload. device_id is "" when the
        payload has none; timestamp, signal_strength, position and error are
        the values under those keys as given, None when absent. metrics is a
        copy of the payload's, so the raw state added to it never reaches the
        caller's dict: it goes under "raw_state" when it is not None and the
        canonical state differs from it. raw is a copy of the payload with its
        own copy of a metrics dict. received_at and original stay None.

        Args:
            payload: The state payload, as decoded.

        Returns:
            The message.

        Raises:
            TypeError: metrics holds something that cannot be read as a dict,
                such as a number.
            ValueError: The same, for a value dict() refuses with a
                ValueError, such as a string.
        """
        raw_state = _raw_state(payload, ("state", "status", "vehicleState"))
        normalized_state = canonical_state(raw_state)
        # raw is the payload as decoded, with its own copy of a metrics dict.
        raw = dict(payload)
        if isinstance(payload.get("metrics"), dict):
            raw["metrics"] = dict(payload["metrics"])
        # A copy, so the raw_state added below never reaches the caller's dict.
        metrics = dict(payload.get("metrics") or {})
        if raw_state is not None and normalized_state != raw_state:
            metrics["raw_state"] = raw_state

        return cls(
            device_id=payload.get("device_id", ""),
            timestamp=payload.get("timestamp"),
            state=normalized_state,
            battery=battery_from_payload(payload),
            signal_strength=payload.get("signal_strength"),
            position=payload.get("position"),
            error=payload.get("error"),
            metrics=metrics or None,
            raw=raw,
        )

    @classmethod
    def from_status(
        cls, status: "DeviceStatus", received_at: datetime | None = None
    ) -> "DeviceStateMessage":
        """Build a state message from a REST DeviceStatus; the status is not changed.

        state is the status's enum value; battery, signal_strength and position
        are carried over; a non-NONE error code becomes {"code", "message"};
        timestamp is the status's in milliseconds (mower_time_ms, None if absent);
        metrics, raw and original are None. mowing_time, total_mowing_time and
        extra have no field on a state message and are not carried.

        Args:
            status: The status to read.
            received_at: The receipt time to give the message; None for none.

        Returns:
            A new DeviceStateMessage.
        """
        error = None
        if status.error_code is not MowerError.NONE:
            error = {"code": status.error_code.value, "message": status.error_message}
        return cls(
            device_id=status.device_id,
            timestamp=mower_time_ms(status.timestamp),
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            received_at=received_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary keyed by the field names.

        Returns:
            device_id, timestamp, state, battery, signal_strength, position,
            error and metrics, each always present, None included. raw,
            received_at and original are left out.
        """
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
    """Unified event message from MQTT.

    NavimowSDK reads each JSON object payload of the event channel into one
    with from_dict and hands it to the on_event callbacks.

    Attributes:
        device_id: The device the message is from. NavimowSDK takes it from
            the topic when the payload names none; from_dict alone gives ""
            for a payload without one.
        timestamp: The payload's timestamp, as given; None without one.
        type: The payload's type; "system" when it names none.
        event: The payload's event; "" when it names none.
        level: The payload's level; None without one.
        message: The payload's message; None without one.
        params: The payload's params; None without any.
        raw: The payload as decoded, for a message made by from_dict; None for
            one built by hand. Not compared, not in repr and not in to_dict().
        received_at: When NavimowSDK received the message (UTC), set by
            NavimowSDK; None for a message built by hand or by from_dict
            directly. Not compared and not in to_dict().
        original: The payload's bytes exactly as the mower sent them (raw is
            decoded, with device_id added), set by NavimowSDK; None for a
            message built by hand or by from_dict directly. Not compared, not
            in repr and not in to_dict().
    """

    device_id: str
    timestamp: int | None
    type: str
    event: str
    level: str | None = None
    message: str | None = None
    params: dict[str, Any] | None = None
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    received_at: datetime | None = field(default=None, compare=False)
    original: bytes | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceEventMessage":
        """Read an event payload.

        Each field is the value under the key of its name, as given. raw is a
        copy of the payload; received_at and original stay None.

        Args:
            payload: The event payload, as decoded.

        Returns:
            The message. Where the payload has no key for a field, device_id
            and event are "", type is "system" and the other fields are None.
        """
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
        """Convert to a dictionary keyed by the field names.

        Returns:
            device_id, timestamp, type, event, level, message and params, each
            always present, None included. raw, received_at and original are
            left out.
        """
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
    """Unified attributes message from MQTT.

    NavimowSDK reads each JSON object payload of the attributes channel into
    one with from_dict, caches the latest for each device and hands it to the
    on_attributes callbacks.

    Attributes:
        device_id: The device the message is from. NavimowSDK takes it from
            the topic when the payload names none; from_dict alone gives ""
            for a payload without one.
        attributes: The payload's attributes; empty when it has none.
        raw: The payload as decoded, for a message made by from_dict; None for
            one built by hand. Not compared, not in repr and not in to_dict().
        received_at: When NavimowSDK received the message (UTC), set by
            NavimowSDK; None for a message built by hand or by from_dict
            directly. Not compared and not in to_dict().
        original: The payload's bytes exactly as the mower sent them (raw is
            decoded, with device_id added), set by NavimowSDK; None for a
            message built by hand or by from_dict directly. Not compared, not
            in repr and not in to_dict().
    """

    device_id: str
    attributes: dict[str, Any]
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    received_at: datetime | None = field(default=None, compare=False)
    original: bytes | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceAttributesMessage":
        """Read an attributes payload.

        raw is a copy of the payload; received_at and original stay None.

        Args:
            payload: The attributes payload, as decoded.

        Returns:
            The message: device_id is the payload's, "" when it has none, and
            attributes the value under ``attributes``, an empty dict when the
            key is absent or its value is falsy.
        """
        return cls(
            device_id=payload.get("device_id", ""),
            attributes=payload.get("attributes", {}) or {},
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary keyed by the field names.

        Returns:
            device_id and attributes. raw, received_at and original are left
            out.
        """
        return {
            "device_id": self.device_id,
            "attributes": self.attributes,
        }


@dataclass
class DeviceCommandMessage:
    """Unified command message for MQTT publish.

    NavimowSDK builds one for each experimental MQTT command and publishes its
    to_dict() to the device's command topic, which the broker accepts and no
    mower has been seen to act on.

    Attributes:
        id: The command's id; NavimowSDK gives each "cmd-" and a random UUID.
        device_id: The device the command is for.
        command: The command's name, such as "start_mowing" or "pause".
        params: The command's parameters; None for none.
    """

    id: str
    device_id: str
    command: str
    params: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceCommandMessage":
        """Read a command payload, such as to_dict()'s output.

        Args:
            payload: The command payload, as decoded.

        Returns:
            The message. Where the payload has no key for a field, id,
            device_id and command are "" and params is None.
        """
        return cls(
            id=payload.get("id", ""),
            device_id=payload.get("device_id", ""),
            command=payload.get("command", ""),
            params=payload.get("params"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to the dictionary that is published.

        Returns:
            id, device_id, command and params, with params an empty dict when
            the message has none.
        """
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
    """Read a vendor number (often a string such as "100.00") as a float.

    Args:
        value: The value to read: a number or a numeric string.

    Returns:
        The float; None for a bool, a non-finite value or anything float()
        cannot read.
    """
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _whole(value: Any) -> int | None:
    """Read a vendor integer (an int, a float or a numeric string) as an int.

    Args:
        value: The value to read.

    Returns:
        The number _number reads, with any fraction dropped; None when it
        reads none.
    """
    number = _number(value)
    return None if number is None else int(number)


def mower_time_ms(value: Any) -> int | None:
    """Give a mower timestamp as epoch milliseconds, whether sent in seconds or milliseconds.

    A value above 100,000,000,000 is read as milliseconds, one at or below it
    as seconds.

    Args:
        value: The timestamp as the mower sent it; numbers may arrive as
            strings.

    Returns:
        The time in epoch milliseconds; None when the value is absent,
        unreadable, or not positive.
    """
    number = _whole(value)
    if number is None or number <= 0:
        return None
    return number if number > _MILLISECONDS_ABOVE else number * 1000


def _status_of(vehicle_state: int | None) -> MowerStatus | None:
    """Give the MowerStatus a pose code stands for.

    Args:
        vehicle_state: A pose entry's vehicleState code; None when the pose
            carried none.

    Returns:
        The code's status in VEHICLE_STATE_TO_STATUS, UNKNOWN for a code not
        in the table, and None for None.
    """
    if vehicle_state is None:
        return None
    return VEHICLE_STATE_TO_STATUS.get(vehicle_state, MowerStatus.UNKNOWN)


def _iso(value: datetime | None) -> str | None:
    """Give a time as an ISO 8601 string.

    Args:
        value: The time, or None.

    Returns:
        value.isoformat(), or None for None.
    """
    return None if value is None else value.isoformat()


def _from_iso(value: Any) -> datetime | None:
    """Read a time from an ISO 8601 string.

    Args:
        value: The string, as _iso wrote it, or a datetime.

    Returns:
        A datetime as it is and a string as datetime.fromisoformat reads it;
        None for a string it cannot read and for any other value.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_LOCATION_WHOLE_FIELDS = (
    "vehicle_state",
    "pose_at",
    "current_zone",
    "zone_at",
    "route_progress",
    "progress_at",
    "action",
    "sub_action",
    "mow_start_type",
    "task_at",
    "target_at",
    "target_last_at",
    "dock_at",
)
_LOCATION_NUMBER_FIELDS = (
    "x",
    "y",
    "theta",
    "mowing_percentage",
    "area_m2",
    "week_area_m2",
    "dock_x",
    "dock_y",
    "dock_theta",
)


@dataclass(frozen=True)
class DeviceLocation:
    """The location channel's merged record for one device, as it stands.

    Built by merging the channel's entries one by one (mower_sdk.location): each
    entry type updates its own fields and leaves the others as they were. A
    field other than device_id, dock_samples and marks is None until an entry
    has filled it. Times ending in ``_at`` without ``received`` are mower times
    in epoch milliseconds, None when the entry was sent without a time;
    ``pose_received_at`` and ``delay_received_at`` are the UTC receipt times.

    The dock fields are learned from pose entries whose code says docked or
    charging (see LocationDecoder). The estimate is a capped mean: once the cap
    (200 poses by default) is reached each docked pose moves it by one over the
    cap of the way, so a dock moved by less than the move distance (1 m by
    default) is followed only slowly, about 63 % of the way after 200 further
    docked poses, roughly seventeen hours docked at one pose per five minutes.
    A dock moved farther than that is picked up after a few docked poses in a
    row that agree, each within the move distance of their running mean (three
    by default), and dock_samples then starts again from that count.

    Attributes:
        device_id: The device the record is of.
        x: Pose (type 1 entries): the position's x in metres on the lawn's
            local grid (origin near the dock or RTK reference, not latitude
            and longitude). A pose is replaced whole.
        y: Pose: the position's y in metres on the same grid.
        theta: Pose: the heading in radians; None when the latest pose entry
            carried no heading.
        vehicle_state: Pose: the pose code (see status and
            VEHICLE_STATE_TO_STATUS); None when the latest pose entry carried
            none, and a lifted mower sends none.
        pose_at: Pose: the latest pose entry's mower time; None when it was
            sent without one.
        pose_received_at: Pose: when the latest pose entry was received (UTC).
        current_zone: Task (type 2 entries): the partition the mower is in now
            (it changes once the mower has crossed into the next one), kept
            when a newer task entry omits it.
        zone_at: Task: the mower time of the entry current_zone came from.
        route_progress: Task: the last route reading seen (0 to 10000, 10000
            at the end of the route), kept when a newer task entry omits it.
        progress_at: Task: the mower time of the entry route_progress came
            from.
        mowing_percentage: Task: the latest task entry's mowingPercentage.
            This and the fields down to task_at are replaced whole by each
            task entry, so one the latest entry did not carry is None.
        area_m2: Task: the latest task entry's subtotalArea, in square metres.
        week_area_m2: Task: the latest task entry's mowingWeekArea, in square
            metres.
        action: Task: the latest task entry's action, a code.
        sub_action: Task: the latest task entry's subAction, a code.
        mow_start_type: Task: the latest task entry's mowStartType, a code.
        map_work_position: Task: the latest task entry's mapWorkPosition, kept
            as the 128-hex-digit string the mower sends: it holds sixteen
            32-bit words whose order two readings of captures disagree on, so
            it is not decoded.
        task_at: Task: the latest task entry's mower time; None when it was
            sent without one.
        partition_ids: Target (type 3 entries): the targeted partitions. None
            until a target report has arrived and empty for a report with no
            active target (a mow-all task sends the same empty report as an
            idle mower; mower_sdk.location.target_zone reads it with the
            mower's state).
        target_at: Target: when this set was first reported, as a mower time.
        target_last_at: Target: the mower time of the set's latest repeat.
        task_delay: Delay (type 4 entries): the latest delay entry's taskDelay
            (a rain or schedule delay).
        delay_received_at: Delay: when the latest delay entry was received
            (UTC); the entry carries no time of its own.
        dock_x: Dock: the estimated dock position's x, on the same grid as the
            pose; None until the first pose whose code says docked or
            charging.
        dock_y: Dock: the estimated dock position's y, as dock_x.
        dock_theta: Dock: the heading of the latest pose in the estimate (the
            previous one's when a pose omits it; after a detected move, the
            latest heading among the poses that showed it, never the old
            dock's).
        dock_at: Dock: that pose's mower time, None when it was sent without
            one.
        dock_samples: Dock: how many poses the estimate holds, capped (200 by
            default), 0 without an estimate.
        marks: The high-water marks: the entry types 1, 2 and 3 mapped to the
            mower time of the newest entry of that type applied, at or below
            which a later entry is stale. Kept apart from the observation
            times because an entry without a time leaves the time None while
            the mark must stand. to_dict() and from_dict() carry it, so a
            record persisted and handed back after a restart keeps rejecting
            late entries. Left out of the hash, since a dict is unhashable;
            equality compares it.
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
    dock_x: float | None = None
    dock_y: float | None = None
    dock_theta: float | None = None
    dock_at: int | None = None
    dock_samples: int = 0
    marks: dict[int, int] = field(default_factory=dict, hash=False)

    @property
    def status(self) -> MowerStatus | None:
        """The pose code as a MowerStatus; None without one, UNKNOWN for a code not in the table."""
        return _status_of(self.vehicle_state)

    @property
    def progress_percent(self) -> float | None:
        """The mowing progress as a percentage, or None.

        The route reading when there is one, else the latest task entry's
        mowingPercentage.
        """
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
        """Give the record as JSON-ready values.

        Returns:
            Every field under its own name. The two receipt times are ISO 8601
            strings, the mower times stay integers, partition_ids is a list
            and marks is keyed by the entry type as a string. A None stays
            None.
        """
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
        """Rebuild the record from to_dict()'s output.

        A number may be a numeric string, and a receipt time an ISO 8601
        string or a datetime. Keys that name no field are ignored.

        Args:
            data: The record as to_dict() gave it.

        Returns:
            The record. Absent or unreadable values are None, absent marks
            empty. partition_ids keeps the ids that can be read from a list or
            tuple, and marks the entries whose type and time can be read.
            dock_samples is 0 when absent, unreadable or negative, and
            device_id "" when absent.
        """
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
        partition_ids = data.get("partition_ids")
        values["partition_ids"] = (
            tuple(pid for pid in (_whole(v) for v in partition_ids) if pid is not None)
            if isinstance(partition_ids, list | tuple)
            else None
        )
        marks = data.get("marks")
        values["marks"] = {
            entry_type: mark
            for entry_type, mark in (
                (_whole(key), _whole(value))
                for key, value in (marks.items() if isinstance(marks, dict) else ())
            )
            if entry_type is not None and mark is not None
        }
        samples = _whole(data.get("dock_samples"))
        values["dock_samples"] = samples if samples is not None and samples > 0 else 0
        values["device_id"] = str(data.get("device_id") or "")
        return cls(**values)


@dataclass(frozen=True)
class DeviceLocationMessage:
    """One entry of a location message, decoded, with the merged record as it stood after it.

    LocationDecoder makes one for each entry it applies, and NavimowSDK hands
    them to the on_location callbacks. The fields from x to task_delay are the
    entry's own, None for those it does not carry; DeviceLocation says what
    each means.

    Attributes:
        device_id: The device the entry is from.
        entry_type: 1 (pose), 2 (task), 3 (target) or 4 (delay).
        timestamp: The entry's mower time in epoch milliseconds; None for a
            delay entry, or an entry sent without a time.
        received_at: The UTC receipt time.
        location: The DeviceLocation after this entry was applied, so a
            consumer acting per entry sees the record at that point, not only
            after the message.
        x: A pose entry's x.
        y: A pose entry's y.
        theta: A pose entry's heading; None when it carried none.
        vehicle_state: A pose entry's code; None when it carried none.
        current_zone: A task entry's partition; None when the entry did not
            send its key, which leaves the record's value as it was.
        route_progress: A task entry's route reading; None when the entry did
            not send its key, which leaves the record's value as it was.
        mowing_percentage: A task entry's mowingPercentage.
        area_m2: A task entry's subtotalArea.
        week_area_m2: A task entry's mowingWeekArea.
        action: A task entry's action.
        sub_action: A task entry's subAction.
        mow_start_type: A task entry's mowStartType.
        map_work_position: A task entry's mapWorkPosition.
        partition_ids: A target entry's partitions, empty when it names none;
            None for the other entry types.
        task_delay: A delay entry's taskDelay; None when it is not a boolean.
        raw: The entry as decoded. Not compared and not in repr.
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
class SkippedLocationEntry:
    """One entry of a location message that was not applied, decoded as far as it goes.

    Nothing in it reached the record, so it carries no DeviceLocation; a
    consumer that keeps a history can store it marked as skipped. The fields
    from x to task_delay are the entry's own, read the same way as
    DeviceLocationMessage's and None for those it does not carry (all of them
    for an unknown type).

    Attributes:
        device_id: The device the entry is from.
        entry_type: The type as sent when it is an integer, else None.
        timestamp: The entry's time as read, which for implausible_time may be
            zero, negative or far off; None when the entry has none or it
            cannot be read.
        reason: Why it was skipped: stale (at or below the newest applied time
            of its type), implausible_time, placeholder (an all-zero pose: x
            and y zero, the heading zero, missing or unreadable), unparsable
            (a pose whose x or y cannot be read) or unknown_type.
        received_at: The UTC receipt time.
        x: A pose entry's x; None when it cannot be read.
        y: A pose entry's y; None when it cannot be read.
        theta: A pose entry's heading.
        vehicle_state: A pose entry's code.
        current_zone: A task entry's partition.
        route_progress: A task entry's route reading.
        mowing_percentage: A task entry's mowingPercentage.
        area_m2: A task entry's subtotalArea.
        week_area_m2: A task entry's mowingWeekArea.
        action: A task entry's action.
        sub_action: A task entry's subAction.
        mow_start_type: A task entry's mowStartType.
        map_work_position: A task entry's mapWorkPosition.
        partition_ids: A target entry's partitions.
        task_delay: A delay entry's taskDelay.
        raw: The entry as decoded. Not compared and not in repr.
    """

    device_id: str
    entry_type: int | None
    timestamp: int | None
    reason: str
    received_at: datetime | None
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

    NavimowSDK hands one to the on_rejected callbacks. Recording the payload
    is the consumer's.

    Attributes:
        channel: The channel the message came on: state, event, attributes or
            location.
        topic: The topic the message came on.
        device_id: The device the message is from.
        reason: The deciding one of reasons.
        reasons: Every reason the message earned, of unparsable,
            implausible_time, unknown_type, unknown_field, stale and
            placeholder.
        payload: The bytes the facade received: the wire bytes for anything
            but a JSON object (an array, say), and for an object the MQTT
            client's re-encoded form with device_id added.
        received_at: The UTC receipt time.
        skipped: For a location message, each entry that was not applied, in
            the order the decoder met them; empty for the other channels, and
            for a location message that was unparsable as a whole.
        original: The bytes exactly as the mower sent them: the same as
            payload except for an object the MQTT client re-encoded. Left out
            of equality and of repr. NavimowSDK always sets it; None is for a
            message built by hand.
    """

    channel: str
    topic: str
    device_id: str
    reason: str
    reasons: tuple[str, ...]
    payload: bytes
    received_at: datetime
    skipped: tuple[SkippedLocationEntry, ...] = ()
    original: bytes | None = field(default=None, compare=False, repr=False)


# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
_LEGACY_NAMES = {
    "ThingParams": ("thing_models", "ThingParams"),
    "ThingStatusMessage": ("thing_models", "ThingStatusMessage"),
    "ThingPropertiesMessage": ("thing_models", "ThingPropertiesMessage"),
    "ThingEventMessage": ("thing_models", "ThingEventMessage"),
}


def __getattr__(name: str) -> Any:
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module.

    The warning is warn_legacy's DeprecationWarning, given once per process
    for the legacy module, whichever of its names is asked for first. The name
    is then stored in this module's globals, so a later access does not come
    here.

    Args:
        name: The attribute asked of the module.

    Returns:
        The class of that name in mower_sdk.legacy.thing_models.

    Raises:
        AttributeError: name is not one of the moved names.
        DeprecationWarning: A warnings filter turns the warning into an error
            (see warn_legacy); nothing is imported or stored then.
    """
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value

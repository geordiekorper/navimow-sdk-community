"""The mower's state as one object, from either transport.

The SDK observes a mower in two ways. The MQTT state channel gives a
DeviceStateMessage for each state message NavimowSDK applies, and a REST
getVehicleStatus reply gives a DeviceStatus per device. The location channel
adds the DeviceLocation record that LocationDecoder merges entry by entry.
MowerState joins the two: a status half built from one of the two
observations, which StateSource names, and the location record as given, with
the target zone read from both halves and the state's age on the monotonic
clock. The client that builds these states from both transports and delivers
each new one follows in this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from mower_sdk.location import TargetZone
from mower_sdk.location import target_zone as _target_zone
from mower_sdk.models import (
    DeviceLocation,
    DeviceStateMessage,
    DeviceStatus,
    MowerError,
    MowerStatus,
    _mower_error,
    _mower_status,
    mower_time_ms,
)

__all__ = ["MowerState", "StateSource"]


class StateSource(StrEnum):
    """Which transport a MowerState's status half came from.

    MQTT is a state message NavimowSDK applied; REST an entry of a
    getVehicleStatus reply.
    """

    MQTT = "mqtt"  # the status half comes from a state message the facade applied
    REST = "rest"  # the status half comes from an entry of a getVehicleStatus reply


def _message_raw_state(message: DeviceStateMessage) -> str | None:
    """The raw state a state message carries, as text.

    Args:
        message: The state message.

    Returns:
        metrics["raw_state"] as text when DeviceStateMessage.from_dict kept
        the raw value there because normalisation changed it; else
        message.state when the payload named a state (its raw payload has a
        non-null state, status or vehicleState, or, for a message built by
        hand, its state is not "unknown"); else None.
    """
    kept = (message.metrics or {}).get("raw_state")
    if kept is not None:
        return str(kept)
    if message.raw is not None:
        named = any(message.raw.get(key) is not None for key in ("state", "status", "vehicleState"))
    else:
        named = message.state != MowerStatus.UNKNOWN.value
    return message.state if named else None


@dataclass(frozen=True)
class MowerState:
    """The state of one mower: what it is doing and where it is, as one object.

    The status half (status, raw_state, battery, error_code, error_message,
    observed_at and received_at) is built from one observation, a state
    message with from_state_message or a REST status with from_status, and
    source says which. The location half is the DeviceLocation record as the
    builder's caller gave it. Equality compares the status half and the
    location and ignores received_monotonic, message and rest_status, which
    repr leaves out too.

    Attributes:
        device_id: The device the state is of.
        status: The mower's state in the SDK's canonical names; UNKNOWN for a
            raw state the SDK cannot name.
        raw_state: The state as the cloud sent it, as text; None when the
            source named none.
        battery: Battery level in percent; None when the source carried no
            readable value.
        error_code: The error the mower reports; NONE without one, UNKNOWN
            for a code the enum lacks.
        error_message: The error's message; None without one.
        source: Which transport the status half came from.
        observed_at: The mower's time for the status half, in epoch
            milliseconds (mower_time_ms); None when the source carried none.
        received_at: When the status half arrived (UTC): the state message's
            receipt, or when the REST reply was read.
        location: The location record; None before any location entry or
            restore.
        received_monotonic: time.monotonic() at received_at, what age()
            measures from. Not compared and not in repr.
        message: The state message the status half was built from; None for
            a state built from a REST status. Not compared and not in repr.
        rest_status: The REST status the status half was built from; None for
            a state built from a state message. Not compared and not in repr.
    """

    device_id: str
    status: MowerStatus
    raw_state: str | None
    battery: int | None
    error_code: MowerError
    error_message: str | None
    source: StateSource
    observed_at: int | None
    received_at: datetime
    location: DeviceLocation | None
    received_monotonic: float = field(compare=False, repr=False)
    message: DeviceStateMessage | None = field(default=None, compare=False, repr=False)
    rest_status: DeviceStatus | None = field(default=None, compare=False, repr=False)

    @property
    def target_zone(self) -> int | TargetZone | None:
        """The zone the mower is targeting: the location record read with the status.

        What mower_sdk.location.target_zone gives for location and status:
        None before any target report, the first partition id of a report
        that names zones, and for an empty report TargetZone.ALL while
        mowing or paused and TargetZone.NONE otherwise.
        """
        return _target_zone(self.location, self.status)

    @classmethod
    def from_state_message(
        cls,
        message: DeviceStateMessage,
        *,
        location: DeviceLocation | None,
        received_monotonic: float,
    ) -> MowerState:
        """Build the state from a state message the facade applied; the message is not changed.

        status is MowerStatus(message.state), UNKNOWN for a value the enum
        lacks. raw_state is the raw state kept in metrics["raw_state"] when
        normalisation changed it, as text; else message.state when the
        payload named a state (its raw payload has a non-null state, status
        or vehicleState, or, for a message built by hand, its state is not
        "unknown"); else None. battery is the message's. The error dict's
        code (under ``code``, else ``error_code``) and message become
        error_code (UNKNOWN for a code the enum lacks, NONE without a code)
        and error_message, as DeviceStatus.from_state_message reads them.
        observed_at is the message's timestamp in milliseconds
        (mower_time_ms), and received_at the message's.

        Args:
            message: The state message, with the receipt time NavimowSDK set
                when it applied it.
            location: The location record to carry; None without one.
            received_monotonic: time.monotonic() when the message arrived.

        Returns:
            The state, with source MQTT, the message as message and
            rest_status None.

        Raises:
            ValueError: The message has no received_at.
        """
        if message.received_at is None:
            # Decision: the state's receipt time is the receipt NavimowSDK stamped on
            # the message when it applied it, so received_at and received_monotonic
            # describe one moment and age() measures from it. A message without one
            # (built by hand, or by from_dict without the facade) is refused rather
            # than stamped now: a receipt invented here would date the observation at
            # the build, not at its arrival, and the state could not be aged.
            raise ValueError("a state needs the message's received_at, and this message has none")
        error_code, error_message = MowerError.NONE, None
        if isinstance(message.error, dict):
            code = message.error.get("code") or message.error.get("error_code")
            error_message = message.error.get("message")
            if code:
                error_code = _mower_error(code)
        return cls(
            device_id=message.device_id,
            status=_mower_status(message.state),
            raw_state=_message_raw_state(message),
            battery=message.battery,
            error_code=error_code,
            error_message=error_message,
            source=StateSource.MQTT,
            observed_at=mower_time_ms(message.timestamp),
            received_at=message.received_at,
            location=location,
            received_monotonic=received_monotonic,
            message=message,
        )

    @classmethod
    def from_status(
        cls,
        status: DeviceStatus,
        *,
        location: DeviceLocation | None,
        received_at: datetime,
        received_monotonic: float,
    ) -> MowerState:
        """Build the state from a REST status entry; the status is not changed.

        status, battery, error_code and error_message are the status's.
        raw_state is extra["vehicleState"] as text when the status has one,
        else None. observed_at is the status's timestamp in milliseconds
        (mower_time_ms); the cloud's status entries carry none, so for them
        it is None.

        Args:
            status: The status, as DeviceStatus.from_dict read the entry.
            location: The location record to carry; None without one.
            received_at: When the reply was read (UTC).
            received_monotonic: time.monotonic() when the reply was read.

        Returns:
            The state, with source REST, the status as rest_status and
            message None.
        """
        # Decision: the REST reader keeps only vehicleState raw (DeviceStatus.from_dict
        # puts it in extra), so an entry that named its state under status or state
        # alone has no raw value left to carry: raw_state is None for it although
        # status is read. The live cloud sends vehicleState, so on it the raw state
        # is always carried.
        raw_state: str | None = None
        if isinstance(status.extra, dict) and status.extra.get("vehicleState") is not None:
            raw_state = str(status.extra["vehicleState"])
        return cls(
            device_id=status.device_id,
            status=status.status,
            raw_state=raw_state,
            battery=status.battery,
            error_code=status.error_code,
            error_message=status.error_message,
            source=StateSource.REST,
            observed_at=mower_time_ms(status.timestamp),
            received_at=received_at,
            location=location,
            received_monotonic=received_monotonic,
            rest_status=status,
        )

    def age(self, now: float | None = None) -> float:
        """Seconds since the status half arrived, on the monotonic clock.

        Args:
            now: The monotonic time to measure at; None for time.monotonic().

        Returns:
            now minus received_monotonic, in seconds.
        """
        return (time.monotonic() if now is None else now) - self.received_monotonic

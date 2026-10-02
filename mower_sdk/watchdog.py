"""Find an MQTT connection that is up but no longer delivering.

The keepalive finds a dead link. It cannot find a broker that has stopped
delivering to a client whose link is still up, which the Navimow broker has
been seen to do. MqttWatchdog finds that from the data, with two rules:

1. After a REST poll. REST is current (its reading was taken at least the REST
   cache lag after the last accepted MQTT state report arrived), it disagrees
   with that report, and its state is one the state channel could have
   reported. The state channel missed a transition. Acted on once per MQTT
   report, so a mower that stays offline does not ask for a rebuild on every
   poll; agreement re-arms the device.
2. Periodically. A mower that is out sends a pose every two seconds, so the
   location silence (180 s by default) without a location message, from a
   mower with a timestamped pose on record, while it mows or returns (the
   shown state or REST's), and while the client says it is connected, means
   the broker stopped delivering. A docked mower is quiet by design.

Neither rule asks before the client has connected, and rebuilds are debounced
to one per debounce window (300 s by default), whichever rule asks. The
watchdog has no timer and does no I/O: the consumer runs its REST poll and its
schedule, hands in what only it knows (the state it shows and REST's latest),
and rebuilds the client itself (NavimowMQTT.rebuild, off the event loop) when a
check returns a RebuildRequest. Everything else comes from the facade: the
accepted state and its age, the pose on record, the location channel's
message age and the connection.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from mower_sdk.models import MowerStatus

if TYPE_CHECKING:
    from mower_sdk.sdk import NavimowSDK

__all__ = [
    "IGNORED_REST_STATES",
    "LOCATION_SILENCE_SECONDS",
    "MOVING_STATES",
    "MqttWatchdog",
    "REST_CACHE_LAG_SECONDS",
    "RebuildRequest",
    "WATCHDOG_DEBOUNCE_SECONDS",
    "WatchInput",
]

# The cloud's REST status lags the mower by one to two minutes: a REST reading
# counts as newer than an MQTT state report only when it was taken this long
# after the report arrived.
REST_CACHE_LAG_SECONDS = 120
# A mower that is out sends a pose every two seconds; this long without a
# location message while it mows or returns means the broker stopped delivering.
LOCATION_SILENCE_SECONDS = 180
# At most one rebuild request per this long, whichever rule asks.
WATCHDOG_DEBOUNCE_SECONDS = 300
# REST states the state channel never reports (offline, a software update, and
# the catch-all), so REST disagreeing with MQTT in one of them says nothing
# about MQTT.
IGNORED_REST_STATES = frozenset({"unknown", "offline", "updating"})
# States in which a mower that is out sends a pose every two seconds. Not
# mapping: see MqttWatchdog.check_silence.
MOVING_STATES = frozenset({"mowing", "returning"})


def _status(value: str | MowerStatus | None) -> str | None:
    """Give a state as its canonical string.

    Args:
        value: A MowerStatus, a canonical state string, or None.

    Returns:
        The MowerStatus's value; a string or None as given.
    """
    return value.value if isinstance(value, MowerStatus) else value


@dataclass(frozen=True)
class WatchInput:
    """What a consumer knows about one device that the SDK does not.

    Attributes:
        device_id: The device.
        name: The device's name, for the reason text.
        shown_state: The state the consumer displays (which may be REST's
            fallback), a canonical MowerStatus string or a MowerStatus.
        rest_state: REST's latest state, a canonical MowerStatus string or a
            MowerStatus; None without one.
        rest_observed_at: When that REST reading was taken, on
            time.monotonic(); None without one.
    """

    device_id: str
    name: str
    shown_state: str | MowerStatus | None
    rest_state: str | MowerStatus | None
    rest_observed_at: float | None


@dataclass(frozen=True)
class RebuildRequest:
    """A watchdog's proposal to rebuild the MQTT client.

    Pass the request to MqttWatchdog.acknowledge once the rebuild is made or
    scheduled.

    Attributes:
        reason: Why the rebuild is proposed, as a sentence for a log line.
        device_ids: The devices the evidence came from.
        reports: For each device of a missed state change, its id and the
            receipt time of the MQTT state report the request is about;
            acknowledge() marks these acted on. Empty for a location silence.
    """

    reason: str
    device_ids: tuple[str, ...]
    reports: tuple[tuple[str, datetime], ...] = field(default=(), repr=False)


class MqttWatchdog:
    """Decide when an MQTT client that is up has stopped delivering.

    The two rules of the module's description, over a facade's caches and its
    MQTT client's connection and message times. Both checks and acknowledge()
    read the facade's caches, which are written on its bound event loop, so
    they run there too; only the rebuild itself goes off the loop. Every time
    is time.monotonic(), the clock of the facade's cache ages and the client's
    message ages.

    Attributes:
        sdk: The facade whose caches and MQTT client are read.
        rest_cache_lag: Seconds a REST reading must be newer than the accepted
            MQTT state report to count against it.
        location_silence: Seconds without a location message, from a mower
            that is out, that mean the broker stopped delivering.
        debounce: Seconds after an acknowledged request in which no other is
            made.
    """

    def __init__(
        self,
        sdk: NavimowSDK,
        *,
        rest_cache_lag: float = REST_CACHE_LAG_SECONDS,
        location_silence: float = LOCATION_SILENCE_SECONDS,
        debounce: float = WATCHDOG_DEBOUNCE_SECONDS,
    ) -> None:
        """Watch one facade.

        Args:
            sdk: The facade whose caches and MQTT client the rules read.
            rest_cache_lag: Seconds a REST reading must be newer than the
                accepted MQTT state report to count against it.
            location_silence: Seconds without a location message that count
                as silence.
            debounce: Seconds after an acknowledged request in which no other
                is made.

        Raises:
            ValueError: A threshold is negative.
        """
        for name, value in (
            ("rest_cache_lag", rest_cache_lag),
            ("location_silence", location_silence),
            ("debounce", debounce),
        ):
            if value < 0:
                raise ValueError(f"MqttWatchdog: {name} must not be negative, got {value!r}")
        self.sdk = sdk
        self.rest_cache_lag = rest_cache_lag
        self.location_silence = location_silence
        self.debounce = debounce
        self._last_request_acknowledged_at: float | None = None
        # device id -> the receipt time of the MQTT report a rebuild was acknowledged for
        self._acted_on: dict[str, datetime] = {}

    def _debounced(self, now: float) -> bool:
        """Say whether the debounce window of the last acknowledged request is open.

        Args:
            now: The current time.monotonic() reading.

        Returns:
            True while less than debounce seconds have passed since a request
            was acknowledged; False when none has been.
        """
        acknowledged = self._last_request_acknowledged_at
        return acknowledged is not None and now - acknowledged < self.debounce

    def after_poll(self, inputs: Iterable[WatchInput]) -> RebuildRequest | None:
        """Apply rule 1 after a REST poll: a state change the state channel missed.

        A device whose accepted MQTT state agrees with REST is re-armed. One whose
        REST state is in IGNORED_REST_STATES, whose REST reading is not at least
        rest_cache_lag newer than the accepted MQTT report, or whose report was
        already acted on, is skipped. Nothing is asked before the client's first
        connect or inside the debounce window; a mismatch found then stays live
        for the next poll, since the missed transition may never come to clear it.

        Args:
            inputs: One WatchInput for each device the poll answered for, with
                the time its REST reading was taken. A device the poll left
                out is not passed.

        Returns:
            A RebuildRequest naming every device with a live mismatch, or None
            when there is none or nothing may be asked yet.
        """
        now = time.monotonic()
        mismatches: list[tuple[WatchInput, str, str, datetime]] = []
        # Decision: only the devices the poll answered for are passed, each with the
        # time its reading was taken. A batch poll can leave a device out, and an
        # old REST reading must not count as current against a newer MQTT report.
        for watched in inputs:
            message = self.sdk.get_cached_state(watched.device_id)
            mqtt_state = message.state if message is not None else None
            rest_state = _status(watched.rest_state)
            if mqtt_state is None or rest_state is None:
                continue
            if mqtt_state == rest_state:
                self._acted_on.pop(watched.device_id, None)
                continue
            if rest_state in IGNORED_REST_STATES or watched.rest_observed_at is None:
                continue
            # The accepted state's own times, never the client's raw message times:
            # with reject_late_state a rejected state message does not move them.
            age = self.sdk.get_cached_state_age(watched.device_id)
            received_at = self.sdk.get_cached_state_received_at(watched.device_id)
            if age is None or received_at is None:
                continue
            if min(watched.rest_observed_at, now) - (now - age) < self.rest_cache_lag:
                continue  # REST may simply not have caught up with the report yet
            if self._acted_on.get(watched.device_id) == received_at:
                continue
            mismatches.append((watched, mqtt_state, rest_state, received_at))
        if not mismatches or not self.sdk.mqtt.connects or self._debounced(now):
            return None
        first, mqtt_state, rest_state, _ = mismatches[0]
        return RebuildRequest(
            reason=(
                f"missed a state change (REST says {rest_state} "
                f"but MQTT last said {mqtt_state} for {first.name})"
            ),
            device_ids=tuple(watched.device_id for watched, *_ in mismatches),
            reports=tuple(
                (watched.device_id, received_at) for watched, _, _, received_at in mismatches
            ),
        )

    def check_silence(self, inputs: Iterable[WatchInput]) -> RebuildRequest | None:
        """Apply rule 2: a connected client that delivers no location message.

        For each device with a timestamped pose on record whose shown or REST
        state is in MOVING_STATES (and whose shown state is not mapping), the
        time since the last location message, or since the connect when none has
        arrived since, is compared with location_silence. Nothing is asked while
        the client is not connected (reconnecting is paho's work), inside the
        debounce window, or when the client does not subscribe the location
        channel (subscribe_location=False, the default), where silence is
        expected and a rebuild, which keeps that setting, could not end it.

        Args:
            inputs: One WatchInput for each device to check.

        Returns:
            A RebuildRequest for the first device found silent for at least
            location_silence seconds, or None.
        """
        mqtt = self.sdk.mqtt
        now = time.monotonic()
        connected_at = mqtt.last_connected_monotonic
        if not mqtt.subscribe_location:
            return None
        if not mqtt.is_connected or connected_at is None or self._debounced(now):
            return None
        for watched in inputs:
            location = self.sdk.get_cached_location(watched.device_id)
            if location is None or location.pose_at is None:
                continue  # a mower whose location channel is silent altogether is not watched
            shown_state = _status(watched.shown_state)
            # Decision: a shown mapping is never moving, whatever REST says. During a
            # map edit the mower sends a pose only where it halts, minutes apart (up
            # to eight minutes, observed September 2026), and REST's lagging mowing
            # must not override that. A broker that stops delivering during a map
            # edit is therefore found only by rule 1 or by the keepalive.
            if shown_state == "mapping":
                continue
            if (
                shown_state not in MOVING_STATES
                and _status(watched.rest_state) not in MOVING_STATES
            ):
                continue
            # Decision: any location traffic counts, a payload that was rejected or
            # empty included: the question is whether the broker delivers at all, not
            # whether a pose was applied. Rule 1 keys on accepted state only. Nothing
            # can have arrived before this client connected.
            quiet_for = now - connected_at
            location_age = mqtt.last_message_age(watched.device_id, "location")
            if location_age is not None:
                quiet_for = min(quiet_for, location_age)
            if quiet_for >= self.location_silence:
                return RebuildRequest(
                    reason=f"no location message for {int(quiet_for)} s while {watched.name} runs",
                    device_ids=(watched.device_id,),
                )
        return None

    def acknowledge(self, request: RebuildRequest) -> None:
        """Record that the consumer rebuilt the client, or scheduled the rebuild.

        Starts the debounce window and marks the request's MQTT reports acted on.

        Args:
            request: The request the rebuild was made for.
        """
        # Decision: the debounce starts here, not when a check returns a request. A
        # consumer may decline one (another operation holds its lock, say); starting
        # the window on the request would then silence the mismatch for the whole
        # window with nothing rebuilt.
        self._last_request_acknowledged_at = time.monotonic()
        for device_id, received_at in request.reports:
            self._acted_on[device_id] = received_at

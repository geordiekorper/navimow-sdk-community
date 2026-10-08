"""The account-level client: one state per mower from both transports.

The SDK observes a mower in two ways. The MQTT state channel gives a
DeviceStateMessage for each state message NavimowSDK applies, and a REST
getVehicleStatus reply gives a DeviceStatus per device. The location channel
adds the DeviceLocation record that LocationDecoder merges entry by entry.
MowerState joins the two: a status half built from one of the two
observations, which StateSource names, and the location record as given, with
the target zone read from both halves and the state's age on the monotonic
clock.

NavimowClient owns one MowerAPI and one NavimowSDK for an account, keeps the
latest observation from each transport for every mower, decides which one the
status half comes from, and delivers each new state to the on_state callbacks.
It connects the feed, runs the clock that polls the status and re-evaluates
the merge by time, pushes a rotated token to the MQTT client and recovers
after a refused connection, so a consumer registers its callbacks, connects
and receives.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mower_sdk.api import MowerAPI
from mower_sdk.errors import MowerAPIError
from mower_sdk.location import TargetZone
from mower_sdk.location import target_zone as _target_zone
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceLocationMessage,
    DeviceStateMessage,
    DeviceStatus,
    MowerError,
    MowerStatus,
    RejectedMessage,
    _mower_error,
    _mower_status,
    mower_time_ms,
)
from mower_sdk.mqtt import ConnectionEvent, _resolve_event_loop
from mower_sdk.sdk import NavimowSDK

if TYPE_CHECKING:
    import aiohttp

__all__ = [
    "COMMAND_POLL_DELAY_SECONDS",
    "MQTT_STALE_SECONDS",
    "MowerState",
    "NavimowClient",
    "REST_POLL_MAX_BACKOFF_SECONDS",
    "REST_POLL_SECONDS",
    "SILENCE_CHECK_SECONDS",
    "StateSource",
]

_LOGGER = logging.getLogger(__name__)

# An MQTT observation older than this yields the status half to a REST
# observation received after it.
MQTT_STALE_SECONDS = 300
# The clock's poll interval.
REST_POLL_SECONDS = 120
# The cap of the doubling wait after failed polls; never below the interval.
REST_POLL_MAX_BACKOFF_SECONDS = 600
# The silence tick: how often the merge is re-evaluated by time.
SILENCE_CHECK_SECONDS = 30
# The delay of the poll that follows a command.
COMMAND_POLL_DELAY_SECONDS = 5

# The NavimowSDK constructor arguments a consumer may pass through the client.
_MQTT_OPTION_KEYS = frozenset(
    {"keepalive_seconds", "reconnect_min_delay", "reconnect_max_delay", "extra_topics"}
)


async def _sleep(seconds: float) -> None:
    """Wait for a number of seconds on the loop's clock.

    The clock's tasks wait through this function and nothing else, so a test
    can stand in for it.

    Args:
        seconds: How long to wait.
    """
    await asyncio.sleep(seconds)


def _positive(value: Any) -> bool:
    """Say whether a value is a positive number, for the constructor's checks.

    Args:
        value: The value given.

    Returns:
        True for an int or float above zero; False for a bool, anything else
        or a value at or below zero.
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


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


@dataclass(frozen=True)
class _MqttObservation:
    """The latest state message the facade applied for a device, and when it arrived.

    Attributes:
        message: The state message.
        received_monotonic: time.monotonic() when the client's handler
            received it.
    """

    message: DeviceStateMessage
    received_monotonic: float

    def build(self, location: DeviceLocation | None) -> MowerState:
        """The state whose status half is this observation.

        Args:
            location: The location half to carry.

        Returns:
            MowerState.from_state_message of the message.
        """
        return MowerState.from_state_message(
            self.message, location=location, received_monotonic=self.received_monotonic
        )

    def is_source_of(self, state: MowerState) -> bool:
        """Say whether a state's status half was built from this observation.

        Args:
            state: The state to check.

        Returns:
            True when the state's message is this observation's message.
        """
        return state.message is self.message


@dataclass(frozen=True)
class _RestObservation:
    """The latest REST status read for a device, and when its reply was read.

    Attributes:
        status: The status entry.
        received_at: When the reply was read (UTC).
        received_monotonic: time.monotonic() when the reply was read.
    """

    status: DeviceStatus
    received_at: datetime
    received_monotonic: float

    def build(self, location: DeviceLocation | None) -> MowerState:
        """The state whose status half is this observation.

        Args:
            location: The location half to carry.

        Returns:
            MowerState.from_status of the status.
        """
        return MowerState.from_status(
            self.status,
            location=location,
            received_at=self.received_at,
            received_monotonic=self.received_monotonic,
        )

    def is_source_of(self, state: MowerState) -> bool:
        """Say whether a state's status half was built from this observation.

        Args:
            state: The state to check.

        Returns:
            True when the state's rest_status is this observation's status.
        """
        return state.rest_status is self.status


_Observation = _MqttObservation | _RestObservation


class NavimowClient:
    """One client for an account: the REST client and the MQTT facade under one state per mower.

    The client owns the MowerAPI it is given and the NavimowSDK it builds at
    async_connect(). For every mower it keeps the latest state message the
    facade applied (the MQTT observation), the latest status of a
    getVehicleStatus reply (the REST observation) and the facade's location
    record, and builds the mower's MowerState from them: the status half from
    the MQTT observation while it is younger than mqtt_stale_seconds, else
    from a REST observation received after it, else still from the MQTT one.
    Each new state goes to the on_state callbacks and is returned by state()
    and states(). A state is rebuilt after each state message, each applied
    location entry, each poll whose REST observation becomes or stays
    current, each silence tick at which the rule's choice changed by time,
    and each restore_location() once the facade exists.

    The clock: from async_connect() the client polls every poll_interval
    seconds (the first poll is the connect's own), doubling the wait after a
    failed poll up to poll_backoff_max and never below the interval, and
    re-evaluates the merge every SILENCE_CHECK_SECONDS, so a flip by time is
    seen within one tick. async_disconnect() cancels both tasks. A failure
    on the clock is logged and reported through on_error, never raised.

    Use: construct it on the MowerAPI, or with from_token, register the
    callbacks, then
    ``await client.async_connect()``, which lists the devices when none were
    given, polls their status once, builds the facade and connects the feed.
    ``await client.async_disconnect()`` stops the feed; a later
    async_connect() reconnects it. The SDK stays token-in: the client obtains
    no token, but asks token_provider, when one is given, for a fresh one
    before each operation that sends one, and pushes a token that changed to
    the MQTT client as its bearer header. After a refused connection it
    fetches the broker credentials again, unless recover_on_connect_fail is
    False.

    Every coroutine runs on one event loop, bound at construction (loop=,
    else the running loop, else the loop set as current) or at the first
    coroutine awaited, and raises RuntimeError when awaited from another
    running loop. The synchronous members (the getters, the registration
    methods, restore_location) are called on that loop's thread. Blocking
    facade calls run in the default executor. Every callback is a plain
    function called on the bound loop, each in its own try with a failure
    logged, so one that raises does not stop the others.

    Attributes:
        api: The REST client.
        devices: The account's devices: those given to the constructor, else
            those async_connect() or async_poll() fetched; empty until known.
            One list, which the facade holds as its records.
        last_poll_at: The UTC time of the last successful poll; None before
            one.
        last_poll_error: The exception of the last failed poll; None after a
            success.
    """

    def __init__(
        self,
        api: MowerAPI,
        *,
        devices: list[Device] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
        recover_on_connect_fail: bool = True,
        poll_interval: float | None = REST_POLL_SECONDS,
        poll_backoff_max: float = REST_POLL_MAX_BACKOFF_SECONDS,
        mqtt_stale_seconds: float = MQTT_STALE_SECONDS,
        subscribe_location: bool = True,
        reject_late_state: bool = True,
        **mqtt_options: Any,
    ) -> None:
        """Create the client; nothing is fetched or connected until a coroutine is awaited.

        Args:
            api: The REST client, with its token set or to be set through
                async_set_token or token_provider.
            devices: The account's devices; None fetches them in
                async_connect() or async_poll(). An empty list is known and
                empty, and async_connect() refuses it.
            loop: The event loop the client runs on; None binds the running
                loop, else the loop set as current, else the loop of the first
                coroutine awaited.
            token_provider: A coroutine function returning a fresh access
                token, or None to keep the current one; awaited before each
                operation that sends a token. None for no provider.
            recover_on_connect_fail: True fetches the broker credentials again
                after a refused connection (see on_connection for the event).
            poll_interval: Seconds between the clock's polls; None runs no
                poll task, and a consumer polls with async_poll() itself.
            poll_backoff_max: The cap of the doubling wait after failed
                polls; a cap below poll_interval is raised to it.
            mqtt_stale_seconds: Seconds after which an MQTT observation yields
                the status half to a REST observation received after it.
            subscribe_location: True subscribes each device's location topic,
                so states carry the location record.
            reject_late_state: True keeps a late or implausible state message
                from being applied (NavimowSDK says more).
            **mqtt_options: keepalive_seconds, reconnect_min_delay,
                reconnect_max_delay and extra_topics, passed to NavimowSDK
                unchanged.

        Raises:
            TypeError: mqtt_options names another argument.
            ValueError: poll_interval (when given) or poll_backoff_max is not
                a positive number, or mqtt_stale_seconds is not a number (a
                bool included) or is negative.
        """
        # Decision: mqtt_options accepts the four facade arguments a consumer may
        # tune and refuses the rest at construction: the connection arguments come
        # from the credential reply, loop, records, subscribe_location and
        # reject_late_state are the client's own, and the experimental MQTT
        # commands stay off. A wrong key is found here, not at async_connect().
        unknown = sorted(set(mqtt_options) - _MQTT_OPTION_KEYS)
        if unknown:
            raise TypeError(
                "NavimowClient() takes only keepalive_seconds, reconnect_min_delay, "
                f"reconnect_max_delay and extra_topics as MQTT options, not {', '.join(unknown)}"
            )
        # Decision: the poll interval is a deployment setting with a default, not
        # something the client learns from the cloud: it backs off on every failed
        # poll, the cloud's "too frequent" included, and comes back to the interval
        # on the next success.
        if poll_interval is not None and not _positive(poll_interval):
            raise ValueError(
                "NavimowClient: poll_interval must be a positive number or None, "
                f"got {poll_interval!r}"
            )
        if not _positive(poll_backoff_max):
            raise ValueError(
                "NavimowClient: poll_backoff_max must be a positive number, "
                f"got {poll_backoff_max!r}"
            )
        if (
            isinstance(mqtt_stale_seconds, bool)
            or not isinstance(mqtt_stale_seconds, int | float)
            or mqtt_stale_seconds < 0
        ):
            raise ValueError(
                "NavimowClient: mqtt_stale_seconds must not be negative, "
                f"got {mqtt_stale_seconds!r}"
            )
        self.api = api
        self.devices: list[Device] = list(devices) if devices is not None else []
        self._devices_known = devices is not None
        self._loop = _resolve_event_loop(loop)
        self._token_provider = token_provider
        self._recover_on_connect_fail = recover_on_connect_fail
        self._poll_interval = None if poll_interval is None else float(poll_interval)
        self._poll_backoff_max = max(float(poll_backoff_max), self._poll_interval or 0.0)
        self._mqtt_stale_seconds = float(mqtt_stale_seconds)
        # Decision: reject_late_state and subscribe_location default to True here,
        # unlike on the facade: the client's state is meant to be shown, so a late
        # message must not replace a newer state, and the location half needs the
        # channel.
        self._subscribe_location = subscribe_location
        self._reject_late_state = reject_late_state
        self._mqtt_options = dict(mqtt_options)

        self._sdk: NavimowSDK | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._token: str | None = api.token
        self._applied_bearer: dict[str, str] | None = None

        self._mqtt_observations: dict[str, _MqttObservation] = {}
        self._rest_observations: dict[str, _RestObservation] = {}
        self._states: dict[str, MowerState] = {}
        self._queued_restores: dict[str, DeviceLocation] = {}
        self._queued_forwards: list[tuple[str, Callable[[Any], None]]] = []
        self._state_callbacks: list[tuple[Callable[[MowerState], None], str | None]] = []
        self._connection_callbacks: list[Callable[[ConnectionEvent], None]] = []
        self._error_callbacks: list[Callable[[str, Exception], None]] = []
        self.last_poll_at: datetime | None = None
        self.last_poll_error: Exception | None = None
        # The clock: the wait before the next poll (the interval, doubled after each
        # failure), when that poll is due on time.monotonic(), and the tasks.
        self._poll_wait = self._poll_interval or 0.0
        self._next_poll_due: float | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @classmethod
    def from_token(
        cls, session: aiohttp.ClientSession, token: str, base_url: str, **options: Any
    ) -> NavimowClient:
        """Build the client on a new MowerAPI for a session, a token and a base URL.

        The same as NavimowClient(MowerAPI(session, token, base_url), **options):
        the REST client is built with its defaults (a request timeout of 20
        seconds) and is reachable as api. A consumer that wants another
        request timeout, or already holds a MowerAPI, uses the constructor.

        Args:
            session: The aiohttp session every request is sent on; the
                caller creates and closes it.
            token: The OAuth access token, as MowerAPI takes it.
            base_url: The API base URL, as MowerAPI takes it.
            **options: The constructor's keyword arguments, passed unchanged.

        Returns:
            The client, not yet connected.

        Raises:
            TypeError: options names an argument the constructor refuses.
            ValueError: options holds a value the constructor refuses.
        """
        return cls(MowerAPI(session, token, base_url), **options)

    # ---- the layers underneath -------------------------------------------------------------------

    @property
    def sdk(self) -> NavimowSDK | None:
        """The MQTT facade; None until async_connect() built it."""
        return self._sdk

    @property
    def mqtt(self) -> Any:
        """The MQTT client under the facade (sdk.mqtt); None until async_connect() built it."""
        return None if self._sdk is None else self._sdk.mqtt

    @property
    def is_connected(self) -> bool:
        """Whether the MQTT client is connected to the broker; False before async_connect()."""
        return self._sdk is not None and self._sdk.is_connected

    def device(self, device_id: str) -> Device | None:
        """The device with this id.

        Args:
            device_id: The device.

        Returns:
            The Device in devices, or None when none has the id.
        """
        return next((device for device in self.devices if device.id == device_id), None)

    # ---- the loop and the token ------------------------------------------------------------------

    def _bind(self, caller: str) -> asyncio.AbstractEventLoop:
        """Bind the running loop when none is bound, and refuse a call from another loop.

        Args:
            caller: The coroutine's name, for the message.

        Returns:
            The bound loop, which is the running one.

        Raises:
            RuntimeError: The running loop is not the bound one.
        """
        running = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = running
        elif self._loop is not running:
            raise RuntimeError(
                f"NavimowClient is bound to event loop {self._loop!r}; {caller}() was awaited "
                f"from another, {running!r}. Await it on the bound loop, or construct the client "
                "in (or with loop=) the loop that will run it."
            )
        return running

    @staticmethod
    def _bearer(token: str | None) -> dict[str, str] | None:
        """The WebSocket upgrade header for a token.

        Args:
            token: The access token, or None.

        Returns:
            {"Authorization": "Bearer <token>"}, or None for None.
        """
        return None if token is None else {"Authorization": f"Bearer {token}"}

    def _learn(self, token: str) -> None:
        """Make a token the current one: set it on the REST client and remember it.

        Args:
            token: The token.
        """
        self.api.set_token(token)
        self._token = token

    async def _refresh_token(self) -> None:
        """Ask the provider for a token and learn it, when there is a provider and it gives one.

        Raises:
            Exception: Whatever the provider raises; nothing is changed then.
        """
        # Decision: the provider is awaited before an operation that is about to
        # send a token (a connect that starts or restarts, a credential refresh, a
        # rebuild, a device refresh, each poll) and never by itself: the
        # client runs no refresh schedule of its own, the consumer's provider does.
        # None keeps the token as it is; an exception fails the operation.
        if self._token_provider is None:
            return
        token = await self._token_provider()
        if token is not None:
            self._learn(token)

    async def _reconcile(self, loop: asyncio.AbstractEventLoop) -> None:
        """Push the current token's bearer to the MQTT client when it differs from the applied one.

        Called under the lifecycle lock. Nothing happens before the facade
        exists, while the client is closed, or while the current bearer is
        the applied one.

        Args:
            loop: The bound loop, whose default executor runs the push.
        """
        # Decision: while closed the MQTT client is left alone: a bearer push on a
        # disconnected client would rebuild and connect it, which only
        # async_connect() may do, and it clears the closed mark before it
        # reconciles. The push sends the current token, never a call's argument,
        # since a push that waited for the lock may have been overtaken by a newer
        # token, and records exactly what it pushed as the applied bearer.
        sdk = self._sdk
        if sdk is None or self._closed:
            return
        bearer = self._bearer(self._token)
        if bearer is None or bearer == self._applied_bearer:
            return
        await loop.run_in_executor(
            None, functools.partial(sdk.update_mqtt_credentials, auth_headers=bearer)
        )
        self._applied_bearer = bearer

    # ---- lifecycle -------------------------------------------------------------------------

    async def async_connect(self) -> None:
        """List the devices when needed, poll them once, build the facade and connect the feed.

        Started already (the facade exists and the client is not closed):
        nothing. Otherwise, under the lifecycle lock: the provider is asked
        for a token when one is given. Before the facade exists, the devices
        are fetched when none were given, the connection information is
        fetched, the facade is built in the default executor with the current
        token as its bearer, the client's handlers and the callbacks and
        restores registered before this call are installed, every device is
        polled once (a failure is reported through on_error as "poll" and
        does not stop the connect), and the feed is connected. After
        async_disconnect(), the closed mark is cleared, the devices are polled
        once, and a token that changed while closed is pushed, which rebuilds
        and connects the MQTT client; else the client is reconnected. A
        connect interrupted after the facade was built (cancelled, say)
        leaves the client closed, and the next async_connect() takes that
        path.

        Raises:
            MowerAPIError: A REST request failed (its subclass says which
                kind); nothing is built then.
            ValueError: The account has no devices, the connection
                information names no WebSocket path, or an extra topic is
                invalid (NavimowSDK.from_connection_info says more).
            RuntimeError: Awaited from a running loop other than the bound
                one.
            Exception: Whatever token_provider raises; nothing is changed
                then.
        """
        loop = self._bind("async_connect")
        async with self._lock:
            if self._sdk is not None and not self._closed:
                return
            await self._refresh_token()
            if self._sdk is None:
                await self._build(loop)
            else:
                await self._restart(loop)

    async def _build(self, loop: asyncio.AbstractEventLoop) -> None:
        """The first connect: fetch what is missing, build the facade and connect it.

        Called under the lifecycle lock, with the token learned.

        Args:
            loop: The bound loop.

        Raises:
            MowerAPIError: A REST request failed.
            ValueError: The account has no devices, or the facade refused the
                connection information or an extra topic.
        """
        if not self._devices_known:
            self.devices[:] = await self.api.async_get_devices()
            self._devices_known = bool(self.devices)
        # Decision: an empty device list refuses to connect. The facade would
        # subscribe with the + wildcard, which the broker grants and has never been
        # seen to deliver on, so the feed would be dead while looking connected.
        if not self.devices:
            raise ValueError(
                "NavimowClient.async_connect(): the account has no devices to subscribe; "
                "nothing was built"
            )
        info = await self.api.async_get_mqtt_connection_info()
        token = self._token
        sdk = await loop.run_in_executor(
            None,
            functools.partial(
                NavimowSDK.from_connection_info,
                info,
                access_token=token,
                records=self.devices,
                loop=loop,
                subscribe_location=self._subscribe_location,
                reject_late_state=self._reject_late_state,
                **self._mqtt_options,
            ),
        )
        # Decision: the client's handlers are registered at build, before the
        # facade is reachable as client.sdk, so they run before any callback a
        # consumer registers on the facade itself and state() already shows a
        # message inside such a callback. The facade connects nothing until
        # connect(), so no message can arrive before the hooks are in place.
        sdk.mqtt.on_connect_fail = self._on_connect_fail
        sdk.mqtt.on_connection_event = self._on_connection_event
        sdk.on_state(self._on_state_message)
        sdk.on_location(self._on_location_entry)
        for channel, callback in self._queued_forwards:
            self._register_forward(sdk, channel, callback)
        self._queued_forwards.clear()
        restored = list(self._queued_restores.items())
        self._queued_restores.clear()
        for device_id, location in restored:
            sdk.restore_location(device_id, location)
        self._sdk = sdk
        try:
            for device_id, _location in restored:
                if device_id in self._states:
                    self._rebuild(device_id, sdk.get_cached_location(device_id))
            await self._startup_poll()
            await loop.run_in_executor(None, sdk.connect)
        except BaseException:
            # Decision: a first connect interrupted after the build (cancelled during
            # the startup poll, say) leaves the facade built and the client marked
            # closed, so the next async_connect() restarts it: polls, reconciles and
            # connects, instead of finding it started and doing nothing.
            self._closed = True
            raise
        self._applied_bearer = self._bearer(token)
        self._start_clock(loop)

    async def _restart(self, loop: asyncio.AbstractEventLoop) -> None:
        """A connect after async_disconnect(): clear the mark, poll, reconcile and reconnect.

        Called under the lifecycle lock, with the token learned.

        Args:
            loop: The bound loop.
        """
        sdk = self._sdk
        assert sdk is not None
        self._closed = False
        try:
            await self._startup_poll()
            bearer = self._bearer(self._token)

            def reconnect() -> None:
                if bearer is not None and bearer != self._applied_bearer:
                    # Changed while closed: on the disconnected client this rebuilds
                    # and connects; connect() after it is then a no-op.
                    sdk.update_mqtt_credentials(auth_headers=bearer)
                sdk.connect()

            await loop.run_in_executor(None, reconnect)
        except BaseException:
            # Interrupted before the reconnect: still closed, so the next
            # async_connect() restarts again.
            self._closed = True
            raise
        if bearer is not None:
            self._applied_bearer = bearer
        self._start_clock(loop)

    async def _startup_poll(self) -> None:
        """The poll inside async_connect(): every device once, a failure reported, never raised."""
        # Decision: the startup poll skips the provider and the reconcile. The token
        # was learned at the start of the connect, and the bearer is applied by the
        # build or, on a restart, by the reconcile that follows the poll; a reconcile
        # here would take the lifecycle lock inside itself.
        try:
            await self._scheduled_poll(None, provider=False)
        except Exception as exc:
            _LOGGER.exception("NavimowClient: the poll inside async_connect() failed")
            self._report("poll", exc)

    async def async_disconnect(self) -> None:
        """Stop the feed: mark the client closed and disconnect the MQTT client.

        Under the lifecycle lock, so an operation in flight finishes first.
        The clock's tasks are cancelled and awaited before the MQTT client is
        disconnected in the default executor, so nothing of the client's runs
        afterwards. A no-op before async_connect() and when already closed.
        While closed the
        MQTT client is left alone until the next async_connect(); REST stays
        usable, and async_set_token() changes the REST token only.

        Raises:
            RuntimeError: Awaited from a running loop other than the bound
                one.
        """
        loop = self._bind("async_disconnect")
        async with self._lock:
            if self._sdk is None or self._closed:
                return
            self._closed = True
            await self._cancel_tasks()
            await loop.run_in_executor(None, self._sdk.disconnect)

    async def async_set_token(self, token: str) -> None:
        """Make a new access token the current one, on REST at once and on MQTT under the lock.

        The REST client uses it from the next request. While the client is
        started, the bearer header is pushed to the MQTT client when it
        differs from the one applied, which the client uses at its next
        connect, automatic reconnects included (NavimowSDK.update_mqtt_credentials
        says more). Before async_connect() the token is used by the build;
        while closed, by the next async_connect().

        Args:
            token: The new access token.

        Raises:
            RuntimeError: Awaited from a running loop other than the bound
                one.
        """
        loop = self._bind("async_set_token")
        self._learn(token)
        async with self._lock:
            await self._reconcile(loop)

    async def async_refresh_broker_credentials(self, *, force_reconnect: bool = False) -> bool:
        """Fetch the broker credentials again and apply them, with the current token's bearer.

        A no-op returning False before async_connect() and while closed.
        Otherwise, under the lifecycle lock: the provider is asked for a
        token when one is given, then the facade's
        async_refresh_broker_credentials runs with the current token's
        bearer header, its own cooldown (one request a minute) in force. When
        it applied the credentials, that bearer is recorded as applied; when
        it made no request, or raised, the bearer is reconciled on its own
        before the result or the exception is passed on, so a rotated token
        reaches the MQTT client either way.

        Args:
            force_reconnect: True rebuilds the MQTT client and reconnects even
                when nothing changed.

        Returns:
            True when the credentials were fetched and applied; False inside
            the cooldown, before async_connect() and while closed.

        Raises:
            MowerAPIError: The request failed, or the reply carried no
                credentials.
            RuntimeError: Awaited from a running loop other than the bound
                one.
            Exception: Whatever token_provider raises.
        """
        loop = self._bind("async_refresh_broker_credentials")
        if self._sdk is None or self._closed:
            return False
        async with self._lock:
            sdk = self._sdk
            if sdk is None or self._closed:
                return False
            await self._refresh_token()
            bearer = self._bearer(self._token)
            try:
                refreshed = await sdk.async_refresh_broker_credentials(
                    self.api, auth_headers=bearer, force_reconnect=force_reconnect
                )
            except Exception:
                await self._reconcile(loop)
                raise
            if refreshed and bearer is not None:
                self._applied_bearer = bearer
            else:
                await self._reconcile(loop)
            return refreshed

    async def async_rebuild(self, reason: str) -> None:
        """Replace the MQTT client with a new one and connect it, with the current token's bearer.

        A no-op before async_connect() and while closed. Otherwise, under the
        lifecycle lock: the provider is asked for a token when one is given,
        then NavimowMQTT.rebuild runs in the default executor with that
        bearer header and the reason, which is logged and kept as
        mqtt.last_rebuild_reason, and the bearer is recorded as applied.

        Args:
            reason: Why the client is rebuilt, for the log line.

        Raises:
            RuntimeError: Awaited from a running loop other than the bound
                one.
            Exception: Whatever token_provider raises.
        """
        loop = self._bind("async_rebuild")
        if self._sdk is None or self._closed:
            return
        async with self._lock:
            sdk = self._sdk
            if sdk is None or self._closed:
                return
            await self._refresh_token()
            bearer = self._bearer(self._token)
            await loop.run_in_executor(
                None, functools.partial(sdk.mqtt.rebuild, auth_headers=bearer, reason=reason)
            )
            if bearer is not None:
                self._applied_bearer = bearer

    async def async_refresh_devices(self) -> list[Device]:
        """Fetch the device list again and update devices in place.

        Under the lifecycle lock: the provider is asked for a token when one
        is given, the list is fetched and updated in place, and a token that
        changed is pushed to the MQTT client. The facade holds the same list
        as its records, so a device added here has its topics subscribed at
        the next connect or rebuild; the facade has no call to subscribe one
        more topic on a live connection.

        Returns:
            The devices, as a new list.

        Raises:
            MowerAPIError: The request failed; the list is unchanged.
            RuntimeError: Awaited from a running loop other than the bound
                one.
            Exception: Whatever token_provider raises; nothing is changed
                then.
        """
        loop = self._bind("async_refresh_devices")
        async with self._lock:
            await self._refresh_token()
            devices = await self.api.async_get_devices()
            # Decision: the list is updated in place rather than replaced, since the
            # facade reads its records at every subscribe; the new topics are
            # therefore subscribed at the next connect or rebuild, not at once.
            self.devices[:] = devices
            self._devices_known = True
            await self._reconcile(loop)
            return list(self.devices)

    # ---- the state -------------------------------------------------------------------------

    async def async_poll(self, device_ids: Iterable[str] | None = None) -> dict[str, DeviceStatus]:
        """Poll the status over REST and apply the reply to the states.

        The provider is asked for a token when one is given, and a token that
        changed is pushed to the MQTT client through async_set_token(). Then
        one getVehicleStatus request is made for the devices (all of them,
        fetched first when none are known), at most one at a time, so two
        polls never overlap and their replies are applied in order. For each
        device the reply covers, the REST observation is replaced and the
        state rebuilt when the merge selects that observation, which fires
        on_state; a device whose MQTT observation is current is left as it
        is. last_poll_at and last_poll_error are updated. The poll counts
        as a tick of the clock: a success sets the next poll one interval
        from now and resets the backoff, a failure doubles the wait.

        Args:
            device_ids: The devices to poll; None polls every known device.

        Returns:
            The statuses by device id, as MowerAPI.async_get_device_statuses
            returns them.

        Raises:
            MowerAPIError: The request failed; the states are unchanged and
                last_poll_error holds the exception.
            RuntimeError: Awaited from a running loop other than the bound
                one.
            Exception: Whatever token_provider raises; last_poll_error holds
                it.
        """
        self._bind("async_poll")
        statuses = await self._scheduled_poll(device_ids, provider=True)
        assert statuses is not None  # a manual poll is never skipped
        return statuses

    async def _scheduled_poll(
        self, device_ids: Iterable[str] | None, *, provider: bool, only_when_due: bool = False
    ) -> dict[str, DeviceStatus] | None:
        """A poll that counts as a tick of the clock, with the token step when asked.

        Args:
            device_ids: The devices to poll; None for every known device.
            provider: True asks the provider first and pushes a token that
                changed through async_set_token(); False skips the token step.
            only_when_due: True makes no request when the next poll is no
                longer due once the poll lock is held, as for the poll task:
                a poll that held the lock meanwhile has moved the deadline.

        Returns:
            The statuses by device id; None when only_when_due skipped the
            request, with nothing recorded.

        Raises:
            MowerAPIError: The request failed; last_poll_error holds the
                exception and the backoff counts the failure.
            Exception: Whatever the provider raises; the same bookkeeping.
        """
        try:
            if provider and self._token_provider is not None:
                token = await self._token_provider()
                if token is not None:
                    await self.async_set_token(token)
            statuses = await self._poll(device_ids, only_when_due=only_when_due)
        except Exception as exc:
            self.last_poll_error = exc
            self._record_poll_failure()
            raise
        if statuses is None:
            return None
        self._record_poll_success()
        return statuses

    def _record_poll_success(self) -> None:
        """Set the next poll one interval from now and reset the backoff.

        Nothing happens in manual mode (poll_interval None).
        """
        if self._poll_interval is None:
            return
        self._poll_wait = self._poll_interval
        self._next_poll_due = time.monotonic() + self._poll_wait

    def _record_poll_failure(self) -> None:
        """Double the wait, up to the cap, and set the next poll that far from now.

        Nothing happens in manual mode (poll_interval None).
        """
        if self._poll_interval is None:
            return
        self._poll_wait = min(self._poll_wait * 2, self._poll_backoff_max)
        self._next_poll_due = time.monotonic() + self._poll_wait

    def _start_clock(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the poll task, unless poll_interval is None, and the silence task.

        Args:
            loop: The bound loop.
        """
        # Decision: the client runs the clock, so a consumer runs no timer of its
        # own: the poll task polls every poll_interval seconds with a doubling
        # backoff after failures that never goes below the interval, and the
        # silence task re-evaluates the merge every SILENCE_CHECK_SECONDS. Every
        # task is tracked and cancelled by async_disconnect(), so none outlives the
        # connection or runs while closed.
        if self._poll_interval is not None:
            self._spawn(loop, self._poll_task(), "poll")
        self._spawn(loop, self._silence_task(), "silence_check")

    def _spawn(self, loop: asyncio.AbstractEventLoop, coroutine: Any, name: str) -> None:
        """Run a coroutine as a tracked task on the loop.

        Args:
            loop: The bound loop.
            coroutine: The task's body.
            name: The task's name, after "NavimowClient ".
        """
        task = loop.create_task(coroutine, name=f"NavimowClient {name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cancel_tasks(self) -> None:
        """Cancel every tracked task and wait for each to end."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _poll_task(self) -> None:
        """The poll task: poll when the next poll is due, else sleep until it is."""
        while True:
            now = time.monotonic()
            due = self._next_poll_due
            if due is None:
                due = now + self._poll_wait
                self._next_poll_due = due
            if due > now:
                await _sleep(due - now)
                continue  # a poll made meanwhile may have moved the due time
            await self._clock_poll()

    async def _clock_poll(self) -> None:
        """One poll of the poll task: a failure is logged and reported as "poll", never raised."""
        # Decision: failures on the clock are logged with their traceback and
        # reported through on_error with the operation's name, and the task goes
        # on: nothing escapes a task, and a MowerAuthRequiredError here is the one
        # failure a consumer must act on, which it does through the provider or
        # async_set_token() while the client keeps polling with backoff.
        try:
            await self._scheduled_poll(None, provider=True, only_when_due=True)
        except Exception as exc:
            _LOGGER.exception("NavimowClient: the clock's poll failed")
            self._report("poll", exc)

    async def _silence_task(self) -> None:
        """The silence task: every SILENCE_CHECK_SECONDS, re-evaluate the merge for every device."""
        while True:
            await _sleep(SILENCE_CHECK_SECONDS)
            try:
                self._silence_tick()
            except Exception as exc:
                _LOGGER.exception("NavimowClient: the silence tick failed")
                self._report("silence_check", exc)

    def _silence_tick(self) -> None:
        """Rebuild the state of every device whose observation the rule now chooses differently."""
        # Decision: the tick re-evaluates the merge for every device with a state,
        # so a flip by time (an MQTT observation going stale with a newer REST one
        # stored) is seen within one tick; only a changed choice stores a new state
        # and fires on_state, else every tick would deliver every mower again.
        for device_id, current in list(self._states.items()):
            chosen = self._select(device_id)
            if chosen is None or chosen.is_source_of(current):
                continue
            self._rebuild(device_id, self._current_location(device_id))

    async def _poll(
        self, device_ids: Iterable[str] | None, *, only_when_due: bool = False
    ) -> dict[str, DeviceStatus] | None:
        """The request and the merge of a poll, under the poll lock; no token step.

        Args:
            device_ids: The devices to poll; None for every known device,
                fetched first when none are known.
            only_when_due: True makes no request when, once the lock is
                held, the next poll is not due yet.

        Returns:
            The statuses by device id; None when only_when_due skipped the
            request.

        Raises:
            MowerAPIError: The request failed; last_poll_error holds the
                exception and nothing changed.
        """
        # Decision: polls are serialised on a lock of the client's own and applied
        # in reply order, so a manual or command poll waits for one in flight and an
        # older reply never replaces a newer observation. The poll task checks its
        # deadline again once it holds the lock: a poll that held it meanwhile has
        # moved the deadline, and a request now would be a duplicate.
        async with self._poll_lock:
            if (
                only_when_due
                and self._next_poll_due is not None
                and self._next_poll_due > time.monotonic()
            ):
                return None
            try:
                if device_ids is None:
                    if not self._devices_known:
                        self.devices[:] = await self.api.async_get_devices()
                        self._devices_known = bool(self.devices)
                    ids = [device.id for device in self.devices]
                else:
                    ids = list(device_ids)
                statuses = await self.api.async_get_device_statuses(ids)
            except Exception as exc:
                self.last_poll_error = exc
                raise
            received_at = datetime.now(UTC)
            received_monotonic = time.monotonic()
            self.last_poll_at = received_at
            self.last_poll_error = None
            for device_id, status in statuses.items():
                observation = _RestObservation(status, received_at, received_monotonic)
                self._rest_observations[device_id] = observation
                if self._select(device_id) is observation:
                    self._rebuild(device_id, self._current_location(device_id))
            return statuses

    def _select(self, device_id: str) -> _Observation | None:
        """Choose the observation the status half comes from, by the merge rule.

        Args:
            device_id: The device.

        Returns:
            The REST observation when there is no MQTT one; the MQTT
            observation while it is younger than mqtt_stale_seconds; the REST
            observation when the MQTT one is at least that old and the REST
            one was received after it; else the MQTT observation; None with
            neither.
        """
        mqtt = self._mqtt_observations.get(device_id)
        rest = self._rest_observations.get(device_id)
        if mqtt is None:
            return rest
        # Decision: the status half prefers a fresh MQTT observation, which the
        # cloud's REST status lags by one to two minutes, and yields to a REST
        # observation received after the MQTT one went stale; an older REST
        # observation never replaces a newer MQTT one, whatever the age.
        if time.monotonic() - mqtt.received_monotonic < self._mqtt_stale_seconds:
            return mqtt
        if rest is not None and rest.received_monotonic > mqtt.received_monotonic:
            return rest
        return mqtt

    def _current_location(self, device_id: str) -> DeviceLocation | None:
        """The facade's location record for a device.

        Args:
            device_id: The device.

        Returns:
            The decoder's record, or None before the facade exists or before
            any entry or restore.
        """
        return None if self._sdk is None else self._sdk.get_cached_location(device_id)

    def _rebuild(self, device_id: str, location: DeviceLocation | None) -> None:
        """Build the device's state from the selected observation, store it and deliver it.

        Nothing happens while the device has no observation.

        Args:
            device_id: The device.
            location: The location half to carry.
        """
        # Decision: the state is rebuilt at a state message, an applied location
        # entry, a poll whose REST observation the rule selects, a silence tick
        # at which the rule's choice changed, and a restore;
        # each rebuild stores a new state and fires on_state, so a consumer that
        # wants only the status half compares those fields.
        observation = self._select(device_id)
        if observation is None:
            return
        state = observation.build(location)
        self._states[device_id] = state
        self._deliver_state(state)

    def _deliver_state(self, state: MowerState) -> None:
        """Call the on_state callbacks whose filter admits the state, each in its own try.

        Args:
            state: The new state.
        """
        for callback, device_id in list(self._state_callbacks):
            if device_id is not None and device_id != state.device_id:
                continue
            try:
                callback(state)
            except Exception:
                _LOGGER.exception(
                    "NavimowClient state callback %r failed for device %s",
                    callback,
                    state.device_id,
                )

    def _on_state_message(self, message: DeviceStateMessage) -> None:
        """The client's handler on the facade's on_state: a new MQTT observation, a new state.

        Args:
            message: The state message the facade applied.
        """
        self._mqtt_observations[message.device_id] = _MqttObservation(message, time.monotonic())
        self._rebuild(message.device_id, self._current_location(message.device_id))

    def _on_location_entry(self, entry: DeviceLocationMessage) -> None:
        """The client's handler on the facade's on_location: a new state with the entry's record.

        Args:
            entry: The applied location entry, with the record as it stood
                after it.
        """
        # Decision: a location entry makes a new state once a status half exists,
        # and its location half is the entry's own snapshot, not the decoder's
        # cache: the decoder caches only a message's final record before the facade
        # dispatches the entries, so a multi-entry message gives one state per entry
        # with its intermediate record. While the mower is out this is a new state
        # about every two seconds, one per pose. An entry before any observation
        # updates the record and makes no state.
        if self._select(entry.device_id) is None:
            return
        self._rebuild(entry.device_id, entry.location)

    def state(self, device_id: str) -> MowerState | None:
        """The device's state as last built.

        Args:
            device_id: The device.

        Returns:
            The MowerState, or None before any observation of the device.
        """
        return self._states.get(device_id)

    def states(self) -> dict[str, MowerState]:
        """Every device with a state.

        Returns:
            A new dict from device id to its state as last built.
        """
        return dict(self._states)

    def restore_location(self, device_id: str, location: DeviceLocation) -> None:
        """Install a location record persisted earlier (DeviceLocation.to_dict / from_dict).

        Before the facade exists the record is queued and installed by the
        build; a state built before then has no location half. Once the
        facade exists the record replaces the decoder's, and a device that
        has a state gets a new one carrying it. Call it before
        async_connect(), so a late entry older than what was applied before a
        restart is rejected as stale.

        Args:
            device_id: The device the record is of.
            location: The record, rebuilt with DeviceLocation.from_dict.
        """
        if self._sdk is None:
            self._queued_restores[device_id] = location
            return
        self._sdk.restore_location(device_id, location)
        if device_id in self._states:
            self._rebuild(device_id, self._sdk.get_cached_location(device_id))

    # ---- callbacks -------------------------------------------------------------------------

    def on_state(
        self, callback: Callable[[MowerState], None], *, device_id: str | None = None
    ) -> None:
        """Call callback with each new state.

        A state is new after each state message the facade applies, each
        applied location entry, each poll whose REST observation becomes or
        stays current, and each restore_location() once the facade exists.

        Args:
            callback: A synchronous function taking the MowerState.
            device_id: Deliver only this device's states; None, the default,
                delivers every device's. An id no device has delivers nothing.
        """
        self._state_callbacks.append((callback, device_id))

    def on_connection(self, callback: Callable[[ConnectionEvent], None]) -> None:
        """Call callback with each ConnectionEvent of the MQTT client.

        An event is a connect, a disconnect or a connect failure (its kind
        says which). The client sets the MQTT client's on_connection_event hook for this
        at build; a consumer that replaces that hook on client.mqtt disables
        these callbacks.

        Args:
            callback: A synchronous function taking the ConnectionEvent.
        """
        self._connection_callbacks.append(callback)

    def on_error(self, callback: Callable[[str, Exception], None]) -> None:
        """Call callback(operation, exception) for each failure on the client's own schedule.

        The operations are "poll" for the poll inside async_connect() and
        the poll task's polls, "silence_check" for the silence tick and
        "recovery" for a failed recovery after a refused connection. A
        MowerAuthRequiredError here is the one failure a consumer must act
        on: only it can re-authenticate, through async_set_token() or the
        provider.

        Args:
            callback: A synchronous function taking the operation's name and
                the exception.
        """
        self._error_callbacks.append(callback)

    def on_event(
        self, callback: Callable[[DeviceEventMessage], None], *, device_id: str | None = None
    ) -> None:
        """Call callback with each event message, as the facade's on_event does.

        Args:
            callback: A synchronous function taking the DeviceEventMessage.
            device_id: Deliver only this device's messages; None delivers
                every device's.
        """
        self._forward("event", callback, device_id)

    def on_attributes(
        self,
        callback: Callable[[DeviceAttributesMessage], None],
        *,
        device_id: str | None = None,
    ) -> None:
        """Call callback with each attributes message, as the facade's on_attributes does.

        Args:
            callback: A synchronous function taking the
                DeviceAttributesMessage.
            device_id: Deliver only this device's messages; None delivers
                every device's.
        """
        self._forward("attributes", callback, device_id)

    def on_rejected(
        self, callback: Callable[[RejectedMessage], None], *, device_id: str | None = None
    ) -> None:
        """Call callback with each message not applied cleanly, as the facade's on_rejected does.

        Args:
            callback: A synchronous function taking the RejectedMessage.
            device_id: Deliver only this device's messages; None delivers
                every device's.
        """
        self._forward("rejected", callback, device_id)

    def _forward(
        self, channel: str, callback: Callable[[Any], None], device_id: str | None
    ) -> None:
        """Register a consumer's callback with the facade, wrapped in the client's device filter.

        Before the facade exists the wrapper is queued and registered by the
        build.

        Args:
            channel: "event", "attributes" or "rejected".
            callback: The consumer's callback.
            device_id: The filter; None admits every device.
        """

        # Decision: the client forwards on_event, on_attributes and on_rejected, the
        # three feeds it does not consume, and not the facade's on_state,
        # on_location, on_raw or on_message_seen: the first two are what the state
        # is built from and the others are diagnostics, all reachable on client.sdk
        # after async_connect(). The device filter is the client's, applied in this
        # wrapper, so the facade's callbacks and their signatures are unchanged.
        def filtered(message: Any) -> None:
            if device_id is None or message.device_id == device_id:
                callback(message)

        if self._sdk is None:
            self._queued_forwards.append((channel, filtered))
        else:
            self._register_forward(self._sdk, channel, filtered)

    @staticmethod
    def _register_forward(sdk: NavimowSDK, channel: str, callback: Callable[[Any], None]) -> None:
        """Register a wrapped callback with the facade's method for its channel.

        Args:
            sdk: The facade.
            channel: "event", "attributes" or "rejected".
            callback: The wrapped callback.
        """
        if channel == "event":
            sdk.on_event(callback)
        elif channel == "attributes":
            sdk.on_attributes(callback)
        else:
            sdk.on_rejected(callback)

    def _report(self, operation: str, exc: Exception) -> None:
        """Call the on_error callbacks with a failure, each in its own try.

        Args:
            operation: The operation's name.
            exc: The exception.
        """
        for callback in list(self._error_callbacks):
            try:
                callback(operation, exc)
            except Exception:
                _LOGGER.exception(
                    "NavimowClient error callback %r failed for %s", callback, operation
                )

    # ---- the inner hooks -------------------------------------------------------------------

    async def _on_connection_event(self, event: ConnectionEvent) -> None:
        """The MQTT client's on_connection_event hook: deliver the event to on_connection.

        Args:
            event: The connection event.
        """
        for callback in list(self._connection_callbacks):
            try:
                callback(event)
            except Exception:
                _LOGGER.exception(
                    "NavimowClient connection callback %r failed for %s", callback, event.kind
                )

    async def _on_connect_fail(self, reason: str) -> None:
        """The MQTT client's on_connect_fail hook: fetch the broker credentials again.

        Args:
            reason: Why the connect failed, as the MQTT client reports it.
        """
        # Decision: recovery runs after a refused connection only, never on a plain
        # disconnect, which paho reconnects by itself with the stored values. It is
        # skipped while the client is closed or while the lifecycle lock is held:
        # paho keeps retrying, and the next refusal comes back here. The provider is
        # asked first, since the broker credentials are bound to the token, and the
        # facade's cooldown makes repeated refusals cost one request a minute.
        # Failures are logged and reported through on_error as "recovery", never
        # raised: the hook runs as a task of its own with nobody to catch them.
        if (
            not self._recover_on_connect_fail
            or self._sdk is None
            or self._closed
            or self._lock.locked()
        ):
            return
        _LOGGER.info(
            "NavimowClient: connection refused (%s), refreshing the broker credentials", reason
        )
        try:
            await self.async_refresh_broker_credentials()
        except MowerAPIError as exc:
            _LOGGER.warning("NavimowClient recovery after a refused connection failed: %s", exc)
            self._report("recovery", exc)
        except Exception as exc:
            _LOGGER.exception("NavimowClient recovery after a refused connection failed")
            self._report("recovery", exc)

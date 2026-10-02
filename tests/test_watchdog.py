"""MqttWatchdog: the missed-state rule after a REST poll, the location-silence rule, the debounce.

A real NavimowSDK runs on a recording fake paho client, so the watchdog reads
the facade's own caches and the client's own connection and message times.
One fake clock stands in for time.monotonic() and datetime.now() in the
client, the facade and the watchdog together, so their ages agree.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import mower_sdk
from mower_sdk import mqtt as mqtt_module
from mower_sdk import sdk as sdk_module
from mower_sdk import watchdog as watchdog_module
from mower_sdk.models import DeviceLocation, MowerStatus
from mower_sdk.sdk import NavimowSDK
from mower_sdk.watchdog import (
    IGNORED_REST_STATES,
    LOCATION_SILENCE_SECONDS,
    MOVING_STATES,
    REST_CACHE_LAG_SECONDS,
    WATCHDOG_DEBOUNCE_SECONDS,
    MqttWatchdog,
    RebuildRequest,
    WatchInput,
)

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DEV = "dev-1"
OTHER = "dev-2"


class FakeClock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = T0

    def monotonic(self) -> float:
        return self.mono

    def now(self, tz: Any = None) -> datetime:
        assert tz is UTC
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.connected = False

    def __getattr__(self, name: str) -> Any:
        return lambda *_args, **_kwargs: None

    def subscribe(self, *_args: Any, **_kwargs: Any) -> tuple[int, int]:
        return 0, 1  # paho's (result, message id)

    def is_connected(self) -> bool:
        return self.connected


class _Success:
    value = 0
    is_failure = False


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakeClient)
    for module in (mqtt_module, sdk_module):
        monkeypatch.setattr(module, "time", fake)
        monkeypatch.setattr(module, "datetime", fake)
    monkeypatch.setattr(watchdog_module, "time", fake)
    return fake


@pytest.fixture
def sdk(clock: FakeClock) -> NavimowSDK:  # noqa: ARG001 - the clock must be patched first
    return NavimowSDK(
        broker="broker.example.invalid", port=443, ws_path="/mqtt", reject_late_state=True, subscribe_location=True
    )


def connect(sdk: NavimowSDK) -> None:
    sdk.mqtt._on_connect(sdk.mqtt.client, None, {}, _Success(), None)
    sdk.mqtt.client.connected = True


class Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


def arrive(sdk: NavimowSDK, channel: str, payload: bytes, device_id: str) -> str:
    """The client's bookkeeping for a message (its time, whatever the payload); returns the topic.

    The facade's handler is taken off the client for the call, since with no
    loop running the client would drop what it schedules.
    """
    topic = f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
    handler, sdk.mqtt.on_message = sdk.mqtt.on_message, None
    try:
        sdk.mqtt._on_message(sdk.mqtt.client, None, Message(topic, payload))
    finally:
        sdk.mqtt.on_message = handler
    return topic


async def state(sdk: NavimowSDK, value: str, device_id: str = DEV, timestamp: int | None = None) -> None:
    """A state message: through the client's bookkeeping, then the facade's handler."""
    payload: dict[str, Any] = {"state": value}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    encoded = json.dumps(payload).encode()
    topic = arrive(sdk, "state", encoded, device_id)
    await sdk._on_mqtt_message(topic, encoded, device_id)


def location_message(sdk: NavimowSDK, payload: bytes = b"[]", device_id: str = DEV) -> None:
    """A location message as the client sees it: the time is recorded whatever the payload."""
    arrive(sdk, "location", payload, device_id)


def watched(
    rest: str | MowerStatus | None,
    rest_at: float | None,
    shown: str | MowerStatus | None = None,
    device_id: str = DEV,
) -> WatchInput:
    return WatchInput(device_id=device_id, name=f"Mower {device_id}", shown_state=shown, rest_state=rest, rest_observed_at=rest_at)


def test_the_defaults_are_the_documented_values() -> None:
    assert (REST_CACHE_LAG_SECONDS, LOCATION_SILENCE_SECONDS, WATCHDOG_DEBOUNCE_SECONDS) == (120, 180, 300)
    assert {"unknown", "offline", "updating"} == IGNORED_REST_STATES
    assert {"mowing", "returning"} == MOVING_STATES


def test_the_watchdog_is_exported_from_the_package() -> None:
    for name in ("MqttWatchdog", "RebuildRequest", "WatchInput"):
        assert getattr(mower_sdk, name) is getattr(watchdog_module, name)
        assert name in mower_sdk.__all__


@pytest.mark.parametrize("name", ["rest_cache_lag", "location_silence", "debounce"])
def test_a_negative_threshold_is_refused(sdk: NavimowSDK, name: str) -> None:
    with pytest.raises(ValueError, match=name):
        MqttWatchdog(sdk, **{name: -1})


# ---- rule 1: after a REST poll -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_disagreeing_with_an_old_enough_mqtt_report_asks_for_a_rebuild(
    sdk: NavimowSDK, clock: FakeClock
) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(119)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None  # REST may not have caught up
    clock.advance(1)
    request = dog.after_poll([watched(MowerStatus.MOWING, clock.mono)])
    assert request == RebuildRequest(
        reason="missed a state change (REST says mowing but MQTT last said docked for Mower dev-1)",
        device_ids=(DEV,),
        reports=((DEV, T0),),
    )


@pytest.mark.asyncio
async def test_an_old_rest_reading_does_not_count_as_current(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    reading_taken = clock.mono + 60  # 60 s after the report, whatever the time now
    clock.advance(600)
    assert dog.after_poll([watched("mowing", reading_taken)]) is None
    assert dog.after_poll([watched("mowing", None)]) is None
    # A reading from the future counts as taken now.
    assert dog.after_poll([watched("mowing", clock.mono + 1000)]) is not None


@pytest.mark.asyncio
async def test_a_future_reading_counts_as_taken_now_not_later(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(60)  # the report is younger than the REST cache lag
    assert dog.after_poll([watched("mowing", clock.mono + 1000)]) is None
    clock.advance(60)
    assert dog.after_poll([watched("mowing", clock.mono + 1000)]) is not None


@pytest.mark.parametrize("rest", sorted(IGNORED_REST_STATES))
@pytest.mark.asyncio
async def test_a_rest_state_the_state_channel_never_reports_is_ignored(
    sdk: NavimowSDK, clock: FakeClock, rest: str
) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(600)
    assert dog.after_poll([watched(rest, clock.mono)]) is None


@pytest.mark.asyncio
async def test_nothing_is_asked_without_an_mqtt_report_or_a_rest_state(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    clock.advance(600)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None  # no state message yet
    await state(sdk, "isDocked")
    clock.advance(600)
    assert dog.after_poll([watched(None, clock.mono)]) is None


@pytest.mark.asyncio
async def test_nothing_is_asked_before_the_first_connect(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    await state(sdk, "isDocked")
    clock.advance(600)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None
    connect(sdk)
    assert dog.after_poll([watched("mowing", clock.mono)]) is not None


@pytest.mark.asyncio
async def test_a_report_is_acted_on_once_and_agreement_re_arms_the_device(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk, debounce=0)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(600)
    request = dog.after_poll([watched("offline", clock.mono), watched("mowing", clock.mono)])
    assert request is not None and request.reports == ((DEV, T0),)
    dog.acknowledge(request)
    clock.advance(600)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None  # the same report: acted on
    # REST agrees with the same report: the device is re-armed, and the same report
    # can ask again when REST disagrees later.
    assert dog.after_poll([watched("docked", clock.mono)]) is None
    clock.advance(1)
    again = dog.after_poll([watched("mowing", clock.mono)])
    assert again is not None and again.reports == ((DEV, T0),)


@pytest.mark.asyncio
async def test_a_new_report_that_disagrees_asks_again(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk, debounce=0)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(600)
    request = dog.after_poll([watched("mowing", clock.mono)])
    assert request is not None
    dog.acknowledge(request)
    await state(sdk, "isPaused")  # a new report, still not REST's
    clock.advance(600)
    again = dog.after_poll([watched("mowing", clock.mono)])
    assert again is not None and again.reports == ((DEV, T0 + timedelta(seconds=600)),)


@pytest.mark.asyncio
async def test_a_declined_request_leaves_the_mismatch_live(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(600)
    first = dog.after_poll([watched("mowing", clock.mono)])
    clock.advance(120)
    assert dog.after_poll([watched("mowing", clock.mono)]) == first  # not acknowledged: asked again


@pytest.mark.asyncio
async def test_the_debounce_starts_on_acknowledge_and_a_mismatch_inside_it_stays_live(
    sdk: NavimowSDK, clock: FakeClock
) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    await state(sdk, "isDocked", device_id=OTHER)
    await state(sdk, "isDocked")
    clock.advance(600)
    request = dog.after_poll([watched("mowing", clock.mono, device_id=OTHER)])
    assert request is not None and request.device_ids == (OTHER,)
    dog.acknowledge(request)
    clock.advance(299)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None  # debounced, not marked
    clock.advance(1)
    later = dog.after_poll([watched("mowing", clock.mono)])
    assert later is not None and later.device_ids == (DEV,)


@pytest.mark.asyncio
async def test_every_mismatched_device_is_in_the_request_and_marked_on_acknowledge(
    sdk: NavimowSDK, clock: FakeClock
) -> None:
    dog = MqttWatchdog(sdk, debounce=0)
    connect(sdk)
    await state(sdk, "isDocked")
    await state(sdk, "isRunning", device_id=OTHER)
    clock.advance(600)
    request = dog.after_poll([watched("mowing", clock.mono), watched("docked", clock.mono, device_id=OTHER)])
    assert request is not None and request.device_ids == (DEV, OTHER)
    dog.acknowledge(request)
    assert dog.after_poll([watched("mowing", clock.mono), watched("docked", clock.mono, device_id=OTHER)]) is None


@pytest.mark.asyncio
async def test_a_rejected_late_state_does_not_refresh_the_report(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    stamp = int(T0.timestamp() * 1000)
    await state(sdk, "isDocked", timestamp=stamp)
    clock.advance(600)
    await state(sdk, "isRunning", timestamp=stamp - 1000)  # older than the accepted one: rejected as stale
    assert sdk.mqtt.last_message_age(DEV, "state") == 0  # the raw traffic moved
    assert sdk.get_cached_state_age(DEV) == 600  # the accepted state did not
    request = dog.after_poll([watched("mowing", clock.mono)])  # the accepted report is 600 s old
    assert request is not None and "MQTT last said docked" in request.reason


# ---- rule 2: location silence ------------------------------------------------------------------


def with_pose(sdk: NavimowSDK, device_id: str = DEV) -> None:
    sdk.restore_location(device_id, DeviceLocation(device_id=device_id, x=1.0, y=2.0, pose_at=1))


def test_a_moving_mower_with_no_location_message_since_the_connect_asks_for_a_rebuild(
    sdk: NavimowSDK, clock: FakeClock
) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    connect(sdk)
    clock.advance(179)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    clock.advance(1)
    assert dog.check_silence([watched(None, None, shown="mowing")]) == RebuildRequest(
        reason="no location message for 180 s while Mower dev-1 runs", device_ids=(DEV,)
    )


def test_any_location_traffic_counts_even_a_payload_that_is_not_applied(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    connect(sdk)
    clock.advance(170)
    location_message(sdk, b"not json")
    clock.advance(170)
    assert dog.check_silence([watched(None, None, shown="returning")]) is None
    clock.advance(10)
    request = dog.check_silence([watched(None, None, shown="returning")])
    assert request is not None and "for 180 s" in request.reason


@pytest.mark.parametrize(
    ("shown", "rest", "asks"),
    [
        ("mowing", None, True),
        ("returning", None, True),
        (MowerStatus.MOWING, None, True),
        ("docked", "mowing", True),  # REST's moving state counts
        (None, MowerStatus.RETURNING, True),
        ("docked", "docked", False),
        ("paused", "idle", False),
        ("mapping", None, False),
        ("mapping", "mowing", False),  # a shown mapping is never moving, whatever REST says
        (MowerStatus.MAPPING, "returning", False),
    ],
)
def test_only_a_moving_mower_is_watched_and_mapping_never_counts(
    sdk: NavimowSDK, clock: FakeClock, shown: Any, rest: Any, asks: bool
) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    connect(sdk)
    clock.advance(600)
    assert (dog.check_silence([watched(rest, clock.mono, shown=shown)]) is not None) is asks


def test_without_the_location_channel_subscribed_silence_is_expected(clock: FakeClock) -> None:
    sdk = NavimowSDK(broker="broker.example.invalid", port=443, ws_path="/mqtt")  # subscribe_location=False
    dog = MqttWatchdog(sdk)
    with_pose(sdk)  # a pose restored from an earlier run
    connect(sdk)
    clock.advance(600)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None


def test_a_mower_without_a_timestamped_pose_is_not_watched(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    connect(sdk)
    clock.advance(600)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    sdk.restore_location(DEV, DeviceLocation(device_id=DEV, x=1.0, y=2.0))  # a pose without a time
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    with_pose(sdk)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is not None


def test_nothing_is_asked_while_the_client_is_not_connected(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    clock.advance(600)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None  # never connected
    connect(sdk)
    sdk.mqtt.client.connected = False  # connected once, down now
    clock.advance(600)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None


def test_the_quiet_time_starts_at_the_latest_connect(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    connect(sdk)
    location_message(sdk)
    clock.advance(1000)
    connect(sdk)  # reconnected: nothing can have arrived on the new link yet
    clock.advance(179)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    clock.advance(1)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is not None


@pytest.mark.asyncio
async def test_the_silence_rule_is_debounced_like_the_other(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk)
    with_pose(sdk)
    connect(sdk)
    clock.advance(600)
    request = dog.check_silence([watched(None, None, shown="mowing")])
    assert request is not None
    assert dog.check_silence([watched(None, None, shown="mowing")]) == request  # not acknowledged
    dog.acknowledge(request)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    await state(sdk, "isDocked")
    clock.advance(299)
    assert dog.after_poll([watched("mowing", clock.mono)]) is None  # the window covers both rules
    clock.advance(1)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is not None


@pytest.mark.asyncio
async def test_custom_thresholds(sdk: NavimowSDK, clock: FakeClock) -> None:
    dog = MqttWatchdog(sdk, rest_cache_lag=10, location_silence=20, debounce=30)
    with_pose(sdk)
    connect(sdk)
    await state(sdk, "isDocked")
    clock.advance(10)
    request = dog.after_poll([watched("mowing", clock.mono)])
    assert request is not None
    dog.acknowledge(request)
    clock.advance(29)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is None
    clock.advance(1)
    assert dog.check_silence([watched(None, None, shown="mowing")]) is not None

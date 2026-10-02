"""NavimowSDK's state checks: the opt-in late-state filter, unknown fields, malformed payloads, receipt times.

The shared fake stands in for NavimowMQTT, the shared clock replaces time and
datetime, and _on_mqtt_message is driven directly. Mower timestamps are
milliseconds around T, the fake receipt time.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from mower_sdk.models import (
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceStateMessage,
    RejectedMessage,
)
from mower_sdk.sdk import NavimowSDK

from .fakes import DEVICE_ID, T0, FakeClock, topic

T = int(T0.timestamp() * 1000)


pytestmark = pytest.mark.usefixtures("fake_mqtt", "clock")


class Recorder:
    def __init__(self, sdk: NavimowSDK) -> None:
        self.states: list[DeviceStateMessage] = []
        self.events: list[DeviceEventMessage] = []
        self.attributes: list[DeviceAttributesMessage] = []
        self.rejected: list[RejectedMessage] = []
        sdk.on_state(self.states.append)
        sdk.on_event(self.events.append)
        sdk.on_attributes(self.attributes.append)
        sdk.on_rejected(self.rejected.append)


def make(**options: Any) -> tuple[NavimowSDK, Recorder]:
    sdk = NavimowSDK(broker="broker.example.invalid", port=443, **options)
    return sdk, Recorder(sdk)


async def deliver(sdk: NavimowSDK, channel: str, payload: Any) -> bytes:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    await sdk._on_mqtt_message(topic(channel), data, DEVICE_ID)
    return data


def state(timestamp: Any = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": "isRunning", "battery": 80, **extra}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return payload


@pytest.mark.asyncio
async def test_the_option_is_off_by_default_and_not_forwarded_to_the_client() -> None:
    sdk, seen = make()
    assert "reject_late_state" not in sdk.mqtt.kwargs
    await deliver(sdk, "state", state(T))
    await deliver(sdk, "state", state(T - 60_000))  # older: applied anyway with the filter off
    await deliver(sdk, "state", state(5))  # 1970: applied anyway
    assert [m.timestamp for m in seen.states] == [T, T - 60_000, 5]
    assert seen.rejected == []
    assert sdk._state_marks == {}


@pytest.mark.asyncio
async def test_accepted_messages_advance_the_mark_and_an_older_one_is_stale() -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T - 60_000))
    assert sdk._state_marks == {DEVICE_ID: T - 60_000}
    await deliver(sdk, "state", state(T - 30_000))
    await deliver(sdk, "state", state(T - 30_000))  # equal to the mark: applied
    assert sdk._state_marks == {DEVICE_ID: T - 30_000}
    data = await deliver(sdk, "state", state(T - 45_000, state="isDocked"))
    assert [m.timestamp for m in seen.states] == [T - 60_000, T - 30_000, T - 30_000]
    assert sdk.get_cached_state(DEVICE_ID).timestamp == T - 30_000
    (rejection,) = seen.rejected
    assert (rejection.channel, rejection.reason, rejection.reasons, rejection.payload) == (
        "state",
        "stale",
        ("stale",),
        data,
    )
    assert rejection.received_at == T0


@pytest.mark.asyncio
async def test_a_timestamp_in_seconds_is_compared_in_milliseconds() -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T // 1000))
    await deliver(sdk, "state", state(T - 10_000))
    assert [r.reason for r in seen.rejected] == ["stale"]
    assert sdk._state_marks == {DEVICE_ID: (T // 1000) * 1000}


@pytest.mark.parametrize(
    "timestamp", [5, 1_500_000_000_000, T + 5 * 60 * 1000 + 1], ids=["1970", "2017", "ahead"]
)
@pytest.mark.asyncio
async def test_an_implausible_timestamp_is_rejected_and_leaves_the_mark(timestamp: int) -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T - 10_000))
    await deliver(sdk, "state", state(timestamp))
    assert [r.reason for r in seen.rejected] == ["implausible_time"]
    assert len(seen.states) == 1
    assert sdk._state_marks == {DEVICE_ID: T - 10_000}


@pytest.mark.asyncio
async def test_a_message_without_a_timestamp_is_applied_and_leaves_the_mark() -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T - 10_000))
    await deliver(sdk, "state", state(None, state="isDocked"))
    await deliver(sdk, "state", state("soon"))
    assert [m.state for m in seen.states] == ["mowing", "docked", "mowing"]
    assert seen.rejected == []
    assert sdk._state_marks == {DEVICE_ID: T - 10_000}


@pytest.mark.asyncio
async def test_marks_are_per_device() -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T))
    await sdk._on_mqtt_message(
        "/downlink/vehicle/dev-2/realtimeDate/state",
        json.dumps(state(T - 60_000)).encode(),
        "dev-2",
    )
    assert seen.rejected == []
    assert sdk._state_marks == {DEVICE_ID: T, "dev-2": T - 60_000}


@pytest.mark.asyncio
async def test_an_unknown_field_is_applied_and_reported_and_never_blocks() -> None:
    sdk, seen = make()
    await deliver(sdk, "state", state(T, speed=1))
    assert len(seen.states) == 1
    assert [(r.reason, r.reasons) for r in seen.rejected] == [("unknown_field", ("unknown_field",))]


@pytest.mark.asyncio
async def test_a_stale_message_with_an_unknown_field_earns_both_is_not_applied_and_names_stale() -> (
    None
):
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T))
    await deliver(sdk, "state", state(T - 1000, speed=1))
    await deliver(sdk, "state", state(5, speed=1))
    assert len(seen.states) == 1
    assert [(r.reason, r.reasons) for r in seen.rejected] == [
        ("stale", ("unknown_field", "stale")),
        ("implausible_time", ("implausible_time", "unknown_field")),
    ]


@pytest.mark.asyncio
async def test_the_known_state_fields_earn_no_reason() -> None:
    sdk, seen = make()
    await deliver(
        sdk,
        "state",
        {
            "state": "isDocked",
            "vehicleState": "isDocked",
            "status": "x",
            "battery": 1,
            "capacityRemaining": [],
            "timestamp": T,
            "device_id": DEVICE_ID,
        },
    )
    assert seen.rejected == []


@pytest.mark.parametrize("channel", ["state", "event", "attributes"])
@pytest.mark.parametrize("payload", [b"not json", b"[1]"], ids=["not_json", "array"])
@pytest.mark.asyncio
async def test_a_malformed_payload_on_each_channel_is_reported_with_the_filter_off(
    channel: str, payload: bytes
) -> None:
    sdk, seen = make()
    await deliver(sdk, channel, payload)
    assert (seen.states, seen.events, seen.attributes) == ([], [], [])
    assert sdk.get_cached_state(DEVICE_ID) is None and sdk.get_cached_attributes(DEVICE_ID) is None
    assert [(r.channel, r.topic, r.reason, r.payload) for r in seen.rejected] == [
        (channel, topic(channel), "unparsable", payload)
    ]


@pytest.mark.parametrize("metrics", [1, "fast", [1, 2]], ids=["number", "string", "list"])
@pytest.mark.asyncio
async def test_a_state_whose_fields_cannot_be_read_is_reported_as_unparsable(metrics: Any) -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T - 10_000))  # the mark
    data = await deliver(sdk, "state", state(T, metrics=metrics))
    assert [m.timestamp for m in seen.states] == [T - 10_000]
    assert sdk.get_cached_state(DEVICE_ID).timestamp == T - 10_000
    assert [(r.channel, r.device_id, r.reason, r.reasons, r.payload) for r in seen.rejected] == [
        ("state", DEVICE_ID, "unparsable", ("unparsable", "unknown_field"), data)
    ]
    assert sdk._state_marks == {DEVICE_ID: T - 10_000}  # not advanced to T
    await deliver(sdk, "state", state(T - 5000))  # older than the unparsable one: still applies
    assert [m.timestamp for m in seen.states] == [T - 10_000, T - 5000]


@pytest.mark.asyncio
async def test_a_rejected_state_leaves_the_cache_and_its_times_untouched(clock: FakeClock) -> None:
    sdk, seen = make(reject_late_state=True)
    await deliver(sdk, "state", state(T))
    clock.monotonic_now += 10
    clock.wall_now += timedelta(seconds=10)
    await deliver(sdk, "state", state(T - 1000, state="isDocked"))
    assert sdk.get_cached_state(DEVICE_ID).state == "mowing"
    assert sdk.get_cached_state_age(DEVICE_ID) == 10.0
    assert sdk.get_cached_state_received_at(DEVICE_ID) == T0
    assert seen.rejected[0].received_at == T0 + timedelta(seconds=10)


@pytest.mark.asyncio
async def test_every_delivered_message_carries_its_receipt_time_outside_equality(
    clock: FakeClock,
) -> None:
    sdk, seen = make()
    await deliver(sdk, "state", state(T))
    clock.wall_now += timedelta(seconds=1)
    await deliver(sdk, "event", {"type": "system", "event": "started"})
    clock.wall_now += timedelta(seconds=1)
    await deliver(sdk, "attributes", {"attributes": {"a": 1}})
    assert [
        seen.states[0].received_at,
        seen.events[0].received_at,
        seen.attributes[0].received_at,
    ] == [T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)]
    assert seen.events[0] == DeviceEventMessage(
        device_id=DEVICE_ID, timestamp=None, type="system", event="started"
    )
    for message in (seen.states[0], seen.events[0], seen.attributes[0]):
        assert "received_at" not in message.to_dict()
    assert seen.attributes[0] == DeviceAttributesMessage(device_id=DEVICE_ID, attributes={"a": 1})
    assert (
        DeviceStateMessage(device_id=DEVICE_ID, timestamp=None, state="docked").received_at is None
    )
    assert (
        DeviceEventMessage(
            device_id=DEVICE_ID, timestamp=None, type="system", event="x"
        ).received_at
        is None
    )
    assert DeviceAttributesMessage(device_id=DEVICE_ID, attributes={}).received_at is None

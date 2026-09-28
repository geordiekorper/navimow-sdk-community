"""Tests for NavimowSDK's cache bookkeeping: ages and receipt times.

A minimal fake stands in for ``NavimowMQTT`` (the facade only sets its
``on_message``), and a fake clock is patched into the sdk module's namespace in
place of ``time`` and ``datetime``, so the ages are exact. ``_on_mqtt_message``
is driven directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mower_sdk import sdk as sdk_module
from mower_sdk.sdk import NavimowSDK

DEVICE_ID = "dev-1"
OTHER_ID = "dev-2"
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class FakeMQTT:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.on_message: Any = None


class FakeClock:
    """``monotonic()`` and ``now(tz)`` read from settable values."""

    def __init__(self) -> None:
        self.monotonic_now = 100.0
        self.wall_now = T0

    def monotonic(self) -> float:
        return self.monotonic_now

    def now(self, tz: Any) -> datetime:
        assert tz is UTC
        return self.wall_now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(sdk_module, "NavimowMQTT", FakeMQTT)
    monkeypatch.setattr(sdk_module, "time", fake)
    monkeypatch.setattr(sdk_module, "datetime", fake)
    return fake


def run(test: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(test())


def topic(device_id: str, channel: str) -> str:
    return f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"


async def deliver(sdk: NavimowSDK, device_id: str, channel: str, payload: bytes) -> None:
    await sdk._on_mqtt_message(topic(device_id, channel), payload, device_id)


def ages(sdk: NavimowSDK, device_id: str) -> tuple[float | None, float | None, datetime | None]:
    return (
        sdk.get_cached_state_age(device_id),
        sdk.get_cached_attributes_age(device_id),
        sdk.get_cached_state_received_at(device_id),
    )


def test_no_cached_message_means_no_age_and_no_receipt_time(clock: FakeClock) -> None:
    async def test() -> None:
        sdk = NavimowSDK(broker="broker.example.invalid", port=443)
        assert ages(sdk, DEVICE_ID) == (None, None, None)
        await deliver(sdk, DEVICE_ID, "event", b'{"type": "system", "event": "started"}')
        clock.monotonic_now += 60
        assert ages(sdk, DEVICE_ID) == (None, None, None)  # events are not cached

    run(test)


def test_state_and_attributes_ages_are_measured_from_their_own_arrival(clock: FakeClock) -> None:
    async def test() -> None:
        sdk = NavimowSDK(broker="broker.example.invalid", port=443)

        await deliver(sdk, DEVICE_ID, "state", b'{"state": "isRunning", "battery": 80}')
        assert ages(sdk, DEVICE_ID) == (0.0, None, T0)
        assert sdk.get_cached_state_received_at(DEVICE_ID).tzinfo is UTC

        clock.monotonic_now += 30.5
        clock.wall_now += timedelta(seconds=30.5)
        assert ages(sdk, DEVICE_ID) == (30.5, None, T0)

        await deliver(sdk, DEVICE_ID, "attributes", b'{"attributes": {"a": 1}}')
        assert ages(sdk, DEVICE_ID) == (30.5, 0.0, T0)

        clock.monotonic_now += 1.25
        assert ages(sdk, DEVICE_ID) == (31.75, 1.25, T0)
        assert sdk.get_cached_state(DEVICE_ID).battery == 80
        assert sdk.get_cached_attributes(DEVICE_ID).attributes == {"a": 1}

    run(test)


def test_a_newer_message_replaces_the_age_and_receipt_time_of_its_device_only(clock: FakeClock) -> None:
    async def test() -> None:
        sdk = NavimowSDK(broker="broker.example.invalid", port=443)
        await deliver(sdk, DEVICE_ID, "state", b'{"state": "isRunning"}')
        clock.monotonic_now += 10
        clock.wall_now += timedelta(seconds=10)
        await deliver(sdk, OTHER_ID, "state", b'{"state": "isDocked"}')
        assert ages(sdk, DEVICE_ID) == (10.0, None, T0)
        assert ages(sdk, OTHER_ID) == (0.0, None, T0 + timedelta(seconds=10))

        clock.monotonic_now += 5
        clock.wall_now += timedelta(seconds=5)
        await deliver(sdk, DEVICE_ID, "state", b'{"state": "isPaused"}')
        assert ages(sdk, DEVICE_ID) == (0.0, None, T0 + timedelta(seconds=15))
        assert ages(sdk, OTHER_ID) == (5.0, None, T0 + timedelta(seconds=10))
        assert sdk.get_cached_state(DEVICE_ID).state == "paused"

    run(test)


def test_the_real_clocks_are_used_when_nothing_is_patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_module, "NavimowMQTT", FakeMQTT)

    async def test() -> None:
        sdk = NavimowSDK(broker="broker.example.invalid", port=443)
        before = datetime.now(UTC)
        await deliver(sdk, DEVICE_ID, "state", b'{"state": "isRunning"}')
        await deliver(sdk, DEVICE_ID, "attributes", b'{"attributes": {}}')
        after = datetime.now(UTC)
        assert 0.0 <= sdk.get_cached_state_age(DEVICE_ID) < 5.0
        assert 0.0 <= sdk.get_cached_attributes_age(DEVICE_ID) < 5.0
        received_at = sdk.get_cached_state_received_at(DEVICE_ID)
        assert received_at.tzinfo is UTC
        assert before <= received_at <= after

    run(test)

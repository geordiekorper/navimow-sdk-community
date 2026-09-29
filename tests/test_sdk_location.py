"""NavimowSDK and the payloads it does not model: location messages and malformed state.

A minimal fake stands in for NavimowMQTT (the facade only sets its
on_message), and _on_mqtt_message is driven directly. Today both kinds of
payload are dropped without a trace: nothing reaches a callback or a cache,
and nothing is logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from mower_sdk import sdk as sdk_module
from mower_sdk.sdk import NavimowSDK

DEVICE_ID = "dev-1"


class FakeMQTT:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.on_message: Any = None


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> NavimowSDK:
    monkeypatch.setattr(sdk_module, "NavimowMQTT", FakeMQTT)
    return NavimowSDK(broker="broker.example.invalid", port=443)


def topic(channel: str) -> str:
    return f"/downlink/vehicle/{DEVICE_ID}/realtimeDate/{channel}"


def record_everything(sdk: NavimowSDK) -> list[Any]:
    seen: list[Any] = []
    sdk.on_state(seen.append)
    sdk.on_event(seen.append)
    sdk.on_attributes(seen.append)
    return seen


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"type":"1","time":"1790000000000","postureX":"1.5","postureY":"2.5"}]',
        b'{"type":"1","time":"1790000000000","device_id":"dev-1"}',
        b"[]",
    ],
    ids=["array", "object", "empty_array"],
)
def test_a_location_message_reaches_no_callback_and_no_cache(
    sdk: NavimowSDK, payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    seen = record_everything(sdk)
    with caplog.at_level(logging.DEBUG, logger="mower_sdk.sdk"):
        asyncio.run(sdk._on_mqtt_message(topic("location"), payload, DEVICE_ID))
    assert seen == []
    assert sdk.get_cached_state(DEVICE_ID) is None
    assert sdk.get_cached_attributes(DEVICE_ID) is None
    assert caplog.records == []


@pytest.mark.parametrize(
    "payload", [b"not json", b"\xff", b'[{"state":"isDocked"}]', b'"isDocked"'], ids=["not_json", "not_utf8", "array", "string"]
)
def test_a_malformed_state_payload_is_dropped_without_a_trace(
    sdk: NavimowSDK, payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    seen = record_everything(sdk)
    with caplog.at_level(logging.DEBUG, logger="mower_sdk"):
        asyncio.run(sdk._on_mqtt_message(topic("state"), payload, DEVICE_ID))
    assert seen == []
    assert sdk.get_cached_state(DEVICE_ID) is None
    assert sdk.get_cached_state_age(DEVICE_ID) is None
    assert caplog.records == []

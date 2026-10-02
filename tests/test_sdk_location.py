"""NavimowSDK and the payloads it does not model: location messages and malformed state.

The shared fake stands in for NavimowMQTT, and _on_mqtt_message is driven
directly. A location message is decoded into per-entry messages and a cached
record, and what was not applied is reported through on_rejected, as is a
malformed state payload.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from mower_sdk.models import (
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceLocationMessage,
    DeviceStateMessage,
    RejectedMessage,
)
from mower_sdk.mqtt import ReceivedPayload
from mower_sdk.sdk import NavimowSDK

from .fakes import DEVICE_ID, topic

pytestmark = pytest.mark.usefixtures("fake_mqtt")


@pytest.fixture
def sdk() -> NavimowSDK:
    return NavimowSDK(broker="broker.example.invalid", port=443)


async def deliver(
    sdk: NavimowSDK, channel: str, payload: bytes, topic_name: str | None = None
) -> None:
    await sdk._on_mqtt_message(topic_name or topic(channel), payload, DEVICE_ID)


def pose(t: int, x: str = "1.5") -> dict[str, Any]:
    return {"type": 1, "time": str(t), "postureX": x, "postureY": "2.5"}


def record_everything(sdk: NavimowSDK) -> list[Any]:
    seen: list[Any] = []
    sdk.on_state(seen.append)
    sdk.on_event(seen.append)
    sdk.on_attributes(seen.append)
    return seen


@pytest.mark.parametrize(
    ("payload", "applied"),
    [
        (b'[{"type":1,"time":"1790000000000","postureX":"1.5","postureY":"2.5"}]', 1),
        (
            b'{"type": 1, "time": "1790000000000", "postureX": "1.5", "postureY": "2.5", '
            b'"device_id": "dev-1"}',
            1,
        ),
        (b"[]", 0),
    ],
    ids=["array", "object", "empty_array"],
)
@pytest.mark.asyncio
async def test_a_location_message_reaches_the_location_callbacks_and_cache_only(
    sdk: NavimowSDK, payload: bytes, applied: int, caplog: pytest.LogCaptureFixture
) -> None:
    seen = record_everything(sdk)
    located: list[DeviceLocationMessage] = []
    rejected: list[RejectedMessage] = []
    sdk.on_location(located.append)
    sdk.on_rejected(rejected.append)
    with caplog.at_level(logging.DEBUG, logger="mower_sdk.sdk"):
        await deliver(sdk, "location", payload)
    assert seen == []
    assert sdk.get_cached_state(DEVICE_ID) is None
    assert sdk.get_cached_attributes(DEVICE_ID) is None
    assert len(located) == applied
    assert rejected == []  # the object's injected device_id is not an unknown field
    if applied:
        assert sdk.get_cached_location(DEVICE_ID) == located[0].location
        assert located[0].location.x == 1.5
    else:
        assert sdk.get_cached_location(DEVICE_ID) is None
    assert caplog.records == []


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"\xff", b'[{"state":"isDocked"}]', b'"isDocked"'],
    ids=["not_json", "not_utf8", "array", "string"],
)
@pytest.mark.asyncio
async def test_a_malformed_state_payload_is_reported_as_unparsable_and_not_applied(
    sdk: NavimowSDK, payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    seen = record_everything(sdk)
    rejected: list[RejectedMessage] = []
    sdk.on_rejected(rejected.append)
    with caplog.at_level(logging.DEBUG, logger="mower_sdk"):
        await sdk._on_mqtt_message(topic("state"), payload, DEVICE_ID)
    assert seen == []
    assert sdk.get_cached_state(DEVICE_ID) is None
    assert sdk.get_cached_state_age(DEVICE_ID) is None
    assert [(r.channel, r.reason, r.reasons, r.payload) for r in rejected] == [
        ("state", "unparsable", ("unparsable",), payload)
    ]
    assert caplog.records == []


T = int(datetime.now(UTC).timestamp() * 1000) - 60_000


@pytest.mark.asyncio
async def test_one_callback_per_applied_entry_with_the_record_as_of_that_entry_and_one_rejection(
    sdk: NavimowSDK,
) -> None:
    located: list[DeviceLocationMessage] = []
    rejected: list[RejectedMessage] = []
    order: list[str] = []
    sdk.on_location(located.append)
    sdk.on_rejected(rejected.append)
    sdk.on_location(lambda _message: order.append("location"))
    sdk.on_rejected(lambda _message: order.append("rejected"))
    payload = json.dumps(
        [
            pose(T - 1000, "3"),
            pose(T - 3000, "1"),
            {"type": 9},
            pose(T - 2000, "2"),
            {**pose(T, "0"), "postureY": "0"},
        ]
    ).encode()
    await deliver(sdk, "location", payload)
    assert order == ["location", "location", "location", "rejected"]
    assert [m.x for m in located] == [1.0, 2.0, 3.0]
    assert [m.location.x for m in located] == [1.0, 2.0, 3.0]
    assert sdk.get_cached_location(DEVICE_ID) == located[-1].location
    (rejection,) = rejected
    assert (rejection.channel, rejection.topic, rejection.device_id) == (
        "location",
        topic("location"),
        DEVICE_ID,
    )
    assert (rejection.reason, rejection.reasons) == (
        "unknown_type",
        ("unknown_type", "placeholder"),
    )
    assert [(s.entry_type, s.reason, s.x) for s in rejection.skipped] == [
        (9, "unknown_type", None),
        (1, "placeholder", 0.0),
    ]
    assert all(s.received_at == rejection.received_at for s in rejection.skipped)
    assert rejection.payload is payload
    assert rejection.received_at.tzinfo is UTC


@pytest.mark.parametrize(
    "payload", [b"not json", b"\xff", b'"text"'], ids=["not_json", "not_utf8", "string"]
)
@pytest.mark.asyncio
async def test_an_unreadable_location_payload_is_rejected_as_unparsable(
    sdk: NavimowSDK, payload: bytes
) -> None:
    rejected: list[RejectedMessage] = []
    sdk.on_rejected(rejected.append)
    await deliver(sdk, "location", payload)
    assert [(r.reason, r.reasons, r.payload, r.skipped) for r in rejected] == [
        ("unparsable", ("unparsable",), payload, ())
    ]
    assert sdk.get_cached_location(DEVICE_ID) is None


@pytest.mark.asyncio
async def test_an_unknown_field_is_applied_and_reported(sdk: NavimowSDK) -> None:
    located: list[DeviceLocationMessage] = []
    rejected: list[RejectedMessage] = []
    sdk.on_location(located.append)
    sdk.on_rejected(rejected.append)
    await deliver(
        sdk, "location", json.dumps({**pose(T), "speed": "1", "device_id": DEVICE_ID}).encode()
    )
    assert len(located) == 1
    assert [r.reasons for r in rejected] == [("unknown_field",)]


@pytest.mark.asyncio
async def test_a_restored_location_rejects_an_older_pose(sdk: NavimowSDK) -> None:
    rejected: list[RejectedMessage] = []
    sdk.on_rejected(rejected.append)
    sdk.restore_location(
        DEVICE_ID, DeviceLocation(device_id=DEVICE_ID, x=9.0, pose_at=T, marks={1: T})
    )
    await deliver(sdk, "location", json.dumps([pose(T - 1000)]).encode())
    assert [r.reason for r in rejected] == ["stale"]
    assert sdk.get_cached_location(DEVICE_ID).x == 9.0


@pytest.mark.asyncio
async def test_a_raising_location_callback_does_not_stop_the_others(
    sdk: NavimowSDK, caplog: pytest.LogCaptureFixture
) -> None:
    located: list[DeviceLocationMessage] = []

    def broken(_message: DeviceLocationMessage) -> None:
        raise RuntimeError("consumer bug")

    sdk.on_location(broken)
    sdk.on_location(located.append)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.sdk"):
        await deliver(sdk, "location", json.dumps([pose(T)]).encode())
    assert len(located) == 1
    assert "Navimow location callback" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_raw_callbacks_are_installed_on_the_client_only_once_registered(
    sdk: NavimowSDK, caplog: pytest.LogCaptureFixture
) -> None:
    assert sdk.mqtt.on_raw is None
    seen: list[tuple[str, bytes]] = []

    def broken(_topic: str, _payload: bytes) -> None:
        raise RuntimeError("consumer bug")

    sdk.on_raw(broken)
    sdk.on_raw(lambda topic_name, payload: seen.append((topic_name, payload)))
    assert sdk.mqtt.on_raw == sdk._on_mqtt_raw
    with caplog.at_level(logging.ERROR, logger="mower_sdk.sdk"):
        await sdk.mqtt.on_raw("custom/topic", b"\x01")
    assert seen == [("custom/topic", b"\x01")]
    assert "Navimow raw callback" in caplog.records[0].getMessage()
    assert "custom/topic" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_message_seen_callbacks_are_installed_on_the_client_only_once_registered(
    sdk: NavimowSDK, caplog: pytest.LogCaptureFixture
) -> None:
    assert sdk.mqtt.on_message_seen is None
    seen: list[tuple[str, str, datetime]] = []

    def broken(_device_id: str, _channel: str, _received_at: datetime) -> None:
        raise RuntimeError("consumer bug")

    sdk.on_message_seen(broken)
    sdk.on_message_seen(
        lambda device_id, channel, received_at: seen.append((device_id, channel, received_at))
    )
    assert sdk.mqtt.on_message_seen == sdk._on_mqtt_message_seen
    at = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.sdk"):
        await sdk.mqtt.on_message_seen(DEVICE_ID, "location", at)
    assert seen == [(DEVICE_ID, "location", at)]
    message = caplog.records[0].getMessage()
    assert "Navimow message-seen callback" in message
    assert DEVICE_ID in message and "location" in message


def test_the_facade_forwards_subscribe_location_and_extra_topics() -> None:
    facade = NavimowSDK(
        broker="broker.example.invalid", port=443, subscribe_location=True, extra_topics=["a/b"]
    )
    assert (facade.mqtt.kwargs["subscribe_location"], facade.mqtt.kwargs["extra_topics"]) == (
        True,
        ["a/b"],
    )


# ---- the bytes as the mower sent them ------------------------------------------------------------


def re_encoded(wire: bytes) -> ReceivedPayload:
    """What NavimowMQTT hands on for an object payload: device_id added, the original kept."""
    return ReceivedPayload(json.dumps({**json.loads(wire), "device_id": DEVICE_ID}).encode(), wire)


@pytest.mark.parametrize(
    ("channel", "wire"),
    [
        ("state", b'{ "state":"isDocked", "battery" : 50 }'),
        ("event", b'{"event":"bladeBlocked",  "type":"alarm"}'),
        ("attributes", b'{"attributes": {"firmware":"1.2"} }'),
    ],
)
@pytest.mark.asyncio
async def test_each_typed_message_carries_the_bytes_as_sent(
    sdk: NavimowSDK, channel: str, wire: bytes
) -> None:
    delivered: list[Any] = []
    getattr(sdk, f"on_{channel}")(delivered.append)
    await deliver(sdk, channel, re_encoded(wire))
    (message,) = delivered
    assert message.original is wire
    assert message.raw["device_id"] == DEVICE_ID  # raw stays the decoded payload with device_id
    assert "original" not in message.to_dict()


@pytest.mark.asyncio
async def test_a_message_not_re_encoded_carries_its_own_bytes(sdk: NavimowSDK) -> None:
    states: list[DeviceStateMessage] = []
    sdk.on_state(states.append)
    payload = b'{"state":"isDocked","device_id":"dev-1"}'
    await deliver(sdk, "state", payload)
    assert states[0].original is payload


@pytest.mark.asyncio
async def test_a_rejection_carries_the_bytes_as_sent(sdk: NavimowSDK) -> None:
    rejected: list[RejectedMessage] = []
    sdk.on_rejected(rejected.append)
    wire = b'{"state":"isDocked", "speed": 1}'
    payload = re_encoded(wire)
    await deliver(sdk, "state", payload)
    array = json.dumps([pose(T, "0") | {"postureY": "0"}]).encode()
    await deliver(sdk, "location", array)
    first, second = rejected
    assert (first.reason, first.payload, first.original) == ("unknown_field", payload, wire)
    assert first.original is wire
    assert (second.reason, second.original) == ("placeholder", array)
    assert second.original is array


def test_original_is_left_out_of_equality_and_none_by_default() -> None:
    for cls, payload in (
        (DeviceStateMessage, {"device_id": "d", "state": "isDocked"}),
        (DeviceEventMessage, {"device_id": "d", "event": "e"}),
        (DeviceAttributesMessage, {"device_id": "d", "attributes": {}}),
    ):
        message = cls.from_dict(payload)
        assert message.original is None
        other = cls.from_dict(payload)
        other.original = b"{}"
        assert message == other
    base = RejectedMessage("state", "t", "d", "stale", ("stale",), b"{}", datetime.now(UTC))
    assert base.original is None
    assert base == RejectedMessage(
        "state", "t", "d", "stale", ("stale",), b"{}", base.received_at, original=b"x"
    )

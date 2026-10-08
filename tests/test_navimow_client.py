"""NavimowClient without its clock: construction, the lifecycle, the token step, the state.

The real NavimowSDK runs on the fake paho client, REST goes to the fake aiohttp
session with queued replies, and the shared clock stands in for time and
datetime in the client, the facade and the MQTT client, so the ages agree.
paho's callbacks are driven as paho's thread would call them, and drain()
delivers what they schedule on the loop. A test that needs an operation to
wait somewhere holds it in a provider or a facade build that waits for the
test, never for a fixed time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import pytest

import mower_sdk
from mower_sdk import navimow_client as client_module
from mower_sdk.errors import MowerAuthRequiredError, MowerTransportError
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceLocationMessage,
    DeviceStateMessage,
    MowerStatus,
    RejectedMessage,
)
from mower_sdk.mqtt import ConnectionEvent
from mower_sdk.navimow_client import MQTT_STALE_SECONDS, MowerState, NavimowClient, StateSource
from mower_sdk.sdk import NavimowSDK

from .fakes import (
    SUCCESS,
    T0,
    TOKEN,
    FakeClient,
    FakeClock,
    FakeMessage,
    FakeResponse,
    FakeSession,
    api_with,
    ok,
    topic,
)

pytestmark = pytest.mark.usefixtures("fake_paho", "clock", "sleeper")

DEVICE = "dev-1"
OTHER = "dev-2"
DEVICES = [
    Device(id=DEVICE, name="Lawn", model="X430", firmware_version="1.0", serial_number="SN1"),
    Device(id=OTHER, name="Orchard", model="X430", firmware_version="1.0", serial_number="SN2"),
]
T = int(T0.timestamp() * 1000)  # the fake receipt time, as a mower time in milliseconds
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- replies -------------------------------------------------------------------------------------


def devices_reply(*ids: str) -> FakeResponse:
    return FakeResponse(ok({"devices": [{"id": i, "name": f"Mower {i}"} for i in ids]}))


def status_reply(**states: str) -> FakeResponse:
    """A getVehicleStatus reply: device id -> raw vehicleState, each at 80 %."""
    return FakeResponse(
        ok(
            {
                "devices": [
                    {
                        "id": device_id,
                        "vehicleState": raw,
                        "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": "80"}],
                    }
                    for device_id, raw in states.items()
                ]
            }
        )
    )


def broker_reply(user: str = "user", path: str | None = "/mqtt") -> FakeResponse:
    data: dict[str, Any] = {
        "mqttHost": "wss://broker.example.invalid",
        "userName": user,
        "pwdInfo": "secret",
    }
    if path is not None:
        data["mqttUrl"] = path
    return FakeResponse({"code": 1, "desc": "success", "data": data})


def failure(status: int = 500) -> FakeResponse:
    return FakeResponse(status=status, body=b"down")


def both_docked() -> FakeResponse:
    return status_reply(**{DEVICE: "isDocked", OTHER: "isDocked"})


# ---- fakes of the consumer's side ----------------------------------------------------------------


class Provider:
    """A token provider answering from a queue: a token, None, or an exception to raise."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


class GatedProvider(Provider):
    """A provider that waits for the test to release it on every call."""

    def __init__(self, *answers: Any) -> None:
        super().__init__(*answers)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> str | None:
        self.entered.set()
        await self.release.wait()
        self.release.clear()
        self.entered.clear()
        return await super().__call__()


class GatedBuild(NavimowSDK):
    """A facade whose build waits for the test, as a slow executor hop would."""

    entered = threading.Event()
    release = threading.Event()

    @classmethod
    def from_connection_info(cls, info: Any, **kwargs: Any) -> NavimowSDK:
        GatedBuild.entered.set()
        assert GatedBuild.release.wait(5)
        return super().from_connection_info(info, **kwargs)


class Recorder:
    def __init__(self, client: NavimowClient, device_id: str | None = None) -> None:
        self.states: list[MowerState] = []
        self.events: list[DeviceEventMessage] = []
        self.attributes: list[DeviceAttributesMessage] = []
        self.rejected: list[RejectedMessage] = []
        self.connections: list[ConnectionEvent] = []
        self.errors: list[tuple[str, Exception]] = []
        client.on_state(self.states.append, device_id=device_id)
        client.on_event(self.events.append, device_id=device_id)
        client.on_attributes(self.attributes.append, device_id=device_id)
        client.on_rejected(self.rejected.append, device_id=device_id)
        client.on_connection(self.connections.append)
        client.on_error(lambda operation, exc: self.errors.append((operation, exc)))


# ---- building and driving a client ---------------------------------------------------------------


def make(
    *responses: FakeResponse, devices: list[Device] | None = DEVICES, **options: Any
) -> tuple[NavimowClient, FakeSession]:
    api, session = api_with(*responses)
    return NavimowClient(api, devices=devices, **options), session


def connect_replies() -> list[FakeResponse]:
    """What async_connect() needs when the devices are given: the broker reply, then the poll."""
    return [broker_reply(), both_docked()]


async def settle(client: NavimowClient, *held: asyncio.Task[Any]) -> None:
    """Let everything run to its end but the client's clock and the tasks the test holds.

    A marker queued on the loop runs after every callback queued before it,
    so once it has run the tasks those callbacks created exist; they are
    awaited, and the round repeats until none is left. The clock's tasks wait
    in the sleeper until a test releases them, and a held task waits in a
    provider, so neither is waited for.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(5):
        while True:
            marker = loop.create_future()
            loop.call_soon(marker.set_result, None)
            await marker
            tasks = asyncio.all_tasks() - {asyncio.current_task(), *client._tasks, *held}
            if not tasks:
                return
            await asyncio.wait(tasks)


async def connected(client: NavimowClient) -> None:
    """Paho's thread: the broker answered the connect."""
    mqtt = client.mqtt
    mqtt._on_connect(mqtt.client, None, {}, SUCCESS, None)
    mqtt.client.connected = True
    await settle(client)


async def deliver(
    client: NavimowClient, channel: str, payload: Any, device_id: str = DEVICE
) -> None:
    """Paho's thread: a message arrived on a device's topic."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    mqtt = client.mqtt
    mqtt._on_message(mqtt.client, None, FakeMessage(topic(channel, device_id), data))
    await settle(client)


async def state_message(
    client: NavimowClient, raw: str, device_id: str = DEVICE, **extra: Any
) -> None:
    await deliver(client, "state", {"state": raw, "battery": 55, **extra}, device_id)


def pose(t: int, x: str = "1.5", vehicle_state: str = "3") -> dict[str, Any]:
    return {
        "type": 1,
        "time": str(t),
        "postureX": x,
        "postureY": "2.5",
        "vehicleState": vehicle_state,
    }


def target(t: int, *ids: int) -> dict[str, Any]:
    return {"type": 3, "time": str(t), "partitionIds": [str(i) for i in ids]}


def requests(session: FakeSession) -> list[str]:
    return [request["url"].rsplit("/", 1)[1] for request in session.requests]


def paho(client: NavimowClient) -> FakeClient:
    return client.mqtt.client  # type: ignore[no-any-return]


# ---- construction and connect --------------------------------------------------------------------


def test_the_defaults_and_the_constants() -> None:
    assert MQTT_STALE_SECONDS == 300
    client, _session = make()
    assert client.devices == DEVICES and client.devices is not DEVICES
    assert client.device(DEVICE) == DEVICES[0] and client.device("nope") is None
    assert (client.sdk, client.mqtt, client.is_connected) == (None, None, False)
    assert (client.state(DEVICE), client.states()) == (None, {})
    assert (client.last_poll_at, client.last_poll_error) == (None, None)
    assert client.api.token == TOKEN


def test_the_client_and_the_constants_are_exported_from_the_package() -> None:
    for name in ("NavimowClient", "MQTT_STALE_SECONDS"):
        assert getattr(mower_sdk, name) is getattr(client_module, name)
        assert name in mower_sdk.__all__ and name in client_module.__all__


@pytest.mark.parametrize(
    "options",
    [
        {"allow_experimental_mqtt_commands": True},
        {"broker": "x"},
        {"records": []},
        {"ws_path": "/mqtt"},
        {"auth_headers": {}},
        {"keepalive_seconds": 90, "port": 1},
    ],
)
def test_a_wrong_mqtt_option_raises_at_construction(options: dict[str, Any]) -> None:
    api, _session = api_with()
    with pytest.raises(TypeError, match="MQTT options"):
        NavimowClient(api, devices=DEVICES, **options)


@pytest.mark.parametrize("value", [-1, "300", None, True])
def test_a_bad_stale_threshold_raises_at_construction(value: Any) -> None:
    api, _session = api_with()
    with pytest.raises(ValueError, match="mqtt_stale_seconds"):
        NavimowClient(api, devices=DEVICES, mqtt_stale_seconds=value)
    NavimowClient(api, devices=DEVICES, mqtt_stale_seconds=0)  # zero is allowed


@pytest.mark.asyncio
async def test_connect_builds_the_facade_with_the_bound_loop_the_devices_and_the_token() -> None:
    client, session = make(*connect_replies())
    await client.async_connect()
    assert isinstance(client.sdk, NavimowSDK)
    assert client.sdk.loop is asyncio.get_running_loop()
    assert client.mqtt.records is client.devices
    assert client.mqtt.auth_headers == BEARER
    assert (client.mqtt.username, client.mqtt.password) == ("user", "secret")
    assert (client.mqtt.subscribe_location, client.sdk._reject_late_state) == (True, True)
    assert requests(session) == ["v2", "getVehicleStatus"]
    assert paho(client).named("connect_async") == [
        ("connect_async", ("broker.example.invalid", 443, 60), {})
    ]
    assert not client.is_connected
    await connected(client)
    assert client.is_connected
    assert sorted(client.states()) == [DEVICE, OTHER]
    assert client.state(DEVICE) is not None and client.state(DEVICE).source is StateSource.REST


@pytest.mark.asyncio
async def test_the_location_topics_are_subscribed_by_default_and_left_out_when_asked() -> None:
    client, _session = make(*connect_replies())
    await client.async_connect()
    topics, _ids = client.mqtt._topics()
    assert topic("location") in topics
    other, _session = make(*connect_replies(), subscribe_location=False, reject_late_state=False)
    await other.async_connect()
    topics, _ids = other.mqtt._topics()
    assert topic("location") not in topics
    assert other.sdk._reject_late_state is False


@pytest.mark.asyncio
async def test_the_mqtt_options_reach_the_facade_and_a_bad_extra_topic_raises() -> None:
    client, _session = make(
        *connect_replies(), keepalive_seconds=90, reconnect_max_delay=5, extra_topics=["a/b"]
    )
    await client.async_connect()
    assert (client.mqtt.keepalive_seconds, client.mqtt.reconnect_max_delay) == (90, 5)
    assert client.mqtt.extra_topics == ["a/b"]
    bad, _session = make(*connect_replies(), extra_topics=["a/#/b"])
    with pytest.raises(ValueError, match="extra topic"):
        await bad.async_connect()
    assert bad.sdk is None


@pytest.mark.asyncio
async def test_devices_are_fetched_when_none_were_given() -> None:
    client, session = make(devices_reply(DEVICE, OTHER), *connect_replies(), devices=None)
    assert client.devices == []
    await client.async_connect()
    assert [device.id for device in client.devices] == [DEVICE, OTHER]
    assert requests(session) == ["authList", "v2", "getVehicleStatus"]
    assert client.mqtt.records is client.devices


@pytest.mark.asyncio
async def test_an_empty_device_list_refuses_to_connect() -> None:
    given, session = make(devices=[])
    with pytest.raises(ValueError, match="no devices"):
        await given.async_connect()
    assert (given.sdk, session.requests) == (None, [])
    fetched, session = make(
        devices_reply(), devices_reply(DEVICE), *connect_replies(), devices=None
    )
    with pytest.raises(ValueError, match="no devices"):
        await fetched.async_connect()
    assert requests(session) == ["authList"]
    await fetched.async_connect()  # the list is fetched again, not remembered as empty
    assert [device.id for device in fetched.devices] == [DEVICE]


@pytest.mark.asyncio
async def test_a_reply_without_a_websocket_path_raises_and_builds_nothing() -> None:
    client, _session = make(broker_reply(path=None))
    with pytest.raises(ValueError, match="WebSocket path"):
        await client.async_connect()
    assert client.sdk is None and FakeClient.instances == []


@pytest.mark.asyncio
async def test_a_second_connect_while_started_builds_nothing_and_calls_nothing() -> None:
    client, session = make(*connect_replies())
    await client.async_connect()
    await client.async_connect()
    await connected(client)
    await client.async_connect()
    assert len(FakeClient.instances) == 1
    assert len(paho(client).named("connect_async")) == 1
    assert requests(session) == ["v2", "getVehicleStatus"]


@pytest.mark.asyncio
async def test_a_connect_after_a_disconnect_reconnects_the_same_facade() -> None:
    client, session = make(*connect_replies(), both_docked())
    recorder = Recorder(client)
    await client.async_connect()
    await connected(client)
    sdk = client.sdk
    await client.async_disconnect()
    assert paho(client).named("loop_stop") and paho(client).named("disconnect")
    assert [event.kind for event in recorder.connections] == ["connected"]
    await client.async_disconnect()  # already closed: nothing more
    assert len(paho(client).named("disconnect")) == 1
    await client.async_connect()
    assert client.sdk is sdk and len(FakeClient.instances) == 1
    assert len(paho(client).named("connect_async")) == 2
    assert requests(session) == ["v2", "getVehicleStatus", "getVehicleStatus"]


@pytest.mark.asyncio
async def test_a_call_from_another_loop_raises_before_anything_changes() -> None:
    client, session = make(*connect_replies())
    raised: list[Exception] = []

    def elsewhere(coroutine: Any) -> None:
        try:
            asyncio.run(coroutine)
        except RuntimeError as exc:
            raised.append(exc)

    for coroutine in (client.async_connect(), client.async_poll(), client.async_set_token("t2")):
        await asyncio.to_thread(elsewhere, coroutine)
    assert [str(exc).startswith("NavimowClient is bound to event loop") for exc in raised] == [
        True,
        True,
        True,
    ]
    assert (client.sdk, session.requests, client.api.token) == (None, [], TOKEN)


# ---- the forwarded callbacks, the filters and the restores ---------------------------------------


@pytest.mark.asyncio
async def test_forwarded_callbacks_registered_before_connect_receive_the_first_message() -> None:
    client, _session = make(*connect_replies())
    before = Recorder(client)
    await client.async_connect()
    await connected(client)
    await deliver(client, "event", {"type": "system", "event": "started"})
    after = Recorder(client)
    await deliver(client, "event", {"type": "system", "event": "stopped"})
    await deliver(client, "attributes", {"attributes": {"a": 1}})
    await deliver(client, "state", b"not json")
    assert [event.event for event in before.events] == ["started", "stopped"]
    assert [event.event for event in after.events] == ["stopped"]
    assert [a.attributes for a in before.attributes] == [{"a": 1}]
    assert [r.reason for r in before.rejected] == ["unparsable"]
    assert [r.reason for r in after.rejected] == ["unparsable"]


@pytest.mark.asyncio
async def test_the_device_filter_on_on_state_and_the_three_forwarded_callbacks() -> None:
    client, _session = make(*connect_replies())
    every = Recorder(client)
    lawn = Recorder(client, device_id=DEVICE)
    nobody = Recorder(client, device_id="nope")
    await client.async_connect()
    await connected(client)
    for device_id in (DEVICE, OTHER):
        await state_message(client, "isRunning", device_id)
        await deliver(client, "event", {"type": "system", "event": "e"}, device_id)
        await deliver(client, "attributes", {"attributes": {"a": 1}}, device_id)
        await deliver(client, "state", b"not json", device_id)
    # The startup poll gave every device a state before the messages.
    assert [state.device_id for state in every.states] == [DEVICE, OTHER, DEVICE, OTHER]
    assert [state.device_id for state in lawn.states] == [DEVICE, DEVICE]
    assert nobody.states == []
    for recorder, expected in ((every, [DEVICE, OTHER]), (lawn, [DEVICE]), (nobody, [])):
        assert [m.device_id for m in recorder.events] == expected
        assert [m.device_id for m in recorder.attributes] == expected
        assert [m.device_id for m in recorder.rejected] == expected
    assert [event.kind for event in nobody.connections] == ["connected"]  # no filter


@pytest.mark.asyncio
async def test_a_restore_before_connect_is_applied_at_build_and_rebuilds_the_state() -> None:
    client, _session = make(both_docked(), *connect_replies())
    recorder = Recorder(client)
    record = DeviceLocation(device_id=DEVICE, x=1.0, y=2.0, partition_ids=(7, 3), marks={3: T})
    client.restore_location(DEVICE, record)
    await client.async_poll()  # REST only, before the facade: no location half
    assert [state.location for state in recorder.states] == [None, None]
    await client.async_connect()
    assert client.sdk is not None
    assert client.sdk.get_cached_location(DEVICE) == record
    # The build rebuilt the restored device's state with its record, then the startup
    # poll gave both devices a new state; the other device still has no record.
    assert [(s.device_id, s.location == record) for s in recorder.states[2:]] == [
        (DEVICE, True),
        (DEVICE, True),
        (OTHER, False),
    ]
    assert client.state(DEVICE) is not None and client.state(DEVICE).target_zone == 7


@pytest.mark.asyncio
async def test_a_restore_after_connect_replaces_the_record_and_fires_on_state() -> None:
    client, _session = make(*connect_replies())
    recorder = Recorder(client)
    await client.async_connect()
    assert len(recorder.states) == 2
    record = DeviceLocation(device_id=DEVICE, partition_ids=(), marks={3: T})
    client.restore_location(DEVICE, record)
    assert len(recorder.states) == 3
    assert recorder.states[-1].location == record
    assert recorder.states[-1].target_zone == client_module._target_zone(record, MowerStatus.DOCKED)
    assert client.sdk is not None and client.sdk.get_cached_location(DEVICE) == record


@pytest.mark.asyncio
async def test_a_location_entry_or_a_restore_before_any_observation_makes_no_state() -> None:
    client, _session = make(broker_reply(), failure())
    recorder = Recorder(client)
    await client.async_connect()  # the startup poll fails: no state yet
    assert [operation for operation, _ in recorder.errors] == ["poll"]
    assert isinstance(recorder.errors[0][1], MowerTransportError)
    assert client.last_poll_error is recorder.errors[0][1]
    await connected(client)
    await deliver(client, "location", [pose(T + 1000)])
    client.restore_location(OTHER, DeviceLocation(device_id=OTHER, partition_ids=(4,)))
    assert (recorder.states, client.states()) == ([], {})
    await state_message(client, "isRunning")
    assert len(recorder.states) == 1
    assert recorder.states[0].location is not None and recorder.states[0].location.x == 1.5
    await state_message(client, "isRunning", OTHER)
    assert recorder.states[-1].target_zone == 4


# ---- the token step ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_token_before_start_is_used_at_connect_and_pushes_nothing() -> None:
    client, session = make(*connect_replies())
    await client.async_set_token("t2")
    assert client.api.token == "t2"
    await client.async_connect()
    assert client.mqtt.auth_headers == bearer("t2")
    assert session.requests[0]["headers"]["Authorization"] == "Bearer t2"
    assert len(paho(client).named("ws_set_options")) == 1  # the build's; no push


@pytest.mark.asyncio
async def test_set_token_after_start_pushes_once_per_new_token() -> None:
    client, _session = make(*connect_replies())
    await client.async_connect()
    await connected(client)
    await client.async_set_token("t2")
    assert client.mqtt.auth_headers == bearer("t2")
    assert len(paho(client).named("ws_set_options")) == 2
    await client.async_set_token("t2")
    assert len(paho(client).named("ws_set_options")) == 2
    await asyncio.gather(client.async_set_token("t3"), client.async_set_token("t3"))
    assert client.mqtt.auth_headers == bearer("t3")
    assert len(paho(client).named("ws_set_options")) == 3
    assert len(FakeClient.instances) == 1  # connected: set on the live client, no rebuild


@pytest.mark.asyncio
async def test_set_token_during_the_build_waits_and_is_reconciled_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GatedBuild.entered = threading.Event()
    GatedBuild.release = threading.Event()
    monkeypatch.setattr(client_module, "NavimowSDK", GatedBuild)
    client, _session = make(*connect_replies())
    connecting = asyncio.ensure_future(client.async_connect())
    await asyncio.to_thread(GatedBuild.entered.wait, 5)
    setting = asyncio.ensure_future(client.async_set_token("t2"))
    await drain_until(lambda: client.api.token == "t2")  # learned at once, before the lock
    assert not setting.done()
    GatedBuild.release.set()
    await connecting
    await setting
    assert isinstance(client.sdk, GatedBuild)
    # The build used the token it started with; the push that waited found the client not
    # yet connected, so the facade rebuilt it with the new bearer.
    first, second = FakeClient.instances
    assert first.named("ws_set_options")[0][2]["headers"] == BEARER
    assert second.named("ws_set_options")[0][2]["headers"] == bearer("t2")
    assert (client.mqtt.rebuilds, client.mqtt.auth_headers) == (1, bearer("t2"))


async def drain_until(condition: Any) -> None:
    """Let the loop run until the condition holds, with a timeout as the bound on a failure."""
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_set_token_overtaken_by_a_newer_token_pushes_nothing() -> None:
    provider = GatedProvider(None, "t3")
    client, session = make(*connect_replies(), broker_reply(), token_provider=provider)
    provider.release.set()  # the connect's call, which keeps the token
    await client.async_connect()
    await connected(client)
    assert provider.calls == 1
    refreshing = asyncio.ensure_future(client.async_refresh_broker_credentials())
    await provider.entered.wait()
    setting = asyncio.ensure_future(client.async_set_token("t2"))
    await drain_until(lambda: client.api.token == "t2")
    provider.release.set()
    assert await refreshing is True
    await setting
    assert (client.api.token, client.mqtt.auth_headers) == ("t3", bearer("t3"))
    assert requests(session) == ["v2", "getVehicleStatus", "v2"]
    assert len(paho(client).named("ws_set_options")) == 2  # the build's, the helper's


@pytest.mark.asyncio
async def test_a_poll_asks_the_provider_and_pushes_a_token_that_changed() -> None:
    provider = Provider(None, "t2", "t2")
    client, _session = make(
        *connect_replies(), both_docked(), both_docked(), token_provider=provider
    )
    await client.async_connect()
    await connected(client)
    assert (provider.calls, client.api.token) == (1, TOKEN)
    await client.async_poll()
    assert (provider.calls, client.api.token, client.mqtt.auth_headers) == (2, "t2", bearer("t2"))
    await client.async_poll()
    assert len(paho(client).named("ws_set_options")) == 2  # one push for one new token


@pytest.mark.asyncio
async def test_a_refused_token_on_a_manual_poll_raises_and_is_last_poll_error() -> None:
    client, _session = make(*connect_replies(), failure(401))
    await client.async_connect()
    before = client.states()
    with pytest.raises(MowerAuthRequiredError) as info:
        await client.async_poll()
    assert client.last_poll_error is info.value
    assert client.last_poll_at == T0
    assert client.states() == before
    provider = Provider(RuntimeError("no token"))
    failing, _session = make(token_provider=provider)
    with pytest.raises(RuntimeError, match="no token"):
        await failing.async_poll()
    assert isinstance(failing.last_poll_error, RuntimeError)


# ---- the closed state ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_while_closed_set_token_changes_rest_only_and_the_next_connect_applies_it() -> None:
    client, session = make(*connect_replies(), both_docked())
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    await client.async_set_token("t2")
    assert (client.api.token, client.mqtt.auth_headers) == ("t2", BEARER)
    assert len(FakeClient.instances) == 1
    await client.async_connect()
    # Changed while closed: the bearer push on the disconnected client rebuilt it.
    assert len(FakeClient.instances) == 2
    assert client.mqtt.auth_headers == bearer("t2") and client.mqtt.rebuilds == 1
    assert paho(client).named("connect_async") and paho(client) is FakeClient.instances[-1]
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"


@pytest.mark.asyncio
async def test_while_closed_refresh_and_rebuild_do_nothing_and_a_poll_touches_no_mqtt() -> None:
    provider = Provider(None, "t2")
    client, session = make(*connect_replies(), both_docked(), token_provider=provider)
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    assert await client.async_refresh_broker_credentials() is False
    await client.async_rebuild("asked")
    assert (client.mqtt.rebuilds, len(FakeClient.instances)) == (0, 1)
    await client.async_poll()
    assert (client.api.token, client.mqtt.auth_headers) == ("t2", BEARER)
    assert requests(session) == ["v2", "getVehicleStatus", "getVehicleStatus"]
    assert client.state(DEVICE) is not None  # REST stays usable while closed


@pytest.mark.asyncio
async def test_a_restart_awaits_the_provider_and_rebuilds_when_the_token_changed() -> None:
    provider = Provider(None, "t2", None)
    client, _session = make(
        *connect_replies(), both_docked(), both_docked(), token_provider=provider
    )
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    await client.async_connect()  # the provider gives t2: rebuilt with it
    assert (provider.calls, client.mqtt.rebuilds, client.mqtt.auth_headers) == (2, 1, bearer("t2"))
    await client.async_disconnect()
    await client.async_set_token("t3")  # learned while closed; the provider then says None
    await client.async_connect()
    assert (provider.calls, client.mqtt.rebuilds, client.mqtt.auth_headers) == (3, 2, bearer("t3"))


@pytest.mark.asyncio
async def test_a_connect_after_a_disconnect_with_nothing_changed_reconnects() -> None:
    client, _session = make(*connect_replies(), both_docked())
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    await client.async_connect()
    assert (client.mqtt.rebuilds, len(FakeClient.instances)) == (0, 1)
    assert len(paho(client).named("connect_async")) == 2


@pytest.mark.asyncio
async def test_a_provider_that_raises_during_the_restart_leaves_the_client_closed() -> None:
    provider = Provider(None, RuntimeError("expired"), None)
    client, session = make(*connect_replies(), both_docked(), token_provider=provider)
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    with pytest.raises(RuntimeError, match="expired"):
        await client.async_connect()
    assert len(paho(client).named("connect_async")) == 1
    assert requests(session) == ["v2", "getVehicleStatus"]
    assert await client.async_refresh_broker_credentials() is False  # still closed
    await client.async_connect()
    assert len(paho(client).named("connect_async")) == 2


@pytest.mark.asyncio
async def test_a_consumer_set_token_during_the_restart_waits_and_is_reconciled_after() -> None:
    provider = GatedProvider(None, None)
    client, _session = make(*connect_replies(), both_docked(), token_provider=provider)
    provider.release.set()
    await client.async_connect()
    await connected(client)
    await client.async_disconnect()
    restarting = asyncio.ensure_future(client.async_connect())
    await provider.entered.wait()
    setting = asyncio.ensure_future(client.async_set_token("t2"))
    await drain_until(lambda: client.api.token == "t2")
    assert not setting.done()
    provider.release.set()
    await restarting
    await setting
    # The restart saw the token learned meanwhile, rebuilt with it, and the push found
    # nothing left to do.
    assert (client.mqtt.rebuilds, client.mqtt.auth_headers) == (1, bearer("t2"))
    assert len(paho(client).named("ws_set_options")) == 1  # the rebuilt client's own


# ---- recovery after a refused connection ---------------------------------------------------------


async def refused(client: NavimowClient, *held: asyncio.Task[Any]) -> None:
    """Paho's thread: the connect failed before the broker answered."""
    mqtt = client.mqtt
    mqtt._on_connect_fail(mqtt.client, None)
    await settle(client, *held)


@pytest.mark.asyncio
async def test_a_refused_connection_runs_one_recovery_provider_first_then_the_helper() -> None:
    provider = Provider(None, "t2")
    client, session = make(*connect_replies(), broker_reply(user="user2"), token_provider=provider)
    recorder = Recorder(client)
    await client.async_connect()
    await refused(client)
    assert provider.calls == 2
    assert requests(session) == ["v2", "getVehicleStatus", "v2"]
    # Applied on the disconnected client: rebuilt with the new username and bearer.
    assert (client.mqtt.username, client.mqtt.auth_headers, client.mqtt.rebuilds) == (
        "user2",
        bearer("t2"),
        1,
    )
    assert [event.kind for event in recorder.connections] == ["connect_failed"]
    assert recorder.errors == []


@pytest.mark.asyncio
async def test_recovery_is_skipped_while_the_lock_is_held_or_after_a_disconnect() -> None:
    provider = GatedProvider(None, None)
    client, session = make(*connect_replies(), broker_reply(), token_provider=provider)
    provider.release.set()
    await client.async_connect()
    refreshing = asyncio.ensure_future(client.async_refresh_broker_credentials())
    await provider.entered.wait()  # the refresh holds the lock, waiting for the provider
    await refused(client, refreshing)
    assert provider.calls == 1  # the hook returned at once
    provider.release.set()
    assert await refreshing is True
    assert provider.calls == 2
    await client.async_disconnect()
    await refused(client)
    assert provider.calls == 2
    assert requests(session) == ["v2", "getVehicleStatus", "v2"]


@pytest.mark.asyncio
async def test_recovery_in_the_helpers_cooldown_pushes_the_bearer_alone() -> None:
    provider = Provider(None, None, "t2")
    client, session = make(*connect_replies(), broker_reply(), token_provider=provider)
    await client.async_connect()
    assert await client.async_refresh_broker_credentials() is True  # starts the cooldown
    await refused(client)
    assert provider.calls == 3
    assert requests(session) == ["v2", "getVehicleStatus", "v2"]  # no second request
    assert (client.mqtt.auth_headers, client.mqtt.rebuilds) == (bearer("t2"), 1)


@pytest.mark.asyncio
async def test_recovery_failures_are_logged_and_reported_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = Provider(None, RuntimeError("no token"), None)
    client, _session = make(
        *connect_replies(),
        FakeResponse({"code": 4005, "desc": "token expired"}),
        token_provider=provider,
    )
    recorder = Recorder(client)
    await client.async_connect()
    with caplog.at_level(logging.WARNING, logger="mower_sdk.navimow_client"):
        await refused(client)
        await refused(client)
    assert [operation for operation, _ in recorder.errors] == ["recovery", "recovery"]
    assert isinstance(recorder.errors[0][1], RuntimeError)
    assert isinstance(recorder.errors[1][1], MowerAuthRequiredError)
    assert [record.levelname for record in caplog.records if "recovery" in record.getMessage()] == [
        "ERROR",
        "WARNING",
    ]
    assert client.mqtt.rebuilds == 0


@pytest.mark.asyncio
async def test_recover_on_connect_fail_false_leaves_a_refused_connection_to_paho() -> None:
    provider = Provider(None, "t2")
    client, session = make(
        *connect_replies(), token_provider=provider, recover_on_connect_fail=False
    )
    recorder = Recorder(client)
    await client.async_connect()
    await refused(client)
    assert (provider.calls, requests(session)) == (1, ["v2", "getVehicleStatus"])
    assert [event.kind for event in recorder.connections] == ["connect_failed"]


@pytest.mark.asyncio
async def test_disconnect_waits_for_an_operation_in_flight() -> None:
    provider = GatedProvider(None, None)
    client, _session = make(*connect_replies(), broker_reply(), token_provider=provider)
    provider.release.set()
    await client.async_connect()
    await connected(client)
    order: list[str] = []
    refreshing = asyncio.ensure_future(client.async_refresh_broker_credentials())
    await provider.entered.wait()
    disconnecting = asyncio.ensure_future(client.async_disconnect())
    refreshing.add_done_callback(lambda _: order.append("refresh"))
    disconnecting.add_done_callback(lambda _: order.append("disconnect"))
    await settle(client, refreshing, disconnecting)  # the disconnect waits at the lock
    assert not disconnecting.done() and not paho(client).named("disconnect")
    provider.release.set()
    await refreshing
    await disconnecting
    assert order == ["refresh", "disconnect"]
    assert paho(client).named("disconnect")


# ---- the merge rule and on_state -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_merge_rule(clock: FakeClock) -> None:
    client, _session = make(*connect_replies(), both_docked(), both_docked(), both_docked())
    recorder = Recorder(client)
    await client.async_connect()
    await connected(client)
    # 1. No MQTT observation: the REST observation.
    assert client.state(DEVICE) is not None and client.state(DEVICE).source is StateSource.REST
    # 2. An MQTT observation younger than the threshold wins; a poll then changes nothing.
    await state_message(client, "isRunning")
    assert client.state(DEVICE).status is MowerStatus.MOWING
    assert client.state(DEVICE).source is StateSource.MQTT
    clock.advance(MQTT_STALE_SECONDS - 1)
    count = len(recorder.states)
    await client.async_poll()
    assert client.state(DEVICE).source is StateSource.MQTT
    assert [state.device_id for state in recorder.states[count:]] == [OTHER]  # REST stays current
    # 3. At the threshold, a REST observation received after the MQTT one wins.
    clock.advance(1)
    await client.async_poll()
    assert client.state(DEVICE).source is StateSource.REST
    assert client.state(DEVICE).status is MowerStatus.DOCKED
    # 4. A REST observation older than the MQTT one never replaces it, however old both are.
    await state_message(client, "isPaused")
    assert client.state(DEVICE).source is StateSource.MQTT
    clock.advance(MQTT_STALE_SECONDS * 2)
    await deliver(client, "location", [target(T + 1000, 7)])  # a rebuild re-evaluates the rule
    assert client.state(DEVICE).source is StateSource.MQTT
    assert client.state(DEVICE).target_zone == 7
    await client.async_poll()  # a new REST observation, received after: it wins
    assert client.state(DEVICE).source is StateSource.REST
    assert client.state(DEVICE).target_zone == 7  # the record stays


@pytest.mark.asyncio
async def test_on_state_fires_per_state_message_and_per_location_entry_with_its_record() -> None:
    client, _session = make(*connect_replies())
    recorder = Recorder(client)
    entries: list[DeviceLocationMessage] = []
    await client.async_connect()
    assert client.sdk is not None
    client.sdk.on_location(entries.append)
    await connected(client)
    await state_message(client, "isRunning", battery=50)
    await state_message(client, "isRunning", battery=49)
    assert [state.battery for state in recorder.states[2:]] == [50, 49]
    await deliver(client, "location", [pose(T + 2000, "2.0"), pose(T + 1000, "1.0")])
    assert [entry.x for entry in entries] == [1.0, 2.0]  # applied in time order
    assert [state.location for state in recorder.states[4:]] == [
        entries[0].location,
        entries[1].location,
    ]
    assert [state.location.x for state in recorder.states[4:]] == [1.0, 2.0]
    assert recorder.states[-1].status is MowerStatus.MOWING and recorder.states[-1].battery == 49
    assert client.state(DEVICE) is recorder.states[-1]
    assert client.state(DEVICE).location == client.sdk.get_cached_location(DEVICE)


@pytest.mark.asyncio
async def test_a_poll_fires_when_its_observation_becomes_current_and_on_each_later_poll() -> None:
    client, _session = make(*connect_replies(), both_docked(), status_reply(**{OTHER: "isRunning"}))
    recorder = Recorder(client)
    await client.async_connect()
    assert [state.device_id for state in recorder.states] == [DEVICE, OTHER]
    await client.async_poll()  # REST stays current for both: a new state each
    assert [state.device_id for state in recorder.states] == [DEVICE, OTHER, DEVICE, OTHER]
    assert recorder.states[2] == recorder.states[0] and recorder.states[2] is not recorder.states[0]
    statuses = await client.async_poll([OTHER])  # a subset poll: only the device covered
    assert sorted(statuses) == [OTHER]
    assert [state.device_id for state in recorder.states[4:]] == [OTHER]
    assert recorder.states[-1].status is MowerStatus.MOWING
    assert client.state(DEVICE).status is MowerStatus.DOCKED


@pytest.mark.asyncio
async def test_states_holds_every_device_and_a_failed_poll_leaves_them_unchanged() -> None:
    client, _session = make(*connect_replies(), failure())
    await client.async_connect()
    before = client.states()
    assert sorted(before) == [DEVICE, OTHER] and before is not client.states()
    with pytest.raises(MowerTransportError) as info:
        await client.async_poll()
    assert client.states() == before
    assert (client.last_poll_error, client.last_poll_at) == (info.value, T0)
    assert client.state(DEVICE) is before[DEVICE]


@pytest.mark.asyncio
async def test_state_inside_a_consumer_facade_callback_already_shows_the_message() -> None:
    client, _session = make(*connect_replies())
    await client.async_connect()
    assert client.sdk is not None
    seen: list[DeviceStateMessage | None] = []
    client.sdk.on_state(lambda _message: seen.append(client.state(DEVICE).message))  # type: ignore[union-attr]
    await connected(client)
    await state_message(client, "isRunning")
    assert len(seen) == 1 and seen[0] is not None and seen[0].state == "mowing"


@pytest.mark.asyncio
async def test_a_poll_before_connect_fetches_the_devices_and_gives_rest_only_states() -> None:
    client, session = make(devices_reply(DEVICE, OTHER), both_docked(), devices=None)
    statuses = await client.async_poll()
    assert sorted(statuses) == [DEVICE, OTHER]
    assert requests(session) == ["authList", "getVehicleStatus"]
    assert [device.id for device in client.devices] == [DEVICE, OTHER]
    assert client.state(DEVICE) is not None and client.state(DEVICE).location is None
    assert (
        client.state(DEVICE).received_at == T0 and client.state(DEVICE).received_monotonic == 100.0
    )


@pytest.mark.asyncio
async def test_async_refresh_devices_updates_the_list_the_facade_holds() -> None:
    client, _session = make(*connect_replies(), devices_reply(DEVICE, OTHER, "dev-3"))
    await client.async_connect()
    records = client.mqtt.records
    refreshed = await client.async_refresh_devices()
    assert [device.id for device in refreshed] == [DEVICE, OTHER, "dev-3"]
    assert client.devices is records and refreshed is not records
    assert client.device("dev-3") is not None
    assert topic("state", "dev-3") in client.mqtt._topics()[0]


@pytest.mark.asyncio
async def test_a_raising_state_callback_is_logged_and_the_next_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _session = make(*connect_replies())

    def explode(_state: MowerState) -> None:
        raise RuntimeError("boom")

    client.on_state(explode)
    recorder = Recorder(client)
    client.on_error(lambda _operation, _exc: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        await client.async_connect()
    assert len(recorder.states) == 2
    assert "state callback" in caplog.text and "boom" in caplog.text


# ---- interrupted connects, the device refresh, the active rebuild, serialised polls ------------


class GatedResponse(FakeResponse):
    """A reply held until the test releases it."""

    def __init__(self, payload: Any) -> None:
        super().__init__(payload)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self) -> FakeResponse:
        self.entered.set()
        await self.release.wait()
        return await super().__aenter__()


@pytest.mark.asyncio
async def test_a_connect_cancelled_during_the_startup_poll_is_retried_by_the_next_connect() -> None:
    held = GatedResponse(ok({"devices": [{"id": DEVICE, "vehicleState": "isDocked"}]}))
    client, session = make(broker_reply(), held, both_docked(), both_docked(), both_docked())
    connecting = asyncio.ensure_future(client.async_connect())
    await held.entered.wait()
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert client.sdk is not None and not paho(client).named("connect_async")
    await client.async_connect()  # restarts: polls again, then connects
    assert len(paho(client).named("connect_async")) == 1
    assert requests(session) == ["v2", "getVehicleStatus", "getVehicleStatus"]
    assert client.mqtt.auth_headers == BEARER and len(FakeClient.instances) == 1
    await connected(client)
    # The same during a restart.
    await client.async_disconnect()
    held = GatedResponse(ok({"devices": [{"id": DEVICE, "vehicleState": "isDocked"}]}))
    session.responses.insert(0, held)
    connecting = asyncio.ensure_future(client.async_connect())
    await held.entered.wait()
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert len(paho(client).named("connect_async")) == 1
    await client.async_connect()
    assert len(paho(client).named("connect_async")) == 2
    assert requests(session).count("getVehicleStatus") == 4


@pytest.mark.asyncio
async def test_async_refresh_devices_waits_for_the_lock_asks_the_provider_and_pushes() -> None:
    provider = GatedProvider(None, None, "t2")
    client, session = make(
        *connect_replies(),
        broker_reply(),
        devices_reply(DEVICE, OTHER, "dev-3"),
        token_provider=provider,
    )
    provider.release.set()
    await client.async_connect()
    await connected(client)
    refreshing = asyncio.ensure_future(client.async_refresh_broker_credentials())
    await provider.entered.wait()  # the refresh holds the lock in the provider
    listing = asyncio.ensure_future(client.async_refresh_devices())
    await settle(client, refreshing, listing)
    assert not listing.done() and requests(session) == ["v2", "getVehicleStatus"]
    provider.release.set()
    await refreshing
    await provider.entered.wait()  # now the device refresh asks the provider
    provider.release.set()
    devices = await listing
    assert [device.id for device in devices] == [DEVICE, OTHER, "dev-3"]
    assert session.requests[-1]["url"].endswith("authList")
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"
    assert (provider.calls, client.api.token, client.mqtt.auth_headers) == (3, "t2", bearer("t2"))


@pytest.mark.asyncio
async def test_an_active_rebuild_asks_the_provider_and_rebuilds_with_the_current_bearer() -> None:
    provider = Provider(None, "t2")
    client, _session = make(*connect_replies(), token_provider=provider)
    recorder = Recorder(client)
    await client.async_connect()
    await connected(client)
    first = paho(client)
    await client.async_rebuild("the feed went quiet")
    assert (provider.calls, client.mqtt.rebuilds) == (2, 1)
    assert client.mqtt.last_rebuild_reason == "the feed went quiet"
    assert paho(client) is not first and paho(client).named("connect_async")
    assert paho(client).named("ws_set_options")[0][2]["headers"] == bearer("t2")
    assert client.mqtt.auth_headers == bearer("t2")
    await client.async_set_token("t2")  # recorded as applied by the rebuild: nothing to push
    assert len(paho(client).named("ws_set_options")) == 1
    assert [event.kind for event in recorder.connections] == ["connected"]


@pytest.mark.asyncio
async def test_two_polls_never_overlap_and_observations_follow_reply_order() -> None:
    first = GatedResponse(ok({"devices": [{"id": DEVICE, "vehicleState": "isRunning"}]}))
    second = GatedResponse(ok({"devices": [{"id": DEVICE, "vehicleState": "isPaused"}]}))
    client, session = make(*connect_replies(), first, second)
    recorder = Recorder(client)
    await client.async_connect()
    polls = [asyncio.ensure_future(client.async_poll()), asyncio.ensure_future(client.async_poll())]
    await first.entered.wait()
    await settle(client, *polls)
    assert not second.entered.is_set()  # the second request waits for the poll lock
    assert requests(session).count("getVehicleStatus") == 2  # the request was taken, not sent
    second.release.set()
    await settle(client, *polls)
    assert not second.entered.is_set()
    first.release.set()
    await second.entered.wait()
    await asyncio.gather(*polls)
    assert [state.status for state in recorder.states[2:]] == [
        MowerStatus.MOWING,
        MowerStatus.PAUSED,
    ]
    assert client.state(DEVICE) is not None and client.state(DEVICE).status is MowerStatus.PAUSED

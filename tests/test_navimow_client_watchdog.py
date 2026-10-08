"""NavimowClient and the watchdog: the rules, the requests and the rebuilds that coalesce.

The real NavimowSDK runs on the fake paho client and REST goes to the fake
aiohttp session. A fake watchdog with real methods answers the rules from
queues and records what it was given and what was acknowledged; one test runs
the real MqttWatchdog through the client. The sleeper holds the clock's tasks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import pytest

from mower_sdk.models import MowerStatus
from mower_sdk.navimow_client import NavimowClient
from mower_sdk.sdk import NavimowSDK
from mower_sdk.watchdog import REST_CACHE_LAG_SECONDS, MqttWatchdog, RebuildRequest, WatchInput

from .fakes import SUCCESS, FakeClient, FakeClock, FakeSleeper
from .test_navimow_client import (
    DEVICE,
    OTHER,
    Provider,
    Recorder,
    bearer,
    both_docked,
    broker_reply,
    connect_replies,
    connected,
    make,
    requests,
    settle,
    state_message,
    status_reply,
)
from .test_navimow_client_clock import SILENCE, HoldingProvider, connect, tick
from .test_navimow_client_commands import refused as refused_command

pytestmark = pytest.mark.usefixtures("fake_paho", "clock", "sleeper")

REBUILD = "NavimowClient rebuild"


class FakeWatchdog:
    """Stands in for MqttWatchdog: answers the rules from queues and records what it was given."""

    def __init__(self, sdk: NavimowSDK) -> None:
        self.sdk = sdk
        self.poll_inputs: list[list[WatchInput]] = []
        self.silence_inputs: list[list[WatchInput]] = []
        self.poll_answers: list[RebuildRequest | None] = []
        self.silence_answers: list[RebuildRequest | None] = []
        self.acknowledged: list[RebuildRequest] = []
        self.poll_error: Exception | None = None

    def after_poll(self, inputs: Iterable[WatchInput]) -> RebuildRequest | None:
        self.poll_inputs.append(list(inputs))
        if self.poll_error is not None:
            raise self.poll_error
        return self.poll_answers.pop(0) if self.poll_answers else None

    def check_silence(self, inputs: Iterable[WatchInput]) -> RebuildRequest | None:
        self.silence_inputs.append(list(inputs))
        return self.silence_answers.pop(0) if self.silence_answers else None

    def acknowledge(self, request: RebuildRequest) -> None:
        self.acknowledged.append(request)


def request(reason: str = "asked") -> RebuildRequest:
    return RebuildRequest(reason=reason, device_ids=(DEVICE,))


def watched(**options: Any) -> tuple[NavimowClient, Any, list[FakeWatchdog]]:
    """A client on a fake watchdog, the dogs made by the factory returned as a list."""
    dogs: list[FakeWatchdog] = []

    def factory(sdk: NavimowSDK) -> MqttWatchdog:
        dogs.append(FakeWatchdog(sdk))
        return dogs[-1]  # type: ignore[return-value]

    client, session = make(*options.pop("replies", connect_replies()), watchdog=factory, **options)
    return client, session, dogs


async def rebuilt(client: NavimowClient) -> None:
    """Wait for the rebuild task the client spawned to end."""
    tasks = [task for task in client._tasks if task.get_name() == REBUILD]
    if tasks:
        async with asyncio.timeout(5):
            await asyncio.wait(tasks)
    await settle(client)


@pytest.mark.asyncio
async def test_a_custom_factory_is_called_once_with_the_facade() -> None:
    client, _session, dogs = watched()
    assert dogs == []
    await connect(client)
    assert len(dogs) == 1 and dogs[0].sdk is client.sdk
    await client.async_disconnect()
    await client.async_connect()
    assert len(dogs) == 1  # the facade lives as long as the client, and so does its watchdog


@pytest.mark.asyncio
async def test_rule_1_runs_after_a_poll_over_the_devices_covered_with_the_state_and_rest() -> None:
    client, _session, dogs = watched(
        replies=[*connect_replies(), status_reply(**{DEVICE: "isDocked"})]
    )
    await connect(client)
    await connected(client)
    dog = dogs[0]
    assert [sorted(i.device_id for i in inputs) for inputs in dog.poll_inputs] == [[DEVICE, OTHER]]
    first = {i.device_id: i for i in dog.poll_inputs[0]}
    assert (first[DEVICE].name, first[OTHER].name) == ("Lawn", "Orchard")
    assert first[DEVICE].shown_state is MowerStatus.DOCKED  # the REST state, shown
    assert first[DEVICE].rest_state is MowerStatus.DOCKED
    assert first[DEVICE].rest_observed_at == 100.0
    await state_message(client, "isRunning")  # MQTT now shown for the device
    await client.async_poll([DEVICE])  # a subset poll: only the device covered
    assert [i.device_id for i in dog.poll_inputs[1]] == [DEVICE]
    only = dog.poll_inputs[1][0]
    assert (only.shown_state, only.rest_state) == (MowerStatus.MOWING, MowerStatus.DOCKED)
    assert dog.silence_inputs == [] and dog.acknowledged == []


@pytest.mark.asyncio
async def test_rule_2_runs_on_the_silence_tick_over_every_known_device(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, _session, dogs = watched()
    await connect(client)
    await tick(sleeper, clock, 30, SILENCE)
    dog = dogs[0]
    assert [sorted(i.device_id for i in inputs) for inputs in dog.silence_inputs] == [
        [DEVICE, OTHER]
    ]
    assert {i.name for i in dog.silence_inputs[0]} == {"Lawn", "Orchard"}
    assert len(dog.poll_inputs) == 1  # the startup poll's only


@pytest.mark.asyncio
async def test_a_request_is_delivered_and_with_auto_rebuild_rebuilt_and_acknowledged() -> None:
    provider = Provider(None, "t2")
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked()], token_provider=provider
    )
    recorder = Recorder(client)
    seen: list[RebuildRequest] = []
    client.on_rebuild_request(seen.append)
    await connect(client)
    await connected(client)
    dog = dogs[0]
    asked = request("the feed went quiet")
    dog.poll_answers.append(asked)
    await client.async_poll()
    assert seen == [asked]
    await rebuilt(client)
    assert (client.mqtt.rebuilds, client.mqtt.last_rebuild_reason) == (1, "the feed went quiet")
    assert client.mqtt.auth_headers == bearer("t2")  # a fresh bearer through the provider
    assert FakeClient.instances[-1].named("ws_set_options")[0][2]["headers"] == bearer("t2")
    assert dog.acknowledged == [asked]
    assert recorder.errors == []


@pytest.mark.asyncio
async def test_without_auto_rebuild_a_request_is_delivered_and_nothing_more() -> None:
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked()], auto_rebuild=False
    )
    seen: list[RebuildRequest] = []
    client.on_rebuild_request(seen.append)
    await connect(client)
    asked = request()
    dogs[0].poll_answers.append(asked)
    await client.async_poll()
    await settle(client)
    assert seen == [asked] and client.mqtt.rebuilds == 0 and dogs[0].acknowledged == []
    assert not any(task.get_name() == REBUILD for task in client._tasks)
    await client.async_rebuild(asked)  # the consumer does it itself
    assert client.mqtt.rebuilds == 1 and dogs[0].acknowledged == [asked]


@pytest.mark.asyncio
async def test_a_second_request_made_while_a_rebuild_runs_is_acknowledged_without_another() -> None:
    # Calls: the connect's, the first poll's, the first rebuild task's (held), the second poll's.
    provider = HoldingProvider(None, None, None, None, None, hold=3)
    client, session, dogs = watched(
        replies=[*connect_replies(), both_docked(), both_docked()], token_provider=provider
    )
    await connect(client)
    dog = dogs[0]
    first, second = request("first"), request("second")
    dog.poll_answers.extend([first, second])
    await client.async_poll()  # the first request: its rebuild task waits in the provider
    await provider.entered.wait()
    await client.async_poll()  # the second, made while the first rebuild runs
    await settle(client, *(t for t in client._tasks if t.get_name() == REBUILD))
    assert [t.get_name() for t in client._tasks if t.get_name() == REBUILD] == [REBUILD, REBUILD]
    provider.release.set()
    await rebuilt(client)
    assert client.mqtt.rebuilds == 1  # one rebuild for both
    assert dog.acknowledged == [first, second]
    assert client.mqtt.last_rebuild_reason == "first"


@pytest.mark.asyncio
async def test_a_request_made_before_a_recovery_rebuild_is_obsolete_afterwards() -> None:
    provider = Provider(None, None, "t2")
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked(), broker_reply(user="user2")],
        auto_rebuild=False,
        token_provider=provider,
    )
    await connect(client)
    dog = dogs[0]
    asked = request()
    dog.poll_answers.append(asked)
    await client.async_poll()
    # A refused connection: the recovery applies new credentials, which rebuilds.
    mqtt = client.mqtt
    mqtt._on_connect_fail(mqtt.client, None)
    await settle(client)
    assert (client.mqtt.rebuilds, client.mqtt.username) == (1, "user2")
    await client.async_rebuild(asked)  # older than that rebuild: acknowledged, not rebuilt
    assert client.mqtt.rebuilds == 1 and dog.acknowledged == [asked]


@pytest.mark.asyncio
async def test_a_request_older_than_a_rebuild_that_failed_stays_eligible() -> None:
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked()], auto_rebuild=False
    )
    await connect(client)
    dog = dogs[0]
    asked = request()
    dog.poll_answers.append(asked)
    await client.async_poll()
    original = client.mqtt.rebuild

    def refuse(**_kwargs: Any) -> None:
        raise RuntimeError("no certificates")

    client.mqtt.rebuild = refuse
    with pytest.raises(RuntimeError, match="no certificates"):
        await client.async_rebuild("asked by hand")
    client.mqtt.rebuild = original
    await client.async_rebuild(asked)  # the failed rebuild advanced nothing
    assert client.mqtt.rebuilds == 1 and dog.acknowledged == [asked]


@pytest.mark.asyncio
async def test_a_request_acted_on_after_disconnect_does_nothing() -> None:
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked()], auto_rebuild=False
    )
    await connect(client)
    asked = request()
    dogs[0].poll_answers.append(asked)
    await client.async_poll()
    await client.async_disconnect()
    await client.async_rebuild(asked)
    assert client.mqtt.rebuilds == 0 and dogs[0].acknowledged == []


@pytest.mark.asyncio
async def test_a_rebuild_that_raises_is_not_acknowledged_and_reaches_on_error_as_rebuild(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _session, dogs = watched(replies=[*connect_replies(), both_docked()])
    recorder = Recorder(client)
    await connect(client)

    def refuse(**_kwargs: Any) -> None:
        raise RuntimeError("no certificates")

    client.mqtt.rebuild = refuse
    dogs[0].poll_answers.append(request())
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        await client.async_poll()
        await rebuilt(client)
    assert [(operation, str(exc)) for operation, exc in recorder.errors] == [
        ("rebuild", "no certificates")
    ]
    assert dogs[0].acknowledged == [] and client.mqtt.rebuilds == 0
    assert "the rebuild the watchdog asked for failed" in caplog.text


@pytest.mark.asyncio
async def test_the_connect_after_a_rebuild_reaches_on_connection_and_no_disconnect_does() -> None:
    client, _session, dogs = watched(replies=[*connect_replies(), both_docked()])
    recorder = Recorder(client)
    await connect(client)
    await connected(client)
    old = client.mqtt.client
    dogs[0].poll_answers.append(request())
    await client.async_poll()
    await rebuilt(client)
    assert client.mqtt.rebuilds == 1 and client.mqtt.client is not old
    # paho reports the old client's disconnect during its teardown: ignored.
    client.mqtt._on_disconnect(old, None, {}, SUCCESS, None)
    await connected(client)  # the new client connects
    assert [event.kind for event in recorder.connections] == ["connected", "connected"]
    assert recorder.connections[-1].rebuilds == 1


@pytest.mark.asyncio
async def test_the_real_watchdog_rule_1_runs_through_the_client(clock: FakeClock) -> None:
    client, _session = make(*connect_replies(), status_reply(**{DEVICE: "isRunning"}))
    seen: list[RebuildRequest] = []
    client.on_rebuild_request(seen.append)
    await connect(client)
    await connected(client)
    assert isinstance(client._watchdog, MqttWatchdog)
    await state_message(client, "isDocked")  # MQTT says docked
    clock.advance(REST_CACHE_LAG_SECONDS)
    await client.async_poll()  # REST says mowing, read the cache lag later: a missed change
    assert len(seen) == 1 and "missed a state change" in seen[0].reason
    assert seen[0].device_ids == (DEVICE,)
    await rebuilt(client)
    assert client.mqtt.rebuilds == 1
    assert client.mqtt.last_rebuild_reason == seen[0].reason


@pytest.mark.asyncio
async def test_a_refused_command_still_runs_rule_1_through_its_poll(sleeper: FakeSleeper) -> None:
    from mower_sdk.errors import MowerAPIError
    from mower_sdk.models import MowerCommand

    client, _session, dogs = watched(replies=[*connect_replies(), refused_command(), both_docked()])
    await connect(client)
    with pytest.raises(MowerAPIError):
        await client.async_send_command(DEVICE, MowerCommand.START)
    await sleeper.until_sleeping("NavimowClient command_poll")
    sleeper.wake("NavimowClient command_poll")
    await settle(client)
    assert len(dogs[0].poll_inputs) == 2  # the startup poll and the poll after the command


@pytest.mark.asyncio
async def test_two_equal_requests_coalesce_into_one_rebuild_and_both_are_acknowledged() -> None:
    provider = HoldingProvider(None, None, None, None, None, hold=3)
    client, _session, dogs = watched(
        replies=[*connect_replies(), both_docked(), both_docked()], token_provider=provider
    )
    await connect(client)
    dog = dogs[0]
    first, second = request("the same"), request("the same")
    assert first == second and first is not second  # the watchdog repeats a live mismatch
    dog.poll_answers.extend([first, second])
    await client.async_poll()
    await provider.entered.wait()  # the first rebuild task waits in the provider
    await client.async_poll()
    await settle(client, *(t for t in client._tasks if t.get_name() == REBUILD))
    provider.release.set()
    await rebuilt(client)
    assert client.mqtt.rebuilds == 1
    assert [a is first for a in dog.acknowledged] == [True, False]
    assert dog.acknowledged[1] is second


@pytest.mark.asyncio
async def test_a_factory_that_raises_leaves_the_client_closed_and_the_next_connect_retries() -> (
    None
):
    attempts: list[NavimowSDK] = []
    dogs: list[FakeWatchdog] = []

    def factory(sdk: NavimowSDK) -> MqttWatchdog:
        attempts.append(sdk)
        if len(attempts) == 1:
            raise ValueError("no watchdog today")
        dogs.append(FakeWatchdog(sdk))
        return dogs[-1]  # type: ignore[return-value]

    client, session = make(*connect_replies(), both_docked(), watchdog=factory)
    with pytest.raises(ValueError, match="no watchdog today"):
        await client.async_connect()
    assert client.sdk is not None and not client.is_connected
    assert not FakeClient.instances[-1].named("connect_async") and client._tasks == set()
    assert requests(session) == ["v2"]  # nothing after the factory
    await client.async_connect()  # the restart builds the watchdog again and connects
    assert len(attempts) == 2 and client._watchdog is dogs[0]
    assert FakeClient.instances[-1].named("connect_async")
    assert requests(session) == ["v2", "getVehicleStatus"]


@pytest.mark.asyncio
async def test_a_check_that_raises_after_a_poll_is_reported_as_poll_and_the_poll_still_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _session, dogs = watched(
        replies=[*connect_replies(), status_reply(**{DEVICE: "isRunning"})]
    )
    recorder = Recorder(client)
    await connect(client)
    dogs[0].poll_error = RuntimeError("the check broke")
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        statuses = await client.async_poll([DEVICE])
    assert sorted(statuses) == [DEVICE]
    assert client.state(DEVICE) is not None
    assert client.state(DEVICE).status is MowerStatus.MOWING  # the observation was applied
    assert [(operation, str(exc)) for operation, exc in recorder.errors] == [
        ("poll", "the check broke")
    ]
    assert client.last_poll_error is None and "check after a poll failed" in caplog.text

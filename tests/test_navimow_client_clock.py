"""NavimowClient's clock: the poll task with its backoff, the silence tick, cancellation.

The real NavimowSDK runs on the fake paho client and REST goes to the fake
aiohttp session, as in test_navimow_client.py. The shared clock stands in for
time and datetime, and a sleeper of this module's stands in for the function
the clock's tasks wait through: it records each wait and completes it when
the test says, so a tick happens when the test advances the clock and releases
the task, never after a fixed time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import mower_sdk
from mower_sdk import navimow_client as client_module
from mower_sdk.errors import MowerAuthRequiredError, MowerRateLimitedError, MowerTransportError
from mower_sdk.models import MowerStatus
from mower_sdk.navimow_client import (
    COMMAND_POLL_DELAY_SECONDS,
    MQTT_STALE_SECONDS,
    REST_POLL_MAX_BACKOFF_SECONDS,
    REST_POLL_SECONDS,
    SILENCE_CHECK_SECONDS,
    MowerState,
    NavimowClient,
    StateSource,
)

from .fakes import FakeClient, FakeClock, FakeResponse, FakeSession, FakeSleeper, api_with, ok
from .test_navimow_client import (
    DEVICE,
    DEVICES,
    OTHER,
    Provider,
    Recorder,
    both_docked,
    broker_reply,
    connected,
    failure,
    requests,
    settle,
    state_message,
)

pytestmark = pytest.mark.usefixtures("fake_paho", "clock", "sleeper")

POLL = "NavimowClient poll"
SILENCE = "NavimowClient silence_check"


class OrderedSession(FakeSession):
    """Also records, per request, whether a paho client had started connecting by then."""

    def __init__(self, *responses: FakeResponse) -> None:
        super().__init__(*responses)
        self.paho_connecting: list[bool] = []
        self.clients: list[int] = []

    def request(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.paho_connecting.append(
            any(client.named("connect_async") for client in FakeClient.instances)
        )
        self.clients.append(len(FakeClient.instances))
        return super().request(*args, **kwargs)


def make(*responses: FakeResponse, **options: Any) -> tuple[NavimowClient, OrderedSession]:
    session = OrderedSession(*responses)
    api, _plain = api_with()
    api._session = session  # type: ignore[assignment]
    return NavimowClient(api, devices=DEVICES, **options), session


async def connect(client: NavimowClient) -> None:
    """Connect, and let the clock's tasks reach their first wait."""
    await client.async_connect()
    await settle(client)


async def tick(sleeper: FakeSleeper, clock: FakeClock, seconds: float, name: str = POLL) -> None:
    """Advance the clock and let the task's wait end, as if that long had passed."""
    clock.advance(seconds)
    await sleeper.release(name)


def test_the_constants_and_their_exports() -> None:
    assert (
        REST_POLL_SECONDS,
        REST_POLL_MAX_BACKOFF_SECONDS,
        SILENCE_CHECK_SECONDS,
        COMMAND_POLL_DELAY_SECONDS,
    ) == (120, 600, 30, 5)
    for name in (
        "REST_POLL_SECONDS",
        "REST_POLL_MAX_BACKOFF_SECONDS",
        "SILENCE_CHECK_SECONDS",
        "COMMAND_POLL_DELAY_SECONDS",
    ):
        assert getattr(mower_sdk, name) is getattr(client_module, name)
        assert name in mower_sdk.__all__ and name in client_module.__all__


@pytest.mark.parametrize(
    "options",
    [
        {"poll_interval": 0},
        {"poll_interval": -5},
        {"poll_interval": True},
        {"poll_interval": "120"},
        {"poll_backoff_max": 0},
        {"poll_backoff_max": None},
    ],
)
def test_a_non_positive_interval_or_cap_raises_at_construction(options: dict[str, Any]) -> None:
    api, _session = api_with()
    with pytest.raises(ValueError, match="NavimowClient"):
        NavimowClient(api, devices=DEVICES, **options)
    NavimowClient(api, devices=DEVICES, poll_interval=None, poll_backoff_max=1)


@pytest.mark.asyncio
async def test_connect_polls_once_before_the_feed_connects_and_the_task_polls_an_interval_later(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    provider = Provider(None, None)
    client, session = make(broker_reply(), both_docked(), both_docked(), token_provider=provider)
    await connect(client)
    assert requests(session) == ["v2", "getVehicleStatus"]
    assert session.paho_connecting == [False, False]  # the poll came before the connect
    assert provider.calls == 1  # the connect's call, not one of the poll's own
    assert sorted(task.get_name() for task in client._tasks) == [POLL, SILENCE]
    await sleeper.release(POLL)  # released without time passing: nothing is due
    assert sleeper.waits[POLL] == [120.0, 120.0]
    assert requests(session) == ["v2", "getVehicleStatus"]
    await tick(sleeper, clock, 120)
    assert requests(session) == ["v2", "getVehicleStatus", "getVehicleStatus"]
    assert (provider.calls, sleeper.waits[POLL][-1]) == (2, 120.0)
    assert client.last_poll_at == clock.wall_now


@pytest.mark.asyncio
async def test_a_failed_startup_poll_is_reported_starts_the_backoff_and_does_not_stop_the_connect(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, session = make(broker_reply(), failure(), both_docked())
    recorder = Recorder(client)
    await connect(client)
    assert [operation for operation, _ in recorder.errors] == ["poll"]
    assert isinstance(recorder.errors[0][1], MowerTransportError)
    assert FakeClient.instances[-1].named("connect_async")
    await settle(client)  # the tasks reach their first wait
    assert sleeper.waits[POLL] == [240.0]
    await tick(sleeper, clock, 240)
    assert requests(session) == ["v2", "getVehicleStatus", "getVehicleStatus"]
    assert client.last_poll_error is None and sleeper.waits[POLL][-1] == 120.0


@pytest.mark.asyncio
async def test_a_restart_polls_with_the_new_token_and_reconciles_after_the_poll(
    sleeper: FakeSleeper,
) -> None:
    client, session = make(broker_reply(), both_docked(), both_docked())
    await connect(client)
    await connected(client)
    await client.async_disconnect()
    assert client._tasks == set() and not sleeper.sleeping(POLL)
    await client.async_set_token("t2")
    await connect(client)
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"
    # No paho client before the build; one during both polls: the rebuild came after.
    assert session.clients == [0, 1, 1]
    assert (len(FakeClient.instances), client.mqtt.auth_headers) == (
        2,
        {"Authorization": "Bearer t2"},
    )
    assert sorted(task.get_name() for task in client._tasks) == [POLL, SILENCE]


@pytest.mark.asyncio
async def test_a_failed_poll_doubles_the_wait_up_to_the_cap_and_a_success_restores_the_interval(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, _session = make(
        broker_reply(),
        both_docked(),
        failure(),
        failure(),
        failure(),
        failure(),
        both_docked(),
        both_docked(),
    )
    recorder = Recorder(client)
    await connect(client)
    for wait in (240.0, 480.0, 600.0, 600.0):
        await tick(sleeper, clock, sleeper.waits[POLL][-1])
        assert sleeper.waits[POLL][-1] == wait
    assert [operation for operation, _ in recorder.errors] == ["poll"] * 4
    assert all(isinstance(exc, MowerTransportError) for _, exc in recorder.errors)
    await tick(sleeper, clock, 600)
    assert sleeper.waits[POLL][-1] == 120.0
    await tick(sleeper, clock, 120)
    assert sleeper.waits[POLL][-1] == 120.0
    assert len(recorder.errors) == 4


@pytest.mark.asyncio
async def test_a_cap_below_the_interval_is_raised_to_the_interval(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, _session = make(
        broker_reply(), both_docked(), failure(), failure(), poll_interval=100, poll_backoff_max=50
    )
    await connect(client)
    assert sleeper.waits[POLL] == [100.0]
    await tick(sleeper, clock, 100)
    await tick(sleeper, clock, 100)
    assert sleeper.waits[POLL] == [100.0, 100.0, 100.0]


@pytest.mark.asyncio
async def test_a_rate_limited_reply_backs_off_like_a_failure(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, _session = make(
        broker_reply(), both_docked(), FakeResponse({"code": 4001, "desc": "too frequent"})
    )
    recorder = Recorder(client)
    await connect(client)
    await tick(sleeper, clock, 120)
    assert isinstance(recorder.errors[0][1], MowerRateLimitedError)
    assert sleeper.waits[POLL][-1] == 240.0


@pytest.mark.asyncio
async def test_a_consumers_poll_resets_the_wait_and_a_failed_one_counts_for_the_backoff(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, session = make(
        broker_reply(), both_docked(), failure(), both_docked(), both_docked(), failure()
    )
    await connect(client)
    await tick(sleeper, clock, 120)  # the task's poll fails: the next is due in 240
    assert sleeper.waits[POLL][-1] == 240.0
    clock.advance(100)
    await client.async_poll()  # a success resets the wait: the next poll is 120 from now
    await tick(sleeper, clock, 120)
    assert requests(session).count("getVehicleStatus") == 4
    assert sleeper.waits[POLL][-1] == 120.0
    with pytest.raises(MowerTransportError):
        await client.async_poll()  # a failure counts: the next poll is 240 from now
    await tick(sleeper, clock, 120)  # the task wakes, finds nothing due yet, sleeps the rest
    assert sleeper.waits[POLL][-1] == 120.0
    assert requests(session).count("getVehicleStatus") == 5


@pytest.mark.asyncio
async def test_poll_interval_none_runs_no_poll_task_and_a_manual_poll_still_delivers(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, session = make(broker_reply(), both_docked(), both_docked(), poll_interval=None)
    recorder = Recorder(client)
    await connect(client)
    assert [task.get_name() for task in client._tasks] == [SILENCE]
    assert POLL not in sleeper.waits and sleeper.waits[SILENCE] == [30.0]
    await tick(sleeper, clock, 30, SILENCE)
    assert requests(session) == ["v2", "getVehicleStatus"]
    await client.async_poll()
    assert len(recorder.states) == 4
    assert client.last_poll_at == clock.wall_now


@pytest.mark.asyncio
async def test_the_silence_tick_delivers_a_flip_by_time_within_one_tick(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, _session = make(broker_reply(), both_docked(), both_docked())
    recorder = Recorder(client)
    await connect(client)
    await connected(client)
    await state_message(client, "isRunning")
    clock.advance(50)
    await client.async_poll()  # a newer REST observation, stored while MQTT is current
    assert client.state(DEVICE).source is StateSource.MQTT
    count = len(recorder.states)
    await tick(sleeper, clock, 30, SILENCE)  # nothing changed by time yet
    assert len(recorder.states) == count
    await tick(sleeper, clock, MQTT_STALE_SECONDS, SILENCE)  # the MQTT observation went stale
    assert len(recorder.states) == count + 1
    assert recorder.states[-1].device_id == DEVICE
    assert recorder.states[-1].source is StateSource.REST
    assert recorder.states[-1].status is MowerStatus.DOCKED
    assert client.state(DEVICE) is recorder.states[-1]
    await tick(sleeper, clock, 30, SILENCE)  # the choice stands: no new state
    assert len(recorder.states) == count + 1


def record_disconnect(client: NavimowClient, seen: list[list[bool]]) -> None:
    """Make the facade's disconnect record, when it runs, which of the client's tasks are done."""
    sdk = client.sdk
    assert sdk is not None
    tasks = list(client._tasks)
    original = sdk.disconnect

    def disconnect() -> None:
        seen.append([task.done() for task in tasks])
        original()

    sdk.disconnect = disconnect  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_disconnect_cancels_every_task_before_the_executor_disconnect(
    clock: FakeClock,
) -> None:
    client, session = make(broker_reply(), both_docked(), both_docked(), both_docked())
    await connect(client)
    await connected(client)
    tasks = set(client._tasks)
    done_at_disconnect: list[list[bool]] = []
    record_disconnect(client, done_at_disconnect)
    await client.async_disconnect()
    assert done_at_disconnect == [[True, True]]  # both tasks had ended before the disconnect
    assert all(task.cancelled() for task in tasks) and client._tasks == set()
    assert FakeClient.instances[-1].named("disconnect")
    clock.advance(1000)
    await settle(client)
    assert requests(session) == ["v2", "getVehicleStatus"]  # nothing of the clock's ran after


class HoldingProvider:
    """A provider with one answer per call, in call order, that holds the one call it is told to."""

    def __init__(self, *answers: str | None, hold: int) -> None:
        self.answers = answers
        self.hold = hold
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> str | None:
        self.calls += 1
        number = self.calls
        if number == self.hold:
            self.entered.set()
            await self.release.wait()
        return self.answers[number - 1]


@pytest.mark.asyncio
async def test_disconnect_cancels_a_task_waiting_for_the_lock(sleeper: FakeSleeper) -> None:
    provider = HoldingProvider(None, None, "t2", hold=2)
    client, session = make(broker_reply(), both_docked(), broker_reply(), token_provider=provider)
    await connect(client)
    await connected(client)
    refreshing = asyncio.ensure_future(client.async_refresh_broker_credentials())
    await provider.entered.wait()  # the refresh holds the lock in the provider
    disconnecting = asyncio.ensure_future(client.async_disconnect())
    await settle(client, refreshing, disconnecting)  # the disconnect waits at the lock
    poll_task = next(task for task in client._tasks if task.get_name() == POLL)
    client._next_poll_due = 0.0  # due now: the poll's token step learns t2, then waits at the lock
    sleeper.wake(POLL)
    await settle(client, refreshing, disconnecting)
    assert client.api.token == "t2"
    assert not poll_task.done() and not sleeper.sleeping(POLL)
    done_at_disconnect: list[list[bool]] = []
    record_disconnect(client, done_at_disconnect)
    provider.release.set()
    assert await refreshing is True
    await disconnecting
    assert done_at_disconnect == [[True, True]]  # the waiting task too, before the disconnect
    assert poll_task.cancelled() and client._tasks == set()
    assert requests(session) == ["v2", "getVehicleStatus", "v2"]  # the waiting poll never ran
    assert FakeClient.instances[-1].named("disconnect")


@pytest.mark.asyncio
async def test_a_task_failure_is_logged_reported_and_the_task_continues(
    sleeper: FakeSleeper, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    client, session = make(broker_reply(), both_docked(), failure(), both_docked())
    recorder = Recorder(client)
    await connect(client)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        await tick(sleeper, clock, 120)
    assert [operation for operation, _ in recorder.errors] == ["poll"]
    assert "the clock's poll failed" in caplog.text
    await tick(sleeper, clock, 240)
    assert requests(session).count("getVehicleStatus") == 3

    def broken() -> None:
        raise RuntimeError("tick")

    client._silence_tick = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        await tick(sleeper, clock, 30, SILENCE)
    assert recorder.errors[-1][0] == "silence_check"
    assert isinstance(recorder.errors[-1][1], RuntimeError)
    assert sleeper.waits[SILENCE][-1] == 30.0  # the task went on


@pytest.mark.asyncio
async def test_a_refused_token_on_the_clock_is_reported_and_polling_continues_with_backoff(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    client, session = make(broker_reply(), both_docked(), failure(401), both_docked())
    recorder = Recorder(client)
    await connect(client)
    await tick(sleeper, clock, 120)
    assert isinstance(recorder.errors[0][1], MowerAuthRequiredError)
    assert client.last_poll_error is recorder.errors[0][1]
    assert sleeper.waits[POLL][-1] == 240.0
    await client.async_set_token("t2")  # the consumer re-authenticates
    await tick(sleeper, clock, 240)
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"
    assert client.last_poll_error is None


@pytest.mark.asyncio
async def test_a_provider_that_raises_on_the_clock_is_reported_the_same_way(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    provider = Provider(None, RuntimeError("no token"), "t2")
    client, session = make(broker_reply(), both_docked(), both_docked(), token_provider=provider)
    recorder = Recorder(client)
    await connect(client)
    await tick(sleeper, clock, 120)
    assert [(operation, str(exc)) for operation, exc in recorder.errors] == [("poll", "no token")]
    assert isinstance(client.last_poll_error, RuntimeError)
    assert requests(session) == ["v2", "getVehicleStatus"]  # no request after the provider failed
    assert sleeper.waits[POLL][-1] == 240.0
    await tick(sleeper, clock, 240)
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"
    assert client.mqtt.auth_headers == {"Authorization": "Bearer t2"}


@pytest.mark.asyncio
async def test_a_poll_whose_request_succeeded_returns_the_statuses_when_a_callback_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _session = make(broker_reply(), both_docked(), both_docked())

    def explode(_state: MowerState) -> None:
        raise RuntimeError("boom")

    client.on_state(explode)
    await connect(client)
    with caplog.at_level(logging.ERROR, logger="mower_sdk.navimow_client"):
        statuses = await client.async_poll()
    assert sorted(statuses) == [DEVICE, OTHER]
    assert "boom" in caplog.text


@pytest.mark.asyncio
async def test_a_clock_poll_queued_behind_a_manual_poll_that_moved_the_deadline_sends_nothing(
    sleeper: FakeSleeper, clock: FakeClock
) -> None:
    from .test_navimow_client import GatedResponse

    held = GatedResponse(ok({"devices": [{"id": DEVICE, "vehicleState": "isRunning"}]}))
    client, session = make(broker_reply(), both_docked(), held, both_docked())
    await connect(client)
    clock.advance(110)
    manual = asyncio.ensure_future(client.async_poll())  # holds the poll lock at its request
    await held.entered.wait()
    clock.advance(10)  # the clock's poll is due
    sleeper.wake(POLL)
    await settle(client, manual)  # the poll task now waits for the poll lock
    held.release.set()
    await manual
    await sleeper.until_sleeping(POLL)  # the poll task found nothing due and sleeps again
    assert requests(session).count("getVehicleStatus") == 2  # the startup poll and the manual one
    assert client.state(DEVICE) is not None and client.state(DEVICE).status is MowerStatus.MOWING
    assert sleeper.waits[POLL][-1] == 120.0  # the manual poll's success set the next poll

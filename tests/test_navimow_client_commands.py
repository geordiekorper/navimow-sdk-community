"""NavimowClient.async_send_command: the receipt, the token step and the poll that follows.

The real NavimowSDK runs on the fake paho client, REST goes to the fake
aiohttp session, the shared clock stands in for time and datetime, and the
sleeper holds the clock's tasks, the poll after a command included, until the
test releases them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mower_sdk.errors import MowerAPIError, MowerTransportError
from mower_sdk.models import CommandVerdict, MowerCommand, MowerStatus
from mower_sdk.navimow_client import COMMAND_POLL_DELAY_SECONDS, NavimowClient

from .fakes import FakeResponse, FakeSleeper
from .test_navimow_client import (
    DEVICE,
    GatedResponse,
    Provider,
    Recorder,
    bearer,
    both_docked,
    connect_replies,
    connected,
    failure,
    make,
    requests,
    settle,
    status_reply,
)
from .test_navimow_client_clock import POLL, connect, tick

pytestmark = pytest.mark.usefixtures("fake_paho", "clock", "sleeper")

COMMAND_POLL = "NavimowClient command_poll"


def accepted(command_number: str = "42") -> FakeResponse:
    """A sendCommands reply the cloud accepted."""
    return FakeResponse(
        {
            "code": 1,
            "desc": "success",
            "data": {
                "payload": {
                    "commands": [
                        {"status": "SUCCESS", "devices": [{"id": DEVICE, "cmdNum": command_number}]}
                    ]
                }
            },
        }
    )


def refused(error_code: str = "lowBattery") -> FakeResponse:
    """A sendCommands reply the cloud refused."""
    return FakeResponse(
        {
            "code": 1,
            "desc": "success",
            "data": {"payload": {"commands": [{"status": "ERROR", "errorCode": error_code}]}},
        }
    )


async def command_poll_done(client: NavimowClient) -> None:
    """Wait for the pending poll after a command to end."""
    task = client._command_poll
    assert task is not None
    async with asyncio.timeout(5):
        await asyncio.wait([task])


@pytest.mark.asyncio
async def test_a_command_asks_the_provider_first_and_keeps_the_receipt(
    sleeper: FakeSleeper,
) -> None:
    provider = Provider(None, "t2")
    client, session = make(*connect_replies(), accepted(), token_provider=provider)
    await connect(client)
    await connected(client)
    receipt = await client.async_send_command(DEVICE, MowerCommand.START)
    assert (receipt.device_id, receipt.command) == (DEVICE, MowerCommand.START)
    assert receipt.verdict is CommandVerdict.ACCEPTED and receipt.command_number == "42"
    assert client.last_receipt(DEVICE) is receipt and client.last_receipt("nope") is None
    assert session.requests[-1]["url"].endswith("sendCommands")
    assert session.requests[-1]["headers"]["Authorization"] == "Bearer t2"
    assert (provider.calls, client.mqtt.auth_headers) == (2, bearer("t2"))
    await sleeper.until_sleeping(COMMAND_POLL)
    assert sleeper.waits[COMMAND_POLL] == [float(COMMAND_POLL_DELAY_SECONDS)]


@pytest.mark.asyncio
async def test_a_refused_command_raises_the_rest_clients_error_unchanged_and_keeps_no_receipt(
    sleeper: FakeSleeper,
) -> None:
    client, _session = make(*connect_replies(), refused(), both_docked(), failure())
    await connect(client)
    with pytest.raises(MowerAPIError) as info:
        await client.async_send_command(DEVICE, MowerCommand.DOCK)
    assert info.value.error_code == "lowBattery"
    assert info.value.results == ({"status": "ERROR", "errorCode": "lowBattery"},)
    assert client.last_receipt(DEVICE) is None
    await sleeper.until_sleeping(COMMAND_POLL)
    assert sleeper.waits[COMMAND_POLL] == [5.0]  # the poll follows a refusal too
    sleeper.wake(COMMAND_POLL)
    await command_poll_done(client)
    with pytest.raises(MowerTransportError):
        await client.async_send_command(DEVICE, MowerCommand.PAUSE)
    assert client.last_receipt(DEVICE) is None


@pytest.mark.asyncio
async def test_every_command_schedules_one_poll_and_a_second_leaves_the_pending_one(
    sleeper: FakeSleeper,
) -> None:
    client, session = make(
        *connect_replies(), accepted(), accepted(), status_reply(**{DEVICE: "isRunning"})
    )
    recorder = Recorder(client)
    await connect(client)
    await client.async_send_command(DEVICE, MowerCommand.START)
    await client.async_send_command(DEVICE, MowerCommand.RESUME)
    await sleeper.until_sleeping(COMMAND_POLL)
    assert sleeper.waits[COMMAND_POLL] == [5.0]  # one pending poll for two commands
    assert requests(session).count("getVehicleStatus") == 1
    sleeper.wake(COMMAND_POLL)
    await command_poll_done(client)
    assert requests(session).count("getVehicleStatus") == 2
    assert client.state(DEVICE) is not None
    assert client.state(DEVICE).status is MowerStatus.MOWING
    assert recorder.states[-1].status is MowerStatus.MOWING
    assert recorder.errors == []


@pytest.mark.asyncio
async def test_disconnect_cancels_a_pending_command_poll_and_a_reply_after_it_schedules_none(
    sleeper: FakeSleeper,
) -> None:
    client, session = make(*connect_replies(), accepted())
    await connect(client)
    await connected(client)
    await client.async_send_command(DEVICE, MowerCommand.START)
    pending = client._command_poll
    await sleeper.until_sleeping(COMMAND_POLL)
    assert pending is not None and sleeper.sleeping(COMMAND_POLL)
    await client.async_disconnect()
    assert pending.cancelled() and client._tasks == set()
    assert requests(session).count("getVehicleStatus") == 1
    # A command whose reply arrives after the disconnect: no poll is scheduled.
    held = GatedResponse(
        {
            "code": 1,
            "desc": "success",
            "data": {"payload": {"commands": [{"status": "SUCCESS", "devices": [{"id": DEVICE}]}]}},
        }
    )
    session.responses.extend([both_docked(), held])  # the restart's poll, then the command
    await client.async_connect()
    await connected(client)
    sending = asyncio.ensure_future(client.async_send_command(DEVICE, MowerCommand.PAUSE))
    await held.entered.wait()
    await client.async_disconnect()
    held.release.set()
    receipt = await sending
    assert receipt.accepted and client.last_receipt(DEVICE) is receipt
    assert client._command_poll is pending  # nothing new was scheduled
    assert client._tasks == set()


@pytest.mark.asyncio
async def test_a_failed_command_poll_reaches_on_error_as_command_poll(
    sleeper: FakeSleeper,
) -> None:
    client, _session = make(*connect_replies(), accepted(), failure())
    recorder = Recorder(client)
    await connect(client)
    await client.async_send_command(DEVICE, MowerCommand.START)
    await sleeper.until_sleeping(COMMAND_POLL)
    sleeper.wake(COMMAND_POLL)
    await command_poll_done(client)
    assert [operation for operation, _ in recorder.errors] == ["command_poll"]
    assert isinstance(recorder.errors[0][1], MowerTransportError)
    assert client.last_poll_error is recorder.errors[0][1]


@pytest.mark.asyncio
async def test_a_command_poll_resets_the_backoff_on_success_and_never_the_deadline(
    sleeper: FakeSleeper, clock: Any
) -> None:
    client, _session = make(*connect_replies(), failure(), accepted(), both_docked())
    await connect(client)
    await tick(sleeper, clock, 120)  # the clock's poll fails: the wait doubles
    assert (client._poll_wait, sleeper.waits[POLL][-1]) == (240.0, 240.0)
    due = client._next_poll_due
    await client.async_send_command(DEVICE, MowerCommand.START)
    await sleeper.until_sleeping(COMMAND_POLL)
    sleeper.wake(COMMAND_POLL)
    await command_poll_done(client)
    assert client._poll_wait == 120.0  # the backoff is reset
    assert client._next_poll_due == due  # the next scheduled poll is where it was
    assert client.last_poll_error is None


@pytest.mark.asyncio
async def test_a_provider_that_raises_sends_no_command_and_schedules_no_poll(
    sleeper: FakeSleeper,
) -> None:
    provider = Provider(None, RuntimeError("no token"))
    client, session = make(*connect_replies(), token_provider=provider)
    await connect(client)
    with pytest.raises(RuntimeError, match="no token"):
        await client.async_send_command(DEVICE, MowerCommand.START)
    assert requests(session) == ["v2", "getVehicleStatus"]
    assert COMMAND_POLL not in sleeper.waits and client.last_receipt(DEVICE) is None


@pytest.mark.asyncio
async def test_a_command_before_connect_sends_and_schedules_no_poll(sleeper: FakeSleeper) -> None:
    client, session = make(accepted())
    receipt = await client.async_send_command(DEVICE, MowerCommand.START)
    assert receipt.accepted and client.last_receipt(DEVICE) is receipt
    assert requests(session) == ["sendCommands"]
    assert COMMAND_POLL not in sleeper.waits and client._tasks == set()
    await settle(client)


@pytest.mark.asyncio
async def test_a_command_poll_scheduled_during_an_interrupted_connect_is_cancelled_with_it(
    sleeper: FakeSleeper,
) -> None:
    held = GatedResponse(
        {
            "code": 1,
            "desc": "success",
            "data": {"payload": {"devices": [{"id": DEVICE, "vehicleState": "isDocked"}]}},
        }
    )
    client, session = make(connect_replies()[0], held, accepted(), both_docked(), both_docked())
    connecting = asyncio.ensure_future(client.async_connect())
    await held.entered.wait()  # the startup poll is in flight: the facade exists, not closed
    await client.async_send_command(DEVICE, MowerCommand.START)  # its poll is scheduled
    await sleeper.until_sleeping(COMMAND_POLL)
    pending = client._command_poll
    assert pending is not None
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert pending.cancelled() and client._tasks == set()  # cancelled with the connect
    await client.async_disconnect()  # already closed: nothing more
    held.release.set()
    await settle(client)
    assert requests(session) == ["v2", "getVehicleStatus", "sendCommands"]
    await client.async_connect()  # the restart works, and polls once more
    assert requests(session) == ["v2", "getVehicleStatus", "sendCommands", "getVehicleStatus"]

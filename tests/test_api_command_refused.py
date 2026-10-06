"""Tests for the MowerAPIError a refused command raises: it carries the reply's results.

A fake aiohttp session with no network answers the sendCommands endpoint. When
a result in ``data.payload.commands`` is an ERROR other than alreadyInState,
``async_send_command`` and ``async_send_command_receipt`` raise a plain
``MowerAPIError`` whose ``results`` are the reply's result dicts, read as a
``CommandReceipt`` reads them: every dict entry in order, the refusing one and
those after it included, entries that are not dicts left out. The message and
``error_code`` are as before. An error raised before any result is read (no
token, an HTTP status, an envelope code, a null ``data``, a command that is
not a ``MowerCommand``) has an empty tuple.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mower_sdk.api import MowerAPI
from mower_sdk.errors import (
    ERROR_MESSAGES,
    MowerAPIError,
    MowerAuthRequiredError,
    MowerTransportError,
)
from mower_sdk.models import MowerCommand

from .fakes import TOKEN, FakeResponse, api_with, ok

DEVICE_ID = "dev-1"
SUCCESS = {"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"}
ALREADY = {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": "alreadyInState"}
# A START refused for a low battery, as the cloud answered one.
LOW_BATTERY = {
    "devices": [{"id": DEVICE_ID, "cmdNum": None}],
    "status": "ERROR",
    "errorCode": "lowBattery",
}

Send = Callable[[MowerAPI, str, MowerCommand], Awaitable[Any]]
SENDS = [
    pytest.param(MowerAPI.async_send_command, id="send_command"),
    pytest.param(MowerAPI.async_send_command_receipt, id="send_command_receipt"),
]


@pytest.mark.parametrize("send", SENDS)
@pytest.mark.asyncio
async def test_a_refusal_with_a_code_carries_every_result_dict(send: Send) -> None:
    payload = {"commands": [ALREADY, LOW_BATTERY, SUCCESS, "SUCCESS", None]}
    api, _ = api_with(FakeResponse(ok(payload)))
    with pytest.raises(MowerAPIError) as info:
        await send(api, DEVICE_ID, MowerCommand.START)
    assert type(info.value) is MowerAPIError
    assert info.value.results == (ALREADY, LOW_BATTERY, SUCCESS)
    assert info.value.error_code == "lowBattery"
    assert str(info.value) == (
        f"{ERROR_MESSAGES['COMMAND_FAILED']}: lowBattery | Error Code: lowBattery"
    )


@pytest.mark.parametrize(
    "refused",
    [
        {"devices": [{"id": DEVICE_ID}], "status": "ERROR"},
        {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": None},
        {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": ""},
    ],
    ids=["no_errorCode", "null_errorCode", "empty_errorCode"],
)
@pytest.mark.parametrize("send", SENDS)
@pytest.mark.asyncio
async def test_a_refusal_without_a_code_carries_the_result_dict(
    send: Send, refused: dict[str, Any]
) -> None:
    api, _ = api_with(FakeResponse(ok({"commands": [refused]})))
    with pytest.raises(MowerAPIError) as info:
        await send(api, DEVICE_ID, MowerCommand.DOCK)
    assert info.value.results == (refused,)
    assert info.value.error_code == "COMMAND_FAILED"


@pytest.mark.parametrize(
    ("responses", "token", "command", "cls", "requests"),
    [
        ((), None, MowerCommand.START, MowerAuthRequiredError, 0),
        (
            (FakeResponse(status=400, body=b'{"code": 400}'),),
            TOKEN,
            MowerCommand.START,
            MowerAPIError,
            1,
        ),
        (
            (FakeResponse({"code": 4000, "desc": "refused", "data": None}),),
            TOKEN,
            MowerCommand.START,
            MowerAPIError,
            1,
        ),
        (
            (FakeResponse({"code": 1, "desc": "success", "data": None}),),
            TOKEN,
            MowerCommand.START,
            MowerTransportError,
            1,
        ),
        ((), TOKEN, "bogus", MowerAPIError, 0),
    ],
    ids=["no_token", "http_status", "envelope_code", "null_data", "invalid_command"],
)
@pytest.mark.parametrize("send", SENDS)
@pytest.mark.asyncio
async def test_an_error_raised_before_the_results_are_read_has_an_empty_tuple(
    send: Send,
    responses: tuple[FakeResponse, ...],
    token: str | None,
    command: Any,
    cls: type[MowerAPIError],
    requests: int,
) -> None:
    api, session = api_with(*responses, token=token)
    with pytest.raises(MowerAPIError) as info:
        await send(api, DEVICE_ID, command)
    assert type(info.value) is cls
    assert info.value.results == ()
    assert len(session.requests) == requests

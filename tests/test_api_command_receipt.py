"""Tests for MowerAPI.async_send_command_receipt and the command-number extractor.

A fake aiohttp session with no network answers the sendCommands endpoint. The
receipt's verdict is read from ``data.payload.commands``: alreadyInState wins
over SUCCESS, SUCCESS gives accepted, anything else is unknown. Any other ERROR
and any transport failure
raise as ``async_send_command`` does and return no receipt, and
``async_send_command`` itself keeps returning the raw reply data. A ``commands``
that is null, not a list or holds entries that are not dicts gives fewer
results, not an exception, and the receipt is hashable. The extractor reads a
command number only from a recognised key, never a bare scalar from a list.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import aiohttp
import pytest

import mower_sdk
from mower_sdk import models
from mower_sdk.api import MowerAPI, _extract_command_number
from mower_sdk.errors import ERROR_MESSAGES, MowerAPIError
from mower_sdk.models import CommandReceipt, CommandVerdict, MowerCommand

from .fakes import BASE_URL, FakeResponse, api_with, ok

DEVICE_ID = "dev-1"
SUCCESS = {"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"}
ALREADY = {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": "alreadyInState"}
NO_STATUS = {"devices": [{"id": DEVICE_ID}]}


async def receipt_for(payload: Any, command: MowerCommand = MowerCommand.START) -> CommandReceipt:
    api, _ = api_with(FakeResponse(ok(payload)))
    return await api.async_send_command_receipt(DEVICE_ID, command)


@pytest.mark.parametrize(
    ("payload", "verdict"),
    [
        ({"commands": []}, CommandVerdict.UNKNOWN),
        ({"commands": [NO_STATUS]}, CommandVerdict.UNKNOWN),
        ({}, CommandVerdict.UNKNOWN),
        ({"commands": [SUCCESS]}, CommandVerdict.ACCEPTED),
        ({"commands": [SUCCESS, NO_STATUS]}, CommandVerdict.ACCEPTED),
        ({"commands": [SUCCESS, ALREADY]}, CommandVerdict.ALREADY_IN_STATE),
        ({"commands": [ALREADY, SUCCESS]}, CommandVerdict.ALREADY_IN_STATE),
        ({"commands": [ALREADY]}, CommandVerdict.ALREADY_IN_STATE),
    ],
    ids=[
        "empty_list",
        "neither_status",
        "no_commands_key",
        "success",
        "success_then_no_status",
        "success_then_already_in_state",
        "already_in_state_then_success",
        "already_in_state",
    ],
)
@pytest.mark.asyncio
async def test_verdict(payload: dict[str, Any], verdict: CommandVerdict) -> None:
    receipt = await receipt_for(payload)
    assert receipt.verdict is verdict
    assert receipt.accepted is (verdict is CommandVerdict.ACCEPTED)
    assert receipt.already_in_state is (verdict is CommandVerdict.ALREADY_IN_STATE)
    assert receipt.results == tuple(payload.get("commands", []))


@pytest.mark.asyncio
async def test_receipt_carries_the_device_the_command_and_the_results() -> None:
    receipt = await receipt_for({"commands": [ALREADY, SUCCESS]}, MowerCommand.DOCK)
    assert receipt == CommandReceipt(
        device_id=DEVICE_ID,
        command=MowerCommand.DOCK,
        verdict=CommandVerdict.ALREADY_IN_STATE,
        command_number=None,
        results=(ALREADY, SUCCESS),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.verdict = CommandVerdict.UNKNOWN  # type: ignore[misc]


@pytest.mark.parametrize(
    ("payload", "verdict", "results"),
    [
        ({"commands": None}, CommandVerdict.UNKNOWN, ()),
        ({"commands": "SUCCESS"}, CommandVerdict.UNKNOWN, ()),
        ({"commands": [None, "SUCCESS", 1]}, CommandVerdict.UNKNOWN, ()),
        ({"commands": [None, SUCCESS, ["ERROR"]]}, CommandVerdict.ACCEPTED, (SUCCESS,)),
        (None, CommandVerdict.UNKNOWN, ()),
    ],
    ids=[
        "null_commands",
        "commands_not_a_list",
        "only_non_dict_entries",
        "non_dict_entries_left_out",
        "null_payload",
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_command_list_gives_fewer_results_not_an_exception(
    payload: Any, verdict: CommandVerdict, results: tuple[dict[str, Any], ...]
) -> None:
    receipt = await receipt_for(payload)
    assert receipt.verdict is verdict
    assert receipt.results == results
    api, _ = api_with(FakeResponse(ok(payload)))
    raw = await api.async_send_command(DEVICE_ID, MowerCommand.START)
    assert raw == {"payload": payload}


@pytest.mark.asyncio
async def test_receipt_is_hashable_and_equal_receipts_hash_alike() -> None:
    first = await receipt_for({"commands": [SUCCESS]})
    second = await receipt_for({"commands": [SUCCESS]})
    assert first == second
    assert hash(first) == hash(second)
    assert {first, second} == {first}
    assert await receipt_for({"commands": [ALREADY]}) not in {first}
    only_results_differ = await receipt_for({"commands": [SUCCESS, NO_STATUS]})
    assert only_results_differ != first
    assert hash(only_results_differ) == hash(first)  # results are left out of the hash


@pytest.mark.asyncio
async def test_receipt_request_is_the_send_commands_request() -> None:
    api, session = api_with(FakeResponse(ok({"commands": [SUCCESS]})))
    await api.async_send_command_receipt(DEVICE_ID, MowerCommand.PAUSE)
    (request,) = session.requests
    assert request["method"] == "POST"
    assert request["url"] == f"{BASE_URL.rstrip('/')}/openapi/smarthome/sendCommands"
    assert request["json"] == {
        "commands": [
            {
                "devices": [{"id": DEVICE_ID}],
                "execution": {"command": "action.devices.commands.PauseUnpause", "params": {"on": False}},
            }
        ]
    }


@pytest.mark.asyncio
async def test_another_error_raises_as_before_and_returns_no_receipt() -> None:
    payload = {"commands": [ALREADY, {"status": "ERROR", "errorCode": "deviceOffline"}]}
    api, _ = api_with(FakeResponse(ok(payload)), FakeResponse(ok(payload)))
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command_receipt(DEVICE_ID, MowerCommand.START)
    assert info.value.message == f"{ERROR_MESSAGES['COMMAND_FAILED']}: deviceOffline"
    assert info.value.error_code == "deviceOffline"
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command(DEVICE_ID, MowerCommand.START)
    assert info.value.error_code == "deviceOffline"


@pytest.mark.asyncio
async def test_an_unknown_command_is_rejected_before_any_request() -> None:
    api, session = api_with()
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command_receipt(DEVICE_ID, "bogus")  # type: ignore[arg-type]
    assert info.value.error_code == "INVALID_COMMAND"
    assert session.requests == []


@pytest.mark.parametrize(
    "cause",
    [aiohttp.ClientConnectionError("connection reset"), TimeoutError()],
    ids=["client_error", "timeout"],
)
@pytest.mark.asyncio
async def test_a_transport_error_raises_with_its_cause_and_returns_no_receipt(cause: Exception) -> None:
    api, _ = api_with(FakeResponse(error=cause))
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command_receipt(DEVICE_ID, MowerCommand.START)
    assert info.value.__cause__ is cause


@pytest.mark.asyncio
async def test_async_send_command_still_returns_the_raw_data() -> None:
    payload = {"commands": [ALREADY, SUCCESS]}
    api, _ = api_with(FakeResponse(ok(payload)))
    assert await api.async_send_command(DEVICE_ID, MowerCommand.START) == {"payload": payload}


def test_no_synchronous_wrapper_is_added() -> None:
    assert not hasattr(MowerAPI, "send_command_receipt")


def test_exports() -> None:
    assert mower_sdk.CommandReceipt is models.CommandReceipt
    assert mower_sdk.CommandVerdict is models.CommandVerdict
    assert {"CommandReceipt", "CommandVerdict"} <= set(mower_sdk.__all__)
    assert CommandVerdict.ACCEPTED == "accepted"
    assert CommandVerdict.ALREADY_IN_STATE == "already_in_state"
    assert CommandVerdict.UNKNOWN == "unknown"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"payload": {"commands": [{"cmdNum": 42}]}}, "42"),
        ({"payload": {"commands": [{"devices": [{"id": DEVICE_ID, "cmd_num": "7"}]}]}}, "7"),
        ({"commandNum": " 9 "}, "9"),
        ({"command_num": 1}, "1"),
        ({"commandNumber": "abc"}, "abc"),
        ({"cmdNum": None, "commands": [{"command_number": 3}]}, "3"),
        ({"cmdNum": "", "commandNum": "5"}, "5"),
        ({"cmdNum": 8, "commandNum": "5"}, "8"),
        ({"payload": {"warnings": ["notice"], "commands": []}}, None),
        ({"devices": ["d"]}, None),
        ({"payload": {"commands": [{"cmdNum": True}]}}, None),
        ({"payload": {"commands": [{"cmdNum": 4.5}]}}, None),
        ({"cmdNum": {"value": 6}}, None),
        ({"payload": {"commands": [["cmdNum", 42]]}}, None),
        ({"payload": {"accepted": True}}, None),
        ({}, None),
        (None, None),
        ("42", None),
        (42, None),
    ],
    ids=[
        "cmdNum_in_result",
        "cmd_num_in_device",
        "commandNum_stripped",
        "command_num_int",
        "commandNumber_text",
        "null_then_nested",
        "empty_then_next_key",
        "first_key_wins",
        "bare_scalar_in_list_not_taken",
        "device_id_in_list_not_taken",
        "bool_not_taken",
        "float_not_taken",
        "nested_under_key_not_taken",
        "list_in_list_not_taken",
        "no_key",
        "empty_dict",
        "none",
        "bare_string",
        "bare_int",
    ],
)
def test_command_number_extraction(value: Any, expected: str | None) -> None:
    assert _extract_command_number(value) == expected


@pytest.mark.asyncio
async def test_receipt_command_number_comes_from_the_reply() -> None:
    assert (await receipt_for({"commands": [{**SUCCESS, "cmdNum": 42}]})).command_number == "42"
    assert (await receipt_for({"commands": [SUCCESS]})).command_number is None

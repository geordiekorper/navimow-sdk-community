"""Tests for MowerAPI.async_get_command_result, the single-device result query.

A fake aiohttp session with no network answers the responseCommands endpoint.
The query names the device and carries cmdNum only when one is given; the
entry whose id equals the device id is returned, and None when no entry
matches, an entry without an id included.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mower_sdk.api import MowerAPI
from mower_sdk.errors import ERROR_MESSAGES, MowerAPIError

BASE_URL = "https://api.example.invalid"
DEVICE_ID = "dev-1"
MINE = {"id": DEVICE_ID, "cmdNum": "7", "status": "SUCCESS"}
OTHERS = {"id": "dev-2", "cmdNum": "8", "status": "SUCCESS"}
NO_ID = {"cmdNum": "9", "status": "SUCCESS"}


class FakeResponse:
    def __init__(self, body: Any) -> None:
        self.status = 200
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def json(self) -> Any:
        return self._body

    async def read(self) -> bytes:
        return json.dumps(self._body).encode()

    async def text(self) -> str:
        return ""


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def ok(payload: Any) -> dict[str, Any]:
    return {"code": 1, "desc": "success", "data": {"payload": payload}}


def api_with(body: Any) -> tuple[MowerAPI, FakeSession]:
    session = FakeSession(FakeResponse(body))
    return MowerAPI(session=session, token="token", base_url=BASE_URL), session  # type: ignore[arg-type]


async def result_for(devices: Any, cmd_num: str | None = None) -> Any:
    api, _ = api_with(ok({"devices": devices}))
    return await api.async_get_command_result(DEVICE_ID, cmd_num)


@pytest.mark.parametrize(
    ("cmd_num", "query"),
    [
        (None, {"id": DEVICE_ID}),
        ("7", {"id": DEVICE_ID, "cmdNum": "7"}),
        ("", {"id": DEVICE_ID, "cmdNum": ""}),
    ],
    ids=["without_command_number", "with_command_number", "empty_command_number_is_sent"],
)
@pytest.mark.asyncio
async def test_query_names_the_device_and_carries_cmd_num_only_when_given(
    cmd_num: str | None, query: dict[str, str]
) -> None:
    api, session = api_with(ok({"devices": [MINE]}))
    await api.async_get_command_result(DEVICE_ID, cmd_num)
    (request,) = session.requests
    assert request["method"] == "POST"
    assert request["url"] == f"{BASE_URL}/openapi/smarthome/responseCommands"
    assert request["json"] == {"devices": [query]}


@pytest.mark.parametrize(
    ("devices", "expected"),
    [
        ([MINE], MINE),
        ([OTHERS, MINE], MINE),
        ([NO_ID, MINE], MINE),
        ([OTHERS], None),
        ([NO_ID], None),
        ([], None),
        (["dev-1"], None),
    ],
    ids=[
        "only_mine",
        "mine_after_another",
        "mine_after_one_without_id",
        "another_device_only",
        "no_id_does_not_match",
        "empty",
        "non_dict_entry_ignored",
    ],
)
@pytest.mark.asyncio
async def test_the_entry_whose_id_matches_is_returned_else_none(devices: list[Any], expected: Any) -> None:
    assert await result_for(devices) == expected


@pytest.mark.asyncio
async def test_missing_devices_key_is_none() -> None:
    api, _ = api_with(ok({}))
    assert await api.async_get_command_result(DEVICE_ID) is None


@pytest.mark.asyncio
async def test_envelope_failure_raises() -> None:
    api, _ = api_with({"code": 4005, "desc": "oauth info illegal", "data": {}})
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_command_result(DEVICE_ID, "7")
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: oauth info illegal"


def test_no_synchronous_wrapper_is_added() -> None:
    assert not hasattr(MowerAPI, "get_command_result")

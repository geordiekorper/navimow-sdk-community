"""Characterisation tests for MowerAPI's REST envelope handling.

A fake aiohttp session, with no network, drives all five endpoints through the
cases the envelope check decides: success, ``code != 1``, a missing ``data``
key against an explicit ``"data": null``, ``alreadyInState`` suppression, a
command ``ERROR``, and the mapping from HTTP 404 to ``DEVICE_NOT_FOUND``. It
also records the ``timeout`` keyword each request carries, so the request
timeout's default, a custom value and ``None`` can be checked.

Every expectation is what the code does today, pinned so the envelope refactor
can show that nothing observable changed. That includes the ugly cases: an
explicit ``"data": null`` raises AttributeError from the two command
endpoints, and that is pinned on purpose rather than corrected here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import mower_sdk
from mower_sdk.api import MowerAPI
from mower_sdk.errors import (
    ERROR_MESSAGES,
    MowerAPIError,
    MowerAuthRequiredError,
    MowerRateLimitedError,
    MowerTransportError,
)
from mower_sdk.models import Device, DeviceStatus, MowerCommand, MowerError, MowerStatus

BASE_URL = "https://api.example.invalid/"
TOKEN = "token-123"
DEVICE_ID = "dev-1"
NO_PAYLOAD = object()
REQUEST_INFO = aiohttp.RequestInfo(
    url=URL(BASE_URL), method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=URL(BASE_URL)
)


class FakeResponse:
    """What ``session.request(...)`` returns: an async context manager.

    It holds the body as bytes with a content type and reads it the way
    aiohttp's ClientResponse does: ``json()`` raises ContentTypeError for a
    type other than JSON, returns None for an empty body, else decodes (UTF-8,
    or the charset given) and parses; ``text()`` decodes; ``read()`` returns
    the bytes. A payload given positionally is sent as a JSON body.
    """

    def __init__(
        self,
        payload: Any = NO_PAYLOAD,
        *,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json",
        error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._body = body if payload is NO_PAYLOAD else json.dumps(payload).encode()
        self.content_type = content_type
        self._error = error
        self._read_error = read_error

    async def __aenter__(self) -> FakeResponse:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def _encoding(self) -> str:
        _, _, charset = self.content_type.partition("charset=")
        return charset.strip() or "utf-8"

    async def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    async def text(self) -> str:
        return self._body.decode(self._encoding())

    async def json(self) -> Any:
        mimetype = self.content_type.split(";")[0].strip().lower()
        if not re.fullmatch(r"application/(?:[\w.+-]+\+)?json", mimetype):
            raise aiohttp.ContentTypeError(
                REQUEST_INFO,
                (),
                status=self.status,
                message=f"Attempt to decode JSON with unexpected mimetype: {mimetype}",
            )
        stripped = self._body.strip()
        if not stripped:
            return None
        return json.loads(stripped.decode(self._encoding()))


# Recorded as a request's ``timeout`` when the keyword was not passed at all, which
# aiohttp treats differently from ``timeout=None`` (no timeout at all).
NOT_PASSED = object()


class FakeSession:
    """Records every request and hands out the queued responses in order."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False  # read by MowerAPI.__del__

    def request(
        self,
        method: str,
        url: str,
        json: Any = None,
        params: Any = None,
        headers: dict[str, str] | None = None,
        timeout: Any = NOT_PASSED,
    ) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def ok(payload: Any) -> dict[str, Any]:
    """A successful envelope whose ``data`` carries ``payload``."""
    return {"code": 1, "desc": "success", "data": {"payload": payload}}


def api_with(*responses: FakeResponse, token: str | None = TOKEN) -> tuple[MowerAPI, FakeSession]:
    session = FakeSession(*responses)
    return MowerAPI(session=session, token=token, base_url=BASE_URL), session  # type: ignore[arg-type]


# One entry per endpoint: name, arguments, expected method, expected path and
# expected request body.
ENDPOINTS = [
    pytest.param(
        "async_get_devices", (), "GET", "/openapi/smarthome/authList", None, id="get_devices"
    ),
    pytest.param(
        "async_get_mqtt_user_info",
        (),
        "GET",
        "/openapi/mqtt/userInfo/get/v2",
        None,
        id="get_mqtt_user_info",
    ),
    pytest.param(
        "async_get_device_statuses",
        ([DEVICE_ID],),
        "POST",
        "/openapi/smarthome/getVehicleStatus",
        {"devices": [{"id": DEVICE_ID}]},
        id="get_device_statuses",
    ),
    pytest.param(
        "async_send_command",
        (DEVICE_ID, MowerCommand.START),
        "POST",
        "/openapi/smarthome/sendCommands",
        {
            "commands": [
                {
                    "devices": [{"id": DEVICE_ID}],
                    "execution": {
                        "command": "action.devices.commands.StartStop",
                        "params": {"on": True},
                    },
                }
            ]
        },
        id="send_command",
    ),
    pytest.param(
        "async_query_command_results",
        ([{"id": DEVICE_ID, "cmdNum": "7"}],),
        "POST",
        "/openapi/smarthome/responseCommands",
        {"devices": [{"id": DEVICE_ID, "cmdNum": "7"}]},
        id="query_command_results",
    ),
]
ENDPOINT_NAMES = [
    pytest.param(name, args, id=p.id) for p in ENDPOINTS for (name, args, *_rest) in [p.values]
]


@pytest.mark.parametrize(("name", "args", "method", "path", "body"), ENDPOINTS)
@pytest.mark.asyncio
async def test_request_shape(name: str, args: tuple, method: str, path: str, body: Any) -> None:
    api, session = api_with(FakeResponse(ok({})))
    await getattr(api, name)(*args)

    (request,) = session.requests
    assert request["method"] == method
    assert request["url"] == BASE_URL.rstrip("/") + path
    assert request["json"] == body
    assert request["params"] is None
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    uuid.UUID(request["headers"]["requestId"])  # a fresh request id per call


@pytest.mark.asyncio
async def test_base_url_and_endpoint_are_joined_with_one_slash() -> None:
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url="https://host/prefix///")  # type: ignore[arg-type]
    await api.async_get_devices()
    assert session.requests[0]["url"] == "https://host/prefix/openapi/smarthome/authList"


@pytest.mark.asyncio
async def test_get_devices_success() -> None:
    api, _ = api_with(
        FakeResponse(ok({"devices": [{"id": DEVICE_ID, "name": "Lawn", "model": "i105"}]}))
    )
    devices = await api.async_get_devices()
    assert devices == [
        Device(
            id=DEVICE_ID,
            name="Lawn",
            model="i105",
            firmware_version="",
            serial_number="",
            device_name="Lawn",
            iot_id=DEVICE_ID,
        )
    ]


@pytest.mark.asyncio
async def test_get_mqtt_user_info_returns_the_whole_data_object() -> None:
    data = {"mqttHost": "wss://broker.example.invalid", "userName": "u", "pwdInfo": "p"}
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": data}))
    assert await api.async_get_mqtt_user_info() == data


@pytest.mark.asyncio
async def test_get_device_statuses_success_keys_by_device_id_and_drops_entries_without_one() -> None:
    api, _ = api_with(
        FakeResponse(
            ok(
                {
                    "devices": [
                        {
                            "id": DEVICE_ID,
                            "vehicleState": "isDocked",
                            "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 88}],
                        },
                        {"vehicleState": "isRunning"},
                    ]
                }
            )
        )
    )
    statuses = await api.async_get_device_statuses([DEVICE_ID])
    assert statuses == {
        DEVICE_ID: DeviceStatus(
            device_id=DEVICE_ID,
            status=MowerStatus.DOCKED,
            battery=88,
            error_code=MowerError.NONE,
            extra={
                "vehicleState": "isDocked",
                "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 88}],
            },
        )
    }


@pytest.mark.asyncio
async def test_send_command_success_returns_the_whole_data_object() -> None:
    payload = {"commands": [{"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"}]}
    api, _ = api_with(FakeResponse(ok(payload)))
    assert await api.async_send_command(DEVICE_ID, MowerCommand.START) == {"payload": payload}


@pytest.mark.asyncio
async def test_query_command_results_success_returns_the_devices_list() -> None:
    devices = [{"id": DEVICE_ID, "cmdNum": "7", "status": "SUCCESS"}]
    api, _ = api_with(FakeResponse(ok({"devices": devices})))
    assert await api.async_query_command_results([{"id": DEVICE_ID, "cmdNum": "7"}]) == devices


@pytest.mark.parametrize(
    ("command", "execution"),
    [
        (MowerCommand.START, {"command": "action.devices.commands.StartStop", "params": {"on": True}}),
        (MowerCommand.STOP, {"command": "action.devices.commands.StartStop", "params": {"on": False}}),
        (
            MowerCommand.PAUSE,
            {"command": "action.devices.commands.PauseUnpause", "params": {"on": False}},
        ),
        (
            MowerCommand.RESUME,
            {"command": "action.devices.commands.PauseUnpause", "params": {"on": True}},
        ),
        (MowerCommand.DOCK, {"command": "action.devices.commands.Dock"}),
    ],
)
@pytest.mark.asyncio
async def test_send_command_mapping(command: MowerCommand, execution: dict[str, Any]) -> None:
    api, session = api_with(FakeResponse(ok({"commands": []})))
    await api.async_send_command(DEVICE_ID, command)
    assert session.requests[0]["json"]["commands"][0]["execution"] == execution


@pytest.mark.asyncio
async def test_send_command_rejects_an_unknown_command_before_any_request() -> None:
    api, session = api_with()
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command(DEVICE_ID, "bogus")  # type: ignore[arg-type]
    assert info.value.message == ERROR_MESSAGES["INVALID_COMMAND"]
    assert info.value.status_code is None
    assert info.value.error_code == "INVALID_COMMAND"
    assert session.requests == []


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
@pytest.mark.asyncio
async def test_code_other_than_one_raises_with_desc(name: str, args: tuple) -> None:
    api, _ = api_with(FakeResponse({"code": 4005, "desc": "oauth info illegal", "data": {}}))
    with pytest.raises(MowerAPIError) as info:
        await getattr(api, name)(*args)
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: oauth info illegal"
    assert str(info.value) == info.value.message
    assert info.value.status_code is None
    assert info.value.error_code is None


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
@pytest.mark.asyncio
async def test_missing_code_counts_as_failure(name: str, args: tuple) -> None:
    api, _ = api_with(FakeResponse({"data": {"payload": {}}}))
    with pytest.raises(MowerAPIError) as info:
        await getattr(api, name)(*args)
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: None"


MISSING_DATA_RESULTS = [
    pytest.param("async_get_devices", (), [], id="get_devices"),
    pytest.param("async_get_mqtt_user_info", (), {}, id="get_mqtt_user_info"),
    pytest.param("async_get_device_statuses", ([DEVICE_ID],), {}, id="get_device_statuses"),
    pytest.param("async_send_command", (DEVICE_ID, MowerCommand.DOCK), {}, id="send_command"),
    pytest.param(
        "async_query_command_results", ([{"id": DEVICE_ID}],), [], id="query_command_results"
    ),
]


@pytest.mark.parametrize(("name", "args", "expected"), MISSING_DATA_RESULTS)
@pytest.mark.asyncio
async def test_missing_data_key_is_an_empty_result(name: str, args: tuple, expected: Any) -> None:
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success"}))
    assert await getattr(api, name)(*args) == expected


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
@pytest.mark.asyncio
async def test_explicit_null_data_is_passed_through_as_none(name: str, args: tuple) -> None:
    """``response.get("data", {})`` returns None for an explicit null, unlike a missing key.

    async_get_mqtt_user_info returns that None; async_get_devices and
    async_get_device_statuses, read through their raw calls, give an empty result
    as for a missing key; the two command calls call ``.get`` on it and raise
    AttributeError.
    """
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": None}))
    if name == "async_get_mqtt_user_info":
        assert await getattr(api, name)(*args) is None
    elif name == "async_get_devices":
        assert await getattr(api, name)(*args) == []
    elif name == "async_get_device_statuses":
        assert await getattr(api, name)(*args) == {}
    else:
        with pytest.raises(AttributeError):
            await getattr(api, name)(*args)


@pytest.mark.asyncio
async def test_already_in_state_is_not_an_error() -> None:
    payload = {
        "commands": [
            {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": "alreadyInState"},
            {"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"},
        ]
    }
    api, _ = api_with(FakeResponse(ok(payload)))
    assert await api.async_send_command(DEVICE_ID, MowerCommand.START) == {"payload": payload}


@pytest.mark.parametrize(
    ("result", "error_code"),
    [
        ({"status": "ERROR", "errorCode": "deviceOffline"}, "deviceOffline"),
        ({"status": "ERROR"}, "COMMAND_FAILED"),
        ({"status": "ERROR", "errorCode": ""}, "COMMAND_FAILED"),
    ],
    ids=["named", "missing_code", "empty_code"],
)
@pytest.mark.asyncio
async def test_command_error_raises(result: dict[str, Any], error_code: str) -> None:
    payload = {"commands": [{"status": "ERROR", "errorCode": "alreadyInState"}, result]}
    api, _ = api_with(FakeResponse(ok(payload)))
    with pytest.raises(MowerAPIError) as info:
        await api.async_send_command(DEVICE_ID, MowerCommand.START)
    assert info.value.message == f"{ERROR_MESSAGES['COMMAND_FAILED']}: {error_code}"
    assert info.value.status_code is None
    assert info.value.error_code == error_code
    assert str(info.value) == f"{info.value.message} | Error Code: {error_code}"


@pytest.mark.asyncio
async def test_http_error_status_raises_with_the_body_text() -> None:
    api, _ = api_with(FakeResponse(status=500, body=b"upstream exploded", content_type="text/plain"))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_devices()
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: upstream exploded"
    assert info.value.status_code == 500
    assert info.value.error_code is None
    assert str(info.value) == f"{info.value.message} | HTTP 500"


@pytest.mark.asyncio
async def test_aiohttp_client_error_is_wrapped() -> None:
    cause = aiohttp.ClientConnectionError("connection refused")
    api, _ = api_with(FakeResponse(error=cause))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_mqtt_user_info()
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: connection refused"
    assert info.value.status_code is None
    assert info.value.__cause__ is cause


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
@pytest.mark.asyncio
async def test_every_request_carries_the_default_timeout(name: str, args: tuple) -> None:
    api, session = api_with(FakeResponse(ok({})))
    await getattr(api, name)(*args)
    assert session.requests[0]["timeout"] == aiohttp.ClientTimeout(total=20.0)


@pytest.mark.asyncio
async def test_a_custom_request_timeout_is_passed() -> None:
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url=BASE_URL, request_timeout=5)  # type: ignore[arg-type]
    await api.async_get_devices()
    assert session.requests[0]["timeout"] == aiohttp.ClientTimeout(total=5)


@pytest.mark.asyncio
async def test_request_timeout_none_passes_no_timeout_keyword() -> None:
    """Without the keyword aiohttp applies the session's own timeout policy."""
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url=BASE_URL, request_timeout=None)  # type: ignore[arg-type]
    await api.async_get_devices()
    assert session.requests[0]["timeout"] is NOT_PASSED


@pytest.mark.asyncio
async def test_timeout_is_wrapped_with_its_cause() -> None:
    # aiohttp raises asyncio.TimeoutError when a ClientTimeout expires; from
    # Python 3.11 that name is the builtin TimeoutError.
    cause = TimeoutError()
    api, _ = api_with(FakeResponse(error=cause))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_devices()
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: TimeoutError"
    assert info.value.status_code is None
    assert info.value.error_code is None
    assert info.value.__cause__ is cause


@pytest.mark.parametrize("token", ["", None])
@pytest.mark.asyncio
async def test_missing_token_is_auth_required_before_any_request(token: str | None) -> None:
    api, session = api_with(token=token)
    with pytest.raises(MowerAuthRequiredError) as info:
        await api.async_get_devices()
    assert isinstance(info.value, MowerAPIError)
    assert not isinstance(info.value, MowerTransportError)
    assert info.value.message == ERROR_MESSAGES["TOKEN_EXPIRED"]
    assert info.value.status_code == 401
    assert info.value.error_code == "TOKEN_EXPIRED"
    assert session.requests == []


@pytest.mark.asyncio
async def test_set_token_changes_the_authorization_header() -> None:
    api, session = api_with(FakeResponse(ok({})))
    api.set_token("token-456")
    await api.async_get_devices()
    assert session.requests[0]["headers"]["Authorization"] == "Bearer token-456"


@pytest.mark.asyncio
async def test_empty_inputs_make_no_request() -> None:
    api, session = api_with()
    assert await api.async_get_device_statuses([]) == {}
    assert await api.async_query_command_results([]) == []
    assert session.requests == []


@pytest.mark.asyncio
async def test_get_device_status_returns_the_matching_status() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [{"id": DEVICE_ID, "vehicleState": "isRunning"}]})))
    status = await api.async_get_device_status(DEVICE_ID)
    assert status.device_id == DEVICE_ID
    assert status.status is MowerStatus.MOWING


def assert_device_not_found(error: MowerAPIError) -> None:
    assert error.message == ERROR_MESSAGES["DEVICE_NOT_FOUND"]
    assert error.status_code == 404
    assert error.error_code == "DEVICE_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_device_status_maps_http_404_to_device_not_found() -> None:
    api, _ = api_with(FakeResponse(status=404, body=b"no such vehicle", content_type="text/plain"))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_device_status(DEVICE_ID)
    assert_device_not_found(info.value)
    cause = info.value.__cause__
    assert isinstance(cause, MowerAPIError)
    assert cause.status_code == 404
    assert cause.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: no such vehicle"


@pytest.mark.parametrize("data", [None, {"payload": None}, {"payload": {"devices": None}}], ids=["data", "payload", "devices"])
@pytest.mark.asyncio
async def test_get_device_status_with_null_entries_is_device_not_found(data: dict[str, Any] | None) -> None:
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": data}))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_device_status(DEVICE_ID)
    assert_device_not_found(info.value)


@pytest.mark.asyncio
async def test_get_device_status_missing_from_the_reply_is_device_not_found() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [{"id": "someone-else"}]})))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_device_status(DEVICE_ID)
    assert_device_not_found(info.value)
    # The inner DEVICE_NOT_FOUND error is caught by the 404 branch and re-raised from itself.
    assert isinstance(info.value.__cause__, MowerAPIError)
    assert_device_not_found(info.value.__cause__)


@pytest.mark.asyncio
async def test_get_device_status_passes_other_errors_through() -> None:
    api, _ = api_with(FakeResponse(status=500, body=b"upstream exploded", content_type="text/plain"))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_device_status(DEVICE_ID)
    assert info.value.status_code == 500
    assert info.value.error_code is None
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: upstream exploded"
    assert info.value.__cause__ is None


# ---- the outcome of each failure kind ----------------------------------------------------------
#
# One precedence: a timeout or connection error is a transport error; then the
# status decides for every non-2xx reply (401 and 403 auth, 5xx, 1xx and 3xx
# transport, other 4xx a plain MowerAPIError, the body cut at 500 characters);
# then a 2xx body is read as UTF-8 JSON and anything but an object is a
# transport error. The envelope's code then decides between auth, rate
# limiting and a plain MowerAPIError, and is kept as envelope_code.

FAILED = ERROR_MESSAGES["API_REQUEST_FAILED"]
HTML = b"<html><body><h1>502 Bad Gateway</h1></body></html>"


@pytest.mark.parametrize(
    ("response", "cls", "message", "status_code", "envelope_code"),
    [
        pytest.param(FakeResponse(error=TimeoutError()), MowerTransportError, f"{FAILED}: TimeoutError", None, None, id="timeout"),
        pytest.param(
            FakeResponse(error=aiohttp.ServerDisconnectedError()),
            MowerTransportError,
            f"{FAILED}: Server disconnected",
            None,
            None,
            id="client_error",
        ),
        pytest.param(
            FakeResponse(status=401, body=b'{"code": 401}'), MowerAuthRequiredError, f'{FAILED}: {{"code": 401}}', 401, None, id="http_401"
        ),
        pytest.param(
            FakeResponse(status=403, body=b"forbidden", content_type="text/plain"), MowerAuthRequiredError, f"{FAILED}: forbidden", 403, None, id="http_403"
        ),
        pytest.param(
            FakeResponse(status=404, body=b"not found", content_type="text/plain"), MowerAPIError, f"{FAILED}: not found", 404, None, id="http_404"
        ),
        pytest.param(
            FakeResponse(status=429, body=b"slow down", content_type="text/plain"), MowerAPIError, f"{FAILED}: slow down", 429, None, id="http_429"
        ),
        pytest.param(
            FakeResponse(status=500, body=b"exploded", content_type="text/plain"), MowerTransportError, f"{FAILED}: exploded", 500, None, id="http_500"
        ),
        pytest.param(
            FakeResponse(status=502, body=HTML, content_type="text/html"), MowerTransportError, f"{FAILED}: {HTML.decode()}", 502, None, id="http_502_html"
        ),
        pytest.param(
            FakeResponse(status=401, body=HTML, content_type="text/html"), MowerAuthRequiredError, f"{FAILED}: {HTML.decode()}", 401, None, id="http_401_html"
        ),
        pytest.param(
            FakeResponse(status=404, body=HTML, content_type="text/html"), MowerAPIError, f"{FAILED}: {HTML.decode()}", 404, None, id="http_404_html"
        ),
        pytest.param(
            FakeResponse(ok({}), status=302), MowerTransportError, f"{FAILED}: " + json.dumps(ok({})), 302, None, id="http_302_json"
        ),
        pytest.param(FakeResponse(ok({}), status=304), MowerTransportError, f"{FAILED}: " + json.dumps(ok({})), 304, None, id="http_304"),
        pytest.param(FakeResponse(status=101, body=b""), MowerTransportError, f"{FAILED}: ", 101, None, id="http_101"),
        pytest.param(
            FakeResponse(status=404, body=b"\xffbad", content_type="text/plain"), MowerAPIError, f"{FAILED}: \ufffdbad", 404, None, id="undecodable_body"
        ),
        pytest.param(
            FakeResponse({"code": 4001, "desc": "url Circuit Breaker"}), MowerRateLimitedError, f"{FAILED}: url Circuit Breaker", None, 4001, id="code_4001"
        ),
        pytest.param(
            FakeResponse({"code": 500, "desc": "Request too frequent"}), MowerRateLimitedError, f"{FAILED}: Request too frequent", None, 500, id="too_frequent"
        ),
        pytest.param(
            FakeResponse({"code": 7, "desc": "circuit BREAKER open"}), MowerRateLimitedError, f"{FAILED}: circuit BREAKER open", None, 7, id="circuit_breaker"
        ),
        pytest.param(
            FakeResponse({"code": 4005, "desc": "token expired"}), MowerAuthRequiredError, f"{FAILED}: token expired", None, 4005, id="code_4005"
        ),
        pytest.param(
            FakeResponse({"code": "4005", "desc": "token expired"}), MowerAuthRequiredError, f"{FAILED}: token expired", None, 4005, id="code_4005_as_string"
        ),
        pytest.param(
            FakeResponse({"code": 500, "desc": "code_oauth_info_illegal"}),
            MowerAuthRequiredError,
            f"{FAILED}: code_oauth_info_illegal",
            None,
            500,
            id="oauth_info_illegal",
        ),
        pytest.param(FakeResponse({"code": 2, "desc": "nope"}), MowerAPIError, f"{FAILED}: nope", None, 2, id="other_code"),
        pytest.param(FakeResponse({"desc": "nope"}), MowerAPIError, f"{FAILED}: nope", None, None, id="no_code"),
    ],
)
@pytest.mark.asyncio
async def test_each_failure_kind_raises_its_class(
    response: FakeResponse, cls: type[MowerAPIError], message: str, status_code: int | None, envelope_code: int | None
) -> None:
    api, _ = api_with(response)
    with pytest.raises(MowerAPIError) as info:  # except MowerAPIError still catches every kind
        await api.async_get_devices()
    assert type(info.value) is cls
    assert info.value.message == message
    assert info.value.status_code == status_code
    assert info.value.envelope_code == envelope_code
    assert info.value.error_code is None
    assert str(info.value).startswith(message)
    assert "4005" not in str(info.value).removeprefix(message)


@pytest.mark.parametrize(
    "cause",
    [TimeoutError(), aiohttp.ClientPayloadError("connection lost mid-body")],
    ids=["timeout", "payload_error"],
)
@pytest.mark.parametrize("status", [200, 404], ids=["ok", "not_found"])
@pytest.mark.asyncio
async def test_a_failure_while_reading_the_body_is_a_transport_error_whatever_the_status(
    cause: Exception, status: int
) -> None:
    api, _ = api_with(FakeResponse(ok({}), status=status, read_error=cause))
    with pytest.raises(MowerTransportError) as info:
        await api.async_get_devices()
    assert info.value.__cause__ is cause
    assert info.value.status_code is None
    api, _ = api_with(FakeResponse(ok({}), status=status, read_error=cause))
    with pytest.raises(MowerTransportError) as info:  # not translated to DEVICE_NOT_FOUND
        await api.async_get_device_status(DEVICE_ID)
    assert info.value.error_code is None


@pytest.mark.asyncio
async def test_an_http_error_body_is_cut_at_500_characters_with_a_marker() -> None:
    body = "x" * 2000
    api, _ = api_with(FakeResponse(status=500, body=body.encode(), content_type="text/plain"))
    with pytest.raises(MowerTransportError) as info:
        await api.async_get_devices()
    assert info.value.message == f"{FAILED}: {'x' * 500}… [truncated, 2000 characters]"
    api, _ = api_with(FakeResponse(status=400, body=b"y" * 500, content_type="text/plain"))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_devices()
    assert info.value.message == f"{FAILED}: {'y' * 500}"


@pytest.mark.parametrize(
    ("body", "content_type", "cause", "message"),
    [
        pytest.param(HTML, "text/html", json.JSONDecodeError, "reply is not JSON", id="html"),
        pytest.param(HTML, "application/json", json.JSONDecodeError, "reply is not JSON", id="not_json"),
        pytest.param(b"\xff", "application/json", UnicodeDecodeError, "reply is not UTF-8", id="not_utf8"),
        pytest.param(b"", "application/json", None, "empty reply", id="empty"),
        pytest.param(b"  ", "application/json", None, "empty reply", id="blank"),
        pytest.param(b"[]", "application/json", None, "reply is not a JSON object", id="array"),
        pytest.param(b"null", "application/json", None, "reply is not a JSON object", id="null"),
    ],
)
@pytest.mark.asyncio
async def test_a_2xx_body_that_is_not_a_json_object_is_a_transport_error(
    body: bytes, content_type: str, cause: type[Exception] | None, message: str
) -> None:
    api, _ = api_with(FakeResponse(body=body, content_type=content_type))
    with pytest.raises(MowerTransportError) as info:
        await api.async_get_devices()
    assert info.value.message == f"{FAILED}: {message}"
    assert info.value.status_code == 200
    if cause is None:
        assert info.value.__cause__ is None
    else:
        assert isinstance(info.value.__cause__, cause)


@pytest.mark.asyncio
async def test_a_2xx_json_object_is_read_whatever_its_content_type() -> None:
    api, _ = api_with(FakeResponse(body=json.dumps(ok({"devices": [{"id": DEVICE_ID}]})).encode(), content_type="text/html"))
    assert [device.id for device in await api.async_get_devices()] == [DEVICE_ID]


@pytest.mark.asyncio
async def test_a_404_still_becomes_device_not_found_and_a_5xx_does_not() -> None:
    api, _ = api_with(FakeResponse(status=404, body=HTML, content_type="text/html"))
    with pytest.raises(MowerAPIError) as info:
        await api.async_get_device_status(DEVICE_ID)
    assert info.value.error_code == "DEVICE_NOT_FOUND"
    api, _ = api_with(FakeResponse(status=503, body=b"down", content_type="text/plain"))
    with pytest.raises(MowerTransportError):
        await api.async_get_device_status(DEVICE_ID)


def test_the_error_classes_are_mower_api_errors_and_exported() -> None:
    for cls in (MowerTransportError, MowerAuthRequiredError, MowerRateLimitedError):
        assert issubclass(cls, MowerAPIError)
        assert getattr(mower_sdk, cls.__name__) is cls
    error = MowerAPIError("m", status_code=400, error_code="E", envelope_code=9)
    assert (error.envelope_code, str(error)) == (9, "m | HTTP 400 | Error Code: E")


# ---- the raw status entries ----------------------------------------------------------------------

X430_ENTRY = {
    "id": DEVICE_ID,
    "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 88}],
    "vehicleState": "isDocked",
    "descriptiveCapacityRemaining": "HIGH",
    "newField": {"nested": [1, 2]},
}


@pytest.mark.asyncio
async def test_the_raw_status_entries_come_back_as_the_cloud_sent_them() -> None:
    api, session = api_with(FakeResponse(ok({"devices": [X430_ENTRY, "not a dict", {"vehicleState": "isRunning"}]})))
    entries = await api.async_get_vehicle_status_raw([DEVICE_ID, "dev-2"])
    assert entries == [X430_ENTRY, {"vehicleState": "isRunning"}]
    (request,) = session.requests
    assert (request["method"], request["url"]) == ("POST", BASE_URL.rstrip("/") + "/openapi/smarthome/getVehicleStatus")
    assert request["json"] == {"devices": [{"id": DEVICE_ID}, {"id": "dev-2"}]}


@pytest.mark.asyncio
async def test_no_ids_make_no_raw_status_request() -> None:
    api, session = api_with()
    assert await api.async_get_vehicle_status_raw([]) == []
    assert session.requests == []


@pytest.mark.parametrize(
    "data",
    [
        {}, {"payload": {}}, None, {"payload": None}, {"payload": {"devices": None}},
        42, {"payload": 42}, {"payload": {"devices": 42}}, {"payload": {"devices": {}}},
    ],
    ids=[
        "no_payload", "no_devices", "null_data", "null_payload", "null_devices",
        "data_a_number", "payload_a_number", "devices_a_number", "devices_an_object",
    ],
)
@pytest.mark.asyncio
async def test_a_reply_without_entries_is_an_empty_list(data: dict[str, Any] | None) -> None:
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": data}))
    assert await api.async_get_vehicle_status_raw([DEVICE_ID]) == []


@pytest.mark.parametrize("call", ["async_get_devices", "async_get_devices_raw"])
@pytest.mark.parametrize(
    "data",
    [
        {}, {"payload": {}}, None, {"payload": None}, {"payload": {"devices": None}},
        42, {"payload": 42}, {"payload": {"devices": 42}}, {"payload": {"devices": {}}},
    ],
    ids=[
        "no_payload", "no_devices", "null_data", "null_payload", "null_devices",
        "data_a_number", "payload_a_number", "devices_a_number", "devices_an_object",
    ],
)
@pytest.mark.asyncio
async def test_a_device_list_without_entries_is_an_empty_list(
    call: str, data: dict[str, Any] | None
) -> None:
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": data}))
    assert await getattr(api, call)() == []


DEVICE_ENTRY = {
    "id": DEVICE_ID,
    "name": "Lawn",
    "model": "i105",
    "firmware": "1.2.3",
    "newField": {"nested": [1, 2]},
}


@pytest.mark.asyncio
async def test_the_raw_device_list_is_the_entries_as_sent() -> None:
    api, session = api_with(FakeResponse(ok({"devices": ["not a dict", DEVICE_ENTRY, None]})))
    assert await api.async_get_devices_raw() == [DEVICE_ENTRY]
    assert session.requests[0]["method"] == "GET"
    assert session.requests[0]["url"].endswith("/openapi/smarthome/authList")


@pytest.mark.asyncio
async def test_the_typed_devices_are_read_from_the_raw_entries() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [DEVICE_ENTRY, "not a dict"]})))
    assert await api.async_get_devices() == [Device.from_dict(DEVICE_ENTRY)]


@pytest.mark.parametrize(
    "entry", [{"name": "No id"}, {"id": None, "name": "Null id"}, {"id": "", "name": "Empty id"}],
    ids=["missing", "null", "empty"],
)
@pytest.mark.asyncio
async def test_a_typed_device_without_an_id_is_skipped_and_logged(
    entry: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [entry, DEVICE_ENTRY]})))
    with caplog.at_level(logging.WARNING, logger="mower_sdk.api"):
        devices = await api.async_get_devices()
    assert [device.id for device in devices] == [DEVICE_ID]
    assert [record.getMessage() for record in caplog.records] == [
        f"Skipping a device entry without an id (keys: {sorted(entry)})"
    ]


@pytest.mark.parametrize(
    "entry", [{"name": "No id"}, {"id": None, "name": "Null id"}, {"id": "", "name": "Empty id"}],
    ids=["missing", "null", "empty"],
)
@pytest.mark.asyncio
async def test_a_raw_device_without_an_id_is_kept(
    entry: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [entry, DEVICE_ENTRY]})))
    with caplog.at_level(logging.WARNING, logger="mower_sdk.api"):
        assert await api.async_get_devices_raw() == [entry, DEVICE_ENTRY]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_refused_raw_device_request_raises_like_the_others() -> None:
    api, _ = api_with(FakeResponse({"code": 4005, "desc": "token expired"}))
    with pytest.raises(MowerAuthRequiredError):
        await api.async_get_devices_raw()


@pytest.mark.asyncio
async def test_the_typed_statuses_are_read_from_the_raw_entries() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [X430_ENTRY]})))
    statuses = await api.async_get_device_statuses([DEVICE_ID])
    assert statuses[DEVICE_ID].battery == 88
    assert statuses[DEVICE_ID].extra["newField"] == {"nested": [1, 2]}


@pytest.mark.asyncio
async def test_a_refused_raw_status_request_raises_like_the_others() -> None:
    api, _ = api_with(FakeResponse({"code": 4005, "desc": "token expired"}))
    with pytest.raises(MowerAuthRequiredError):
        await api.async_get_vehicle_status_raw([DEVICE_ID])


@pytest.mark.asyncio
async def test_the_typed_statuses_skip_an_entry_that_is_not_a_dict() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": ["not a dict", X430_ENTRY, None]})))
    assert list(await api.async_get_device_statuses([DEVICE_ID])) == [DEVICE_ID]

"""Characterisation tests for MowerAPI's REST envelope handling.

A fake aiohttp session, with no network, drives all five endpoints through the
cases the envelope check decides: success, ``code != 1``, a missing ``data``
key against an explicit ``"data": null``, ``alreadyInState`` suppression, a
command ``ERROR``, and the mapping from HTTP 404 to ``DEVICE_NOT_FOUND``. It
also records the ``timeout`` keyword each request carries, so the request
timeout's default, a custom value and ``None`` can be checked.

Every expectation is what the code does today, pinned so the envelope refactor
can show that nothing observable changed. That includes the ugly cases: an
explicit ``"data": null`` raises AttributeError from four of the five
endpoints, and that is pinned on purpose rather than corrected here.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import aiohttp
import pytest

from mower_sdk.api import MowerAPI
from mower_sdk.errors import ERROR_MESSAGES, MowerAPIError
from mower_sdk.models import Device, DeviceStatus, MowerCommand, MowerError, MowerStatus

BASE_URL = "https://api.example.invalid/"
TOKEN = "token-123"
DEVICE_ID = "dev-1"


class FakeResponse:
    """What ``session.request(...)`` returns: an async context manager."""

    def __init__(
        self,
        body: Any = None,
        *,
        status: int = 200,
        text: str = "",
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._text = text
        self._error = error

    async def __aenter__(self) -> FakeResponse:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def json(self) -> Any:
        return self._body

    async def text(self) -> str:
        return self._text


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


def api_with(*responses: FakeResponse, token: str = TOKEN) -> tuple[MowerAPI, FakeSession]:
    session = FakeSession(*responses)
    return MowerAPI(session=session, token=token, base_url=BASE_URL), session  # type: ignore[arg-type]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


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
def test_request_shape(name: str, args: tuple, method: str, path: str, body: Any) -> None:
    api, session = api_with(FakeResponse(ok({})))
    run(getattr(api, name)(*args))

    (request,) = session.requests
    assert request["method"] == method
    assert request["url"] == BASE_URL.rstrip("/") + path
    assert request["json"] == body
    assert request["params"] is None
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    uuid.UUID(request["headers"]["requestId"])  # a fresh request id per call


def test_base_url_and_endpoint_are_joined_with_one_slash() -> None:
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url="https://host/prefix///")  # type: ignore[arg-type]
    run(api.async_get_devices())
    assert session.requests[0]["url"] == "https://host/prefix/openapi/smarthome/authList"


def test_get_devices_success() -> None:
    api, _ = api_with(
        FakeResponse(ok({"devices": [{"id": DEVICE_ID, "name": "Lawn", "model": "i105"}]}))
    )
    devices = run(api.async_get_devices())
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


def test_get_mqtt_user_info_returns_the_whole_data_object() -> None:
    data = {"mqttHost": "wss://broker.example.invalid", "userName": "u", "pwdInfo": "p"}
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": data}))
    assert run(api.async_get_mqtt_user_info()) == data


def test_get_device_statuses_success_keys_by_device_id_and_drops_entries_without_one() -> None:
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
    statuses = run(api.async_get_device_statuses([DEVICE_ID]))
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


def test_send_command_success_returns_the_whole_data_object() -> None:
    payload = {"commands": [{"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"}]}
    api, _ = api_with(FakeResponse(ok(payload)))
    assert run(api.async_send_command(DEVICE_ID, MowerCommand.START)) == {"payload": payload}


def test_query_command_results_success_returns_the_devices_list() -> None:
    devices = [{"id": DEVICE_ID, "cmdNum": "7", "status": "SUCCESS"}]
    api, _ = api_with(FakeResponse(ok({"devices": devices})))
    assert run(api.async_query_command_results([{"id": DEVICE_ID, "cmdNum": "7"}])) == devices


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
def test_send_command_mapping(command: MowerCommand, execution: dict[str, Any]) -> None:
    api, session = api_with(FakeResponse(ok({"commands": []})))
    run(api.async_send_command(DEVICE_ID, command))
    assert session.requests[0]["json"]["commands"][0]["execution"] == execution


def test_send_command_rejects_an_unknown_command_before_any_request() -> None:
    api, session = api_with()
    with pytest.raises(MowerAPIError) as info:
        run(api.async_send_command(DEVICE_ID, "bogus"))  # type: ignore[arg-type]
    assert info.value.message == ERROR_MESSAGES["INVALID_COMMAND"]
    assert info.value.status_code is None
    assert info.value.error_code == "INVALID_COMMAND"
    assert session.requests == []


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
def test_code_other_than_one_raises_with_desc(name: str, args: tuple) -> None:
    api, _ = api_with(FakeResponse({"code": 4005, "desc": "oauth info illegal", "data": {}}))
    with pytest.raises(MowerAPIError) as info:
        run(getattr(api, name)(*args))
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: oauth info illegal"
    assert str(info.value) == info.value.message
    assert info.value.status_code is None
    assert info.value.error_code is None


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
def test_missing_code_counts_as_failure(name: str, args: tuple) -> None:
    api, _ = api_with(FakeResponse({"data": {"payload": {}}}))
    with pytest.raises(MowerAPIError) as info:
        run(getattr(api, name)(*args))
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
def test_missing_data_key_is_an_empty_result(name: str, args: tuple, expected: Any) -> None:
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success"}))
    assert run(getattr(api, name)(*args)) == expected


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
def test_explicit_null_data_is_passed_through_as_none(name: str, args: tuple) -> None:
    """``response.get("data", {})`` returns None for an explicit null, unlike a missing key.

    async_get_mqtt_user_info returns that None; the other four call ``.get`` on
    it and raise AttributeError. Both are today's behaviour.
    """
    api, _ = api_with(FakeResponse({"code": 1, "desc": "success", "data": None}))
    if name == "async_get_mqtt_user_info":
        assert run(getattr(api, name)(*args)) is None
    else:
        with pytest.raises(AttributeError):
            run(getattr(api, name)(*args))


def test_already_in_state_is_not_an_error() -> None:
    payload = {
        "commands": [
            {"devices": [{"id": DEVICE_ID}], "status": "ERROR", "errorCode": "alreadyInState"},
            {"devices": [{"id": DEVICE_ID}], "status": "SUCCESS"},
        ]
    }
    api, _ = api_with(FakeResponse(ok(payload)))
    assert run(api.async_send_command(DEVICE_ID, MowerCommand.START)) == {"payload": payload}


@pytest.mark.parametrize(
    ("result", "error_code"),
    [
        ({"status": "ERROR", "errorCode": "deviceOffline"}, "deviceOffline"),
        ({"status": "ERROR"}, "COMMAND_FAILED"),
        ({"status": "ERROR", "errorCode": ""}, "COMMAND_FAILED"),
    ],
    ids=["named", "missing_code", "empty_code"],
)
def test_command_error_raises(result: dict[str, Any], error_code: str) -> None:
    payload = {"commands": [{"status": "ERROR", "errorCode": "alreadyInState"}, result]}
    api, _ = api_with(FakeResponse(ok(payload)))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_send_command(DEVICE_ID, MowerCommand.START))
    assert info.value.message == f"{ERROR_MESSAGES['COMMAND_FAILED']}: {error_code}"
    assert info.value.status_code is None
    assert info.value.error_code == error_code
    assert str(info.value) == f"{info.value.message} | Error Code: {error_code}"


def test_http_error_status_raises_with_the_body_text() -> None:
    api, _ = api_with(FakeResponse(status=500, text="upstream exploded"))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_devices())
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: upstream exploded"
    assert info.value.status_code == 500
    assert info.value.error_code is None
    assert str(info.value) == f"{info.value.message} | HTTP 500"


def test_aiohttp_client_error_is_wrapped() -> None:
    cause = aiohttp.ClientConnectionError("connection refused")
    api, _ = api_with(FakeResponse(error=cause))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_mqtt_user_info())
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: connection refused"
    assert info.value.status_code is None
    assert info.value.__cause__ is cause


@pytest.mark.parametrize(("name", "args"), ENDPOINT_NAMES)
def test_every_request_carries_the_default_timeout(name: str, args: tuple) -> None:
    api, session = api_with(FakeResponse(ok({})))
    run(getattr(api, name)(*args))
    assert session.requests[0]["timeout"] == aiohttp.ClientTimeout(total=20.0)


def test_a_custom_request_timeout_is_passed() -> None:
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url=BASE_URL, request_timeout=5)  # type: ignore[arg-type]
    run(api.async_get_devices())
    assert session.requests[0]["timeout"] == aiohttp.ClientTimeout(total=5)


def test_request_timeout_none_passes_no_timeout_keyword() -> None:
    """Without the keyword aiohttp applies the session's own timeout policy."""
    session = FakeSession(FakeResponse(ok({})))
    api = MowerAPI(session=session, token=TOKEN, base_url=BASE_URL, request_timeout=None)  # type: ignore[arg-type]
    run(api.async_get_devices())
    assert session.requests[0]["timeout"] is NOT_PASSED


def test_timeout_is_wrapped_with_its_cause() -> None:
    # aiohttp raises asyncio.TimeoutError when a ClientTimeout expires; from
    # Python 3.11 that name is the builtin TimeoutError.
    cause = TimeoutError()
    api, _ = api_with(FakeResponse(error=cause))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_devices())
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: TimeoutError"
    assert info.value.status_code is None
    assert info.value.error_code is None
    assert info.value.__cause__ is cause


def test_empty_token_raises_before_any_request() -> None:
    api, session = api_with(token="")
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_devices())
    assert info.value.message == ERROR_MESSAGES["TOKEN_EXPIRED"]
    assert info.value.status_code == 401
    assert info.value.error_code == "TOKEN_EXPIRED"
    assert session.requests == []


def test_set_token_changes_the_authorization_header() -> None:
    api, session = api_with(FakeResponse(ok({})))
    api.set_token("token-456")
    run(api.async_get_devices())
    assert session.requests[0]["headers"]["Authorization"] == "Bearer token-456"


def test_empty_inputs_make_no_request() -> None:
    api, session = api_with()
    assert run(api.async_get_device_statuses([])) == {}
    assert run(api.async_query_command_results([])) == []
    assert session.requests == []


def test_get_device_status_returns_the_matching_status() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [{"id": DEVICE_ID, "vehicleState": "isRunning"}]})))
    status = run(api.async_get_device_status(DEVICE_ID))
    assert status.device_id == DEVICE_ID
    assert status.status is MowerStatus.MOWING


def assert_device_not_found(error: MowerAPIError) -> None:
    assert error.message == ERROR_MESSAGES["DEVICE_NOT_FOUND"]
    assert error.status_code == 404
    assert error.error_code == "DEVICE_NOT_FOUND"


def test_get_device_status_maps_http_404_to_device_not_found() -> None:
    api, _ = api_with(FakeResponse(status=404, text="no such vehicle"))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_device_status(DEVICE_ID))
    assert_device_not_found(info.value)
    cause = info.value.__cause__
    assert isinstance(cause, MowerAPIError)
    assert cause.status_code == 404
    assert cause.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: no such vehicle"


def test_get_device_status_missing_from_the_reply_is_device_not_found() -> None:
    api, _ = api_with(FakeResponse(ok({"devices": [{"id": "someone-else"}]})))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_device_status(DEVICE_ID))
    assert_device_not_found(info.value)
    # The inner DEVICE_NOT_FOUND error is caught by the 404 branch and re-raised from itself.
    assert isinstance(info.value.__cause__, MowerAPIError)
    assert_device_not_found(info.value.__cause__)


def test_get_device_status_passes_other_errors_through() -> None:
    api, _ = api_with(FakeResponse(status=500, text="upstream exploded"))
    with pytest.raises(MowerAPIError) as info:
        run(api.async_get_device_status(DEVICE_ID))
    assert info.value.status_code == 500
    assert info.value.error_code is None
    assert info.value.message == f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: upstream exploded"
    assert info.value.__cause__ is None

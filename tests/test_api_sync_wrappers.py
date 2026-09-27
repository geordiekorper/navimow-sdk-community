"""The synchronous MowerAPI wrappers warn and still delegate.

get_devices, get_mqtt_user_info, get_device_status, send_command and
query_command_results each wrap their async_* counterpart in asyncio.run.
They stay, because the legacy MowerClient calls them as methods, but each
now emits a DeprecationWarning attributed to its caller and keeps returning
what the async method returns.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from mower_sdk.api import MowerAPI
from mower_sdk.models import Device, DeviceStatus, MowerCommand, MowerStatus

DEVICE_ID = "dev-1"


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

    async def text(self) -> str:
        return ""


class FakeSession:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.calls = 0

    def request(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self.body)


def ok(payload: Any) -> dict[str, Any]:
    return {"code": 1, "desc": "success", "data": {"payload": payload}}


WRAPPERS = [
    pytest.param(
        "get_devices",
        (),
        ok({"devices": [{"id": DEVICE_ID, "name": "Lawn", "model": "i105"}]}),
        [
            Device(
                id=DEVICE_ID,
                name="Lawn",
                model="i105",
                firmware_version="",
                serial_number="",
                device_name="Lawn",
                iot_id=DEVICE_ID,
            )
        ],
        id="get_devices",
    ),
    pytest.param(
        "get_mqtt_user_info",
        (),
        {"code": 1, "desc": "success", "data": {"mqttHost": "h", "userName": "u"}},
        {"mqttHost": "h", "userName": "u"},
        id="get_mqtt_user_info",
    ),
    pytest.param(
        "get_device_status",
        (DEVICE_ID,),
        ok({"devices": [{"id": DEVICE_ID, "vehicleState": "isDocked"}]}),
        DeviceStatus(
            device_id=DEVICE_ID,
            status=MowerStatus.DOCKED,
            battery=0,
            extra={"vehicleState": "isDocked"},
        ),
        id="get_device_status",
    ),
    pytest.param(
        "send_command",
        (DEVICE_ID, MowerCommand.PAUSE),
        ok({"commands": [{"status": "SUCCESS"}]}),
        {"payload": {"commands": [{"status": "SUCCESS"}]}},
        id="send_command",
    ),
    pytest.param(
        "query_command_results",
        ([{"id": DEVICE_ID, "cmdNum": "7"}],),
        ok({"devices": [{"id": DEVICE_ID, "status": "SUCCESS"}]}),
        [{"id": DEVICE_ID, "status": "SUCCESS"}],
        id="query_command_results",
    ),
]


@pytest.mark.parametrize(("name", "args", "body", "expected"), WRAPPERS)
def test_sync_wrapper_warns_and_delegates(name: str, args: tuple, body: Any, expected: Any) -> None:
    session = FakeSession(body)
    api = MowerAPI(session=session, token="token", base_url="https://api.example.invalid")  # type: ignore[arg-type]

    with pytest.warns(DeprecationWarning, match=rf"^MowerAPI\.{name} is deprecated: use MowerAPI\.async_{name}\.") as record:
        result = getattr(api, name)(*args)

    assert result == expected
    assert session.calls == 1
    (warning,) = record
    assert "cannot be used inside a running event loop" in str(warning.message)
    assert warning.filename == __file__  # attributed to the caller, not to api.py


@pytest.mark.parametrize(("name", "args", "body", "expected"), WRAPPERS)
def test_sync_wrapper_warns_on_every_call(name: str, args: tuple, body: Any, expected: Any) -> None:
    api = MowerAPI(session=FakeSession(body), token="token", base_url="https://api.example.invalid")  # type: ignore[arg-type]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(2):
            assert getattr(api, name)(*args) == expected
    assert [w.category for w in caught] == [DeprecationWarning, DeprecationWarning]


def test_async_methods_do_not_warn() -> None:
    import asyncio

    api = MowerAPI(session=FakeSession(ok({"devices": []})), token="token", base_url="https://api.example.invalid")  # type: ignore[arg-type]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert asyncio.run(api.async_get_devices()) == []

"""The README's quick example on NavimowClient, run as printed.

The Python block of README.md's "Quick example" section is executed as it is
printed, with aiohttp.ClientSession and paho's client replaced by recording
fakes, TOKEN and BASE_URL as printed, and the minute's sleep replaced by a wait
for the first state the example prints. A thread standing in for paho's
delivers a state message once the facade has connected. The example is run to
a printed MQTT state and to a REST call that fails, and each run must leave the
session closed and the MQTT client disconnected.
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from mower_sdk import mqtt as mqtt_module
from mower_sdk.errors import MowerAuthRequiredError

from .fakes import SUCCESS, FakeClient, FakeMessage, FakeResponse, FakeSession

README = Path(__file__).resolve().parent.parent / "README.md"
if not README.exists():
    # The wheel check runs a copy of tests/ outside the checkout, without README.md.
    pytest.skip("README.md is not next to the tests", allow_module_level=True)
DEVICE_ID = "dev-1"
STATE_TOPIC = f"/downlink/vehicle/{DEVICE_ID}/realtimeDate/state"
DEVICES_REPLY = {"code": 1, "data": {"payload": {"devices": [{"id": DEVICE_ID, "name": "Lawn"}]}}}
BROKER_REPLY = {
    "code": 1,
    "data": {
        "mqttHost": "wss://broker.example.invalid",
        "mqttUrl": "/mqtt",
        "userName": "user",
        "pwdInfo": "secret",
    },
}
STATUS_REPLY = {
    "code": 1,
    "data": {
        "payload": {
            "devices": [
                {
                    "id": DEVICE_ID,
                    "vehicleState": "isDocked",
                    "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": "80"}],
                }
            ]
        }
    },
}


def section() -> str:
    return README.read_text(encoding="utf-8").split("## Quick example", 1)[1].split("\n## ", 1)[0]


def example() -> str:
    """The first Python block of the section: the example on the client."""
    found = re.findall(r"```python\n(.*?)```", section(), re.DOTALL)
    assert len(found) == 2, "the quick example and the layered example underneath"
    assert "NavimowClient(api)" in found[0] and "NavimowSDK.from_connection_info" in found[1]
    return found[0]


class ContextSession(FakeSession):
    """A fake session the example can enter with ``async with``; leaving it closes it."""

    async def __aenter__(self) -> ContextSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True


class FakePaho(FakeClient):
    """A paho client that hands itself over when it starts a connection."""

    connecting: queue.Queue[FakePaho] = queue.Queue()

    def connect_async(self, *args: Any, **kwargs: Any) -> None:
        super().connect_async(*args, **kwargs)
        FakePaho.connecting.put(self)


@pytest.fixture
def fake_paho(fake_paho: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """The shared fixture, with FakePaho as the client."""
    FakePaho.connecting = queue.Queue()
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakePaho)
    return fake_paho


def serve(monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> list[ContextSession]:
    """Make aiohttp.ClientSession() build a ContextSession with these replies."""
    sessions: list[ContextSession] = []

    def make_session() -> ContextSession:
        sessions.append(ContextSession(*(FakeResponse(reply) for reply in replies)))
        return sessions[-1]

    monkeypatch.setattr(aiohttp, "ClientSession", make_session)
    return sessions


def deliver_when_connected(payload: bytes) -> threading.Thread:
    """Stand in for paho's thread: once the facade's client has connected, deliver a message."""

    def paho() -> None:
        try:
            client = FakePaho.connecting.get(timeout=5)
        except queue.Empty:
            return
        client.on_connect(client, None, {}, SUCCESS, None)
        client.connected = True
        client.on_message(client, None, FakeMessage(STATE_TOPIC, payload))

    thread = threading.Thread(target=paho)
    thread.start()
    return thread


def run_example(printed_lines: int) -> dict[str, Any]:
    """Run the example as printed, its sleep replaced by a wait for that many printed lines.

    What the block printed is returned under "printed", and an exception it
    raised under "raised".
    """
    printed: list[str] = []
    enough = threading.Event()

    def record(*values: Any) -> None:
        printed.append(" ".join(str(value) for value in values))
        if len(printed) >= printed_lines:
            enough.set()

    async def wait() -> None:
        await asyncio.get_running_loop().run_in_executor(None, enough.wait, 5)

    namespace: dict[str, Any] = {"__name__": "readme_example", "print": record, "WAIT": wait}
    code = example()
    assert "await asyncio.sleep(60)" in code
    code = code.replace("await asyncio.sleep(60)", "await WAIT()")
    try:
        exec(compile(code, str(README), "exec"), namespace)
    except Exception as exc:
        namespace["raised"] = exc
    namespace["printed"] = printed
    return namespace


@pytest.mark.usefixtures("fake_paho")
def test_the_example_as_printed_prints_the_rest_state_then_the_mqtt_state_and_releases_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = serve(monkeypatch, DEVICES_REPLY, BROKER_REPLY, STATUS_REPLY)
    paho = deliver_when_connected(b'{"state":"isRunning","battery":55}')
    namespace = run_example(printed_lines=2)
    paho.join(timeout=5)
    assert "raised" not in namespace
    assert namespace["printed"] == [
        f"{DEVICE_ID} docked 80 rest None",
        f"{DEVICE_ID} mowing 55 mqtt None",
    ]
    assert [request["url"].rsplit("/", 1)[1] for request in sessions[0].requests] == [
        "authList",
        "v2",
        "getVehicleStatus",
    ]
    assert sessions[0].closed
    client = FakeClient.instances[-1]
    assert client.named("disconnect") and client.named("loop_stop")


@pytest.mark.usefixtures("fake_paho")
def test_the_example_releases_everything_when_a_rest_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = serve(monkeypatch, {"code": 4005, "desc": "token expired"})
    namespace = run_example(printed_lines=1)
    assert isinstance(namespace["raised"], MowerAuthRequiredError)
    assert namespace["printed"] == []
    assert sessions[0].closed and FakeClient.instances == []


def test_the_section_names_the_client_and_the_layers() -> None:
    text = " ".join(section().split())
    for name in (
        "client.state(device_id)",
        "client.states()",
        "on_event",
        "on_attributes",
        "on_rejected",
        "on_connection",
        "on_error",
        "client.async_set_token()",
        "token_provider",
        "### The layers underneath",
    ):
        assert name in text

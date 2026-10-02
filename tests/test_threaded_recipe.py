"""The README's recipe for threaded applications, run as written.

Both Python blocks of README.md's "Threaded applications" section are executed
as printed, so the documented code is the tested code: the helper class, then
the usage, with aiohttp.ClientSession and paho's client replaced by recording
fakes and TOKEN and BASE_URL supplied. The usage is run to a delivered state
message, to a REST call that fails and to a wait that times out, and each run
must leave the session closed, the MQTT client disconnected when one was made
and the loop stopped and closed. The helper is also driven directly: REST
through run(), callbacks on the loop's thread, the blocking calls made from the
application's thread, and the refusal to wait on the loop's own thread.
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
from mower_sdk.api import MowerAPI
from mower_sdk.errors import MowerAuthRequiredError
from mower_sdk.models import DeviceStateMessage
from mower_sdk.sdk import NavimowSDK

from .fakes import FakeResponse, FakeSession

README = Path(__file__).resolve().parent.parent / "README.md"
if not README.exists():
    # The wheel check runs a copy of tests/ outside the checkout, without README.md.
    pytest.skip("README.md is not next to the tests", allow_module_level=True)
DEVICE_ID = "dev-1"
STATE_TOPIC = f"/downlink/vehicle/{DEVICE_ID}/realtimeDate/state"


def section() -> str:
    return README.read_text(encoding="utf-8").split("## Threaded applications", 1)[1].split("\n## ", 1)[0]


def blocks() -> list[str]:
    found = re.findall(r"```python\n(.*?)```", section(), re.DOTALL)
    assert len(found) == 2, "README's threaded section should hold the helper and its usage"
    return found


def recipe() -> dict[str, Any]:
    """Execute the section's first Python block, the helper, and return what it defines."""
    namespace: dict[str, Any] = {"__name__": "readme_recipe"}
    exec(compile(blocks()[0], str(README), "exec"), namespace)  # noqa: S102
    return namespace


def run_usage(wait: float = 120) -> dict[str, Any]:
    """Execute the usage block after the helper, as printed except for the wait's timeout.

    What it assigned is returned, and also when it raised: the exception is
    stored under "raised".
    """
    namespace = recipe()
    namespace.update(TOKEN="token", BASE_URL="https://api.example.invalid")
    usage = blocks()[1]
    assert "states.get(timeout=120)" in usage
    usage = usage.replace("states.get(timeout=120)", f"states.get(timeout={wait})")
    try:
        exec(compile(usage, str(README), "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001
        namespace["raised"] = exc
    return namespace


class ThreadSession(FakeSession):
    """Also records the thread each request is made on and the loop it was made on; it can be closed."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        super().__init__(*(FakeResponse(reply) for reply in replies))
        self.request_threads: list[threading.Thread] = []
        self.made_on_loop = asyncio.get_running_loop()

    def request(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.request_threads.append(threading.current_thread())
        return super().request(*args, **kwargs)

    async def close(self) -> None:
        self.closed = True


class FakePaho:
    """A paho client that connects to nothing; subscribe answers like paho."""

    instances: list[FakePaho] = []
    # Each client that starts a connection, for the thread standing in for paho's.
    connecting: queue.Queue[FakePaho] = queue.Queue()

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls: list[str] = []
        self.connected = False
        self.mid = 0
        FakePaho.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):
            raise AttributeError(name)

        def record(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)

        return record

    def subscribe(self, _topic: str) -> tuple[int, int]:
        self.mid += 1
        return 0, self.mid

    def connect_async(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("connect_async")
        FakePaho.connecting.put(self)

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


class Success:
    value = 0
    is_failure = False

    def __str__(self) -> str:
        return "Success"


class PahoMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


def as_paho_thread(target: Any, *args: Any) -> None:
    """Call target on a thread of its own, as paho's network thread calls the callbacks."""
    thread = threading.Thread(target=target, args=args)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePaho.instances = []
    FakePaho.connecting = queue.Queue()
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakePaho)


DEVICES_REPLY = {"code": 1, "data": {"payload": {"devices": [{"id": DEVICE_ID, "name": "Lawn"}]}}}
BROKER_REPLY = {
    "code": 1,
    "data": {"mqttHost": "wss://broker.example.invalid", "mqttUrl": "/mqtt", "userName": "user", "pwdInfo": "secret"},
}


def serve(monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> list[ThreadSession]:
    """Make aiohttp.ClientSession() build a ThreadSession with these replies; the sessions made are returned."""
    sessions: list[ThreadSession] = []

    def make_session() -> ThreadSession:
        sessions.append(ThreadSession(*replies))
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
        client.on_connect(client, None, {}, Success(), None)
        client.on_message(client, None, PahoMessage(STATE_TOPIC, payload))

    thread = threading.Thread(target=paho)
    thread.start()
    return thread


def assert_released(namespace: dict[str, Any]) -> None:
    assert namespace["session"].closed
    assert namespace["mowers"].loop.is_closed()
    assert not namespace["mowers"]._thread.is_alive()


@pytest.mark.usefixtures("fake_paho")
def test_the_usage_as_printed_delivers_a_state_message_and_releases_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, DEVICES_REPLY, BROKER_REPLY)
    paho = deliver_when_connected(b'{"state":"isDocked"}')
    namespace = run_usage()
    paho.join(timeout=5)
    assert "raised" not in namespace
    message = namespace["message"]
    assert (message.device_id, message.state) == (DEVICE_ID, "docked")
    assert namespace["sdk"].loop is namespace["mowers"].loop
    assert {"disconnect", "loop_stop"} <= set(FakePaho.instances[-1].calls)
    assert_released(namespace)


@pytest.mark.usefixtures("fake_paho")
def test_the_usage_releases_everything_when_a_rest_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, {"code": 4005, "desc": "token expired"})
    namespace = run_usage()
    assert isinstance(namespace["raised"], MowerAuthRequiredError)
    assert namespace["sdk"] is None
    assert FakePaho.instances == []
    assert_released(namespace)


@pytest.mark.usefixtures("fake_paho")
def test_the_usage_releases_everything_when_no_state_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, DEVICES_REPLY, BROKER_REPLY)
    namespace = run_usage(wait=0.1)
    assert isinstance(namespace["raised"], queue.Empty)
    assert {"disconnect", "loop_stop"} <= set(FakePaho.instances[-1].calls)
    assert_released(namespace)


@pytest.mark.usefixtures("fake_paho")
def test_the_threaded_recipe_delivers_rest_and_mqtt_to_a_plain_thread() -> None:
    NavimowThread = recipe()["NavimowThread"]
    mowers = NavimowThread()
    try:
        replies = (
            {"code": 1, "data": {"payload": {"devices": [{"id": DEVICE_ID, "name": "Lawn"}]}}},
            {"code": 1, "data": {"userName": "user", "pwdInfo": "secret"}},
        )
        session = mowers.call(ThreadSession, *replies)
        assert session.made_on_loop is mowers.loop
        api = MowerAPI(session, "token", "https://api.example.invalid")  # type: ignore[arg-type]
        devices = mowers.run(api.async_get_devices())
        assert [device.id for device in devices] == [DEVICE_ID]
        info = mowers.run(api.async_get_mqtt_user_info())
        assert session.request_threads == [mowers._thread, mowers._thread]

        sdk = NavimowSDK(
            broker="broker.example.invalid", port=1883, username=info["userName"], password=info["pwdInfo"],
            records=devices, loop=mowers.loop,
        )
        assert sdk.loop is mowers.loop
        states: queue.Queue[DeviceStateMessage] = queue.Queue()
        callback_threads: list[threading.Thread] = []
        sdk.on_state(lambda _message: callback_threads.append(threading.current_thread()))
        sdk.on_state(states.put)
        sdk.connect()
        client = sdk.mqtt.client
        assert "connect_async" in client.calls

        as_paho_thread(sdk.mqtt._on_connect, client, None, {}, Success(), None)
        client.connected = True
        as_paho_thread(sdk.mqtt._on_message, client, None, PahoMessage(STATE_TOPIC, b'{"state":"isDocked"}'))
        message = states.get(timeout=5)
        assert (message.device_id, message.state) == (DEVICE_ID, "docked")
        assert callback_threads == [mowers._thread]

        # The plain calls, made from this thread: a connected update keeps the client,
        # a forced one rebuilds it (blocking here, not on the loop).
        sdk.update_mqtt_credentials(password="rotated")
        assert sdk.mqtt.client is client
        sdk.update_mqtt_credentials(force_reconnect=True)
        assert sdk.mqtt.client is not client

        sdk.disconnect()
        mowers.run(session.close())
        assert session.closed
    finally:
        mowers.stop()
    assert mowers.loop.is_closed()
    assert not mowers._thread.is_alive()


def test_run_refuses_to_wait_on_the_loops_own_thread() -> None:
    NavimowThread = recipe()["NavimowThread"]
    mowers = NavimowThread()
    try:

        async def answer() -> int:
            return 42

        async def nested() -> str:
            coro = answer()
            try:
                mowers.run(coro)
            except RuntimeError as exc:
                return str(exc)
            return "waited"

        assert mowers.run(nested()) == "NavimowThread.run() called on the loop's own thread would wait forever"
        assert mowers.run(answer()) == 42
        assert mowers.call(sum, [1, 2, 3]) == 6
    finally:
        mowers.stop()


def test_the_recipe_names_the_calls_that_block() -> None:
    text = " ".join(section().split())
    assert "every one of them can block" in text
    for name in (
        "sdk.connect()", "sdk.disconnect()", "sdk.update_mqtt_credentials()", "sdk.mqtt.rebuild()",
        "sdk.mqtt.update_credentials()", "`disconnect()` joins paho's network thread", "loop=mowers.loop",
    ):
        assert name in text


def test_a_timed_out_run_cancels_its_coroutine_and_stop_cancels_the_rest(capfd: pytest.CaptureFixture[str]) -> None:
    NavimowThread = recipe()["NavimowThread"]
    mowers = NavimowThread()
    cancelled: list[str] = []

    async def wait_forever(name: str, started: threading.Event | None = None) -> None:
        try:
            if started is not None:
                started.set()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    try:
        with pytest.raises(TimeoutError):
            mowers.run(wait_forever("timed out"), timeout=0.05)
        assert mowers.run(asyncio.sleep(0, "after")) == "after"
        assert cancelled == ["timed out"]
        started = threading.Event()
        asyncio.run_coroutine_threadsafe(wait_forever("left running", started), mowers.loop)
        assert started.wait(timeout=5)
    finally:
        mowers.stop()
    assert cancelled == ["timed out", "left running"]
    assert mowers.loop.is_closed()
    assert "Task was destroyed but it is pending" not in capfd.readouterr().err

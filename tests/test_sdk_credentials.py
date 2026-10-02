"""NavimowSDK.async_refresh_broker_credentials: the cooldown, one call at a time, the applied values, the loop.

The real NavimowMQTT runs on a recording fake paho client, so what the helper
applies can be read off the client; a fake API returns the credential reply, and
the shared clock stands in for time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Literal

import pytest

from mower_sdk.errors import MowerAPIError, MowerRateLimitedError
from mower_sdk.sdk import NavimowSDK

from .fakes import SUCCESS, FakeClient, FakeClock

HEADERS = {"Authorization": "Bearer tok"}


class FakeAPI:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def async_get_mqtt_user_info(self) -> Any:
        self.calls += 1
        await asyncio.sleep(0)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


pytestmark = pytest.mark.usefixtures("fake_paho", "clock")


def facade(**kwargs: Any) -> NavimowSDK:
    return NavimowSDK(broker="wss://broker.example.invalid", port=443, ws_path="/mqtt", **kwargs)


def reply(user: Any = "user", password: Any = "secret") -> dict[str, Any]:
    return {"mqttHost": "wss://broker.example.invalid", "userName": user, "pwdInfo": password}


@pytest.mark.asyncio
async def test_the_reply_is_applied_as_strings_with_the_header() -> None:
    sdk = facade(username="old", password="old", auth_headers={"Authorization": "Bearer old"})
    sdk.connect()
    sdk.mqtt.client.connected = True
    assert (
        await sdk.async_refresh_broker_credentials(FakeAPI(reply(12345, 678)), auth_headers=HEADERS)
        is True
    )
    assert (sdk.mqtt.username, sdk.mqtt.password, sdk.mqtt.auth_headers) == (
        "12345",
        "678",
        HEADERS,
    )
    assert sdk.mqtt.client.named("username_pw_set")[-1] == (
        "username_pw_set",
        ("12345", "678"),
        {},
    )  # set on the live client
    assert len(FakeClient.instances) == 1  # connected, not forced: no rebuild


@pytest.mark.asyncio
async def test_a_call_within_the_cooldown_makes_no_request_and_a_failed_attempt_counts(
    clock: FakeClock,
) -> None:
    sdk = facade()
    api = FakeAPI(MowerRateLimitedError("too frequent", envelope_code=4001), reply(), reply("u2"))
    with pytest.raises(MowerRateLimitedError):
        await sdk.async_refresh_broker_credentials(api)
    clock.monotonic_now += 64.9
    assert await sdk.async_refresh_broker_credentials(api) is False
    assert api.calls == 1
    clock.monotonic_now += 0.1
    assert await sdk.async_refresh_broker_credentials(api) is True
    clock.monotonic_now += 10
    assert await sdk.async_refresh_broker_credentials(api, cooldown=5) is True
    assert (api.calls, sdk.mqtt.username) == (3, "u2")


@pytest.mark.asyncio
async def test_concurrent_calls_make_one_request() -> None:
    sdk = facade()
    api = FakeAPI(reply(), reply())
    results = await asyncio.gather(*(sdk.async_refresh_broker_credentials(api) for _ in range(3)))
    assert sorted(results) == [False, False, True]
    assert api.calls == 1


@pytest.mark.asyncio
async def test_force_reconnect_rebuilds_even_with_unchanged_values() -> None:
    sdk = facade(username="user", password="secret")
    first = sdk.mqtt.client
    first.connected = True
    assert (
        await sdk.async_refresh_broker_credentials(FakeAPI(reply()), force_reconnect=True) is True
    )
    assert sdk.mqtt.client is not first
    assert sdk.mqtt.rebuilds == 1
    assert sdk.mqtt.client.named("connect_async") == [
        ("connect_async", ("broker.example.invalid", 443, 60), {})
    ]


@pytest.mark.asyncio
async def test_a_startup_call_with_unchanged_credentials_starts_no_connection() -> None:
    sdk = facade(username="user", password="secret")
    assert await sdk.async_refresh_broker_credentials(FakeAPI(reply())) is True
    assert sdk.mqtt.client.named("connect_async") == []
    assert len(FakeClient.instances) == 1
    sdk.connect()
    assert len(sdk.mqtt.client.named("connect_async")) == 1


@pytest.mark.asyncio
async def test_a_startup_call_with_new_credentials_connects_through_the_rebuild() -> None:
    sdk = facade()  # constructed without credentials, not connected
    assert await sdk.async_refresh_broker_credentials(FakeAPI(reply())) is True
    assert len(FakeClient.instances) == 2
    assert sdk.mqtt.client.named("username_pw_set") == [("username_pw_set", ("user", "secret"), {})]
    assert sdk.mqtt.client.named("connect_async") == [
        ("connect_async", ("broker.example.invalid", 443, 60), {})
    ]
    sdk.connect()  # already started: nothing more
    assert len(sdk.mqtt.client.named("connect_async")) == 1


def test_a_startup_call_binds_the_callers_loop_before_the_executor() -> None:
    sdk = facade()  # outside any loop: nothing bound
    assert sdk.loop is None
    delivered: list[asyncio.AbstractEventLoop] = []

    async def test() -> None:
        called = asyncio.Event()

        async def on_disconnected() -> None:
            delivered.append(asyncio.get_running_loop())
            called.set()

        await sdk.async_refresh_broker_credentials(
            FakeAPI(reply())
        )  # changed while disconnected: a rebuild
        assert sdk.loop is asyncio.get_running_loop()
        sdk.mqtt.on_disconnected = on_disconnected
        sdk.connect()
        sdk.mqtt._on_disconnect(sdk.mqtt.client, None, {}, SUCCESS, None)
        await asyncio.wait_for(called.wait(), 5)
        assert delivered == [asyncio.get_running_loop()]

    asyncio.run(test())


def test_a_call_from_another_loop_is_refused_before_any_request() -> None:
    other = asyncio.new_event_loop()
    try:
        sdk = facade(loop=other)
        api = FakeAPI(reply())
        with pytest.raises(RuntimeError, match="another"):
            asyncio.run(sdk.async_refresh_broker_credentials(api))
        assert api.calls == 0
    finally:
        other.close()


@pytest.mark.parametrize(
    "answer",
    [None, {}, {"mqttHost": "wss://broker.example.invalid"}],
    ids=["null", "empty", "host_only"],
)
@pytest.mark.asyncio
async def test_a_reply_without_credentials_is_an_api_error_and_counts_for_the_cooldown(
    answer: Any,
) -> None:
    sdk = facade(username="old", password="old")
    api = FakeAPI(answer, reply())
    with pytest.raises(MowerAPIError, match="no broker credentials"):
        await sdk.async_refresh_broker_credentials(api)
    assert (sdk.mqtt.username, sdk.mqtt.password) == ("old", "old")
    assert await sdk.async_refresh_broker_credentials(api) is False
    assert api.calls == 1


class GatedAPI(FakeAPI):
    """The first request says it has been made, then waits until released."""

    def __init__(self, *replies: Any) -> None:
        super().__init__(*replies)
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    async def async_get_mqtt_user_info(self) -> Any:
        if self.calls == 0:
            self.calls += 1
            self.requested.set()
            await self.release.wait()
            return self.replies.pop(0)
        return await super().async_get_mqtt_user_info()


class WatchedLock(asyncio.Lock):
    """Stands in for the facade's credentials lock; ``contended`` is set when a call has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_a_second_call_waits_for_the_first_to_finish_even_past_the_cooldown(
    clock: FakeClock,
) -> None:
    sdk = facade()
    sdk._credentials_lock = lock = WatchedLock()
    api = GatedAPI(reply("u1"), reply("u2"))
    first = asyncio.create_task(sdk.async_refresh_broker_credentials(api))
    await asyncio.wait_for(api.requested.wait(), 5)
    clock.monotonic_now += 100  # the cooldown has passed, but the first call still holds the lock
    second = asyncio.create_task(sdk.async_refresh_broker_credentials(api))
    await asyncio.wait_for(lock.contended.wait(), 5)  # the second call has reached the lock
    assert api.calls == 1
    api.release.set()
    assert await first is True
    assert await second is True
    assert (api.calls, sdk.mqtt.username) == (2, "u2")


@pytest.mark.asyncio
async def test_the_credentials_are_applied_off_the_callers_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = facade()
    threads: list[int] = []
    monkeypatch.setattr(
        sdk, "update_mqtt_credentials", lambda *_a, **_k: threads.append(threading.get_ident())
    )
    assert await sdk.async_refresh_broker_credentials(FakeAPI(reply())) is True
    assert threads and threads[0] != threading.get_ident()


# ---- a reply that names another broker ---------------------------------------------------------


@pytest.mark.parametrize(
    ("moved", "address"),
    [
        ({"mqttHost": "wss://moved.example.invalid"}, ("moved.example.invalid", 443, "/mqtt")),
        ({"mqttHost": "moved.example.invalid:8443"}, ("moved.example.invalid", 8443, "/mqtt")),
        ({"mqttUrl": "/mqtt/12345"}, ("broker.example.invalid", 443, "/mqtt/12345")),
        # mqttHost names the host; a full mqttUrl's port and path still apply.
        (
            {"mqttUrl": "wss://moved.example.invalid:9443/mqtt/1"},
            ("broker.example.invalid", 9443, "/mqtt/1"),
        ),
    ],
    ids=["host", "host_and_port", "path", "full_url"],
)
@pytest.mark.asyncio
async def test_a_reply_naming_another_broker_rebuilds_a_live_client_on_it(
    moved: dict[str, Any], address: tuple[str, int, str]
) -> None:
    sdk = facade(username="user", password="secret")
    first = sdk.mqtt.client
    first.connected = True
    assert (
        await sdk.async_refresh_broker_credentials(
            FakeAPI({**reply(), **moved}), auth_headers=HEADERS
        )
        is True
    )
    assert sdk.mqtt.client is not first
    assert (sdk.mqtt.broker, sdk.mqtt.port, sdk.mqtt.ws_path) == address
    assert (sdk.mqtt.rebuilds, sdk.mqtt.last_rebuild_reason) == (1, "broker changed")
    assert sdk.mqtt.auth_headers == HEADERS
    assert sdk.mqtt.client.named("connect_async") == [
        ("connect_async", (address[0], address[1], 60), {})
    ]


@pytest.mark.asyncio
async def test_a_reply_naming_the_same_broker_in_other_case_keeps_a_live_client() -> None:
    sdk = facade(username="user", password="secret")
    sdk.mqtt.client.connected = True
    api = FakeAPI(
        {
            "mqttHost": "WSS://Broker.Example.Invalid",
            "mqttUrl": "/mqtt",
            "userName": "u2",
            "pwdInfo": "p2",
        }
    )
    assert await sdk.async_refresh_broker_credentials(api) is True
    assert sdk.mqtt.rebuilds == 0
    assert (sdk.mqtt.username, sdk.mqtt.password) == ("u2", "p2")


@pytest.mark.asyncio
async def test_a_port_the_reply_does_not_name_is_kept() -> None:
    sdk = NavimowSDK(
        broker="broker.example.invalid",
        port=8443,
        ws_path="/mqtt",
        username="user",
        password="secret",
    )
    sdk.mqtt.client.connected = True
    assert await sdk.async_refresh_broker_credentials(FakeAPI(reply())) is True
    assert (sdk.mqtt.broker, sdk.mqtt.port, sdk.mqtt.rebuilds) == (
        "broker.example.invalid",
        8443,
        0,
    )


@pytest.mark.parametrize(
    "host", [None, "", "tcp://moved.example.invalid", "moved.example.invalid:port"]
)
@pytest.mark.asyncio
async def test_a_reply_without_a_readable_broker_still_applies_its_credentials(
    host: Any, caplog: pytest.LogCaptureFixture
) -> None:
    sdk = facade(username="user", password="secret")
    sdk.mqtt.client.connected = True
    with caplog.at_level(logging.WARNING, logger="mower_sdk.sdk"):
        assert (
            await sdk.async_refresh_broker_credentials(
                FakeAPI({"mqttHost": host, "userName": "u2"})
            )
            is True
        )
    assert (sdk.mqtt.broker, sdk.mqtt.port, sdk.mqtt.ws_path) == (
        "broker.example.invalid",
        443,
        "/mqtt",
    )
    assert (sdk.mqtt.username, sdk.mqtt.rebuilds) == ("u2", 0)
    readable = host in (None, "")
    assert any("broker address kept" in r.getMessage() for r in caplog.records) is not readable


@pytest.mark.asyncio
async def test_a_broker_change_with_force_reconnect_rebuilds_once() -> None:
    sdk = facade(username="user", password="secret")
    api = FakeAPI({**reply(), "mqttHost": "moved.example.invalid"})
    assert await sdk.async_refresh_broker_credentials(api, force_reconnect=True) is True
    assert (sdk.mqtt.broker, sdk.mqtt.rebuilds, sdk.mqtt.last_rebuild_reason) == (
        "moved.example.invalid",
        1,
        "broker changed",
    )

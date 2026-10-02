"""The fakes and helpers the test modules share.

A fake here stands in for something the SDK talks to (an aiohttp session and
its response, paho's client, the MQTT client under the facade, the clock) and has only the methods written for it: code
that calls one it lacks, or with arguments it does not take, fails the test. A
fake that one module alone needs stays in that module. The fixtures that
install these fakes are in conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from mower_sdk.api import MowerAPI

# ---- REST: an aiohttp session and its responses ---------------------------------------------------

# The trailing slash is on purpose: MowerAPI removes it before it builds a URL.
BASE_URL = "https://api.example.invalid/"
TOKEN = "token-123"
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
    """A MowerAPI on a FakeSession that answers with these responses, in order."""
    session = FakeSession(*responses)
    return MowerAPI(session=session, token=token, base_url=BASE_URL), session  # type: ignore[arg-type]


# ---- MQTT: paho's client --------------------------------------------------------------------------

Call = tuple[str, tuple[Any, ...], dict[str, Any]]


class FakeClient:
    """Records every paho call made on it; connects to nothing."""

    instances: list[FakeClient] = []
    # Every call on every instance, in order, for tests about the order across clients.
    events: list[tuple[FakeClient, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[Call] = [("__init__", args, kwargs)]
        self.connected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        FakeClient.instances.append(self)
        FakeClient.events.append((self, "__init__"))

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        FakeClient.events.append((self, name))

    def username_pw_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("username_pw_set", *args, **kwargs)

    def ws_set_options(self, *args: Any, **kwargs: Any) -> None:
        self._record("ws_set_options", *args, **kwargs)

    def tls_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("tls_set", *args, **kwargs)

    def reconnect_delay_set(self, *args: Any, **kwargs: Any) -> None:
        self._record("reconnect_delay_set", *args, **kwargs)

    def subscribe(self, *args: Any, **kwargs: Any) -> tuple[int, int | None]:
        """paho's (result, message id): success with the next id, unless subscribe_result says otherwise."""
        self._record("subscribe", *args, **kwargs)
        self.next_mid = getattr(self, "next_mid", 0) + 1
        result = getattr(self, "subscribe_result", 0)
        return result, (self.next_mid if result == 0 else None)

    def unsubscribe(self, *args: Any, **kwargs: Any) -> None:
        self._record("unsubscribe", *args, **kwargs)

    def connect_async(self, *args: Any, **kwargs: Any) -> None:
        self._record("connect_async", *args, **kwargs)

    def loop_start(self) -> None:
        self._record("loop_start")

    def loop_stop(self) -> None:
        self._record("loop_stop")

    def disconnect(self) -> None:
        self._record("disconnect")
        self.connected = False

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self._record("publish", *args, **kwargs)

    def is_connected(self) -> bool:
        return self.connected

    def named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call[0] == name]

    @property
    def callbacks(self) -> tuple[Any, Any, Any, Any]:
        return (
            self.on_connect,
            self.on_disconnect,
            self.on_message,
            getattr(self, "on_connect_fail", None),
        )


class FakeReasonCode:
    """The parts of paho's ReasonCode the client reads."""

    def __init__(self, value: int, name: str) -> None:
        self.value = value
        self.is_failure = value >= 0x80
        self._name = name

    def __str__(self) -> str:
        return self._name


SUCCESS = FakeReasonCode(0, "Success")


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


async def drain() -> None:
    """Let call_soon_threadsafe callbacks and the tasks they create run to their end.

    A marker queued on the loop runs after every callback queued before it, so
    once it has run the tasks those callbacks created exist; they are awaited,
    and the round repeats until no task is left. Nothing here counts turns of
    the loop, so a delivery may take as many as it needs.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(5):
        while True:
            marker = loop.create_future()
            loop.call_soon(marker.set_result, None)
            await marker
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            if not tasks:
                return
            await asyncio.wait(tasks)


# ---- MQTT: the client under the facade ------------------------------------------------------------

DEVICE_ID = "dev-1"


def topic(channel: str, device_id: str = DEVICE_ID) -> str:
    """The topic a mower publishes this channel on."""
    return f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"


class FakeMQTT:
    """Records what NavimowSDK asks of its MQTT client; connects to nothing."""

    instances: list[FakeMQTT] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.on_message: Any = None
        self.on_raw: Any = None
        self.on_message_seen: Any = None
        self.is_connected = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        FakeMQTT.instances.append(self)

    def connect_async(self) -> None:
        self.calls.append(("connect_async", ()))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    def publish_command(self, device_id: str, payload: dict[str, Any]) -> None:
        self.calls.append(("publish_command", (device_id, payload)))

    def update_credentials(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("update_credentials", (args, kwargs)))


# ---- time -----------------------------------------------------------------------------------------

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeClock:
    """Stands in for ``time`` and ``datetime`` in an SDK module: ``monotonic()`` and ``now(tz)`` read from settable values."""

    def __init__(self) -> None:
        self.monotonic_now = 100.0
        self.wall_now = T0

    def monotonic(self) -> float:
        return self.monotonic_now

    def now(self, tz: Any) -> datetime:
        assert tz is UTC
        return self.wall_now

    def advance(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += timedelta(seconds=seconds)

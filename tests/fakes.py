"""The fakes and helpers the test modules share.

A fake here stands in for something the SDK talks to (an aiohttp session and
its response, for a start) and has only the methods written for it: code that
calls one it lacks, or with arguments it does not take, fails the test. A fake
that one module alone needs stays in that module.
"""

from __future__ import annotations

import json
import re
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

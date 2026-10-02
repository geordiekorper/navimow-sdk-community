"""REST API client module.

Provides access to the mower platform's REST API.
"""

import asyncio
import json
import logging
import uuid
import warnings
from typing import Any

import aiohttp

from mower_sdk.errors import (
    ERROR_MESSAGES,
    MowerAPIError,
    MowerAuthRequiredError,
    MowerRateLimitedError,
    MowerTransportError,
)
from mower_sdk.models import (
    CommandReceipt,
    CommandVerdict,
    Device,
    DeviceStatus,
    MowerCommand,
    MqttConnectionInfo,
    _int,
)

_LOGGER = logging.getLogger(__name__)

# An HTTP error body is kept in the error message up to this many characters.
_ERROR_BODY_LIMIT = 500


def _error_body(body: bytes) -> str:
    """Give an HTTP error body as text for an error message.

    Args:
        body: The body of the reply, as bytes.

    Returns:
        The body decoded as UTF-8 with bad bytes replaced: whole when it has
        at most 500 characters, else its first 500 characters followed by a
        note that says it was truncated and gives its full length in
        characters.
    """
    text = body.decode("utf-8", errors="replace")
    if len(text) <= _ERROR_BODY_LIMIT:
        return text
    return f"{text[:_ERROR_BODY_LIMIT]}… [truncated, {len(text)} characters]"


# The spellings under which a reply might carry its command number.
_COMMAND_NUMBER_KEYS = (
    "cmdNum",
    "cmd_num",
    "commandNum",
    "command_num",
    "commandNumber",
    "command_number",
)


def _scalar_command_number(value: Any) -> str | None:
    """Read a command number from the value found under a recognised key.

    Args:
        value: The value to read.

    Returns:
        A str or an int as its stripped text; None for a bool, for any other
        type, and for text that is empty once stripped.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _extract_command_number(value: Any) -> str | None:
    """Find the command number a reply carries under a recognised key.

    Looks for cmdNum, cmd_num, commandNum, command_num, commandNumber and
    command_number in a dict, its own keys first, then recursing into its
    values that are dicts or lists and into list elements that are dicts. A
    scalar is read only from one of those keys; a bare scalar met in a list (a
    warning text, a device id) is never taken. Where cmdNum comes from is
    undocumented and no captured reply carries one, so this is None in
    practice.

    Args:
        value: A reply's data, or any part of it.

    Returns:
        The first command number found, as a non-empty stripped string; None
        when there is none, or when value is neither a dict nor a list.
    """
    if isinstance(value, dict):
        for key in _COMMAND_NUMBER_KEYS:
            if key in value:
                found = _scalar_command_number(value[key])
                if found is not None:
                    return found
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found = _extract_command_number(nested)
                if found is not None:
                    return found
    elif isinstance(value, list):
        for element in value:
            if isinstance(element, dict):
                found = _extract_command_number(element)
                if found is not None:
                    return found
    return None


def _command_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the per-command results of a sendCommands reply.

    A payload or commands that is missing, null or not the expected type, and
    list entries that are not dicts, give fewer results rather than a TypeError
    or AttributeError; what is left decides the ERROR check and the verdict.

    Args:
        data: The reply's data, a dict.

    Returns:
        The dict entries of data.payload.commands, else an empty list.

    Raises:
        AttributeError: data is not a dict, as when the reply's data is null.
    """
    payload = data.get("payload")
    results = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    return [result for result in results if isinstance(result, dict)]


def _classify_command_results(results: list[dict[str, Any]]) -> CommandVerdict:
    """Classify the cloud's verdict on a command from its result dicts.

    An ERROR with an errorCode other than alreadyInState never reaches this
    function: MowerAPI raises on it.

    Args:
        results: The result dicts _command_results read.

    Returns:
        ALREADY_IN_STATE when a result is an ERROR with errorCode
        alreadyInState, which wins over SUCCESS; ACCEPTED when a result is a
        SUCCESS and none is alreadyInState; UNKNOWN for anything else, an
        empty list included.
    """
    verdict = CommandVerdict.UNKNOWN
    for result in results:
        if result.get("status") == "ERROR" and result.get("errorCode") == "alreadyInState":
            verdict = CommandVerdict.ALREADY_IN_STATE
        elif result.get("status") == "SUCCESS" and verdict is CommandVerdict.UNKNOWN:
            verdict = CommandVerdict.ACCEPTED
    return verdict


def _warn_sync_wrapper(name: str) -> None:
    """Warn that a synchronous MowerAPI wrapper was called.

    Emits a DeprecationWarning that names MowerAPI.<name> and its replacement
    MowerAPI.async_<name>, attributed to the wrapper's caller.

    Args:
        name: The wrapper's method name, such as get_devices.

    Raises:
        DeprecationWarning: The warnings filters turn the warning into an
            error.
    """
    warnings.warn(
        f"MowerAPI.{name} is deprecated: use MowerAPI.async_{name}. The synchronous "
        "wrapper calls asyncio.run and cannot be used inside a running event loop.",
        DeprecationWarning,
        stacklevel=3,
    )


class MowerAPI:
    """REST API client.

    Provides synchronous and asynchronous interfaces to the mower platform API,
    over an aiohttp session the caller supplies. The synchronous methods
    (get_devices, get_mqtt_user_info, get_device_status, send_command,
    query_command_results) are deprecated: each wraps its async_* counterpart
    in asyncio.run, so it cannot run inside a running event loop, and it emits
    a DeprecationWarning when called.

    Every request is bounded by the constructor's request_timeout, 20 seconds
    in total by default.

    A failed request or a refusal is a MowerAPIError; the subclass says which
    kind. MowerTransportError: no usable reply (a timeout, a connection error,
    an HTTP 5xx, a status below 200 or a redirect that was not followed, or a
    2xx whose body is not a JSON object), so a command's outcome is unknown.
    MowerAuthRequiredError: no token is set, HTTP 401 or 403, envelope code
    4005, or CODE_OAUTH_INFO_ILLEGAL in the desc. MowerRateLimitedError:
    envelope code 4001, or "too frequent" or "circuit breaker" in the desc. Any
    other HTTP status of 400 or more, or envelope code other than 1, is a plain
    MowerAPIError. An HTTP error body is kept in the message, cut at 500
    characters; the envelope code is kept as envelope_code.

    Attributes:
        base_url: The API base URL, without a trailing slash.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        base_url: str,
        request_timeout: float | None = 20.0,
    ):
        """Create the API client.

        Args:
            session: The aiohttp session every request is sent on.
            token: The access token, sent as a bearer token. While it is None
                or empty, a request raises MowerAuthRequiredError instead of
                being sent; set_token replaces it.
            base_url: The API base URL; trailing slashes are removed.
            request_timeout: Total seconds allowed for each request, passed to
                aiohttp as ClientTimeout(total=request_timeout). None passes no
                timeout, so the session's own policy applies (a bare aiohttp
                session allows 300 seconds in total).
        """
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._token = token
        self._request_timeout = (
            aiohttp.ClientTimeout(total=request_timeout) if request_timeout is not None else None
        )

    def set_token(self, token: str) -> None:
        """Update the access token.

        Args:
            token: The new access token, sent with every request built from
                now on.
        """
        self._token = token

    def _get_auth_headers(self) -> dict[str, str]:
        """Return the authentication headers.

        Returns:
            A new dict holding the Authorization header, which carries the
            token as a bearer token.

        Raises:
            MowerAuthRequiredError: No token is set (None or empty), before any
                request is sent; it carries status_code 401 and error_code
                TOKEN_EXPIRED, as a 401 from the cloud would call for sign-in.
        """
        if not self._token:
            raise MowerAuthRequiredError(
                ERROR_MESSAGES["TOKEN_EXPIRED"],
                status_code=401,
                error_code="TOKEN_EXPIRED",
            )
        return {"Authorization": f"Bearer {self._token}"}

    async def _async_request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an HTTP request asynchronously.

        The request goes to base_url joined to endpoint, with the bearer token,
        a requestId header holding a fresh UUID and, when one is set, the
        client's request timeout. The reply's status and body are judged by
        _reply; its envelope is not checked here (_unwrap does that).

        Args:
            method: The HTTP method, such as GET or POST, passed to aiohttp as
                given.
            endpoint: The API endpoint, a path relative to base_url; leading
                slashes are ignored.
            data: The request body, sent as JSON; None sends no body.
            params: The query parameters; None sends none.

        Returns:
            The reply's JSON object, envelope included.

        Raises:
            MowerTransportError: No usable reply: a timeout or an aiohttp
                client error while sending or reading, which carries no
                status_code, or a status or body _reply raises it for. The
                aiohttp error, TimeoutError or decoding error, if any, is its
                __cause__.
            MowerAuthRequiredError: No token is set, before anything is sent;
                or HTTP 401 or 403.
            MowerAPIError: Any other HTTP status of 400 or more.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_auth_headers()
        headers["requestId"] = str(uuid.uuid4())
        request_options: dict[str, Any] = {}
        if self._request_timeout is not None:
            request_options["timeout"] = self._request_timeout

        try:
            session = self._session
            async with session.request(
                method, url, json=data, params=params, headers=headers, **request_options
            ) as response:
                status = response.status
                body = await response.read()
        except (TimeoutError, aiohttp.ClientError) as e:
            # asyncio.TimeoutError is TimeoutError from Python 3.11, and aiohttp
            # raises it when a ClientTimeout expires. A TimeoutError carries no
            # text, so its class name stands in.
            raise MowerTransportError(
                f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: {str(e) or type(e).__name__}"
            ) from e
        return self._reply(status, body)

    @staticmethod
    def _reply(status: int, body: bytes) -> dict[str, Any]:
        """Give the reply as a JSON object, or raise the error its status or body calls for.

        The status decides first, for every non-2xx reply whatever its body; the
        body of a 2xx is then read as UTF-8 JSON, whatever its content type.
        Every error carries the status as status_code; one raised for the
        status keeps the body in its message, cut at 500 characters.

        Args:
            status: The HTTP status of the reply.
            body: The body of the reply, as bytes.

        Returns:
            The JSON object the body of a 2xx reply holds.

        Raises:
            MowerAuthRequiredError: The status is 401 or 403.
            MowerTransportError: The status is 500 or more, below 200, or from
                300 to 399; or the body of a 2xx is not UTF-8, is empty or
                blank, is not JSON, or is JSON other than an object. The
                UnicodeDecodeError or JSONDecodeError, if any, is its
                __cause__.
            MowerAPIError: Any other status of 400 or more.
        """
        failed = ERROR_MESSAGES["API_REQUEST_FAILED"]
        if status in (401, 403):
            raise MowerAuthRequiredError(f"{failed}: {_error_body(body)}", status_code=status)
        if status >= 500 or status < 200 or 300 <= status < 400:
            raise MowerTransportError(f"{failed}: {_error_body(body)}", status_code=status)
        if status >= 400:
            raise MowerAPIError(f"{failed}: {_error_body(body)}", status_code=status)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as e:
            raise MowerTransportError(f"{failed}: reply is not UTF-8", status_code=status) from e
        if not text.strip():
            raise MowerTransportError(f"{failed}: empty reply", status_code=status)
        try:
            reply = json.loads(text)
        except json.JSONDecodeError as e:
            raise MowerTransportError(f"{failed}: reply is not JSON", status_code=status) from e
        if not isinstance(reply, dict):
            raise MowerTransportError(f"{failed}: reply is not a JSON object", status_code=status)
        return reply

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> Any:
        """Check the reply envelope and return its data.

        A code other than 1, a missing one included, is an error. Its message
        carries the reply's desc, and its envelope_code is the code read as an
        int, or None when the code cannot be read as one. The desc is matched
        without regard to letter case.

        Args:
            response: The reply's JSON object, as _async_request returned it.

        Returns:
            The envelope's data: {} when the key is missing and None when the
            reply carries an explicit null.

        Raises:
            MowerAuthRequiredError: The code is 4005, or the desc contains
                CODE_OAUTH_INFO_ILLEGAL.
            MowerRateLimitedError: The code is 4001, or the desc contains "too
                frequent" or "circuit breaker".
            MowerAPIError: The code is not 1 otherwise.
        """
        code = response.get("code")
        if code != 1:
            desc = response.get("desc")
            message = f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: {desc}"
            envelope_code = _int(code)
            lowered = str(desc).lower()
            if envelope_code == 4005 or "code_oauth_info_illegal" in lowered:
                raise MowerAuthRequiredError(message, envelope_code=envelope_code)
            if envelope_code == 4001 or "too frequent" in lowered or "circuit breaker" in lowered:
                raise MowerRateLimitedError(message, envelope_code=envelope_code)
            raise MowerAPIError(message, envelope_code=envelope_code)
        return response.get("data", {})

    @staticmethod
    def _device_entries(data: Any) -> list[dict[str, Any]]:
        """Read the device entries of a reply's data.

        A successful reply without entries, whether data, payload or devices is
        missing, null or not of the expected type, is an empty list; an entry
        that is not a dict is left out.

        Args:
            data: The reply's data, as _unwrap returned it.

        Returns:
            The dict entries of data.payload.devices, or [] when there are
            none.
        """
        payload = data.get("payload") if isinstance(data, dict) else None
        devices = payload.get("devices") if isinstance(payload, dict) else None
        return (
            [entry for entry in devices if isinstance(entry, dict)]
            if isinstance(devices, list)
            else []
        )

    async def async_get_devices_raw(self) -> list[dict[str, Any]]:
        """Fetch the device list as the cloud sent it.

        One authList request. Device.from_dict keeps only the fields it names,
        so a field it does not read reaches the caller here. async_get_devices
        reads the same entries into Device.

        Returns:
            The dict entries of the reply's data.payload.devices, unchanged; an
            empty list for a successful reply without entries (data, payload or
            devices missing, null or not of the expected type).

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
        """
        response = await self._async_request("GET", "/openapi/smarthome/authList")
        return self._device_entries(self._unwrap(response))

    async def async_get_devices(self) -> list[Device]:
        """Fetch the device list asynchronously.

        Reads async_get_devices_raw's entries into Device. An entry without an
        id (missing, null or empty) is left out and logged at warning level,
        since a device cannot be addressed without one.

        Returns:
            The devices; an empty list for a successful reply without entries.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
        """
        devices = []
        for entry in await self.async_get_devices_raw():
            if not entry.get("id"):
                _LOGGER.warning(
                    "Skipping a device entry without an id (keys: %s)", sorted(map(str, entry))
                )
                continue
            devices.append(Device.from_dict(entry))
        return devices

    async def async_get_mqtt_user_info(self) -> dict[str, Any]:
        """Fetch the MQTT connection information asynchronously.

        One userInfo request, which the cloud allows about once a minute.
        async_get_mqtt_connection_info makes the same request and reads the
        reply into an MqttConnectionInfo.

        Returns:
            The MQTT connection information: the reply's data, unchanged. It
            is {} when the reply has no data key and None when its data is
            null.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
        """
        response = await self._async_request("GET", "/openapi/mqtt/userInfo/get/v2")
        return self._unwrap(response)

    async def async_get_mqtt_connection_info(self) -> MqttConnectionInfo:
        """Fetch the MQTT connection information, read into an MqttConnectionInfo.

        The same request as async_get_mqtt_user_info, which allows about one
        call a minute; the reply is read by MqttConnectionInfo.from_dict, and
        NavimowSDK.from_connection_info builds the facade from the result.

        Returns:
            The MqttConnectionInfo read from the reply's data.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1; or the reply's data is not an object
                (null included), names no broker, or names one that cannot be
                read (see MqttConnectionInfo.from_dict).
        """
        return MqttConnectionInfo.from_dict(await self.async_get_mqtt_user_info())

    def get_devices(self) -> list[Device]:
        """Fetch the device list synchronously (deprecated).

        Deprecated: use async_get_devices. This wrapper emits a
        DeprecationWarning attributed to its caller, then runs
        async_get_devices in asyncio.run, so it cannot be used inside a running
        event loop.

        Returns:
            The devices, as async_get_devices returns them.

        Raises:
            DeprecationWarning: The warnings filters turn the deprecation
                warning into an error; no request is sent.
            RuntimeError: Called inside a running event loop: asyncio.run
                refuses to start, and no request is sent.
            MowerAPIError: Whatever async_get_devices raises: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError.
        """
        _warn_sync_wrapper("get_devices")
        return asyncio.run(self.async_get_devices())

    def get_mqtt_user_info(self) -> dict[str, Any]:
        """Fetch the MQTT connection information synchronously (deprecated).

        Deprecated: use async_get_mqtt_user_info. This wrapper emits a
        DeprecationWarning attributed to its caller, then runs
        async_get_mqtt_user_info in asyncio.run, so it cannot be used inside a
        running event loop.

        Returns:
            The reply's data, as async_get_mqtt_user_info returns it.

        Raises:
            DeprecationWarning: The warnings filters turn the deprecation
                warning into an error; no request is sent.
            RuntimeError: Called inside a running event loop: asyncio.run
                refuses to start, and no request is sent.
            MowerAPIError: Whatever async_get_mqtt_user_info raises: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError.
        """
        _warn_sync_wrapper("get_mqtt_user_info")
        return asyncio.run(self.async_get_mqtt_user_info())

    async def async_get_vehicle_status_raw(self, device_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch the status entries of several devices as the cloud sent them.

        One getVehicleStatus request that names every id; none is made for no
        ids. An X430's entry carried id, capacityRemaining, vehicleState and
        descriptiveCapacityRemaining; a field the cloud starts sending reaches
        the caller here first. async_get_device_statuses reads the same entries
        into DeviceStatus.

        Args:
            device_ids: The ids of the devices to ask about.

        Returns:
            The dict entries of the reply's data.payload.devices, unchanged; an
            empty list for a successful reply without entries (data, payload or
            devices missing, null or not of the expected type) and, without a
            request, for no ids.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
        """
        if not device_ids:
            return []
        response = await self._async_request(
            "POST",
            "/openapi/smarthome/getVehicleStatus",
            data={"devices": [{"id": device_id} for device_id in device_ids]},
        )
        return self._device_entries(self._unwrap(response))

    async def async_get_device_statuses(self, device_ids: list[str]) -> dict[str, DeviceStatus]:
        """Fetch the status of several devices asynchronously.

        Reads async_get_vehicle_status_raw's entries into DeviceStatus.

        Args:
            device_ids: The ids of the devices to ask about.

        Returns:
            A mapping from device id to DeviceStatus, with an item for each
            entry of the reply; an entry without an id is left out, and a
            device the reply does not name has no item. Empty, without a
            request, for no ids.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
        """
        result: dict[str, DeviceStatus] = {}
        for status_data in await self.async_get_vehicle_status_raw(device_ids):
            status = DeviceStatus.from_dict(status_data)
            if status.device_id:
                result[status.device_id] = status
        return result

    async def async_get_device_status(self, device_id: str) -> DeviceStatus:
        """Fetch a device's status asynchronously.

        One getVehicleStatus request for the one device, read as
        async_get_device_statuses reads it.

        Args:
            device_id: The id of the device.

        Returns:
            The device's status.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: The device is not found: the reply has no status
                for it, or the cloud answered HTTP 404. This error carries
                status_code 404 and error_code DEVICE_NOT_FOUND. Also any
                other HTTP status of 400 or more, or any other envelope code
                than 1.
        """
        try:
            statuses = await self.async_get_device_statuses([device_id])
            status = statuses.get(device_id)
            if not status:
                raise MowerAPIError(
                    ERROR_MESSAGES["DEVICE_NOT_FOUND"],
                    status_code=404,
                    error_code="DEVICE_NOT_FOUND",
                )
            return status
        except MowerAPIError as e:
            if e.status_code == 404:
                raise MowerAPIError(
                    ERROR_MESSAGES["DEVICE_NOT_FOUND"],
                    status_code=404,
                    error_code="DEVICE_NOT_FOUND",
                ) from e
            raise

    def get_device_status(self, device_id: str) -> DeviceStatus:
        """Fetch a device's status synchronously (deprecated).

        Deprecated: use async_get_device_status. This wrapper emits a
        DeprecationWarning attributed to its caller, then runs
        async_get_device_status in asyncio.run, so it cannot be used inside a
        running event loop.

        Args:
            device_id: The id of the device.

        Returns:
            The device's status, as async_get_device_status returns it.

        Raises:
            DeprecationWarning: The warnings filters turn the deprecation
                warning into an error; no request is sent.
            RuntimeError: Called inside a running event loop: asyncio.run
                refuses to start, and no request is sent.
            MowerAPIError: Whatever async_get_device_status raises: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError, which includes
                the device not being found.
        """
        _warn_sync_wrapper("get_device_status")
        return asyncio.run(self.async_get_device_status(device_id))

    async def _async_send_command(
        self, device_id: str, command: MowerCommand
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Send a control command and return the reply's data with its command results.

        One sendCommands request for the one device. START is sent as StartStop
        with on true and STOP with on false, RESUME as PauseUnpause with on
        true and PAUSE with on false, and DOCK as Dock without params. The
        reply's per-command results are then checked in order: a result whose
        status is ERROR raises, unless its errorCode is alreadyInState, which
        is not a failure.

        Args:
            device_id: The id of the device to command.
            command: The command to send.

        Returns:
            The reply's data unchanged, and the dict entries of
            data.payload.commands, the per-command results the ERROR check ran
            over (an empty list when the reply has none).

        Raises:
            MowerAPIError: command is not one of the five commands above, with
                error_code INVALID_COMMAND and before any request; or a result
                is an ERROR other than alreadyInState, with the result's
                errorCode as error_code (COMMAND_FAILED when it has none).
                Also what _async_request and _unwrap raise: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError.
            AttributeError: A successful reply whose data is null, a behaviour
                kept from upstream.
        """
        command_mapping: dict[MowerCommand, tuple[str, dict[str, Any] | None]] = {
            MowerCommand.START: (
                "action.devices.commands.StartStop",
                {"on": True},
            ),
            MowerCommand.STOP: (
                "action.devices.commands.StartStop",
                {"on": False},
            ),
            MowerCommand.PAUSE: (
                "action.devices.commands.PauseUnpause",
                {"on": False},
            ),
            MowerCommand.RESUME: (
                "action.devices.commands.PauseUnpause",
                {"on": True},
            ),
            MowerCommand.DOCK: ("action.devices.commands.Dock", None),
        }
        if command not in command_mapping:
            raise MowerAPIError(
                ERROR_MESSAGES["INVALID_COMMAND"],
                error_code="INVALID_COMMAND",
            )
        command_name, params = command_mapping[command]
        execution: dict[str, Any] = {"command": command_name}
        if params is not None:
            execution["params"] = params

        response = await self._async_request(
            "POST",
            "/openapi/smarthome/sendCommands",
            data={"commands": [{"devices": [{"id": device_id}], "execution": execution}]},
        )
        data = self._unwrap(response)
        command_results = _command_results(data)
        for result in command_results:
            if result.get("status") == "ERROR":
                error_code = result.get("errorCode") or "COMMAND_FAILED"
                # Treat as success when the device is already in the target state,
                # so a repeated tap or a stale state view does not raise
                if error_code == "alreadyInState":
                    continue
                raise MowerAPIError(
                    f"{ERROR_MESSAGES['COMMAND_FAILED']}: {error_code}",
                    error_code=error_code,
                )
        return data, command_results

    async def async_send_command(self, device_id: str, command: MowerCommand) -> dict[str, Any]:
        """Send a control command asynchronously.

        A reply that says the device is already in the target state (an ERROR
        result with errorCode alreadyInState) is not a failure.
        async_send_command_receipt sends the same request and classifies the
        reply.

        Args:
            device_id: The id of the device to command.
            command: The command to send.

        Returns:
            The command execution result: the reply's data, unchanged.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object. The cloud may still
                have accepted the command.
            MowerAPIError: The cloud refused the command: a result in the
                reply is an ERROR other than alreadyInState, and its errorCode
                is the error_code (COMMAND_FAILED when it has none). Also any
                other HTTP status of 400 or more, any other envelope code than
                1, or a command that is not a MowerCommand member (error_code
                INVALID_COMMAND, no request is sent).
            AttributeError: A successful reply whose data is null, a behaviour
                kept from upstream.
        """
        data, _results = await self._async_send_command(device_id, command)
        return data

    async def async_send_command_receipt(
        self, device_id: str, command: MowerCommand
    ) -> CommandReceipt:
        """Send a control command asynchronously and classify the reply.

        The verdict comes from data.payload.commands: an ERROR result with
        errorCode alreadyInState gives ALREADY_IN_STATE and wins over SUCCESS;
        a SUCCESS result gives ACCEPTED; anything else, an empty list included,
        gives UNKNOWN. ACCEPTED means the cloud accepted the command, not that
        the mower acted: pause and resume settle within about 30 seconds and
        docking can take minutes, so poll the status for the target state.
        command_number is the cmdNum the reply carries under a recognised key,
        if any; no captured reply does.

        Two cases return no receipt. Any other ERROR result raises MowerAPIError
        exactly as async_send_command does: the cloud refused the command. No
        usable reply (a timeout, a connection error, an HTTP 5xx, or a body that
        is not a JSON object) raises MowerTransportError: the cloud may still
        have accepted the command.

        Args:
            device_id: The id of the device to command.
            command: The command to send.

        Returns:
            A CommandReceipt with the device id, the command, the verdict, the
            command number and the reply's per-command result dicts.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply; the cloud may still have
                accepted the command.
            MowerAPIError: The cloud refused the command: a result in the
                reply is an ERROR other than alreadyInState, and its errorCode
                is the error_code (COMMAND_FAILED when it has none). Also any
                other HTTP status of 400 or more, any other envelope code than
                1, or a command that is not a MowerCommand member (error_code
                INVALID_COMMAND, no request is sent).
            AttributeError: A successful reply whose data is null, a behaviour
                kept from upstream.
        """
        data, results = await self._async_send_command(device_id, command)
        return CommandReceipt(
            device_id=device_id,
            command=command,
            verdict=_classify_command_results(results),
            command_number=_extract_command_number(data),
            results=tuple(results),
        )

    def send_command(self, device_id: str, command: MowerCommand) -> dict[str, Any]:
        """Send a control command synchronously (deprecated).

        Deprecated: use async_send_command. This wrapper emits a
        DeprecationWarning attributed to its caller, then runs
        async_send_command in asyncio.run, so it cannot be used inside a
        running event loop.

        Args:
            device_id: The id of the device to command.
            command: The command to send.

        Returns:
            The command execution result, as async_send_command returns it.

        Raises:
            DeprecationWarning: The warnings filters turn the deprecation
                warning into an error; no request is sent.
            RuntimeError: Called inside a running event loop: asyncio.run
                refuses to start, and no request is sent.
            MowerAPIError: Whatever async_send_command raises: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError, which includes
                a command the cloud refused.
            AttributeError: A successful reply whose data is null, as
                async_send_command raises it.
        """
        _warn_sync_wrapper("send_command")
        return asyncio.run(self.async_send_command(device_id, command))

    async def async_query_command_results(
        self, devices: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """Query command execution results asynchronously.

        One responseCommands request that carries devices as given; none is
        made for an empty list. The reply's entries are returned unread: see
        async_get_command_result for what is known of the endpoint.

        Args:
            devices: The command targets, each a dict with an id and, to name
                one command, a cmdNum. The dicts are not checked.

        Returns:
            The command execution results: the reply's data.payload.devices,
            as the cloud sent it. An empty list when the reply has no data,
            payload or devices key and, without a request, when the devices
            argument is empty.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
            AttributeError: A successful reply whose data or payload is null,
                a behaviour kept from upstream.
        """
        if not devices:
            return []
        response = await self._async_request(
            "POST",
            "/openapi/smarthome/responseCommands",
            data={"devices": devices},
        )
        payload = self._unwrap(response).get("payload", {})
        return payload.get("devices", [])

    def query_command_results(self, devices: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Query command execution results synchronously (deprecated).

        Deprecated: use async_query_command_results. This wrapper emits a
        DeprecationWarning attributed to its caller, then runs
        async_query_command_results in asyncio.run, so it cannot be used inside
        a running event loop.

        Args:
            devices: The command targets, each a dict with an id and, to name
                one command, a cmdNum.

        Returns:
            The command execution results, as async_query_command_results
            returns them.

        Raises:
            DeprecationWarning: The warnings filters turn the deprecation
                warning into an error; no request is sent.
            RuntimeError: Called inside a running event loop: asyncio.run
                refuses to start, and no request is sent.
            MowerAPIError: Whatever async_query_command_results raises: a
                MowerAuthRequiredError, MowerRateLimitedError or
                MowerTransportError, or a plain MowerAPIError.
            AttributeError: A successful reply whose data or payload is null,
                as async_query_command_results raises it.
        """
        _warn_sync_wrapper("query_command_results")
        return asyncio.run(self.async_query_command_results(devices))

    async def async_get_command_result(
        self, device_id: str, cmd_num: str | None = None
    ) -> dict[str, Any] | None:
        """Query the command execution result of one device asynchronously.

        Wraps async_query_command_results for the single-device case. The query
        is {"id": device_id}, with cmdNum added only when cmd_num is given.

        The endpoint is known from this SDK's code only: no cited capture shows
        a reply, so its shape is unconfirmed, and where a cmdNum would come from
        is undocumented (no captured sendCommands reply carries one; see
        CommandReceipt.command_number).

        Args:
            device_id: The id of the device.
            cmd_num: The command number, to query one command; None leaves
                cmdNum out of the query.

        Returns:
            The first reply entry that is a dict whose id equals device_id, or
            None when the reply has none; an entry without an id never matches.

        Raises:
            MowerAuthRequiredError: No token is set (no request is sent), or
                the cloud refused the credentials: HTTP 401 or 403, or an
                auth refusal in the reply envelope.
            MowerRateLimitedError: The reply envelope says the calls come too
                often.
            MowerTransportError: No usable reply: a timeout, a connection
                error, an HTTP 5xx or another status the client cannot use,
                or a 2xx whose body is not a JSON object.
            MowerAPIError: Any other HTTP status of 400 or more, or any other
                envelope code than 1.
            AttributeError: A successful reply whose data or payload is null,
                as async_query_command_results raises it.
            TypeError: A successful reply whose data.payload.devices is null.
        """
        query: dict[str, str] = {"id": device_id}
        if cmd_num is not None:
            query["cmdNum"] = cmd_num
        results = await self.async_query_command_results([query])
        for entry in results:
            if isinstance(entry, dict) and entry.get("id") == device_id:
                return entry
        return None

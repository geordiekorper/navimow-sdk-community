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
    MowerAPIError,
    MowerAuthRequiredError,
    MowerRateLimitedError,
    MowerTransportError,
    ERROR_MESSAGES,
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
    """An HTTP error body for a message: decoded with bad bytes replaced, cut at the limit."""
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
    """A str or int (not a bool) as a non-empty stripped string, else None."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _extract_command_number(value: Any) -> str | None:
    """Return the command number a reply carries under a recognised key, else None.

    Looks for cmdNum, cmd_num, commandNum, command_num, commandNumber and
    command_number in a dict, recursing into dict values and into list elements
    that are dicts. A scalar is read only from one of those keys; a bare scalar
    met in a list (a warning text, a device id) is never taken. Where cmdNum
    comes from is undocumented and no captured reply carries one, so this is
    None in practice.
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
    """The dict entries of data.payload.commands, else an empty list.

    A payload or commands that is missing, null or not the expected type, and
    list entries that are not dicts, give fewer results rather than a TypeError
    or AttributeError; what is left decides the ERROR check and the verdict.
    """
    payload = data.get("payload")
    results = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    return [result for result in results if isinstance(result, dict)]


def _classify_command_results(results: list[dict[str, Any]]) -> CommandVerdict:
    """The cloud's verdict on a command, from the result dicts _command_results read.

    An ERROR with errorCode alreadyInState gives ALREADY_IN_STATE and wins over
    SUCCESS; SUCCESS gives ACCEPTED while nothing else has been seen; anything
    else, an empty list included, gives UNKNOWN. Any other ERROR never reaches
    this function: MowerAPI raises on it.
    """
    verdict = CommandVerdict.UNKNOWN
    for result in results:
        if result.get("status") == "ERROR" and result.get("errorCode") == "alreadyInState":
            verdict = CommandVerdict.ALREADY_IN_STATE
        elif result.get("status") == "SUCCESS" and verdict is CommandVerdict.UNKNOWN:
            verdict = CommandVerdict.ACCEPTED
    return verdict


def _warn_sync_wrapper(name: str) -> None:
    """Warn that a synchronous MowerAPI wrapper was called; attributed to its caller."""
    warnings.warn(
        f"MowerAPI.{name} is deprecated: use MowerAPI.async_{name}. The synchronous "
        "wrapper calls asyncio.run and cannot be used inside a running event loop.",
        DeprecationWarning,
        stacklevel=3,
    )


class MowerAPI:
    """REST API client.

    Provides synchronous and asynchronous interfaces to the mower platform API.
    The synchronous methods (get_devices, get_mqtt_user_info, get_device_status,
    send_command, query_command_results) are deprecated: each wraps its async_*
    counterpart in asyncio.run, so it cannot run inside a running event loop,
    and it emits a DeprecationWarning when called.

    Every request is bounded by request_timeout, 20 seconds in total by
    default. Pass request_timeout=None to leave the session's own timeout
    policy in force instead.

    A failed request or a refusal is a MowerAPIError; the subclass says which
    kind. (Kept from upstream: a successful reply whose data is null makes the
    endpoints that read into it raise AttributeError.)
    MowerTransportError: no usable reply (a timeout, a connection error, an
    HTTP 5xx, a status below 200 or a redirect that was not followed, or a 2xx
    whose body is not a JSON object), so a command's outcome is unknown.
    MowerAuthRequiredError: HTTP 401 or 403, envelope code 4005, or
    CODE_OAUTH_INFO_ILLEGAL in the desc. MowerRateLimitedError: envelope code
    4001, or "too frequent" or "circuit breaker" in the desc. Any other HTTP
    status of 400 or more, or envelope code other than 1, is a plain
    MowerAPIError. An HTTP error body is kept in the message, cut at 500
    characters; the envelope code is kept as envelope_code.

    Attributes:
        base_url: API base URL
        session: aiohttp session (asynchronous)
        token: Access token
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        base_url: str,
        request_timeout: float | None = 20.0,
    ):
        """Initialize the API client.

        Args:
            session: aiohttp session
            token: Access token
            base_url: API base URL
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
        """Update the access token."""
        self._token = token

    def _get_auth_headers(self) -> dict[str, str]:
        """Return the authentication headers.

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

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (relative path)
            data: Request body (optional)
            params: Query parameters (optional)

        Returns:
            Response JSON data

        Raises:
            MowerTransportError: No usable reply (see the class docstring); the
                aiohttp error, TimeoutError or decoding error, if any, is its
                __cause__.
            MowerAuthRequiredError: HTTP 401 or 403, or no token is set.
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
        """The reply as a JSON object, or the error the status or the body calls for.

        The status decides first, for every non-2xx reply whatever its body; the
        body of a 2xx is then read as UTF-8 JSON, whatever its content type.
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

        Raises:
            MowerAuthRequiredError: code 4005, or CODE_OAUTH_INFO_ILLEGAL in desc
            MowerRateLimitedError: code 4001, or "too frequent" or "circuit
                breaker" in desc
            MowerAPIError: If the envelope's code is not 1 otherwise, with the
                reply's desc; the code is kept as envelope_code

        Returns:
            response["data"]: {} when the key is missing and None when the reply
            carries an explicit null, exactly as each endpoint read it before
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
        """The dict entries of data.payload.devices, or [] when there are none.

        A successful reply without entries, whether data, payload or devices is
        missing, null or not of the expected type, is an empty list; an entry
        that is not a dict is left out.
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

        One authList request; returns the dict entries of the reply's
        data.payload.devices unchanged, and an empty list for a successful reply
        without entries (data, payload or devices missing, null or not of the
        expected type). Device.from_dict keeps only the fields it names, so a
        field it does not read reaches the caller here. async_get_devices reads
        the same entries into Device.

        Returns:
            The device entries, as dicts

        Raises:
            MowerAPIError: If the request fails
        """
        response = await self._async_request("GET", "/openapi/smarthome/authList")
        return self._device_entries(self._unwrap(response))

    async def async_get_devices(self) -> list[Device]:
        """Fetch the device list asynchronously.

        Reads async_get_devices_raw's entries into Device. A successful reply
        without entries gives an empty list; an entry without an id (missing,
        null or empty) is left out and logged at warning level, since a device
        cannot be addressed without one.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
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

        Returns:
            MQTT connection information

        Raises:
            MowerAPIError: If the request fails
        """
        response = await self._async_request("GET", "/openapi/mqtt/userInfo/get/v2")
        return self._unwrap(response)

    async def async_get_mqtt_connection_info(self) -> MqttConnectionInfo:
        """Fetch the MQTT connection information, read into an MqttConnectionInfo.

        The same request as async_get_mqtt_user_info, which allows about one
        call a minute; the reply is read by MqttConnectionInfo.from_dict, and
        NavimowSDK.from_connection_info builds the facade from the result.

        Raises:
            MowerAPIError: If the request fails, or the reply names no broker or
                one that cannot be read (see MqttConnectionInfo.from_dict)
        """
        return MqttConnectionInfo.from_dict(await self.async_get_mqtt_user_info())

    def get_devices(self) -> list[Device]:
        """Fetch the device list synchronously.

        Deprecated: use async_get_devices. This wrapper calls asyncio.run and
        cannot be used inside a running event loop.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
        """
        _warn_sync_wrapper("get_devices")
        return asyncio.run(self.async_get_devices())

    def get_mqtt_user_info(self) -> dict[str, Any]:
        """Fetch the MQTT connection information synchronously.

        Deprecated: use async_get_mqtt_user_info. This wrapper calls asyncio.run
        and cannot be used inside a running event loop.
        """
        _warn_sync_wrapper("get_mqtt_user_info")
        return asyncio.run(self.async_get_mqtt_user_info())

    async def async_get_vehicle_status_raw(self, device_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch the status entries of several devices as the cloud sent them.

        One getVehicleStatus request; returns the dict entries of the reply's
        data.payload.devices unchanged, an empty list for a successful reply
        without entries (data, payload or devices missing or null), and an empty
        list, without a request, for no ids. An X430's entry carried id, capacityRemaining, vehicleState and
        descriptiveCapacityRemaining; a field the cloud starts sending reaches the
        caller here first. async_get_device_statuses reads the same entries into
        DeviceStatus.

        Args:
            device_ids: List of device IDs

        Returns:
            The status entries, as dicts

        Raises:
            MowerAPIError: If the request fails
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

        Args:
            device_ids: List of device IDs

        Returns:
            Mapping from device ID to status; an entry without an id is left out

        Raises:
            MowerAPIError: If the request fails
        """
        result: dict[str, DeviceStatus] = {}
        for status_data in await self.async_get_vehicle_status_raw(device_ids):
            status = DeviceStatus.from_dict(status_data)
            if status.device_id:
                result[status.device_id] = status
        return result

    async def async_get_device_status(self, device_id: str) -> DeviceStatus:
        """Fetch a device's status asynchronously.

        Args:
            device_id: Device ID

        Returns:
            Device status

        Raises:
            MowerAPIError: If the request fails or the device is not found
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
        """Fetch a device's status synchronously.

        Deprecated: use async_get_device_status. This wrapper calls asyncio.run
        and cannot be used inside a running event loop.

        Args:
            device_id: Device ID

        Returns:
            Device status

        Raises:
            MowerAPIError: If the request fails or the device is not found
        """
        _warn_sync_wrapper("get_device_status")
        return asyncio.run(self.async_get_device_status(device_id))

    async def _async_send_command(
        self, device_id: str, command: MowerCommand
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Send a control command and return the reply's data with the command results inspected.

        Args:
            device_id: Device ID
            command: Control command

        Returns:
            The reply's data unchanged, and the dict entries of
            data.payload.commands, the per-command results the ERROR check ran
            over (an empty list when the reply has none)

        Raises:
            MowerAPIError: If the request fails or the command fails; an ERROR
                result with errorCode alreadyInState is not a failure
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
                # Treat as success when the device is already in the target state, so a repeated tap or a stale state view does not raise
                if error_code == "alreadyInState":
                    continue
                raise MowerAPIError(
                    f"{ERROR_MESSAGES['COMMAND_FAILED']}: {error_code}",
                    error_code=error_code,
                )
        return data, command_results

    async def async_send_command(self, device_id: str, command: MowerCommand) -> dict[str, Any]:
        """Send a control command asynchronously.

        Args:
            device_id: Device ID
            command: Control command

        Returns:
            Command execution result: the reply's data, unchanged

        Raises:
            MowerAPIError: If the request fails or the command fails
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
            device_id: Device ID
            command: Control command

        Returns:
            A CommandReceipt

        Raises:
            MowerAPIError: If the request fails or the command fails
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
        """Send a control command synchronously.

        Deprecated: use async_send_command. This wrapper calls asyncio.run and
        cannot be used inside a running event loop.

        Args:
            device_id: Device ID
            command: Control command

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the request fails or the command fails
        """
        _warn_sync_wrapper("send_command")
        return asyncio.run(self.async_send_command(device_id, command))

    async def async_query_command_results(
        self, devices: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """Query command execution results asynchronously.

        Args:
            devices: List of command targets, each with an id and a cmdNum

        Returns:
            List of command execution results

        Raises:
            MowerAPIError: If the request fails
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
        """Query command execution results synchronously.

        Deprecated: use async_query_command_results. This wrapper calls
        asyncio.run and cannot be used inside a running event loop.
        """
        _warn_sync_wrapper("query_command_results")
        return asyncio.run(self.async_query_command_results(devices))

    async def async_get_command_result(
        self, device_id: str, cmd_num: str | None = None
    ) -> dict[str, Any] | None:
        """Query the command execution result of one device asynchronously.

        Wraps async_query_command_results for the single-device case. The query
        is {"id": device_id}, with cmdNum added only when cmd_num is given.
        Returns the reply entry whose id equals device_id, or None when the
        reply has none; an entry without an id never matches.

        The endpoint is known from this SDK's code only: no cited capture shows
        a reply, so its shape is unconfirmed, and where a cmdNum would come from
        is undocumented (no captured sendCommands reply carries one; see
        CommandReceipt.command_number).

        Args:
            device_id: Device ID
            cmd_num: Command number to query a specific command (optional)

        Returns:
            The device's result entry, or None

        Raises:
            MowerAPIError: If the request fails
        """
        query: dict[str, str] = {"id": device_id}
        if cmd_num is not None:
            query["cmdNum"] = cmd_num
        results = await self.async_query_command_results([query])
        for entry in results:
            if isinstance(entry, dict) and entry.get("id") == device_id:
                return entry
        return None

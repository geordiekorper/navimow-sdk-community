"""REST API client module.

Provides access to the mower platform's REST API.
"""

import asyncio
import uuid
from typing import Any

import aiohttp

from mower_sdk.errors import MowerAPIError, ERROR_MESSAGES
from mower_sdk.models import Device, DeviceStatus, MowerCommand


class MowerAPI:
    """REST API client.

    Provides synchronous and asynchronous interfaces to the mower platform API.

    Attributes:
        base_url: API base URL
        session: aiohttp session (asynchronous)
        token: Access token
    """

    def __init__(self, session: aiohttp.ClientSession, token: str, base_url: str):
        """Initialize the API client.

        Args:
            session: aiohttp session
            token: Access token
            base_url: API base URL
        """
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._token = token

    def set_token(self, token: str) -> None:
        """Update the access token."""
        self._token = token

    def _get_auth_headers(self) -> dict[str, str]:
        """Return the authentication headers."""
        if not self._token:
            raise MowerAPIError(
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
            MowerAPIError: If the request fails
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_auth_headers()
        headers["requestId"] = str(uuid.uuid4())

        try:
            session = self._session
            async with session.request(
                method, url, json=data, params=params, headers=headers
            ) as response:
                if response.status >= 400:
                    error_text = await response.text()
                    raise MowerAPIError(
                        f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: {error_text}",
                        status_code=response.status,
                    )

                return await response.json()
        except aiohttp.ClientError as e:
            raise MowerAPIError(
                f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: {str(e)}"
            ) from e

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> Any:
        """Check the reply envelope and return its data.

        Raises:
            MowerAPIError: If the envelope's code is not 1, with the reply's desc

        Returns:
            response["data"]: {} when the key is missing and None when the reply
            carries an explicit null, exactly as each endpoint read it before
        """
        if response.get("code") != 1:
            raise MowerAPIError(
                f"{ERROR_MESSAGES['API_REQUEST_FAILED']}: {response.get('desc')}"
            )
        return response.get("data", {})

    async def async_get_devices(self) -> list[Device]:
        """Fetch the device list asynchronously.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
        """
        response = await self._async_request("GET", "/openapi/smarthome/authList")
        payload = self._unwrap(response).get("payload", {})
        devices_data = payload.get("devices", [])
        return [Device.from_dict(device_data) for device_data in devices_data]

    async def async_get_mqtt_user_info(self) -> dict[str, Any]:
        """Fetch the MQTT connection information asynchronously.

        Returns:
            MQTT connection information

        Raises:
            MowerAPIError: If the request fails
        """
        response = await self._async_request("GET", "/openapi/mqtt/userInfo/get/v2")
        return self._unwrap(response)

    def get_devices(self) -> list[Device]:
        """Fetch the device list synchronously.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
        """
        return asyncio.run(self.async_get_devices())

    def get_mqtt_user_info(self) -> dict[str, Any]:
        """Fetch the MQTT connection information synchronously."""
        return asyncio.run(self.async_get_mqtt_user_info())

    async def async_get_device_statuses(
        self, device_ids: list[str]
    ) -> dict[str, DeviceStatus]:
        """Fetch the status of several devices asynchronously.

        Args:
            device_ids: List of device IDs

        Returns:
            Mapping from device ID to status

        Raises:
            MowerAPIError: If the request fails
        """
        if not device_ids:
            return {}
        response = await self._async_request(
            "POST",
            "/openapi/smarthome/getVehicleStatus",
            data={"devices": [{"id": device_id} for device_id in device_ids]},
        )
        payload = self._unwrap(response).get("payload", {})
        devices_data = payload.get("devices", [])
        result: dict[str, DeviceStatus] = {}
        for status_data in devices_data:
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

        Args:
            device_id: Device ID

        Returns:
            Device status

        Raises:
            MowerAPIError: If the request fails or the device is not found
        """
        return asyncio.run(self.async_get_device_status(device_id))

    async def async_send_command(
        self, device_id: str, command: MowerCommand
    ) -> dict[str, Any]:
        """Send a control command asynchronously.

        Args:
            device_id: Device ID
            command: Control command

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the request fails or the command fails
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
            data={
                "commands": [
                    {"devices": [{"id": device_id}], "execution": execution}
                ]
            },
        )
        data = self._unwrap(response)
        payload = data.get("payload", {})
        command_results = payload.get("commands", [])
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
        return data

    def send_command(
        self, device_id: str, command: MowerCommand
    ) -> dict[str, Any]:
        """Send a control command synchronously.

        Args:
            device_id: Device ID
            command: Control command

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the request fails or the command fails
        """
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
        """Query command execution results synchronously."""
        return asyncio.run(self.async_query_command_results(devices))

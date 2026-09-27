"""Main client module.

Provides a single client that aggregates every feature.
"""

import asyncio
from typing import Any, Callable, TYPE_CHECKING

from mower_sdk.api import MowerAPI
from mower_sdk.models import Device, DeviceStatus, MowerCommand
from mower_sdk.legacy.mqtt_v1 import MowerMQTT

if TYPE_CHECKING:
    import aiohttp


class MowerClient:
    """Main client for the mower platform.

    Aggregates the REST API and MQTT features behind one interface.

    Attributes:
        api: REST API client
        mqtt: MQTT client
    """

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        token: str,
        api_base_url: str = "",
        mqtt_broker: str = "",
        mqtt_port: int = 1883,
        mqtt_username: str | None = None,
        mqtt_password: str | None = None,
    ):
        """Initialize the main client.

        Args:
            session: aiohttp session
            token: Access token
            api_base_url: REST API base URL
            mqtt_broker: MQTT broker address
            mqtt_port: MQTT broker port
            mqtt_username: MQTT username (optional)
            mqtt_password: MQTT password (optional)
        """
        self.api = MowerAPI(session=session, token=token, base_url=api_base_url)
        self.mqtt = MowerMQTT(
            broker=mqtt_broker,
            port=mqtt_port,
            username=mqtt_username,
            password=mqtt_password,
        )
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.mqtt_ws_path: str | None = None
        self._token = token

    def update_token(self, token: str) -> None:
        """Update the access token."""
        self._token = token
        self.api.set_token(token)

    async def async_refresh_mqtt_info(self) -> dict[str, Any]:
        """Refresh the MQTT connection information asynchronously."""
        info = await self.api.async_get_mqtt_user_info()
        mqtt_host = info.get("mqttHost", "")
        mqtt_url = info.get("mqttUrl", "")
        username = info.get("userName")
        password = info.get("pwdInfo")
        self.mqtt_broker = mqtt_host
        self.mqtt_port = 443
        self.mqtt_username = username
        self.mqtt_password = password
        self.mqtt_ws_path = mqtt_url
        auth_headers = {"Authorization": f"Bearer {self._token}"}
        self.mqtt.configure_wss(
            mqtt_host=mqtt_host,
            mqtt_url=mqtt_url,
            username=username,
            password=password,
            auth_headers=auth_headers,
            port=self.mqtt_port,
        )
        return info

    def refresh_mqtt_info(self) -> dict[str, Any]:
        """Refresh the MQTT connection information synchronously."""
        return asyncio.run(self.async_refresh_mqtt_info())

    async def async_discover_devices(self) -> list[Device]:
        """Discover devices asynchronously.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
        """
        return await self.api.async_get_devices()

    def discover_devices(self) -> list[Device]:
        """Discover devices synchronously.

        Returns:
            List of devices

        Raises:
            MowerAPIError: If the request fails
        """
        return self.api.get_devices()

    async def async_subscribe_device_updates(
        self,
        device_id: str,
        callback: Callable[[DeviceStatus], None] | None = None,
    ) -> None:
        """Subscribe to a device's status updates asynchronously.

        Args:
            device_id: Device ID
            callback: Status update callback

        Raises:
            MowerMQTTError: If the subscription fails
        """
        await self.async_refresh_mqtt_info()
        await self.mqtt.async_connect()
        await self.mqtt.async_subscribe_device(
            device_id=device_id,
            on_status_update=callback,
        )

    def subscribe_device_updates(
        self,
        device_id: str,
        callback: Callable[[DeviceStatus], None] | None = None,
    ) -> None:
        """Subscribe to a device's status updates synchronously.

        Args:
            device_id: Device ID
            callback: Status update callback

        Raises:
            MowerMQTTError: If the subscription fails
        """
        self.refresh_mqtt_info()
        self.mqtt.connect()
        self.mqtt.subscribe_device(
            device_id=device_id,
            on_status_update=callback,
        )

    async def async_start_mowing(self, device_id: str) -> dict[str, Any]:
        """Start mowing asynchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return await self.api.async_send_command(device_id, MowerCommand.START)

    def start_mowing(self, device_id: str) -> dict[str, Any]:
        """Start mowing synchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return self.api.send_command(device_id, MowerCommand.START)

    async def async_pause_mowing(self, device_id: str) -> dict[str, Any]:
        """Pause mowing asynchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return await self.api.async_send_command(device_id, MowerCommand.PAUSE)

    def pause_mowing(self, device_id: str) -> dict[str, Any]:
        """Pause mowing synchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return self.api.send_command(device_id, MowerCommand.PAUSE)

    async def async_dock(self, device_id: str) -> dict[str, Any]:
        """Return to the charging station asynchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return await self.api.async_send_command(device_id, MowerCommand.DOCK)

    def dock(self, device_id: str) -> dict[str, Any]:
        """Return to the charging station synchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return self.api.send_command(device_id, MowerCommand.DOCK)

    async def async_resume(self, device_id: str) -> dict[str, Any]:
        """Resume mowing asynchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return await self.api.async_send_command(device_id, MowerCommand.RESUME)

    def resume(self, device_id: str) -> dict[str, Any]:
        """Resume mowing synchronously.

        Args:
            device_id: Device ID

        Returns:
            Command execution result

        Raises:
            MowerAPIError: If the command fails
        """
        return self.api.send_command(device_id, MowerCommand.RESUME)

    def get_cached_status(self, device_id: str) -> DeviceStatus | None:
        """Return the cached device status.

        Args:
            device_id: Device ID

        Returns:
            Device status, or None if none is cached
        """
        return self.mqtt.get_cached_status(device_id)

    async def async_get_device_status(self, device_id: str) -> DeviceStatus:
        """Fetch a device's status asynchronously (via the API).

        Args:
            device_id: Device ID

        Returns:
            Device status

        Raises:
            MowerAPIError: If the request fails
        """
        return await self.api.async_get_device_status(device_id)

    async def async_get_device_statuses(
        self, device_ids: list[str]
    ) -> dict[str, DeviceStatus]:
        """Fetch the status of several devices asynchronously (via the API).

        Args:
            device_ids: List of device IDs

        Returns:
            Mapping from device ID to status

        Raises:
            MowerAPIError: If the request fails
        """
        return await self.api.async_get_device_statuses(device_ids)

    def get_device_status(self, device_id: str) -> DeviceStatus:
        """Fetch a device's status synchronously (via the API).

        Args:
            device_id: Device ID

        Returns:
            Device status

        Raises:
            MowerAPIError: If the request fails
        """
        return self.api.get_device_status(device_id)

    def get_device_statuses(self, device_ids: list[str]) -> dict[str, DeviceStatus]:
        """Fetch the status of several devices synchronously (via the API).

        Args:
            device_ids: List of device IDs

        Returns:
            Mapping from device ID to status

        Raises:
            MowerAPIError: If the request fails
        """
        return asyncio.run(self.api.async_get_device_statuses(device_ids))

    def get_token(self) -> str:
        """Return the current access token."""
        return self._token

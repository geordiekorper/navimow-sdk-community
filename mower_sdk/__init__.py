"""Python SDK for the Navimow mower cloud platform.

Provides access to the cloud mower platform over its REST API and MQTT feed.
"""

from mower_sdk.api import MowerAPI
from mower_sdk.client import MowerClient
from mower_sdk.cloud import NavimowCloud
from mower_sdk.device import NavimowCloudDevice
from mower_sdk.event import DataEvent
from mower_sdk.errors import (
    MowerAPIError,
    MowerAuthError,
    MowerMQTTError,
    ERROR_MESSAGES,
    COMMAND_ERRORS,
)
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceCommandMessage,
    DeviceEventMessage,
    DeviceStateMessage,
    DeviceStatus,
    MowerCommand,
    MowerError,
    MowerStatus,
    ThingEventMessage,
    ThingPropertiesMessage,
    ThingStatusMessage,
)
from mower_sdk.mqtt import MowerMQTT, NavimowMQTT
from mower_sdk.navimow import Navimow
from mower_sdk.sdk import NavimowSDK
from mower_sdk.state_manager import StateManager

__version__ = "0.2.0a1"

__all__ = [
    # Main clients
    "MowerClient",
    "Navimow",
    "NavimowSDK",
    # Submodules
    "MowerAPI",
    "MowerMQTT",
    "NavimowMQTT",
    "NavimowCloud",
    "NavimowCloudDevice",
    "StateManager",
    "DataEvent",
    # Data models
    "Device",
    "DeviceStateMessage",
    "DeviceEventMessage",
    "DeviceAttributesMessage",
    "DeviceCommandMessage",
    "DeviceStatus",
    "MowerStatus",
    "MowerCommand",
    "MowerError",
    "ThingStatusMessage",
    "ThingPropertiesMessage",
    "ThingEventMessage",
    # Exceptions
    "MowerAPIError",
    "MowerMQTTError",
    "ERROR_MESSAGES",
    "COMMAND_ERRORS",
]

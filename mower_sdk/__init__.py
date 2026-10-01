"""Python SDK for the Navimow mower cloud platform.

Provides access to the cloud mower platform over its REST API and MQTT feed.

The live path is imported eagerly: MowerAPI, NavimowMQTT, NavimowSDK, the
models and the errors. The names that moved to mower_sdk.legacy are served by
__getattr__ on first access, with one DeprecationWarning per legacy module per
process, so importing this package loads no legacy code.
"""

import importlib
import importlib.metadata
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mower_sdk._deprecation import warn_legacy
from mower_sdk.api import MowerAPI
from mower_sdk.errors import (
    MowerAPIError,
    MowerAuthRequiredError,
    MowerMQTTError,
    MowerRateLimitedError,
    MowerTransportError,
    MowerUnsupportedOperationError,
    ERROR_MESSAGES,
)
from mower_sdk.models import (
    CommandReceipt,
    CommandVerdict,
    Device,
    DeviceAttributesMessage,
    DeviceCommandMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceLocationMessage,
    DeviceStateMessage,
    DeviceStatus,
    MowerCommand,
    MowerError,
    MowerStatus,
    MqttConnectionInfo,
    RAW_STATE_TO_CANONICAL,
    REST_STATUS_KNOWN_FIELDS,
    RejectedMessage,
    STATE_KNOWN_FIELDS,
    SkippedLocationEntry,
    VEHICLE_STATE_TO_STATUS,
    battery_from_payload,
    canonical_state,
    mower_status_from_raw,
    mower_time_ms,
)
from mower_sdk.mqtt import ConnectionEvent, NavimowMQTT, ReceivedPayload, parse_topic
from mower_sdk.sdk import NavimowSDK
from mower_sdk.watchdog import MqttWatchdog, RebuildRequest, WatchInput

if TYPE_CHECKING:
    from mower_sdk.legacy.client import MowerClient as MowerClient
    from mower_sdk.legacy.cloud import NavimowCloud as NavimowCloud
    from mower_sdk.legacy.device import NavimowCloudDevice as NavimowCloudDevice
    from mower_sdk.legacy.errors import COMMAND_ERRORS as COMMAND_ERRORS
    from mower_sdk.legacy.errors import MowerAuthError as MowerAuthError
    from mower_sdk.legacy.event import DataEvent as DataEvent
    from mower_sdk.legacy.mqtt_v1 import MowerMQTT as MowerMQTT
    from mower_sdk.legacy.navimow import Navimow as Navimow
    from mower_sdk.legacy.state_manager import StateManager as StateManager
    from mower_sdk.legacy.thing_models import (
        ThingEventMessage as ThingEventMessage,
        ThingPropertiesMessage as ThingPropertiesMessage,
        ThingStatusMessage as ThingStatusMessage,
    )

__version__ = "0.2.0a3"


def _warn_if_upstream_installed(version_of: Callable[[str], str] = importlib.metadata.version) -> None:
    """Warn once when the upstream navimow-sdk distribution is listed beside this one.

    Both distributions install the mower_sdk package, so whichever was
    installed last owns the files. This check runs only when this package's
    own __init__ is the one loaded; when the upstream distribution was
    installed last, its files replaced this one and nothing here runs.
    ``version_of`` is importlib.metadata.version, a parameter for the tests.
    """
    try:
        upstream = version_of("navimow-sdk")
        community = version_of("navimow-sdk-community")
    except importlib.metadata.PackageNotFoundError:
        return
    warnings.warn(
        f"navimow-sdk {upstream} is installed beside navimow-sdk-community {community}. "
        "Both provide the mower_sdk package; the files loaded are navimow-sdk-community's "
        f"(this import is version {__version__}), so the other distribution's files are not "
        "the ones running. Uninstall both, then install navimow-sdk-community.",
        UserWarning,
        stacklevel=2,
    )


_warn_if_upstream_installed()

__all__ = [
    # Main clients
    "MowerClient",
    "Navimow",
    "NavimowSDK",
    "MqttWatchdog",
    "RebuildRequest",
    "WatchInput",
    # Submodules
    "MowerAPI",
    "MowerMQTT",
    "ConnectionEvent",
    "NavimowMQTT",
    "ReceivedPayload",
    "parse_topic",
    "NavimowCloud",
    "NavimowCloudDevice",
    "StateManager",
    "DataEvent",
    # Data models
    "CommandReceipt",
    "CommandVerdict",
    "Device",
    "DeviceStateMessage",
    "DeviceEventMessage",
    "DeviceAttributesMessage",
    "DeviceCommandMessage",
    "DeviceLocation",
    "DeviceLocationMessage",
    "DeviceStatus",
    "MowerStatus",
    "MowerCommand",
    "MowerError",
    "MqttConnectionInfo",
    "RAW_STATE_TO_CANONICAL",
    "REST_STATUS_KNOWN_FIELDS",
    "RejectedMessage",
    "STATE_KNOWN_FIELDS",
    "SkippedLocationEntry",
    "VEHICLE_STATE_TO_STATUS",
    "battery_from_payload",
    "canonical_state",
    "mower_status_from_raw",
    "mower_time_ms",
    "ThingStatusMessage",
    "ThingPropertiesMessage",
    "ThingEventMessage",
    # Exceptions
    "MowerAPIError",
    "MowerAuthRequiredError",
    "MowerMQTTError",
    "MowerRateLimitedError",
    "MowerTransportError",
    "MowerUnsupportedOperationError",
    "ERROR_MESSAGES",
    "COMMAND_ERRORS",
]

# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
# MowerAuthError is served like the others but, as upstream had it, stays out of __all__.
_LEGACY_NAMES = {
    "MowerClient": ("client", "MowerClient"),
    "Navimow": ("navimow", "Navimow"),
    "MowerMQTT": ("mqtt_v1", "MowerMQTT"),
    "NavimowCloud": ("cloud", "NavimowCloud"),
    "NavimowCloudDevice": ("device", "NavimowCloudDevice"),
    "StateManager": ("state_manager", "StateManager"),
    "DataEvent": ("event", "DataEvent"),
    "ThingStatusMessage": ("thing_models", "ThingStatusMessage"),
    "ThingPropertiesMessage": ("thing_models", "ThingPropertiesMessage"),
    "ThingEventMessage": ("thing_models", "ThingEventMessage"),
    "COMMAND_ERRORS": ("errors", "COMMAND_ERRORS"),
    "MowerAuthError": ("errors", "MowerAuthError"),
}


def __getattr__(name: str) -> Any:
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module."""
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LEGACY_NAMES})

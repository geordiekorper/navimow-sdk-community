"""Custom exception classes.

Defines every custom exception type the SDK raises.
"""

import importlib
from typing import TYPE_CHECKING, Any

from mower_sdk._deprecation import warn_legacy

if TYPE_CHECKING:
    from mower_sdk.legacy.errors import COMMAND_ERRORS as COMMAND_ERRORS
    from mower_sdk.legacy.errors import MowerAuthError as MowerAuthError

# The public surface upstream published from this module, plus the community
# addition MowerUnsupportedOperationError. MowerAuthError and COMMAND_ERRORS now
# live in mower_sdk.legacy.errors and are served by __getattr__.
__all__ = [
    "COMMAND_ERRORS",
    "ERROR_MESSAGES",
    "MowerAPIError",
    "MowerAuthError",
    "MowerMQTTError",
    "MowerUnsupportedOperationError",
]


class MowerAPIError(Exception):
    """Raised when an API request fails.

    Attributes:
        status_code: HTTP status code (if available)
        message: Error message
        error_code: Business error code (if available)
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        """Initialize the API exception.

        Args:
            message: Error message
            status_code: HTTP status code
            error_code: Business error code
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code

    def __str__(self) -> str:
        """Return the formatted error message."""
        parts = [self.message]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.error_code:
            parts.append(f"Error Code: {self.error_code}")
        return " | ".join(parts)


class MowerMQTTError(Exception):
    """Raised when an MQTT operation fails.

    Attributes:
        message: Error message
    """

    def __init__(self, message: str):
        """Initialize the MQTT exception.

        Args:
            message: Error message
        """
        super().__init__(message)
        self.message = message


class MowerUnsupportedOperationError(Exception):
    """Raised when an operation the SDK cannot vouch for is requested without opting in.

    NavimowSDK's MQTT command methods raise it unless the facade was constructed
    with allow_experimental_mqtt_commands=True. The message names the supported
    alternative, when one exists.
    """


# Error message dictionary
ERROR_MESSAGES = {
    "AUTH_FAILED": "认证失败，请检查 client_id 和 client_secret",
    "TOKEN_EXPIRED": "Token 已过期，请重新登录",
    "TOKEN_REFRESH_FAILED": "Token 刷新失败",
    "DEVICE_NOT_FOUND": "设备未找到",
    "DEVICE_OFFLINE": "设备离线",
    "COMMAND_FAILED": "指令执行失败",
    "API_REQUEST_FAILED": "API 请求失败",
    "MQTT_CONNECTION_FAILED": "MQTT 连接失败",
    "MQTT_SUBSCRIBE_FAILED": "MQTT 订阅失败",
    "INVALID_COMMAND": "无效的指令",
    "INVALID_DEVICE_STATUS": "无效的设备状态",
}


# Names that moved to mower_sdk.legacy: attribute here -> (legacy module, attribute there).
_LEGACY_NAMES = {
    "MowerAuthError": ("errors", "MowerAuthError"),
    "COMMAND_ERRORS": ("errors", "COMMAND_ERRORS"),
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

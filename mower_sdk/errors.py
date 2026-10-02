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
# additions MowerUnsupportedOperationError, MowerTransportError,
# MowerAuthRequiredError and MowerRateLimitedError. MowerAuthError and COMMAND_ERRORS now
# live in mower_sdk.legacy.errors and are served by __getattr__.
__all__ = [
    "COMMAND_ERRORS",
    "ERROR_MESSAGES",
    "MowerAPIError",
    "MowerAuthError",
    "MowerAuthRequiredError",
    "MowerMQTTError",
    "MowerRateLimitedError",
    "MowerTransportError",
    "MowerUnsupportedOperationError",
]


class MowerAPIError(Exception):
    """Raised when an API request fails.

    The subclasses say what kind of failure it was; ``except MowerAPIError``
    still catches every one of them.

    Attributes:
        status_code: HTTP status code (if available)
        message: Error message
        error_code: Business error code (if available)
        envelope_code: The reply envelope's code when the cloud refused the
            request in the envelope (if available); not part of str()
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        envelope_code: int | None = None,
    ):
        """Initialize the API exception.

        Args:
            message: Error message
            status_code: HTTP status code
            error_code: Business error code
            envelope_code: The reply envelope's code
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.envelope_code = envelope_code

    def __str__(self) -> str:
        """Return the formatted error message."""
        parts = [self.message]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.error_code:
            parts.append(f"Error Code: {self.error_code}")
        return " | ".join(parts)


class MowerTransportError(MowerAPIError):
    """No usable reply.

    A timeout, a connection error, an HTTP 5xx, a status the client cannot use
    (below 200, or a redirect aiohttp did not follow), or a 2xx whose body is
    not a JSON object.

    For a command, the outcome is unknown rather than refused: the cloud may have
    acted on it. The aiohttp error, TimeoutError, UnicodeDecodeError or
    JSONDecodeError that caused it, if any, is its __cause__.
    """


class MowerAuthRequiredError(MowerAPIError):
    """The credentials were refused.

    HTTP 401 or 403, envelope code 4005, or a reply whose desc names
    CODE_OAUTH_INFO_ILLEGAL; or MowerAPI has no token to send, in which case
    no request is made. Refresh or re-authorise, then retry.
    """


class MowerRateLimitedError(MowerAPIError):
    """The cloud asked the caller to slow down.

    Envelope code 4001 (its circuit breaker, retry after about a minute) or a
    desc saying "too frequent" or "circuit breaker".
    """


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

    Attributes:
        message: Error message
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# Error message dictionary
ERROR_MESSAGES = {
    "AUTH_FAILED": "Authentication failed; check client_id and client_secret",
    "TOKEN_EXPIRED": "Token expired; sign in again",
    "TOKEN_REFRESH_FAILED": "Token refresh failed",
    "DEVICE_NOT_FOUND": "Device not found",
    "DEVICE_OFFLINE": "Device offline",
    "COMMAND_FAILED": "Command failed",
    "API_REQUEST_FAILED": "API request failed",
    "MQTT_CONNECTION_FAILED": "MQTT connection failed",
    "MQTT_SUBSCRIBE_FAILED": "MQTT subscribe failed",
    "INVALID_COMMAND": "Invalid command",
    "INVALID_DEVICE_STATUS": "Invalid device status",
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

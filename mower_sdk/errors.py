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
        message: The error message, without the status code and the error
            code that str() adds.
        status_code: The HTTP status code of the reply. MowerAPI also sets it
            where no reply carried one: 401 when there is no token to send
            and no request is made, and 404 for a device the status reply did
            not list. None when the failure has none.
        error_code: The business error code, a short name for the failure
            such as a key of ERROR_MESSAGES or the errorCode of a refused
            command, or None when there is none.
        envelope_code: The reply envelope's code when the cloud refused the
            request in the envelope, else None; not part of str().
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
            message: The error message.
            status_code: The HTTP status code, or the status the failure
                stands for where no reply carried one, or None without one.
            error_code: The business error code, or None without one.
            envelope_code: The reply envelope's code, or None without one.
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.envelope_code = envelope_code

    def __str__(self) -> str:
        """Return the formatted error message.

        Returns:
            The message, then "HTTP <status_code>" and "Error Code:
            <error_code>" for each of the two that is set (not None, 0 or
            empty), joined by " | ". The envelope code is left out.
        """
        parts = [self.message]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.error_code:
            parts.append(f"Error Code: {self.error_code}")
        return " | ".join(parts)


class MowerTransportError(MowerAPIError):
    """Raised when a request brings no usable reply.

    A timeout, a connection error, an HTTP 5xx, a status the client cannot use
    (below 200, or a redirect aiohttp did not follow), or a 2xx whose body is
    not a JSON object.

    For a command, the outcome is unknown rather than refused: the cloud may have
    acted on it. The aiohttp error, TimeoutError, UnicodeDecodeError or
    JSONDecodeError that caused it, if any, is its __cause__.

    Attributes:
        message: The error message.
        status_code: The HTTP status code of the reply; None for a timeout or
            a connection error, which bring no reply.
        error_code: None as MowerAPI raises it.
        envelope_code: None as MowerAPI raises it.
    """


class MowerAuthRequiredError(MowerAPIError):
    """Raised when the credentials were refused, or there are none to send.

    HTTP 401 or 403, envelope code 4005, or a reply whose desc names
    CODE_OAUTH_INFO_ILLEGAL; or MowerAPI has no token to send, in which case
    no request is made. Refresh or re-authorise, then retry.

    Attributes:
        message: The error message.
        status_code: 401 or 403 for an HTTP refusal, 401 when there was no
            token to send, None for a refusal in the envelope.
        error_code: "TOKEN_EXPIRED" when there was no token to send, else
            None.
        envelope_code: The envelope's code for a refusal in the envelope (None
            when it cannot be read as an integer), else None.
    """


class MowerRateLimitedError(MowerAPIError):
    """Raised when the cloud asks the caller to slow down.

    Envelope code 4001 (its circuit breaker, retry after about a minute) or a
    desc saying "too frequent" or "circuit breaker".

    Attributes:
        message: The error message.
        status_code: None as MowerAPI raises it: the refusal is in the
            envelope of a reply.
        error_code: None as MowerAPI raises it.
        envelope_code: The envelope's code, None when it cannot be read as an
            integer.
    """


class MowerMQTTError(Exception):
    """Raised when an MQTT operation fails.

    In this package only the legacy MowerMQTT client raises it, when
    connecting or subscribing fails.

    Attributes:
        message: The error message.
    """

    def __init__(self, message: str):
        """Initialize the MQTT exception.

        Args:
            message: The error message.
        """
        super().__init__(message)
        self.message = message


class MowerUnsupportedOperationError(Exception):
    """Raised when an operation the SDK cannot vouch for is requested without opting in.

    NavimowSDK's MQTT command methods raise it unless the facade was constructed
    with allow_experimental_mqtt_commands=True. The message names the supported
    alternative, when one exists.

    Attributes:
        message: The error message.
    """

    def __init__(self, message: str):
        """Initialize the unsupported-operation exception.

        Args:
            message: The error message.
        """
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
    """Serve the names that moved to mower_sdk.legacy, warning once per legacy module.

    Python calls it for a name the module does not have. It serves
    MowerAuthError and COMMAND_ERRORS on their first access: the
    DeprecationWarning is issued through warn_legacy before the legacy module
    is imported, and the value is then stored in this module's globals, so a
    later access to the same name does not come here.

    Args:
        name: The attribute asked for.

    Returns:
        The object of that name in its module under mower_sdk.legacy.

    Raises:
        AttributeError: name is not one of the names in _LEGACY_NAMES.
        DeprecationWarning: A warnings filter turns the warning into an error
            (see warn_legacy); nothing is imported or stored then.
    """
    try:
        legacy_module, attribute = _LEGACY_NAMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warn_legacy(legacy_module, f"{__name__}.{name}")
    value = getattr(importlib.import_module(f"mower_sdk.legacy.{legacy_module}"), attribute)
    globals()[name] = value
    return value

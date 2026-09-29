"""Characterisation tests for mower_sdk.errors: the runtime strings and the exception attributes.

ERROR_MESSAGES is what every MowerAPIError message starts with, so its values
are pinned literally; the other tests read them through the mapping.
"""

from __future__ import annotations

from mower_sdk.errors import ERROR_MESSAGES, MowerUnsupportedOperationError


def test_the_runtime_strings_are_english_under_the_same_keys() -> None:
    assert ERROR_MESSAGES == {
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


def test_unsupported_operation_error_has_a_message_attribute() -> None:
    error = MowerUnsupportedOperationError("not sent")
    assert str(error) == "not sent"
    assert error.message == "not sent"

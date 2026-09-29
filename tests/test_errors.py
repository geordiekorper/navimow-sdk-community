"""Characterisation tests for mower_sdk.errors: the runtime strings and the exception attributes.

ERROR_MESSAGES is what every MowerAPIError message starts with, so its values
are pinned literally; the other tests read them through the mapping.
"""

from __future__ import annotations

from mower_sdk.errors import ERROR_MESSAGES, MowerUnsupportedOperationError


def test_the_runtime_strings_are_the_ones_upstream_shipped() -> None:
    assert ERROR_MESSAGES == {
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


def test_unsupported_operation_error_has_no_message_attribute() -> None:
    error = MowerUnsupportedOperationError("not sent")
    assert str(error) == "not sent"
    assert not hasattr(error, "message")

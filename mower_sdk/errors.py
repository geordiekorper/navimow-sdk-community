"""Custom exception classes.

Defines every custom exception type the SDK raises.
"""


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


class MowerAuthError(Exception):
    """Raised when authentication fails.

    Attributes:
        message: Error message
    """

    def __init__(self, message: str):
        """Initialize the authentication exception.

        Args:
            message: Error message
        """
        super().__init__(message)
        self.message = message


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

# Command error mapping
COMMAND_ERRORS = {
    "START": {
        "DEVICE_OFFLINE": "设备离线，无法启动",
        "ALREADY_MOWING": "设备正在割草中",
        "BATTERY_LOW": "电池电量过低，无法启动",
    },
    "PAUSE": {
        "NOT_MOWING": "设备未在割草中，无法暂停",
        "DEVICE_OFFLINE": "设备离线，无法暂停",
    },
    "DOCK": {
        "ALREADY_DOCKED": "设备已在充电站",
        "DEVICE_OFFLINE": "设备离线，无法返回充电站",
    },
    "RESUME": {
        "NOT_PAUSED": "设备未暂停，无法恢复",
        "DEVICE_OFFLINE": "设备离线，无法恢复",
    },
}

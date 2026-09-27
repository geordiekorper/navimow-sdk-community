"""Exception and mapping upstream published but never raised or read.

MowerAuthError and COMMAND_ERRORS were extracted verbatim from
mower_sdk/errors.py (lines 44 to 58 and 94 to 112 at the fork point 6596aa0).
Nothing in the SDK raises the exception or reads the mapping. The definitions
are unchanged; only this docstring is new.
"""


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

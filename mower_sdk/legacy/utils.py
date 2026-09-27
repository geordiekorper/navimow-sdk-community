"""Utility functions.

Helpers used across the SDK.
"""

import json
import logging
from datetime import datetime
from typing import Any


def setup_logger(name: str = "mower_sdk", level: int = logging.INFO) -> logging.Logger:
    """Configure and return a logger.

    Args:
        name: Logger name
        level: Log level

    Returns:
        The configured logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def parse_json(data: str | bytes) -> dict[str, Any] | list[Any]:
    """Parse a JSON string.

    Args:
        data: JSON as a string or bytes

    Returns:
        The parsed dictionary or list

    Raises:
        ValueError: If the JSON cannot be parsed
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def timestamp_to_datetime(timestamp: int) -> datetime:
    """Convert a Unix timestamp to a datetime.

    Args:
        timestamp: Unix timestamp in seconds

    Returns:
        A datetime object
    """
    return datetime.fromtimestamp(timestamp)


def datetime_to_timestamp(dt: datetime) -> int:
    """Convert a datetime to a Unix timestamp.

    Args:
        dt: A datetime object

    Returns:
        Unix timestamp in seconds
    """
    return int(dt.timestamp())

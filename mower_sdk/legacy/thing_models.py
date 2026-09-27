"""Alibaba IoT "Thing" message envelopes, kept for compatibility.

ThingParams, ThingStatusMessage, ThingPropertiesMessage and ThingEventMessage
were extracted verbatim from mower_sdk/models.py (lines 192 to 271 at the fork
point 6596aa0). Nothing constructs them: the live cloud does not send this
envelope. The class bodies are unchanged; only this import block is new.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ThingParams:
    """Common params wrapper for Thing messages."""

    iot_id: str | None = None
    product_key: str | None = None
    device_name: str | None = None
    identifier: str | None = None
    value: Any | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThingParams":
        return cls(
            iot_id=data.get("iotId") or data.get("iot_id"),
            product_key=data.get("productKey") or data.get("product_key"),
            device_name=data.get("deviceName") or data.get("device_name"),
            identifier=data.get("identifier"),
            value=data.get("value"),
            raw=data,
        )


@dataclass
class ThingStatusMessage:
    """Thing status message."""

    method: str | None
    id: str | None
    params: ThingParams
    version: str | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ThingStatusMessage":
        return cls(
            method=payload.get("method"),
            id=payload.get("id"),
            params=ThingParams.from_dict(payload.get("params", {})),
            version=payload.get("version"),
        )


@dataclass
class ThingPropertiesMessage:
    """Thing properties message."""

    method: str | None
    id: str | None
    params: ThingParams
    version: str | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ThingPropertiesMessage":
        return cls(
            method=payload.get("method"),
            id=payload.get("id"),
            params=ThingParams.from_dict(payload.get("params", {})),
            version=payload.get("version"),
        )


@dataclass
class ThingEventMessage:
    """Thing event message."""

    method: str | None
    id: str | None
    params: ThingParams
    version: str | None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ThingEventMessage":
        return cls(
            method=payload.get("method"),
            id=payload.get("id"),
            params=ThingParams.from_dict(payload.get("params", {})),
            version=payload.get("version"),
        )

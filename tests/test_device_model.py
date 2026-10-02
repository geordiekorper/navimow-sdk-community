"""Tests for ``Device.from_dict``.

The snake_case keys the model defines are read first; an explicit empty or None
snake_case value is kept. Only when a snake_case key is absent is its camelCase
spelling read (``deviceModel``, ``firmwareVersion``, ``serialNumber``,
``macAddress``, ``isOnline``), and ``firmware_version`` also reads
``firmware``, the key the device-list reply of an X430 carries, before
``firmwareVersion``. ``product_key``, ``device_name`` and ``iot_id`` are filled
from their camelCase key, then their snake_case key, then (for the last two)
``name`` and ``id``.
"""

from __future__ import annotations

from typing import Any

import pytest

from mower_sdk.models import Device

SNAKE_CASE = {
    "id": "dev-1",
    "name": "Lawn",
    "model": "i105",
    "firmware_version": "1.2.3",
    "serial_number": "SN-1",
    "mac_address": "00:11:22:33:44:55",
    "online": True,
    "extra": {"k": 1},
}
CAMEL_CASE = {
    "id": "dev-1",
    "name": "Lawn",
    "deviceModel": "i105",
    "firmwareVersion": "1.2.3",
    "serialNumber": "SN-1",
    "macAddress": "00:11:22:33:44:55",
    "isOnline": True,
}
# The device-list entry of an X430, synthetic values; ``firmware`` is the key it carries.
X430_AUTH_LIST_ENTRY = {"id": "dev-1", "name": "Example mower", "model": "X430", "firmware": "00AA"}


def test_snake_case_keys_are_read() -> None:
    assert Device.from_dict(SNAKE_CASE) == Device(
        id="dev-1",
        name="Lawn",
        model="i105",
        firmware_version="1.2.3",
        serial_number="SN-1",
        mac_address="00:11:22:33:44:55",
        online=True,
        extra={"k": 1},
        device_name="Lawn",
        iot_id="dev-1",
    )


def test_missing_keys_take_the_defaults() -> None:
    assert Device.from_dict({}) == Device(
        id="", name="", model="", firmware_version="", serial_number=""
    )


def test_explicit_empty_snake_case_values_are_kept() -> None:
    payload = {
        **CAMEL_CASE,
        "model": "",
        "firmware_version": "",
        "serial_number": "",
        "mac_address": None,
        "online": False,
    }
    device = Device.from_dict(payload)
    assert (device.model, device.firmware_version, device.serial_number) == ("", "", "")
    assert device.mac_address is None
    assert device.online is False


def test_camel_case_keys_are_read_when_the_snake_case_key_is_absent() -> None:
    assert Device.from_dict(CAMEL_CASE) == Device(
        id="dev-1",
        name="Lawn",
        model="i105",
        firmware_version="1.2.3",
        serial_number="SN-1",
        mac_address="00:11:22:33:44:55",
        online=True,
        device_name="Lawn",
        iot_id="dev-1",
    )


def test_explicit_none_snake_case_values_are_kept() -> None:
    payload = {
        **CAMEL_CASE,
        "model": None,
        "firmware_version": None,
        "serial_number": None,
        "mac_address": None,
        "online": None,
    }
    device = Device.from_dict(payload)
    assert (device.model, device.firmware_version, device.serial_number) == (None, None, None)
    assert device.mac_address is None
    assert device.online is None


def test_firmware_key_is_read_for_firmware_version() -> None:
    device = Device.from_dict(X430_AUTH_LIST_ENTRY)
    assert (device.id, device.name, device.model) == ("dev-1", "Example mower", "X430")
    assert device.firmware_version == "00AA"


@pytest.mark.parametrize(
    ("payload", "firmware_version"),
    [
        ({"firmware_version": "a", "firmware": "b", "firmwareVersion": "c"}, "a"),
        ({"firmware": "b", "firmwareVersion": "c"}, "b"),
        ({"firmwareVersion": "c"}, "c"),
        ({"firmware_version": "", "firmware": "b", "firmwareVersion": "c"}, ""),
        ({"firmware_version": None, "firmware": "b"}, None),
        ({"firmware": "", "firmwareVersion": "c"}, ""),
        ({}, ""),
    ],
    ids=[
        "snake_case_wins",
        "firmware_before_camel_case",
        "camel_case_last",
        "explicit_empty_snake_case_wins",
        "explicit_none_snake_case_wins",
        "explicit_empty_firmware_wins",
        "missing",
    ],
)
def test_firmware_version_precedence(payload: dict[str, Any], firmware_version: str | None) -> None:
    assert Device.from_dict(payload).firmware_version == firmware_version


@pytest.mark.parametrize(
    ("payload", "product_key", "device_name", "iot_id"),
    [
        ({"productKey": "pk", "deviceName": "dn", "iotId": "iid"}, "pk", "dn", "iid"),
        ({"product_key": "pk", "device_name": "dn", "iot_id": "iid"}, "pk", "dn", "iid"),
        (
            {
                "productKey": "a",
                "product_key": "b",
                "deviceName": "c",
                "device_name": "d",
                "iotId": "e",
                "iot_id": "f",
            },
            "a",
            "c",
            "e",
        ),
        (
            {
                "productKey": "",
                "product_key": "b",
                "deviceName": "",
                "device_name": "d",
                "iotId": "",
                "iot_id": "f",
            },
            "b",
            "d",
            "f",
        ),
        ({"id": "dev-1", "name": "Lawn"}, None, "Lawn", "dev-1"),
        # The or-chain returns the last key's value when none is truthy, so an
        # empty name or id gives "" rather than None.
        ({"id": "", "name": ""}, None, "", ""),
    ],
    ids=[
        "camel_case",
        "snake_case",
        "camel_case_wins",
        "empty_camel_case_falls_back",
        "name_and_id",
        "empty_name_and_id",
    ],
)
def test_aliyun_fallbacks(
    payload: dict[str, Any], product_key: str | None, device_name: str | None, iot_id: str | None
) -> None:
    device = Device.from_dict(payload)
    assert (device.product_key, device.device_name, device.iot_id) == (
        product_key,
        device_name,
        iot_id,
    )

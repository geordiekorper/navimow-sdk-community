"""The fixtures the test modules share. The fakes they install are in fakes.py."""

from __future__ import annotations

import pytest

from mower_sdk import mqtt as mqtt_module
from mower_sdk import navimow_client as client_module
from mower_sdk import sdk as sdk_module
from mower_sdk import watchdog as watchdog_module

from .fakes import FakeClient, FakeClock, FakeMQTT, FakeSleeper


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """FakeClient in place of paho's client, so nothing connects.

    The clients made are FakeClient.instances.
    """
    FakeClient.instances = []
    FakeClient.events = []
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakeClient)
    return FakeClient


@pytest.fixture
def fake_mqtt(monkeypatch: pytest.MonkeyPatch) -> type[FakeMQTT]:
    """FakeMQTT in place of NavimowMQTT under the facade.

    The clients made are FakeMQTT.instances.
    """
    FakeMQTT.instances = []
    monkeypatch.setattr(sdk_module, "NavimowMQTT", FakeMQTT)
    return FakeMQTT


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A FakeClock in place of ``time`` and ``datetime``.

    It is installed in the modules of the live path that read them.
    """
    fake = FakeClock()
    for module in (mqtt_module, sdk_module, client_module):
        monkeypatch.setattr(module, "time", fake)
        monkeypatch.setattr(module, "datetime", fake)
    monkeypatch.setattr(watchdog_module, "time", fake)
    return fake


@pytest.fixture
def sleeper(monkeypatch: pytest.MonkeyPatch) -> FakeSleeper:
    """A FakeSleeper in place of the function NavimowClient's clock waits through."""
    fake = FakeSleeper()
    monkeypatch.setattr(client_module, "_sleep", fake)
    return fake

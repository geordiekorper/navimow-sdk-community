"""The fixtures the test modules share. The fakes they install are in fakes.py."""

from __future__ import annotations

import pytest

from mower_sdk import mqtt as mqtt_module

from .fakes import FakeClient


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """FakeClient in place of paho's client, so nothing connects; the clients made are FakeClient.instances."""
    FakeClient.instances = []
    FakeClient.events = []
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakeClient)
    return FakeClient

"""The migration guide's "after" fragments, run as printed.

docs/migrating.md shows, for four legacy classes, the code before and the code
on the live path after. Each "after" fragment is taken out of the guide and run
as the body of a coroutine, given what the guide says a fragment may assume: a
facade built as the README's quick example builds it (here on the fake paho
client), the device list, the connection information, and the caller's own
handlers. Every other name has to come from ``mower_sdk``, and a
DeprecationWarning is an error throughout, so a fragment that reaches for
legacy code fails. After a fragment has run, messages and connection events are
delivered to see that what it registered is what receives them.

The "before" fragments are not run: they are the legacy code the guide leads
away from.
"""

from __future__ import annotations

import ast
import builtins
import json
import re
import textwrap
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import mower_sdk
from mower_sdk import (
    Device,
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceStateMessage,
    DeviceStatus,
    MqttConnectionInfo,
    NavimowSDK,
)

from .fakes import DEVICE_ID, SUCCESS, FakeClient, drain, topic

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "migrating.md"
if not GUIDE.exists():
    # The wheel check runs a copy of tests/ outside the checkout, without docs/.
    pytest.skip("docs/migrating.md is not next to the tests", allow_module_level=True)

pytestmark = pytest.mark.usefixtures("fake_paho")

OTHER_ID = "dev-2"
DEVICES = [
    Device(id=DEVICE_ID, name="Lawn", model="X430", firmware_version="1.0", serial_number="SN1"),
    Device(id=OTHER_ID, name="Orchard", model="X430", firmware_version="1.0", serial_number="SN2"),
]
INFO = MqttConnectionInfo(
    broker="broker.example.invalid",
    port=443,
    ws_path="/mqtt/12345",
    username="user",
    password="secret",
)


def fragments() -> dict[str, list[str]]:
    """The Python blocks of the guide's second part, by the heading they stand under."""
    part = GUIDE.read_text(encoding="utf-8").split(
        "## From the legacy classes to the live path", 1
    )[1]
    part = part.split("\n## ", 1)[0]
    found: dict[str, list[str]] = {}
    for section in part.split("\n### ")[1:]:
        heading, _, body = section.partition("\n")
        blocks = re.findall(r"```python\n(.*?)```", body, re.DOTALL)
        if blocks:
            found[heading.strip()] = blocks
    return found


def after(heading: str) -> str:
    blocks = fragments()[heading]
    assert len(blocks) == 2, f"{heading}: the guide should show one fragment before and one after"
    return blocks[1]


def unsupplied_names(fragment: str) -> set[str]:
    """The names a fragment reads without binding them itself: what its reader has to supply."""
    nodes = list(ast.walk(ast.parse(fragment)))
    read = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    bound = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {n.name for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    bound |= {n.arg for n in nodes if isinstance(n, ast.arg)}
    return read - bound - set(dir(builtins))


async def run_fragment(fragment: str, **given: Any) -> dict[str, Any]:
    """Run a fragment as the body of a coroutine and return the names it bound.

    ``given`` is what the guide lets a fragment assume. Any other name is
    imported from ``mower_sdk``, as the guide says the left-out imports do.
    """
    namespace: dict[str, Any] = dict(given)
    for name in unsupplied_names(fragment) - set(given):
        assert hasattr(mower_sdk, name), (
            f"the fragment reads {name}, which the guide does not supply"
        )
        namespace[name] = getattr(mower_sdk, name)
    source = (
        "async def fragment():\n" + textwrap.indent(fragment, "    ") + "\n    return locals()\n"
    )
    exec(compile(source, str(GUIDE), "exec"), namespace)  # noqa: S102
    return await namespace["fragment"]()


@pytest.fixture(autouse=True)
def deprecations_are_errors() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        yield


def facade() -> NavimowSDK:
    """The facade as the README's quick example builds it.

    That is inside a coroutine, so it binds the running loop.
    """
    return NavimowSDK.from_connection_info(INFO, access_token="token", records=DEVICES)


async def deliver(
    sdk: NavimowSDK, channel: str, payload: dict[str, Any], device_id: str = DEVICE_ID
) -> None:
    await sdk._on_mqtt_message(topic(channel, device_id), json.dumps(payload).encode(), device_id)


def test_the_guide_has_the_four_pairs_these_tests_run() -> None:
    assert {heading: len(blocks) for heading, blocks in fragments().items()} == {
        "MowerMQTT": 2,
        "Navimow and NavimowDeviceManager": 2,
        "NavimowCloud": 2,
        "NavimowCloudDevice and StateManager": 2,
    }


@pytest.mark.asyncio
async def test_the_mowermqtt_fragment_passes_one_mowers_state_as_a_device_status() -> None:
    sdk = facade()
    await deliver(sdk, "state", {"state": "isPaused"})  # cached before the fragment runs
    await deliver(sdk, "attributes", {"attributes": {"a": 1}})
    statuses: list[Any] = []
    bound = await run_fragment(
        after("MowerMQTT"), sdk=sdk, device_id=DEVICE_ID, handle_status=statuses.append
    )
    assert sdk.mqtt.client.named("connect_async")
    assert isinstance(bound["state"], DeviceStateMessage)  # the state cache, not the attributes
    assert (bound["state"].device_id, bound["state"].state) == (DEVICE_ID, "paused")

    await deliver(sdk, "state", {"state": "isRunning", "battery": 80})
    await deliver(sdk, "state", {"state": "isDocked"}, device_id=OTHER_ID)
    (status,) = statuses
    assert isinstance(status, DeviceStatus)
    assert (status.device_id, status.battery) == (DEVICE_ID, 80)


@pytest.mark.asyncio
async def test_the_navimow_fragment_builds_a_connecting_facade_and_finds_a_device_by_name() -> None:
    bound = await run_fragment(
        after("Navimow and NavimowDeviceManager"), info=INFO, devices=DEVICES
    )
    built = bound["sdk"]
    assert isinstance(built, NavimowSDK)
    assert built.mqtt.records == DEVICES
    assert built.mqtt.auth_headers == {"Authorization": "Bearer your_access_token"}
    assert FakeClient.instances == [built.mqtt.client]
    assert built.mqtt.client.named("connect_async")
    assert bound["mower"] is DEVICES[0]


@pytest.mark.asyncio
async def test_the_navimowcloud_fragment_registers_the_handlers_and_the_connection_hook() -> None:
    sdk = facade()
    states: list[Any] = []
    events: list[Any] = []
    attributes: list[Any] = []
    connection: list[str] = []

    async def handle_connected() -> None:
        connection.append("connected")

    async def handle_disconnected() -> None:
        connection.append("disconnected")

    await run_fragment(
        after("NavimowCloud"),
        sdk=sdk,
        handle_state=states.append,
        handle_event=events.append,
        handle_attributes=attributes.append,
        handle_connected=handle_connected,
        handle_disconnected=handle_disconnected,
    )
    await deliver(sdk, "state", {"state": "isRunning"})
    await deliver(sdk, "event", {"type": "system", "event": "started"})
    await deliver(sdk, "attributes", {"attributes": {"a": 1}})
    # Each handler received its own kind of message and no other.
    assert [type(message) for message in states] == [DeviceStateMessage]
    assert [type(message) for message in events] == [DeviceEventMessage]
    assert [type(message) for message in attributes] == [DeviceAttributesMessage]

    client = sdk.mqtt.client
    sdk.mqtt._on_connect(client, None, {}, SUCCESS, None)
    await drain()
    assert connection == ["connected"]
    sdk.mqtt._on_disconnect(client, None, {}, SUCCESS, None)
    await drain()
    assert connection == ["connected", "disconnected"]


@pytest.mark.asyncio
async def test_the_cloud_device_fragment_keeps_the_last_event_and_reads_the_caches() -> None:
    sdk = facade()
    device = DEVICES[0]
    await deliver(sdk, "state", {"state": "isPaused"})  # cached before the fragment runs
    await deliver(sdk, "attributes", {"attributes": {"a": 1}})
    await deliver(sdk, "state", {"state": "isDocked"}, device_id=OTHER_ID)
    bound = await run_fragment(after("NavimowCloudDevice and StateManager"), sdk=sdk, device=device)
    assert isinstance(bound["state"], DeviceStateMessage)
    assert (bound["state"].device_id, bound["state"].state) == (DEVICE_ID, "paused")
    assert isinstance(bound["attributes"], DeviceAttributesMessage)
    assert bound["attributes"].device_id == DEVICE_ID
    assert (
        bound["event"] is None
    )  # the consumer keeps the last event, and none has arrived since it began to

    await deliver(sdk, "event", {"type": "system", "event": "started"})
    await deliver(sdk, "event", {"type": "system", "event": "other"}, device_id=OTHER_ID)
    last_event = bound["last_event"]
    assert set(last_event) == {DEVICE_ID, OTHER_ID}
    assert isinstance(last_event[device.id], DeviceEventMessage)
    assert last_event[device.id].device_id == DEVICE_ID

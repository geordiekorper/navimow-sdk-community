"""Construct the MQTT classes outside and inside a running event loop.

It also updates credentials in both connection states.

The core classes use paho's callback API version 2, so building their clients
must not raise paho's "Callback API version 1 is deprecated" warning; it is
turned into an error around them. The legacy MowerMQTT stays on version 1 and
keeps that warning, so its construction is left outside the filter.

Run by the bounds sessions of noxfile.py, on the oldest and the newest
allowed aiohttp and paho-mqtt: paho's constructors and setters, and where
they keep their values, are what could differ between versions.
"""

import asyncio
import sys
import warnings

import aiohttp
import paho.mqtt

from mower_sdk import NavimowMQTT, NavimowSDK

# The legacy client is imported from its legacy path on purpose, so this
# smoke test does not go through a deprecated path.
from mower_sdk.legacy.mqtt_v1 import MowerMQTT

CORE_ON_VERSION_2 = "Callback API version"


async def main() -> None:
    # MowerMQTT builds its paho client lazily; NavimowMQTT builds it in __init__.
    MowerMQTT("broker.invalid")._build_client()
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=CORE_ON_VERSION_2)
        await core()


async def core() -> None:
    NavimowMQTT("broker.invalid", 8883, None, None, records=[])._build_new_client()
    NavimowSDK("broker.invalid", 8883)

    # update_credentials calls paho's username_pw_set and ws_set_options on the
    # live client while connected and rebuilds the client while disconnected; the
    # setters' signatures and where they store their values are what could differ
    # between paho versions. Nothing connects: the paho client is told it is
    # connected through a stub, and the rebuilt client's connect is stubbed out.
    mqtt = NavimowMQTT(
        "wss://broker.invalid",
        443,
        "user",
        "secret",
        records=[],
        ws_path="/mqtt",
        auth_headers={"Authorization": "Bearer old"},
    )
    live = mqtt.client
    assert live.on_connect_fail == mqtt._on_connect_fail, "paho's connect-failure callback is set"
    assert live.on_subscribe == mqtt._on_subscribe, (
        "paho's subscribe-acknowledgement callback is set"
    )
    live.is_connected = lambda: True
    mqtt.update_credentials(password="rotated", auth_headers={"Authorization": "Bearer new"})
    assert mqtt.client is live, "connected: the live client is kept"
    assert (live._username, live._password) == (b"user", b"rotated"), "connected: merged pair set"
    assert live._websocket_path == "/mqtt", "connected: path set"
    assert live._websocket_extra_headers == {"Authorization": "Bearer new"}, (
        "connected: headers set"
    )

    live.is_connected = lambda: False
    mqtt.connect_async = lambda: None  # the rebuilt client would otherwise try to connect
    mqtt.update_credentials(username="user2")
    rebuilt = mqtt.client
    assert rebuilt is not live, "disconnected: the client is rebuilt"
    assert rebuilt._client_id != live._client_id, "disconnected: a fresh client id suffix"
    assert rebuilt._client_id.decode() == mqtt.client_id, (
        "disconnected: client_id names the new client"
    )
    assert (rebuilt._username, rebuilt._password) == (b"user2", b"rotated"), (
        "disconnected: rebuilt pair"
    )
    assert rebuilt._websocket_extra_headers == {"Authorization": "Bearer new"}, (
        "disconnected: rebuilt headers"
    )
    assert mqtt.loop is asyncio.get_running_loop(), (
        "constructed inside a loop: the running loop is bound"
    )
    print(
        f"constructed and updated credentials on Python {sys.version.split()[0]}, "
        f"aiohttp {aiohttp.__version__}, paho-mqtt {paho.mqtt.__version__}"
    )


# Outside any running loop, construction binds no loop and creates none: with no
# current loop set, asyncio.get_event_loop() silently creates a loop on 3.11, warns
# and creates one on 3.12 and 3.13 and raises on 3.14, so on 3.11 to 3.13 the
# policy's current-loop slot is read instead and only 3.14 asks it (the None check
# catches a created loop). A loop set as current with asyncio.set_event_loop() and
# not yet running is bound, on each version's real policy.
with warnings.catch_warnings():
    warnings.filterwarnings("error", message="There is no current event loop")
    warnings.filterwarnings("error", message=CORE_ON_VERSION_2)
    outside = NavimowMQTT("broker.invalid", 8883, None, None, records=[])
    assert outside.loop is None, "constructed outside a loop: no loop bound"
    assert NavimowSDK("broker.invalid", 8883)._mqtt.loop is None, (
        "facade outside a loop: no loop bound"
    )
    current = asyncio.new_event_loop()
    asyncio.set_event_loop(current)
    try:
        assert NavimowMQTT("broker.invalid", 8883, None, None, records=[]).loop is current, (
            "constructed under a current loop: it is bound"
        )
        assert NavimowSDK("broker.invalid", 8883)._mqtt.loop is current, (
            "facade under a current loop: it is bound"
        )
    finally:
        asyncio.set_event_loop(None)
        current.close()

asyncio.run(main())

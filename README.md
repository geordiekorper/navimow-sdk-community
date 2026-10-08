# Navimow Python SDK

<p align="center">
  <img src="https://fra-navimow-prod.s3.eu-central-1.amazonaws.com/img/navimowhomeassistant.png" width="600">
</p>

> **Community edition.** Forked from [segwaynavimow/navimow-sdk](https://github.com/segwaynavimow/navimow-sdk) at 0.1.2 (April 2026), whose repository has had no commits since. The import name is unchanged: `import mower_sdk`. [Why this fork exists](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/why-this-fork.md).

[![CI](https://github.com/geordiekorper/navimow-sdk-community/actions/workflows/ci.yml/badge.svg)](https://github.com/geordiekorper/navimow-sdk-community/actions/workflows/ci.yml)

A lightweight Python SDK for integrating Navimow robotic mowers with cloud platforms and smart home systems.

It provides device discovery, status monitoring and mower control over the Navimow cloud's REST API, and
real-time updates over its MQTT feed.

## Features

- REST API client (`MowerAPI`): devices, status, commands and their results
- Real-time state, events, attributes and (optionally) location over MQTT (`NavimowSDK`)
- Typed models and errors that say whether a failed call may still have been carried out
- Designed for Home Assistant integrations and other long-running consumers

## Installation

```bash
pip install navimow-sdk-community
```

Python 3.11 or later. The distribution is published as pre-releases (`0.2.0aN`) until 0.2.0 is
final. While no final release exists, `pip install navimow-sdk-community` installs the newest
pre-release; to pin one, name it (`pip install navimow-sdk-community==0.2.0a3`), and once a final
release exists, ask for pre-releases explicitly with `pip install --pre navimow-sdk-community`.
The import name is `mower_sdk`.

> **Switching from the upstream package.** `navimow-sdk` and `navimow-sdk-community` both install
> the `mower_sdk` package, so they cannot coexist in one environment: whichever was installed last
> overwrites the other's files. Uninstall the upstream distribution first, or use a fresh
> environment, and change any requirement on `navimow-sdk` to `navimow-sdk-community`.
> [docs/migrating.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/migrating.md) lists what behaves differently afterwards.
>
> While both distributions are listed, importing `mower_sdk` emits a `UserWarning` that names both
> versions. It can only fire when this package's own files are the ones loaded: if `navimow-sdk` was
> installed last, its files are what runs and nothing here can say so, so a consumer that requires
> this distribution should compare `importlib.metadata.version("navimow-sdk-community")` with
> `mower_sdk.__version__`.
>
> ```bash
> pip uninstall navimow-sdk
> pip install navimow-sdk-community
> ```

## Quick example

The example lists the account's mowers, connects to them and prints each new state for a minute. It
sends no command.

```python
import asyncio

import aiohttp

from mower_sdk import MowerState, NavimowClient

BASE_URL = "https://navimow-fra.ninebot.com"
TOKEN = "your_access_token"  # an OAuth access token, obtained separately


def show(state: MowerState) -> None:
    print(state.device_id, state.status.value, state.battery, state.source.value, state.target_zone)


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = NavimowClient.from_token(session, TOKEN, BASE_URL)
        client.on_state(show)
        # Lists the mowers, polls their status once, connects the MQTT feed, and from
        # then on polls every two minutes and delivers each new state to show().
        await client.async_connect()
        try:
            await asyncio.sleep(60)
        finally:
            await client.async_disconnect()


asyncio.run(main())
```

`NavimowClient.from_token()` builds the REST client (`MowerAPI`) on the session, the token and the
base URL; `NavimowClient(api)` takes one the program already holds. The client owns the REST
client and the MQTT facade and keeps one `MowerState` per mower: the
status half from the latest state message while it is fresh, else from a REST status read after it,
and the location record beside it (see [Mower states](#mower-states)). `client.state(device_id)` and
`client.states()` return the states between callbacks; `on_event`, `on_attributes` and `on_rejected`
forward the facade's callbacks, each with an optional `device_id=` filter, and `on_connection` and
`on_error` report the connection's events and the client's own failures. The layers are reachable as
`client.api`, `client.sdk` and `client.mqtt`.

A command goes over REST: `await client.api.async_send_command(device_id, MowerCommand.START)`, or
`async_send_command_receipt()` for a receipt whose result can be looked up later.

**Tokens.** The SDK is token-in: it takes an OAuth access token and never obtains or refreshes one
itself. Obtain the token through the Navimow account's OAuth flow. After each refresh pass the new
token to `client.async_set_token()`, which sets it on the REST client at once and pushes the bearer
header to the MQTT client, or give the client a `token_provider` coroutine, which it awaits before
each operation that sends a token. With the layers alone, pass the token to `api.set_token()` and
the bearer header to `sdk.update_mqtt_credentials(auth_headers=...)`.

### The layers underneath

The same without the client: the REST client, then the facade over the MQTT feed, each run by the
program itself.

```python
import asyncio

import aiohttp

from mower_sdk import DeviceStateMessage, MowerAPI, NavimowSDK

BASE_URL = "https://navimow-fra.ninebot.com"
TOKEN = "your_access_token"  # an OAuth access token, obtained separately


def print_state(message: DeviceStateMessage) -> None:
    print(message.device_id, message.state, message.battery)


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        api = MowerAPI(session, TOKEN, BASE_URL)
        devices = await api.async_get_devices()
        for device in devices:
            status = await api.async_get_device_status(device.id)
            print(device.id, device.name, status.status.value, status.battery)

        # The broker, its WebSocket path and the MQTT credentials come from the
        # cloud; the endpoint allows about one call a minute. The facade connects
        # with TLS over WebSocket and the token as the bearer header.
        info = await api.async_get_mqtt_connection_info()
        sdk = NavimowSDK.from_connection_info(info, access_token=TOKEN, records=devices)
        sdk.on_state(print_state)
        sdk.connect()
        try:
            await asyncio.sleep(60)
        finally:
            sdk.disconnect()


asyncio.run(main())
```

## Threaded applications

The live path is asyncio: MQTT callbacks run on the event loop the facade is bound to, and with no
running loop they are dropped. An application built on threads (a Flask or other WSGI app, a
script, a CLI tool) can give the SDK a loop on a thread of its own and talk to it from anywhere
else. The helper below is the whole bridge; the suite runs this exact code.

```python
import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class NavimowThread:
    """An event loop on a thread of its own, for the SDK's coroutines and callbacks."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="navimow", daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = 60) -> T:
        """Run a coroutine on the loop and wait for its result, from any other thread.

        On timeout the coroutine is cancelled before TimeoutError is raised.
        """
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("NavimowThread.run() called on the loop's own thread would wait forever")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def call(self, function: Callable[..., T], *args: Any, timeout: float | None = 60) -> T:
        """Run a plain function on the loop's thread (to create an aiohttp session, say)."""

        async def call() -> T:
            return function(*args)

        return self.run(call(), timeout)

    def stop(self) -> None:
        """Cancel what is still running on the loop, then stop it, join its thread and close it.

        Close sessions and disconnect first.
        """

        async def cancel_the_rest() -> None:
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(cancel_the_rest(), self.loop).result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()
```

Using it, from the application's own threads:

```python
import queue

import aiohttp

from mower_sdk import MowerAPI, NavimowSDK

mowers = NavimowThread()
session = mowers.call(aiohttp.ClientSession)  # an aiohttp session belongs to one loop
sdk = None
try:
    api = MowerAPI(session, TOKEN, BASE_URL)
    devices = mowers.run(api.async_get_devices())  # every REST call goes through run()
    info = mowers.run(api.async_get_mqtt_connection_info())

    sdk = NavimowSDK.from_connection_info(
        info, access_token=TOKEN, records=devices,
        loop=mowers.loop,  # callbacks go to the helper's loop
    )
    states = queue.Queue()
    sdk.on_state(states.put)  # callbacks run on the loop's thread: hand them over, don't block there
    sdk.connect()

    message = states.get(timeout=120)  # in the application's thread
finally:
    # A failed REST call or a wait that timed out still releases everything.
    if sdk is not None:
        sdk.disconnect()
    mowers.run(session.close())
    mowers.stop()
```

Three rules keep it working:

- Pass `loop=mowers.loop` when constructing the facade (or `NavimowMQTT`), since the constructing
  thread has no loop of its own.
- `sdk.connect()`, `sdk.disconnect()`, `sdk.update_mqtt_credentials()`, `sdk.mqtt.rebuild()` and
  `sdk.mqtt.update_credentials()` are plain calls, and every one of them can block: each waits
  while a rebuild runs on another thread, `disconnect()` joins paho's network thread, and the
  credential updates and `rebuild()` can rebuild the client, which joins that thread and loads
  certificates. Make them from the application's threads, never inside a callback, which runs on
  the loop.
- Coroutines, REST calls and `sdk.async_refresh_broker_credentials(api, ...)` included, go through
  `mowers.run()`, never from inside a callback: `run()` refuses to wait on the loop's own thread,
  where it would wait forever.

## Behaviour notes

### REST

**Request timeout and errors.** Every `MowerAPI` request is bounded at 20 seconds in total by
default. Pass `MowerAPI(..., request_timeout=None)` to leave the session's own timeout policy in
force instead, or another number of seconds to change the bound. A failed request or a refusal
is a `MowerAPIError`; `MowerTransportError` means no usable reply (a timeout, a connection
error, an HTTP 5xx, a reply that is not JSON, or a successful reply whose `data` is null on a
command or command-result call), so a command may still have been carried out; the device list
and the statuses are empty for a null `data`; `MowerAuthRequiredError` means the credentials
were refused, or that no token is set (then no request is sent); `MowerRateLimitedError` means
slow down.

### MQTT

**paho-mqtt 2.1 or later.** The MQTT client uses paho's callback API version 2, so paho-mqtt 1.x is
no longer supported.

**Keepalive.** The MQTT keepalive defaults to 60 seconds: idle links to the cloud die after about
ten minutes, and a ping a minute keeps them alive and finds a dead one quickly. Pass
`keepalive_seconds=2400` for the previous value.

**Subscriptions.** `sdk.mqtt.subscription_results` maps each topic subscribed since the latest
connect to `pending`, `granted`, `refused: <reason>` or `not sent: <error>`; a refused topic is
also logged as a warning, and `sdk.mqtt.on_subscribe` can be set to an async
`(topic, granted, codes)` callback. A refused subscription otherwise looks the same as a mower that
does not publish on that topic.

**Connection events.** `sdk.mqtt.on_connection_event` can be set to an async callback that
receives a `ConnectionEvent` for each connect, disconnect and connect failure: its `kind`, the
`client_id` of the client it came from, the `reason`, the UTC time `at` and the `rebuilds` count,
all as they were when it happened. The zero-argument `on_connected` and `on_disconnected` still
run; code that reads `last_disconnect_reason` or `client_id` inside them may see a later client's
values after a `rebuild()`.

**Payload bytes.** A JSON object payload is re-encoded with `device_id` added before it reaches
`NavimowMQTT.on_message`, so those bytes are not the mower's. They arrive as a
`mower_sdk.mqtt.ReceivedPayload`, still `bytes` and equal to the re-encoded form, whose `original`
holds the bytes exactly as received; the typed messages and `RejectedMessage` carry the same bytes
as `original`.

**Broker address.** `api.async_get_mqtt_connection_info()` reads the credential reply into an
`MqttConnectionInfo` (host, port, WebSocket path, username, password), and
`NavimowSDK.from_connection_info(info, access_token=..., records=...)` builds the facade from it,
as in the layered example under the quick example; `NavimowClient` does the same inside
`async_connect()`. The constructor stays available for another transport.

**Broker credentials.** The MQTT username and password come from the cloud's credential endpoint,
which allows about one call a minute. `await sdk.async_refresh_broker_credentials(api,
auth_headers=...)` fetches and applies them, at most once per 65 seconds: call it at startup
before `connect()`, and again after a failed connect (`sdk.mqtt.on_connect_fail`). When the reply
names another broker host, port or path, the client is rebuilt on it. Do not call it
on a timer or on an OAuth token refresh: after a token refresh, pass the new bearer header with
`sdk.update_mqtt_credentials(auth_headers=...)` alone.

**A broker that stops delivering.** The keepalive finds a dead link, not a broker that has stopped
delivering to a live one, which the cloud's broker has been seen to do. `MqttWatchdog(sdk)` finds
that from the data: call `after_poll(inputs)` after each REST status poll and `check_silence(inputs)`
every half minute or so (it needs `subscribe_location=True`), with a `WatchInput` per mower (the state you show, REST's latest state and
when it was read). When either returns a `RebuildRequest`, rebuild the client off the event loop
(`sdk.mqtt.rebuild(reason=request.reason)`) and pass the request to `acknowledge()`. It has no
timer and makes no request of its own.

**Location channel.** Pose, zone, route progress and target zones arrive on a separate MQTT
channel, off by default (several models never publish on it, and it is a movement trace). Turn it
on with `NavimowSDK(..., subscribe_location=True)`, then register `sdk.on_location(callback)`;
`sdk.get_cached_location(device_id)` returns the merged record, which `DeviceLocation.to_dict()`
and `from_dict()` let you persist and hand back with `sdk.restore_location()` after a restart.
The record also carries the dock's estimated position (`dock_x`, `dock_y`), learned from the poses
the mower sends while docked, and `target_zone(location, status)` reads its target report: a zone
id, or `TargetZone.ALL` or `NONE` for a report that names none, inferred from the state you show.
Messages that could not be applied are reported through `sdk.on_rejected(callback)`; for a
location message, `RejectedMessage.skipped` lists each entry that was not applied, with its reason
and its fields read as far as they go, so a late task reading can still be kept, marked as late.

### Commands

**MQTT commands are off by default.** `NavimowSDK.start_mowing`, `pause`, `return_to_base` and
`set_blade_height` publish to an MQTT command topic that the broker accepts and no mower has been
seen to act on; every command that works goes over REST, through `MowerAPI.async_send_command`.
They therefore raise `MowerUnsupportedOperationError` unless the facade is constructed with
`NavimowSDK(..., allow_experimental_mqtt_commands=True)`.

## Mower states

`MowerStatus` values, as `DeviceStatus.status` from REST gives them. A state message's
`DeviceStateMessage.state` is a string: the known raw states map to the same values, and one the
SDK does not recognise is passed through as the cloud sent it (REST gives `unknown` for it).

* `idle`
* `mowing`
* `paused`
* `docked`
* `returning`
* `mapping`
* `updating`
* `offline`
* `error`
* `unknown` (REST: a state the SDK does not recognise)

`charging` comes only from the location channel's pose code.

The same readers are public for payloads a consumer keeps raw (from
`async_get_vehicle_status_raw()`, `on_raw` or a history): `mower_status_from_raw(raw)` gives the
`MowerStatus` REST would, `canonical_state(raw)` the string a state message would,
`RAW_STATE_TO_CANONICAL` is the table behind both, and `battery_from_payload(data)` reads the
battery percentage from either payload shape.

`MowerState` (`mower_sdk.navimow_client`, also at the package root) is one object for what a mower
is doing and where it is.
`MowerState.from_state_message(message, location=..., received_monotonic=...)` builds it from a
state message the facade applied, and
`MowerState.from_status(status, location=..., received_at=..., received_monotonic=...)` from a REST
status; `source` (`StateSource.MQTT` or `StateSource.REST`) says which. A consumer reads `status`,
`raw_state`, `battery`, `error_code`, `error_message`, `observed_at` (the mower's time, epoch
milliseconds) and `received_at` for the status half, `location` (the `DeviceLocation` record as
given) and the `target_zone` property for the location half, and `age()` for the seconds since the
status half arrived. `NavimowClient` builds these states from both transports and delivers each new
one to its `on_state` callbacks; `client.state(device_id)` returns the latest.

## Documentation

For someone using the SDK:

- [CHANGELOG.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/CHANGELOG.md): what changed in each version.
- [docs/migrating.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/migrating.md): coming from upstream's `navimow-sdk`, and moving off
  the deprecated classes.
- [docs/endpoints.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/endpoints.md): every request the SDK sends to the cloud, what
  the cloud answers and what the caller gets back.
- [docs/why-this-fork.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/why-this-fork.md): why the community edition exists.

For someone working on it:

- [CONTRIBUTING.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/CONTRIBUTING.md): reporting a problem and proposing a change.
- [docs/development.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/development.md): setup, the checks and the commit rules.
- [docs/architecture.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/architecture.md): how the SDK works inside.
- [tests/README.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/tests/README.md) and [tools/README.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/tools/README.md): the test suite, and
  the scripts behind the checks.
- [mower_sdk/legacy/README.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/mower_sdk/legacy/README.md): the code kept from upstream and
  what replaces it.
- [docs/UPSTREAM.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/UPSTREAM.md): where the code came from, and what a merge back to upstream needs.
- [docs/releasing.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/releasing.md): making a release.

## Development

```bash
pip install -e ".[dev]"
pytest
nox -s tests-3.14
```

[docs/development.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/docs/development.md) has the rest.

## License

GPL-3.0-only. See [LICENSE](https://github.com/geordiekorper/navimow-sdk-community/blob/main/LICENSE).

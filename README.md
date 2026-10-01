# Navimow Python SDK

<p align="center">
  <img src="https://fra-navimow-prod.s3.eu-central-1.amazonaws.com/img/navimowhomeassistant.png" width="600">
</p>

> **Community edition.** Forked from [segwaynavimow/navimow-sdk](https://github.com/segwaynavimow/navimow-sdk) at 0.1.2 (April 2026), which has had no maintainer activity since. The import name is unchanged: `import mower_sdk`.

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
The import name is `mower_sdk`. What changed in each version is in
[CHANGELOG.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/CHANGELOG.md),
and what was taken from other forks of the upstream project is in
[UPSTREAM.md](https://github.com/geordiekorper/navimow-sdk-community/blob/main/UPSTREAM.md).

> **Switching from the upstream package.** `navimow-sdk` and `navimow-sdk-community` both install
> the `mower_sdk` package, so they cannot coexist in one environment: whichever was installed last
> overwrites the other's files. Uninstall the upstream distribution first, or use a fresh
> environment, and change any requirement on `navimow-sdk` to `navimow-sdk-community`.
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

The example lists the account's mowers over REST, then connects to the MQTT feed and prints each
state message for a minute. It sends no command.

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
        # cloud; the endpoint allows about one call a minute.
        info = await api.async_get_mqtt_user_info()
        sdk = NavimowSDK(
            broker=info["mqttHost"],  # wss://..., so TLS over WebSocket
            port=443,
            ws_path=info["mqttUrl"],
            username=info["userName"],
            password=info["pwdInfo"],
            auth_headers={"Authorization": f"Bearer {TOKEN}"},
            records=devices,
        )
        sdk.on_state(print_state)
        sdk.connect()
        try:
            await asyncio.sleep(60)
        finally:
            sdk.disconnect()


asyncio.run(main())
```

A command goes over REST: `await api.async_send_command(device.id, MowerCommand.START)`, or
`async_send_command_receipt()` for a receipt whose result can be looked up later.

**Tokens.** The SDK is token-in: it takes an OAuth access token and never obtains or refreshes one
itself. Obtain the token through the Navimow account's OAuth flow, and after each refresh pass the
new token to `api.set_token()` and the new bearer header to
`sdk.update_mqtt_credentials(auth_headers=...)`.

## Behaviour notes

### REST

**Request timeout and errors.** Every `MowerAPI` request is bounded at 20 seconds in total by
default. Pass `MowerAPI(..., request_timeout=None)` to leave the session's own timeout policy in
force instead, or another number of seconds to change the bound. A failed request or a refusal
is a `MowerAPIError` (one exception is kept from upstream: a successful reply whose `data` is
null makes the two command calls raise `AttributeError`; the device list and the statuses are
then empty); `MowerTransportError` means no usable reply (a timeout, a connection error, an
HTTP 5xx, or a reply that is not JSON), so a command may still have been carried out;
`MowerAuthRequiredError` means the credentials were refused, or that no token is set (then no
request is sent); `MowerRateLimitedError` means slow down.

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

**Broker credentials.** The MQTT username and password come from the cloud's credential endpoint,
which allows about one call a minute. `await sdk.async_refresh_broker_credentials(api,
auth_headers=...)` fetches and applies them, at most once per 65 seconds: call it at startup
before `connect()`, and again after a failed connect (`sdk.mqtt.on_connect_fail`). Do not call it
on a timer or on an OAuth token refresh: after a token refresh, pass the new bearer header with
`sdk.update_mqtt_credentials(auth_headers=...)` alone.

**Location channel.** Pose, zone, route progress and target zones arrive on a separate MQTT
channel, off by default (several models never publish on it, and it is a movement trace). Turn it
on with `NavimowSDK(..., subscribe_location=True)`, then register `sdk.on_location(callback)`;
`sdk.get_cached_location(device_id)` returns the merged record, which `DeviceLocation.to_dict()`
and `from_dict()` let you persist and hand back with `sdk.restore_location()` after a restart.
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

## Development

```bash
pip install -e . nox pytest
pytest
nox -s tests-3.14
```

`pytest` runs the suite in the current environment; the nox sessions run it in fresh ones, as CI
does: `tests-3.11` to `tests-3.14` on each supported Python, `"bounds(oldest)"` and
`"bounds(newest)"` against the lowest (Python 3.11, aiohttp 3.9.0, paho-mqtt 2.1.0) and the newest
supported dependencies, and `lint` for ruff. `nox --list` shows them all. Tests are plain
`asyncio.run` tests with recording fakes.

## Contributing

Issues and pull requests are welcome at [github.com/geordiekorper/navimow-sdk-community](https://github.com/geordiekorper/navimow-sdk-community/issues).

## License

GPL-3.0-only. See [LICENSE](https://github.com/geordiekorper/navimow-sdk-community/blob/main/LICENSE).

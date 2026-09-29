# Navimow Python SDK

<p align="center">
  <img src="https://fra-navimow-prod.s3.eu-central-1.amazonaws.com/img/navimowhomeassistant.png" width="600">
</p>

> **Community edition.** Forked from [segwaynavimow/navimow-sdk](https://github.com/segwaynavimow/navimow-sdk) at 0.1.2 (April 2026), which has had no maintainer activity since. The import name is unchanged: `import mower_sdk`.

A lightweight Python SDK for integrating Navimow robotic mowers with cloud platforms and smart home systems.

It provides a simple interface for device discovery, status monitoring, and mower control using REST APIs and MQTT-based real-time communication.

## Features

- REST API client for device management
- MQTT-based real-time status updates
- Device discovery
- Mower control (start, pause, resume, dock)
- Sync and async interfaces
- Designed for Home Assistant integrations

More features are being added over time.

## Installation

Install from PyPI:

```bash
pip install navimow-sdk-community
```

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

## Quick Example

```python
import aiohttp
from mower_sdk import MowerClient

client = MowerClient(
    session=aiohttp.ClientSession(),
    token="your_access_token",
    api_base_url="https://api.example.com",
    mqtt_broker="mqtt.example.com",
)

devices = await client.async_discover_devices()
print(devices)

await client.async_start_mowing("device_id")
```

> The SDK does not handle OAuth2 authentication. You must obtain the access token separately.

## Behaviour notes

**paho-mqtt 2.1 or later.** The MQTT client uses paho's callback API version 2, so paho-mqtt 1.x is
no longer supported.

**Request timeout and errors.** Every `MowerAPI` request is bounded at 20 seconds in total by
default. Pass `MowerAPI(..., request_timeout=None)` to leave the session's own timeout policy in
force instead, or another number of seconds to change the bound. A failed request or a refusal
is a `MowerAPIError` (one exception is kept from upstream: a successful reply whose `data` is
null makes most calls raise `AttributeError`); `MowerTransportError` means no usable reply (a timeout, a connection error, an
HTTP 5xx, or a reply that is not JSON), so a command may still have been carried out;
`MowerAuthRequiredError` means the credentials were refused; `MowerRateLimitedError` means slow
down.

**Keepalive.** The MQTT keepalive defaults to 60 seconds: idle links to the cloud die after about
ten minutes, and a ping a minute keeps them alive and finds a dead one quickly. Pass
`keepalive_seconds=2400` for the previous value.

**Location channel.** Pose, zone, route progress and target zones arrive on a separate MQTT
channel, off by default (several models never publish on it, and it is a movement trace). Turn it
on with `NavimowSDK(..., subscribe_location=True)`, then register `sdk.on_location(callback)`;
`sdk.get_cached_location(device_id)` returns the merged record, which `DeviceLocation.to_dict()`
and `from_dict()` let you persist and hand back with `sdk.restore_location()` after a restart.
Messages that could not be applied are reported through `sdk.on_rejected(callback)`.

**Broker credentials.** The MQTT username and password come from the cloud's credential endpoint,
which allows about one call a minute. `await sdk.async_refresh_broker_credentials(api,
auth_headers=...)` fetches and applies them, at most once per 65 seconds: call it at startup
before `connect()`, and again after a failed connect (`sdk.mqtt.on_connect_fail`). Do not call it
on a timer or on an OAuth token refresh: after a token refresh, pass the new bearer header with
`sdk.update_mqtt_credentials(auth_headers=...)` alone.

**MQTT commands are off by default.** `NavimowSDK.start_mowing`, `pause`, `return_to_base` and
`set_blade_height` publish to an MQTT command topic that the broker accepts and no mower has been
seen to act on; every command that works goes over REST, through `MowerAPI.async_send_command`.
They therefore raise `MowerUnsupportedOperationError` unless the facade is constructed with
`NavimowSDK(..., allow_experimental_mqtt_commands=True)`.

## Core Capabilities

* **Device Discovery** – Retrieve mower devices linked to an account
* **Device Status** – Get current mower state and battery level
* **Real-time Updates** – Receive MQTT status updates
* **Device Control** – Start, pause, resume mowing or return to dock

Typical mower states include:

* `idle`
* `mowing`
* `paused`
* `docked`
* `returning`
* `mapping`
* `updating`
* `offline`
* `error`

`charging` comes only from the location channel's pose code.

## Contributing

Issues and Pull Requests are welcome.

## License

GPL-3.0-only. See [LICENSE](LICENSE).

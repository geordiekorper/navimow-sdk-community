# Migrating

Two moves: from upstream's `navimow-sdk` 0.1.2 to this distribution with no
change to the consumer's code, and from the classes kept in `mower_sdk.legacy`
to the live path (`MowerAPI`, `NavimowSDK`, `NavimowMQTT`, the models and the
errors). Replacing the installed distribution is described in the README under
[Installation](../README.md#installation).

## From navimow-sdk 0.1.2 with no code change

### What keeps working

Every public name 0.1.2 exported still imports from the path it had, and the
import name is still `mower_sdk`. The
[legacy README](../mower_sdk/legacy/README.md#how-the-old-names-still-work)
says how the names that moved resolve, and
[UPSTREAM.md](UPSTREAM.md#rules-that-keep-a-merge-back-possible) names the
inventory and the test that hold this in place.

### What behaves differently

The last column is the version whose [CHANGELOG.md](../CHANGELOG.md) entry
has the detail and the reason.

| What an unchanged consumer meets | The way back | In |
|---|---|---|
| **Request timeout.** Every `MowerAPI` request is bounded at 20 seconds in total. Upstream left the bound to the session, 300 seconds for a bare aiohttp session. | `MowerAPI(..., request_timeout=None)` | [0.2.0a2] |
| **Timeouts.** A request that times out raises `MowerTransportError`, a `MowerAPIError`, with the `TimeoutError` as its `__cause__`. Upstream caught only `aiohttp.ClientError`. | None; catch `MowerAPIError`. | [0.2.0a2], [0.2.0a3] |
| **Error classes.** A failed call is still a `MowerAPIError`, now a subclass where one applies. The HTTP status decides for every non-2xx reply: 401 and 403 raise `MowerAuthRequiredError`; a 5xx, a status below 200 and a redirect raise `MowerTransportError`; any other 4xx raises a plain `MowerAPIError`, as every status of 400 or more did. A 2xx reply that is not a JSON object is a `MowerTransportError` too. | None needed: `except MowerAPIError` catches them all. | [0.2.0a3] |
| **No token.** `MowerAPI` with an empty or `None` token raises `MowerAuthRequiredError`, not a plain `MowerAPIError`, still before any request. | None. | [0.2.0a4] |
| **Null data.** A successful reply whose `data` is null, or not an object, raises `MowerTransportError` from the command calls (`async_send_command`, `async_send_command_receipt`, `send_command`) and the command-result queries (`async_query_command_results`, `query_command_results`, `async_get_command_result`). Upstream let an `AttributeError` out of the command calls, and the result query an `AttributeError` or a `TypeError`. The reads (`async_get_devices`, `async_get_device_statuses`, `async_get_mqtt_user_info`) still give empty results for a null `data`. | None; catch `MowerAPIError`. | [0.2.0a5] |
| **Error text.** The `ERROR_MESSAGES` values, and the exception messages built from them, are English; they were Chinese. The keys are unchanged. An HTTP error body is cut at 500 characters. | None; test the class or `error_code`, not the text. | [0.2.0a3] |
| **Battery.** `DeviceStatus.battery` and `DeviceStateMessage.battery` are `None` when the payload has no readable value, where they were 0, and `capacityRemaining` is read before `battery`. | No parameter; `battery or 0` gives the 0 back. | [0.2.0a2] |
| **Mower states.** `MowerStatus` gains `MAPPING`, `UPDATING` and `OFFLINE`. The raw states `isMapping`, `inSoftwareUpdate` and `Offline` or `offline` map to them, in `DeviceStatus.status` and in `DeviceStateMessage.state`, where they gave mowing, paused and unknown. | None; map the three back in the consumer. | [0.2.0a3] |
| **Device list.** `async_get_devices()` leaves out an entry without an `id`, which became `Device(id="")`, and logs a warning. `Device.from_dict` also reads the camelCase keys and `firmware`, so `model`, `firmware_version`, `serial_number`, `mac_address` and `online` can be filled where they were empty. | None. | [0.2.0a2], [0.2.0a4] |
| **Device identity.** `Device.device_name` and `Device.iot_id` are `None` for an entry without a `deviceName` or `iotId` key (or their snake_case spellings), which the cloud has not been seen to send; they repeated `name` and `id`. | `device.device_name or device.name`, `device.iot_id or device.id` | [Unreleased] |
| **paho-mqtt.** The requirement is `paho-mqtt>=2.1.0,<3`; it was `>=1.6.1`. `NavimowMQTT` uses paho's callback API version 2, so an override or wrapper of `_on_connect` or `_on_disconnect` takes `(client, userdata, flags, reason_code, properties=None)`. | None. | [0.2.0a3] |
| **Keepalive.** The MQTT keepalive defaults to 60 seconds; it was 2400. | `keepalive_seconds=2400` | [0.2.0a3] |
| **MQTT commands.** `NavimowSDK.start_mowing`, `pause`, `return_to_base` and `set_blade_height` raise `MowerUnsupportedOperationError`, which is not a `MowerAPIError`, and publish nothing. | `NavimowSDK(..., allow_experimental_mqtt_commands=True)`; the README says [which commands work](../README.md#commands). | [0.2.0a2] |
| **Event loop.** `NavimowSDK` and `NavimowMQTT` no longer create an event loop when constructed outside one. Constructed and connected with no running or current loop, they drop each callback with a logged warning. A closed `loop=` raises `ValueError`, and a connect from a running loop other than the bound one raises `RuntimeError`. | Pass `loop=`. | [0.2.0a2], [0.2.0a3] |
| **Callbacks.** A state, event or attributes callback that raises is logged with its traceback, and the callbacks after it still receive the message. Before, they were skipped. | None. | [0.2.0a2] |
| **Private attributes.** `NavimowSDK._loop` is gone. | `sdk.loop`, and `sdk.mqtt` for `sdk._mqtt`. | [0.2.0a3] |
| **Star imports.** `from mower_sdk.mqtt import *` and `from mower_sdk.models import *` bring the names in `__all__` and no longer the modules' own imports (`json`, `asyncio`, `Any`, `Enum` and the like). | Import those names from where they are defined. | [0.2.0a2] |
| **Dependencies.** `requests` and `aiomqtt`, which the SDK never imported, are no longer installed with it. | Declare them in the consumer that uses them. | [0.2.0a1] |

The changelog has the changes not listed here, and
[why-this-fork.md](why-this-fork.md) says what the fork set out to fix. What
the live path does today is in the README's
[behaviour notes](../README.md#behaviour-notes) and
[mower states](../README.md#mower-states).

### Warnings

- A name that moved to `mower_sdk.legacy`, reached through its old path, emits
  a `DeprecationWarning` ([0.2.0a2]); the
  [legacy README](../mower_sdk/legacy/README.md#how-the-old-names-still-work)
  says when, and the second part of this guide is the way out.
- `MowerAPI`'s synchronous wrappers (`get_devices`, `get_mqtt_user_info`,
  `get_device_status`, `send_command`, `query_command_results`) each emit a
  `DeprecationWarning` that names the `async_*` counterpart, and are otherwise
  unchanged ([0.2.0a2]). The README's
  [Threaded applications](../README.md#threaded-applications) is the way to
  call the coroutines from a program built on threads.
- The legacy `MowerMQTT` stays on paho's callback API version 1, so paho emits
  a `DeprecationWarning` of its own when that class builds a client
  ([0.2.0a3]).
- Importing `mower_sdk` emits a `UserWarning` while upstream's distribution is
  installed beside this one ([0.2.0a3]); the README's
  [Installation](../README.md#installation) says what to do.

## From the legacy classes to the live path

The [legacy README](../mower_sdk/legacy/README.md#what-is-here) has the table
of what each legacy module is and what replaces it. This part has the code.
How the live path works inside is in [architecture.md](architecture.md).

The snippets are fragments of a coroutine, with the imports left out: every
SDK name in them is imported from `mower_sdk`. `session` is an
`aiohttp.ClientSession`, and `BASE_URL` is the one in the README's
[quick example](../README.md#quick-example). In the "after" fragments `api` is
a `MowerAPI`, `devices` what its `async_get_devices()` returned, `info` what
its `async_get_mqtt_connection_info()` returned and, unless the fragment
builds it, `sdk` the `NavimowSDK` the quick example builds from them. The
test suite runs each "after" fragment as printed
(`tests/test_migration_guide.py`).

### MowerClient

`MowerClient` holds a `MowerAPI` as `client.api`, and its REST methods only
forward to it, so each call has one counterpart:

| `MowerClient` | Live path |
|---|---|
| `MowerClient(session, "your_access_token", api_base_url=BASE_URL)` | `api = MowerAPI(session, "your_access_token", BASE_URL)` |
| `async_discover_devices()` | `api.async_get_devices()` |
| `async_get_device_status(id)`, `async_get_device_statuses(ids)` | The same names on `MowerAPI`. |
| `async_start_mowing(id)`, `async_pause_mowing(id)`, `async_dock(id)`, `async_resume(id)` | `api.async_send_command(id, command)` with `MowerCommand.START`, `PAUSE`, `DOCK`, `RESUME`. |
| `update_token(token)` | `api.set_token(token)`; the README's [quick example](../README.md#quick-example) says what the MQTT side needs after a token refresh. |
| `get_token()` | No replacement: the consumer holds the token it passes in. |
| `async_refresh_mqtt_info()` | `api.async_get_mqtt_connection_info()`, which returns an `MqttConnectionInfo`. |
| `async_subscribe_device_updates(id, callback)`, `get_cached_status(id)` | `NavimowSDK`; see `MowerMQTT` below. |
| The synchronous methods (`discover_devices`, `start_mowing`, `pause_mowing`, `dock`, `resume`, `get_device_status`, `get_device_statuses`, `refresh_mqtt_info`, `subscribe_device_updates`) | No synchronous replacement; see [Threaded applications](../README.md#threaded-applications). |

### MowerMQTT

`MowerMQTT.async_subscribe_device()` served one mower and returned only at
disconnect. Before:

```python
mqtt = MowerMQTT(host, 443, user, password, ws_path=path, auth_headers=headers)
await mqtt.async_connect()
task = asyncio.create_task(  # async_subscribe_device returns at disconnect
    mqtt.async_subscribe_device(device_id, on_status_update=handle_status)
)
status = mqtt.get_cached_status(device_id)  # a DeviceStatus, or None
```

After, one facade serves every mower in `records`, and `connect()` returns at
once:

```python
def handle_state(message: DeviceStateMessage) -> None:
    if message.device_id == device_id:
        handle_status(DeviceStatus.from_state_message(message))

sdk.on_state(handle_state)
sdk.connect()
state = sdk.get_cached_state(device_id)  # a DeviceStateMessage, or None
```

`DeviceStatus.from_state_message()` gives the type `on_status_update`
received; its `fallback_status` and `fallback_battery` fill in what a partial
state message does not carry. `sdk.on_event()` passes a `DeviceEventMessage`
where `on_event=` passed the decoded payload, which is the message's `raw`.
The synchronous `connect()` and `subscribe_device()` have no replacement.

### Navimow and NavimowDeviceManager

Before, with `client` a `MowerClient` and `devices` its device list:

```python
navimow = Navimow(client)
cloud = await navimow.initiate_cloud_connection(devices)
navimow.add_devices(devices)
mower = navimow.get_device_by_name("Lawn")  # a NavimowCloudDevice, or None
```

After, the facade is the connection, and the device manager's lookups are a
dict over the device list:

```python
sdk = NavimowSDK.from_connection_info(
    info, access_token="your_access_token", records=devices
)
sdk.connect()
by_name = {device.name: device for device in devices}
mower = by_name.get("Lawn")  # a Device, or None
```

`initiate_cloud_connection(devices, executor=...)` built the MQTT client
through the `executor` callable, off the event loop. Building the client loads
the TLS certificates, and the facade can be built off the loop the same way:
call `from_connection_info` through `loop.run_in_executor()` and give it
`loop=loop`, the loop its callbacks belong to
([architecture.md](architecture.md#threads-and-the-event-loop) has the rules).

### NavimowCloud

Before:

```python
cloud.mqtt_state_event.add_subscribers(handle_state)  # async def, one argument
cloud.mqtt_event_message_event.add_subscribers(handle_event)
cloud.mqtt_attributes_event.add_subscribers(handle_attributes)
cloud.on_connected_event.add_subscribers(handle_connected)  # no argument
cloud.on_disconnected_event.add_subscribers(handle_disconnected)
```

After:

```python
sdk.on_state(handle_state)  # def, not async def
sdk.on_event(handle_event)
sdk.on_attributes(handle_attributes)

async def handle_connection(event: ConnectionEvent) -> None:
    if event.kind == "connected":
        await handle_connected()
    elif event.kind == "disconnected":
        await handle_disconnected()

sdk.mqtt.on_connection_event = handle_connection
```

- The subscribers were coroutine functions held by weak reference, and
  `remove_subscribers()` took one out. The facade's callbacks are plain
  functions, kept for the life of the facade and called on the event loop in
  the order registered; nothing removes one. A callback that has to await
  starts a task with `asyncio.create_task()`.
- `on_ready_event` fired together with `on_connected_event`; the `connected`
  event stands for both. The README describes the
  [connection hooks](../README.md#mqtt). `cloud.connect_async()` and
  `cloud.disconnect()` are `sdk.connect()` and `sdk.disconnect()`.
- The three message events of `NavimowCloud` never fire, for the reason the
  [legacy README](../mower_sdk/legacy/README.md#what-is-here) gives, so
  `NavimowCloudDevice` and `StateManager` receive nothing either. Code moved
  to the facade starts to receive messages.

### NavimowCloudDevice and StateManager

Before:

```python
mower = NavimowCloudDevice(cloud, device, StateManager(device))
mower.state_manager.state_callback.add_subscribers(handle_state)
state = mower.state_manager.last_state  # or get_device_state()
attributes = mower.state_manager.last_attributes
event = mower.state_manager.last_event
```

After, the callback filters on `device_id`, the facade keeps the last state
and attributes, and the consumer keeps the last event:

```python
last_event: dict[str, DeviceEventMessage] = {}

def handle_state(message: DeviceStateMessage) -> None:
    if message.device_id == device.id:
        ...  # this mower's state

def keep_event(message: DeviceEventMessage) -> None:
    last_event[message.device_id] = message

sdk.on_state(handle_state)
sdk.on_event(keep_event)
state = sdk.get_cached_state(device.id)
attributes = sdk.get_cached_attributes(device.id)
event = last_event.get(device.id)
```

`set_notification_callback()` and `StateManager.notification()` have no
replacement: nothing in the SDK ever called `notification()`.

### DataEvent and Event

No replacement; they stay available in `mower_sdk.legacy.event`. Registering a
callback changes as shown under `NavimowCloud`. A consumer that used
`DataEvent` for events of its own imports it from there.

### ThingStatusMessage, ThingPropertiesMessage, ThingEventMessage, ThingParams

No replacement; they stay available in `mower_sdk.legacy.thing_models`.

### MowerAuthError and COMMAND_ERRORS

Nothing raised `MowerAuthError`, so an `except MowerAuthError` clause never
ran. Catch `MowerAuthRequiredError` in its place, in a clause before
`except MowerAPIError`: it is a `MowerAPIError`, which `MowerAuthError` was
not.

`COMMAND_ERRORS` has no replacement; it stays available in
`mower_sdk.legacy.errors`. A refused command raises `MowerAPIError` with the
cloud's `errorCode` as its `error_code`.

### The helpers in utils

No replacement in the SDK; they stay available in `mower_sdk.legacy.utils`.
The standard library does the same:

| Legacy helper | Standard library |
|---|---|
| `setup_logger(name, level)` | `logging.getLogger(name).setLevel(level)`, with a handler from the application's own logging configuration. The live path logs under `mower_sdk.api`, `mower_sdk.mqtt` and `mower_sdk.sdk`. |
| `parse_json(data)` | `json.loads(data)` |
| `timestamp_to_datetime(timestamp)` | `datetime.fromtimestamp(timestamp)` |
| `datetime_to_timestamp(dt)` | `int(dt.timestamp())` |

## Finding deprecated use, and removal

To find every deprecated use in a program, run it with
`python -W error::DeprecationWarning`, as the
[legacy README](../mower_sdk/legacy/README.md#how-the-old-names-still-work)
explains.

No removal date has been set: not for the legacy classes, not for the old
import paths and not for `MowerAPI`'s synchronous wrappers. For the legacy
classes and the old paths the question is decided when 0.2.0 is final, as the
[legacy README](../mower_sdk/legacy/README.md#why-the-code-is-frozen) says.

[0.2.0a1]: ../CHANGELOG.md#020a1---2026-09-27
[0.2.0a2]: ../CHANGELOG.md#020a2---2026-09-28
[0.2.0a3]: ../CHANGELOG.md#020a3---2026-09-30
[0.2.0a4]: ../CHANGELOG.md#020a4---2026-10-01
[0.2.0a5]: ../CHANGELOG.md#020a5---2026-10-02
[Unreleased]: ../CHANGELOG.md#unreleased

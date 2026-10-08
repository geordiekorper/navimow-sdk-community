# Endpoints

Everything `mower_sdk` sends to the Navimow cloud, call by call: the request,
what the cloud answers, and what the SDK hands back to the caller. The cloud
is reached over two transports: five REST endpoints, through `MowerAPI`, and
one MQTT broker, through `NavimowSDK` and the `NavimowMQTT` client under it.
How to use the calls is in the [README](../README.md#behaviour-notes) and how
the code behind them works in [architecture.md](architecture.md); this
document links there instead of repeating them.

A reply is described as the SDK reads it. Where a key "has been seen", a
mower's reply carried it; any other key named is one the SDK reads when the
cloud sends it.

## Summary

| The caller calls | The SDK sends | The caller gets |
|---|---|---|
| `MowerAPI.async_get_devices()`, `async_get_devices_raw()` | `GET /openapi/smarthome/authList` | A list of `Device`, or the entries as sent |
| `MowerAPI.async_get_mqtt_connection_info()`, `async_get_mqtt_user_info()` | `GET /openapi/mqtt/userInfo/get/v2` | An `MqttConnectionInfo`, or the reply's `data` as sent |
| `NavimowSDK.async_refresh_broker_credentials(api)` | The same request, through `api` | `True` once the credentials are applied, `False` inside the cooldown |
| `MowerAPI.async_get_device_status(id)`, `async_get_device_statuses(ids)`, `async_get_vehicle_status_raw(ids)` | `POST /openapi/smarthome/getVehicleStatus` | A `DeviceStatus`, a dict of them by device id, or the entries as sent |
| `MowerAPI.async_send_command(id, command)`, `async_send_command_receipt(id, command)` | `POST /openapi/smarthome/sendCommands` | The reply's `data`, or a `CommandReceipt` |
| `MowerAPI.async_query_command_results(devices)`, `async_get_command_result(id, cmd_num)` | `POST /openapi/smarthome/responseCommands` | The result entries as sent, or the one entry for the device |
| `NavimowSDK.connect()` | MQTT `CONNECT`, then one `SUBSCRIBE` per topic | Nothing; the outcome and the messages arrive through hooks and callbacks |
| `NavimowSDK.start_mowing(id)`, `pause(id)`, `return_to_base(id)`, `set_blade_height(id, height)` | MQTT `PUBLISH` to `navimow/{device id}/command`, only when the facade was built to allow it | Nothing; no mower has been seen to act on it |
| `NavimowSDK.disconnect()` | MQTT `DISCONNECT` | Nothing |
| `NavimowClient.async_connect()` | The device list when none was given, the connection information, one status poll, then the MQTT connect: `MowerAPI.async_get_devices()`, `async_get_mqtt_connection_info()`, `async_get_device_statuses()` and `NavimowSDK.connect()` | Nothing; the states arrive through `on_state` |
| `NavimowClient.async_poll(ids)`, and its poll task every `poll_interval` seconds (`REST_POLL_SECONDS` by default; no task with `poll_interval=None`) | The status request, through `MowerAPI.async_get_device_statuses()` | The statuses by device id, and the states through `on_state` |
| `NavimowClient.async_refresh_devices()` | The device list, through `MowerAPI.async_get_devices()` | The devices |
| `NavimowClient.async_refresh_broker_credentials()` | The credential request, through `NavimowSDK.async_refresh_broker_credentials(api)` | Its result |
| `NavimowClient.async_rebuild(reason)` | A new MQTT `CONNECT`, through `NavimowMQTT.rebuild()` | Nothing |
| `NavimowClient.async_set_token(token)` | No request of its own: the token goes to `MowerAPI.set_token()` and, when it changed, the bearer header to `NavimowSDK.update_mqtt_credentials()`, which a connected MQTT client uses at its next connect and a started but disconnected one applies by rebuilding and connecting at once | Nothing |
| `NavimowClient.async_disconnect()` | MQTT `DISCONNECT`, through `NavimowSDK.disconnect()` | Nothing |

`MowerAPI` also keeps five synchronous wrappers, listed at the end of the REST
part, and the legacy classes send through the calls above, as the last
section says. `NavimowClient` sends nothing of its own: each of its calls
goes through the `MowerAPI` or `NavimowSDK` call its row names. When a
`token_provider` is given, `async_connect()` (when it starts or restarts),
`async_poll()`, `async_refresh_devices()`, and, while the client is started
and not closed, `async_refresh_broker_credentials()` and `async_rebuild()`
await it first; `async_set_token()` and `async_disconnect()` do not.

## REST

### What every request carries

| Part | Value |
|---|---|
| URL | `base_url`, without trailing slashes, then the endpoint's path |
| `Authorization` header | `Bearer <token>`, the token given to `MowerAPI` or to `set_token()` |
| `requestId` header | A new UUID for each request |
| Body | JSON for the three `POST` endpoints; none for the two `GET` endpoints |
| Query string | None |

While no token is set (None or an empty string) no request is sent: the call
raises `MowerAuthRequiredError` with `status_code` 401 and `error_code`
`TOKEN_EXPIRED`. Each request is bounded by the client's timeout, which the
README's [REST note](../README.md#rest) describes.

### What every reply is put through

The cloud answers each endpoint with a JSON object, the envelope:

| Key | Holds |
|---|---|
| `code` | 1 for success; any other value refuses the request |
| `desc` | A text about the outcome, which the SDK puts in the error's message |
| `data` | The answer, in a shape of the endpoint's own |

The SDK judges a reply in the order of this table, and the first row that
fits decides:

| What comes back | What the caller gets | Set on the error |
|---|---|---|
| No reply: a timeout, or an aiohttp client error while sending or reading | `MowerTransportError` | `__cause__`; no `status_code` |
| HTTP 401 or 403, whatever the body | `MowerAuthRequiredError` | `status_code` |
| HTTP 500 or above, below 200, or 300 to 399 (a redirect aiohttp did not follow) | `MowerTransportError` | `status_code` |
| Any other HTTP status of 400 or above | `MowerAPIError` | `status_code` |
| HTTP 2xx whose body is not UTF-8, is empty or blank, is not JSON, or is JSON other than an object | `MowerTransportError` | `status_code`, and `__cause__` for a decoding error |
| An envelope with `code` 4005, or with `CODE_OAUTH_INFO_ILLEGAL` in its `desc` | `MowerAuthRequiredError` | `envelope_code` |
| An envelope with `code` 4001, or with "too frequent" or "circuit breaker" in its `desc` | `MowerRateLimitedError` | `envelope_code` |
| An envelope with any other `code` than 1, a missing one included | `MowerAPIError` | `envelope_code` |
| An envelope with `code` 1 | The endpoint's result, read from `data` as its section below says | |

An error raised for the HTTP status keeps the reply's body in its message, cut
at 500 characters. One raised for the envelope carries `desc` in its message;
`desc` is matched without regard to letter case, and `envelope_code` is `code`
as an int, or None when it cannot be read as one. Every error of this table
has an empty `results`, which only a refused command fills (see
[Commands](#commands)). A `data` key that is missing
is read as `{}` and an explicit null as None; each section says what its calls
make of the two.

Every call below can raise the errors of this table, so its section lists only
what the call adds. What each class means to a caller is in the README's
[REST note](../README.md#rest), and the class hierarchy in
[architecture.md](architecture.md#errors).

### Device list

`GET /openapi/smarthome/authList`, without a body.

The cloud returns the account's mowers as a list of objects in
`data.payload.devices`. An X430's entry carries `id`, `name`, `model` and
`firmware`, and nothing else (October 2026).

| Call | What the caller gets |
|---|---|
| `async_get_devices_raw()` | `list[dict]`: the entries as sent, unchanged, so a key `Device` does not read is still there |
| `async_get_devices()` | `list[Device]`: each entry read as the second table below says. An entry whose `id` is missing, null or empty is left out and logged as a warning, since a device cannot be addressed without one |

| The cloud returns | Both calls give |
|---|---|
| `devices` as a list | Its entries that are objects; any other entry is left out |
| `data`, `payload` or `devices` missing, null or of another type | An empty list |

How `Device` is filled from an entry:

| `Device` field | Aliases, in order of preference | Value if undefined |
|---|---|---|
| `id` | `id` | `""` |
| `name` | `name` | `""` |
| `model` | `model`, `deviceModel` | `""` |
| `firmware_version` | `firmware_version`, `firmware`, `firmwareVersion` | `""` |
| `serial_number` | `serial_number`, `serialNumber` | `""` |
| `mac_address` | `mac_address`, `macAddress` | None |
| `online` | `online`, `isOnline` | False |
| `extra` | `extra` | None |

The first alias that is present decides, so an explicit null or empty value
is kept.

`Device` has three more fields, and their keys are not Navimow's.
`productKey`, `deviceName` and `iotId` are how Alibaba Cloud IoT Platform
identifies a device, and upstream modelled the SDK on that platform.
The same assumption left the `Thing` message envelopes now in
[`mower_sdk.legacy.thing_models`](../mower_sdk/legacy/README.md#what-is-here)
and the two ignored arguments of `NavimowMQTT.subscribe_all`. The Navimow
cloud has not been seen to send any of the three keys (an X430, October
2026), and nothing in the SDK reads the three fields except
`Device.to_dict()`.

| `Device` field | Aliases, in order of preference | Value if undefined |
|---|---|---|
| `product_key` | `productKey`, `product_key` | None |
| `device_name` | `deviceName`, `device_name` | None |
| `iot_id` | `iotId`, `iot_id` | None |

Here the first alias with a truthy value decides, so an empty value is
skipped; only when the last alias is itself present and empty does the field
hold that empty value. On the live cloud all three are None.

### MQTT connection information

`GET /openapi/mqtt/userInfo/get/v2`, without a body. The cloud allows about
one call a minute.

The cloud returns the broker's address and the MQTT credentials directly in
`data`, with no `payload` level:

| Key | Holds |
|---|---|
| `mqttHost` | The broker; seen as a `wss://` URL |
| `mqttUrl` | The WebSocket path; seen as `/mqtt/{user id}` |
| `userName` | The MQTT username, the account's number |
| `pwdInfo` | The MQTT password |
| `userId` | The same number as `userName`; not read |
| `ak` | Seen as null; not read |
| `subTopics` | Seen as `["mapChange", "realtime"]`; not read |

The two `subTopics` names name no topic the SDK could subscribe. On an X430
in October 2026 the broker refused `mapChange`, `realtime`,
`/downlink/vehicle/{device id}/mapChange`, `.../mapChange/state`,
`.../realtime/state` and `/downlink/user/{user id}/mapChange`, and granted
`/downlink/vehicle/{device id}/realtimeDate/mapChange`, `.../realtimeDate/+`
and `.../realtimeDate/#`. Nothing arrived on `realtimeDate/mapChange` in
five hours of state and location traffic, the lawn being mapped twice over
in that time. A connection whose only subscription matching the mower was
`realtimeDate/#` received nothing in three hours while the exact topics
delivered. `realtimeDate/+` was subscribed only beside the exact topics,
which brought one copy of each message, so whether it delivers anything of
its own was not isolated. A grant means nothing here: the broker also
granted the `state`, `event` and `attributes` topics of a device id that
does not exist.

| Call | What the caller gets |
|---|---|
| `async_get_mqtt_user_info()` | The reply's `data`, unchanged: `{}` when the reply has no `data` key, None when it is null |
| `async_get_mqtt_connection_info()` | An `MqttConnectionInfo`, read from `data` as the next table says |
| `NavimowSDK.async_refresh_broker_credentials(api, ...)` | A bool; the last table of this section |

How `MqttConnectionInfo` is filled:

| Field | Read from | Without it |
|---|---|---|
| `broker` | The host of `mqttHost`, else the host of an `mqttUrl` that is a full URL | `MowerAPIError` |
| `port` | The port of an `mqttUrl` that is a full URL, else the port of `mqttHost` | 443 |
| `ws_path` | `mqttUrl`: a path as given, with a leading slash added, or a full URL's path and query | `""` |
| `username` | `userName`, as text | None |
| `password` | `pwdInfo`, as text | None |
| `raw` | A copy of `data` | |

`async_get_mqtt_connection_info()` raises a plain `MowerAPIError`, with no
status or envelope code, when `data` is not an object (null included), when
it names no broker, or when `mqttHost` or `mqttUrl` cannot be read: a scheme
other than `wss`, a host and port that cannot be split, or a port outside 0
to 65535. The message names the key and not its value, which may carry an
account id or a token.

`NavimowSDK.async_refresh_broker_credentials(api, ...)` makes the same request
through `api.async_get_mqtt_user_info()` and applies the reply to the MQTT
client:

| Situation | What the caller gets |
|---|---|
| The call falls within `cooldown` seconds of the last attempt, a failed one included | `False`; no request is sent |
| The reply carries `userName`, `pwdInfo` or both | `True`: they are applied, with the broker host, port and path the reply names; a value the reply does not name is kept |
| The reply carries neither, or `data` is not an object | `MowerAPIError` |
| The request fails | The error [the reply calls for](#what-every-reply-is-put-through); `MowerRateLimitedError` when the call came too early for the cloud |
| `mqttHost` or `mqttUrl` cannot be read | `True`: the credentials are applied, the address is kept, and a warning is logged |
| The facade is bound to another event loop than the caller's | `RuntimeError`; no request is sent |

When to call it, and what applying the reply does to a live connection, is in
the README's [MQTT notes](../README.md#mqtt).

### Device status

`POST /openapi/smarthome/getVehicleStatus`, one request that names every
device asked about:

```json
{"devices": [{"id": "dev-1"}, {"id": "dev-2"}]}
```

The cloud returns one object per device in `data.payload.devices`. An X430's
entry carries `id`, `vehicleState` (a [raw state](#raw-states)),
`capacityRemaining` (a list of one object, `unit` `PERCENTAGE` and a numeric
`rawValue`) and `descriptiveCapacityRemaining` (a text such as `LOW`), and
nothing else (October 2026).

| Call | What the caller gets |
|---|---|
| `async_get_vehicle_status_raw(device_ids)` | `list[dict]`: the entries as sent, unchanged |
| `async_get_device_statuses(device_ids)` | `dict[str, DeviceStatus]`, keyed by device id. An entry without an id is left out, and a device the reply does not name has no item |
| `async_get_device_status(device_id)` | The device's `DeviceStatus` |

| Situation | What the caller gets |
|---|---|
| `device_ids` is empty | An empty list or dict; no request is sent |
| `devices` in the reply is a list | Its entries that are objects; any other entry is left out |
| `data`, `payload` or `devices` missing, null or of another type | An empty list or dict |
| `async_get_device_status()`: the reply has no status for the device, or the cloud answered HTTP 404 | `MowerAPIError` with `status_code` 404 and `error_code` `DEVICE_NOT_FOUND` |

How `DeviceStatus` is filled from an entry:

| `DeviceStatus` field | Read from | Without it |
|---|---|---|
| `device_id` | `device_id`, else `id` | `""` |
| `status` | The first truthy value of `status`, `state` and `vehicleState`, through the [raw states](#raw-states) table | `MowerStatus.UNKNOWN` |
| `battery` | The `rawValue` of a `capacityRemaining` entry whose `unit` is `PERCENTAGE`, in any letter case; else the first `rawValue` in `capacityRemaining` that can be read; else `battery`. Each is read as an int | None |
| `error_code` | `error_code`, as a `MowerError` | `MowerError.NONE`; `UNKNOWN` for a code the enum lacks |
| `position`, `error_message`, `mowing_time`, `total_mowing_time`, `signal_strength`, `timestamp` | The key of the same name, as given | None |
| `extra` | The entries of the entry's own `extra`, every key no field reads, and `vehicleState`, `capacityRemaining` and `descriptiveCapacityRemaining` as sent | None |

A battery value outside 0 to 100 passes through unchanged.

### Raw states

The REST status and the MQTT state channel name the mower's state with the
same raw values. `MowerStatus` and what its values mean are in the README's
[Mower states](../README.md#mower-states).

| The cloud sends | The SDK gives |
|---|---|
| `isDocked` | `docked` |
| `isIdle`, `isIdel`, `Self-Checking`, `Self-checking` | `idle` |
| `isRunning` | `mowing` |
| `isPaused` | `paused` |
| `isDocking` | `returning` |
| `isMapping` | `mapping` |
| `inSoftwareUpdate` | `updating` |
| `Offline`, `offline` | `offline` |
| `Error`, `error`, `isLifted` | `error` |
| A `MowerStatus` value itself, such as `mowing` | That value |
| Any other text | REST: `MowerStatus.UNKNOWN`. MQTT: the text as sent |
| No state, or a value that is not text | REST: `MowerStatus.UNKNOWN`. MQTT: `"unknown"` |

The raw value stays with the model: a REST entry's `vehicleState` in
`DeviceStatus.extra`, and a state message's in `metrics["raw_state"]` when the
table changed it.

### Commands

`POST /openapi/smarthome/sendCommands`, one request for one device and one
command:

```json
{
  "commands": [
    {
      "devices": [{"id": "dev-1"}],
      "execution": {
        "command": "action.devices.commands.StartStop",
        "params": {"on": true}
      }
    }
  ]
}
```

| `MowerCommand` | `execution.command` | `execution.params` | What the mower does, as observed |
|---|---|---|---|
| `START` | `action.devices.commands.StartStop` | `{"on": true}` | From the dock: starts a one-time mowing; from paused: resumes; it cannot choose a zone. While mowing: `alreadyInState`; at 10 % battery: `lowBattery` |
| `STOP` | `action.devices.commands.StartStop` | `{"on": false}` | Pauses the task, at once; the app shows "paused"; does not end it. On the dock: accepted, the mower beeps, nothing else |
| `PAUSE` | `action.devices.commands.PauseUnpause` | `{"on": false}` | Pauses, within about 30 seconds. On the dock: accepted, nothing happens |
| `RESUME` | `action.devices.commands.PauseUnpause` | `{"on": true}` | Resumes, within about 30 seconds. From the dock: starts a one-time mowing |
| `DOCK` | `action.devices.commands.Dock` | Left out | Returns to the dock, from mowing or paused, which can take minutes. On the dock: `alreadyInState` |

The effects are an X430's, October 2026. A command the cloud accepts can
still be refused by the mower with no sign of it on this API: a `START`
after dark with "Mowing at night" switched off was a `SUCCESS` with a
`cmdNum`, its [result](#command-results) went to 3, the mower stayed on the
dock, and only the app showed a notification.

The cloud returns one result object per command in `data.payload.commands`,
beside a `requestId` in `data`. A result is (an X430, October 2026):

```json
{"devices": [{"id": "dev-1", "cmdNum": "<command number>"}], "status": "SUCCESS", "errorCode": null}
```

`cmdNum` is set on a `SUCCESS` and null on an `ERROR`; `errorCode` is null on
a `SUCCESS` and a text on an `ERROR`. The codes seen are `alreadyInState` (a
start while mowing) and `lowBattery` (a start at 10 %). The SDK reads
`status`, `errorCode` and `cmdNum`. The first row of this table that fits any
of the results decides:

| The cloud returns | `async_send_command()` | `async_send_command_receipt()` |
|---|---|---|
| `data` that is null or not an object | `MowerTransportError` | `MowerTransportError` |
| A result with `status` `ERROR` and an `errorCode` other than `alreadyInState` | `MowerAPIError` with that `errorCode` as `error_code`, `COMMAND_FAILED` when the result has none, and every result object of the reply as `results` | The same error |
| A result with `status` `ERROR` and `errorCode` `alreadyInState` | `data`, unchanged | A `CommandReceipt` with the verdict `ALREADY_IN_STATE` |
| A result with `status` `SUCCESS` | `data`, unchanged | A `CommandReceipt` with the verdict `ACCEPTED` |
| No result that says either: `payload` or `commands` missing, null or of another type, an empty list, or no entry that is an object | `data`, unchanged | A `CommandReceipt` with the verdict `UNKNOWN` |

A `command` that is not one of the five members raises `MowerAPIError` with
`error_code` `INVALID_COMMAND`, and no request is sent.

The `CommandReceipt`:

| Field | Holds |
|---|---|
| `device_id`, `command` | The device and the `MowerCommand` the call was made with |
| `verdict` | A `CommandVerdict`, as the table above says |
| `command_number` | The `cmdNum` of a `SUCCESS` result, found by searching `data` at any depth for `cmdNum`, `cmd_num`, `commandNum`, `command_num`, `commandNumber` or `command_number`; None without one, as after an `ERROR` result. [Command results](#command-results) takes it |
| `results` | The result objects of `data.payload.commands`, as a tuple |
| `accepted`, `already_in_state` | Whether the verdict is `ACCEPTED`, or `ALREADY_IN_STATE` |

`ACCEPTED` is the cloud's answer and not the mower's;
[architecture.md](architecture.md#commands) says what a caller does with each
verdict and with the two errors.

### Command results

`POST /openapi/smarthome/responseCommands`, with the targets as the caller
gave them:

```json
{"devices": [{"id": "dev-1", "cmdNum": "7"}]}
```

The cloud returns one entry per target in `data.payload.devices`, beside a
`requestId` in `data` (an X430, October 2026):

```json
{"id": "dev-1", "cmdNum": "<command number>", "status": 2}
```

A target without a `cmdNum` gets an entry whose `cmdNum` and `status` are
both null, so the query needs the `command_number` of a `CommandReceipt`.
With one, `status` is an integer: 2 at once and 3 within 30 seconds, for
every accepted command, whether or not the mower did anything (a pause sent
while charging went 2 then 3 and changed nothing, and so did a start the
mower refused). No other value has been seen, and a refused command has no
`cmdNum` to query. The result is kept only briefly: it still read 3 two
minutes after the command and was null for every command older than about
half an hour.

| Call | What the caller gets |
|---|---|
| `async_query_command_results(devices)` | `data.payload.devices` as sent, its entries unread. The `devices` argument is sent unchecked |
| `async_get_command_result(device_id, cmd_num=None)` | The first entry that is an object whose `id` equals `device_id`, else None. The query is `{"id": device_id}`, with `cmdNum` added when `cmd_num` is given; without it the entry says nothing |

| Situation | What the caller gets |
|---|---|
| The `devices` argument is empty | An empty list; no request is sent |
| `payload` or `devices` missing, null or of another type, or the reply has no `data` key | An empty list, or None from `async_get_command_result()` |
| `data` that is null or not an object | `MowerTransportError` |

### The synchronous wrappers

| Wrapper | Runs |
|---|---|
| `get_devices()` | `async_get_devices()` |
| `get_mqtt_user_info()` | `async_get_mqtt_user_info()` |
| `get_device_status(device_id)` | `async_get_device_status(device_id)` |
| `send_command(device_id, command)` | `async_send_command(device_id, command)` |
| `query_command_results(devices)` | `async_query_command_results(devices)` |

Each emits a `DeprecationWarning` and then runs its counterpart in
`asyncio.run()`: the same request, the same result and the same errors.
Called inside a running event loop it raises `RuntimeError`, and no request
is sent.

## MQTT

### Connecting

`NavimowSDK.connect()` starts the connection on paho's network thread and
returns at once.

| Part of the connection | Value |
|---|---|
| Transport | With a WebSocket path, MQTT over WebSocket with TLS, which is what `NavimowSDK.from_connection_info()` builds; without one, MQTT over TCP, with TLS only for a `wss://` broker |
| WebSocket upgrade headers | `auth_headers`; `from_connection_info()` sets `Authorization: Bearer <access token>` from its `access_token` |
| Client id | `web_{username}_{ten random hexadecimal characters}`, with `unknown` in place of a missing username. A rebuilt client has a new one |
| Username and password | The credential reply's `userName` and `pwdInfo`, sent only when both are set |
| Keepalive | `keepalive_seconds`, at least 30 |

| The broker answers | What the SDK does with it |
|---|---|
| It accepts the connection | Subscribes the topics, then schedules the `on_connected` and `on_ready` hooks and a `connected` connection event |
| It refuses the connection | Schedules the `on_connect_fail` hook and a `connect_failed` event, with the reason `refused: <reason code> (<its value>)` |
| Nothing: a network failure, or a bearer token refused at the WebSocket upgrade | The same hook and event, with the reason `connection failed before CONNACK` |
| The connection ends | Schedules the `on_disconnected` hook and a `disconnected` event, with the reason `requested` or paho's reason code |

The hooks are attributes of `sdk.mqtt`, which also keeps a count and a time
for each of the three outcomes and the latest reason of a failure and of a
disconnect. The README's [MQTT notes](../README.md#mqtt) describe the
connection event and the keepalive, and
[architecture.md](architecture.md#connection-lifecycle) the reconnects, the
credential updates and the rebuild, which connect in the same way.

### Subscriptions

On every accepted connection the SDK sends one `SUBSCRIBE` per topic, at QoS
0:

| Topic | Subscribed |
|---|---|
| `/downlink/vehicle/{device id}/realtimeDate/state` | Always |
| `/downlink/vehicle/{device id}/realtimeDate/event` | Always |
| `/downlink/vehicle/{device id}/realtimeDate/attributes` | Always |
| `/downlink/vehicle/{device id}/realtimeDate/location` | With `subscribe_location=True` |
| Each topic of `extra_topics`, as given | With `extra_topics` |

The device topics are subscribed once for each device in `records`. With no
device id known, the device level is the `+` wildcard, which the broker has
granted and never delivered on: on an X430 in October 2026 the four `+`
filters were granted and brought nothing in a quarter of an hour in which the
exact topics beside them brought state and location messages, and a `#`
under the device's `realtimeDate` level did the same for three hours. A
device-scoped `#` is refused. Only an exact topic delivers: give the devices.

The broker grants or refuses each topic. The answer is kept per topic in
`sdk.mqtt.subscription_results` and passed to the `on_subscribe` hook; the
README's [MQTT notes](../README.md#mqtt) list the values.
`sdk.mqtt.unsubscribe_all()` sends one `UNSUBSCRIBE` for each of the same
topics, and the broker's answer to it is not read.

### What the subscriptions bring back

| A message on | Goes to the callbacks of | As |
|---|---|---|
| Any topic, an extra topic included | `sdk.on_raw()` | `(topic, payload)`, the bytes as received |
| A topic that names a device and a channel | `sdk.on_message_seen()` | `(device_id, channel, received_at)` |
| A device's `state` topic | `sdk.on_state()` | A `DeviceStateMessage`; the latest is `sdk.get_cached_state(device_id)` |
| A device's `event` topic | `sdk.on_event()` | A `DeviceEventMessage`; not cached |
| A device's `attributes` topic | `sdk.on_attributes()` | A `DeviceAttributesMessage`; the latest is `sdk.get_cached_attributes(device_id)` |
| A device's `location` topic | `sdk.on_location()` | One `DeviceLocationMessage` per entry applied; the merged record is `sdk.get_cached_location(device_id)` |
| A `state` or `location` message that was not applied or carried an unknown field, or an `event` or `attributes` payload that is not a JSON object | `sdk.on_rejected()` | A `RejectedMessage` |
| Another channel of a device | None of the typed callbacks | |

Which messages are rejected, and how a message travels from the broker to a
callback, is in
[architecture.md](architecture.md#from-a-broker-message-to-a-callback). A
consumer that sets `sdk.mqtt.on_message` itself receives the payload the
README's [MQTT notes](../README.md#mqtt) describe under "Payload bytes".

A state, event or attributes message also carries `raw`, the payload as
decoded, `received_at`, the UTC receipt time, and `original`, the bytes as
the mower sent them. Its `device_id` is the payload's, else the topic's.

**State.** A JSON object. The keys it has been seen to carry are `state`,
`status`, `vehicleState`, `battery`, `capacityRemaining` and `timestamp`; an
X430 sends `state`, `battery` and `timestamp` (October 2026). The channel
sends partial messages.

| `DeviceStateMessage` field | Read from | Without it |
|---|---|---|
| `state` | The first truthy value of `state`, `status` and `vehicleState`, through the [raw states](#raw-states) table | `"unknown"` |
| `battery` | As the REST status reads it | None |
| `timestamp` | `timestamp`, as given, in seconds or milliseconds | None |
| `signal_strength`, `position`, `error` | The key of the same name, as given; no mower has been seen to send them | None |
| `metrics` | A copy of `metrics`, with the raw state added under `raw_state` when the table changed it | None |

**Event.** A JSON object. No message has been seen on this channel or on
`attributes`: on an X430 in October 2026, through five hours in which the
state channel reported every change, nothing came on either for a lift, the
stop button, two mappings of the lawn, settings changed in the app, a start
the mower refused, or a protective shutdown, though the app reported several
of these as notifications.

| `DeviceEventMessage` field | Read from | Without it |
|---|---|---|
| `type` | `type` | `"system"` |
| `event` | `event` | `""` |
| `timestamp`, `level`, `message`, `params` | The key of the same name, as given | None |

**Attributes.** A JSON object.

| `DeviceAttributesMessage` field | Read from | Without it |
|---|---|---|
| `attributes` | `attributes` | `{}` |

**Location.** A JSON array of entries, each with an integer `type` and, for
the first three types, the mower's `time` in epoch milliseconds. Numbers may
arrive as text.

| Entry `type` | Keys read | `DeviceLocationMessage` fields they fill |
|---|---|---|
| 1, a pose | `postureX`, `postureY`, `postureTheta`, `vehicleState` | `x`, `y`, `theta`, `vehicle_state` |
| 2, a task | `currentMowBoundary`, `currentMowProgress`, `mowingPercentage`, `subtotalArea`, `mowingWeekArea`, `action`, `subAction`, `mowStartType`, `mapWorkPosition` | `current_zone`, `route_progress`, `mowing_percentage`, `area_m2`, `week_area_m2`, `action`, `sub_action`, `mow_start_type`, `map_work_position` |
| 3, a target | `partitionIds` | `partition_ids` |
| 4, a delay | `taskDelay` | `task_delay` |

Each message also carries `device_id`, the topic's, `entry_type`, `timestamp`
(the entry's `time`), `received_at`, `raw`, the entry as decoded, and
`location`, the device's `DeviceLocation` as it stood after the entry. A
pose's `vehicleState` is a code, which the message's `status` gives as a
`MowerStatus`:

| Pose code | `status` |
|---|---|
| 1 | `docked` |
| 2 | `charging` |
| 3 | `paused` |
| 4 | `mowing` |
| 5 | `returning` |
| 6 | `mapping` |
| Any other | `unknown` |
| None; a lifted mower sends none | None |

How the entries are merged into the record is in
[architecture.md](architecture.md#location).

### Commands over MQTT

The four command methods of `NavimowSDK` publish to
`navimow/{device id}/command`, at QoS 0, a JSON object with a new id for each
call:

```json
{
  "id": "cmd-<uuid4>",
  "device_id": "dev-1",
  "command": "set_blade_height",
  "params": {"height": 40}
}
```

| Method | `command` | `params` |
|---|---|---|
| `start_mowing(device_id)` | `start_mowing` | `{}` |
| `pause(device_id)` | `pause` | `{}` |
| `return_to_base(device_id)` | `return_to_base` | `{}` |
| `set_blade_height(device_id, height)` | `set_blade_height` | `{"height": height}`, the height as given |

Nothing comes back: the publish goes out at QoS 0, so the broker's
acknowledgement is neither asked for nor read, and no mower has been seen to
act on the message. On an X430 in October 2026 all four were published,
while it mowed and while it charged; it did nothing within three minutes of
each, the app showed no change, and the connection stayed up.

| Situation | What the caller gets |
|---|---|
| The facade was built without `allow_experimental_mqtt_commands=True`, the default | `MowerUnsupportedOperationError`; nothing is published |
| Allowed, and the client is not connected | `RuntimeError`; a connect is started first, so a later call can succeed |
| Allowed, and the client is connected | None, once the message is handed to paho |

`sdk.mqtt.publish_command(device_id, payload)` publishes any dict to the same
topic. It is not gated, does not check the connection and returns None. The
README's [command notes](../README.md#commands) say why these commands are off
by default.

## The legacy classes

The classes in `mower_sdk.legacy` reach the cloud through the calls above,
with one exception, the legacy MQTT client:

| Legacy call | What it sends |
|---|---|
| `MowerClient`'s device, status and command methods | The `MowerAPI` request each forwards to; [migrating.md](migrating.md#mowerclient) pairs them |
| `MowerClient.async_refresh_mqtt_info()` | `GET /openapi/mqtt/userInfo/get/v2`, through `async_get_mqtt_user_info()`; it returns the reply's `data` |
| `Navimow.initiate_cloud_connection(devices)` | The same request, then the `CONNECT` and the subscriptions of a `NavimowMQTT` |
| `MowerClient.async_subscribe_device_updates(device_id)` | The same request, then what `MowerMQTT` sends |
| `MowerMQTT.connect()` | A `CONNECT` with a client of its own; no subscription |
| `MowerMQTT.async_subscribe_device(device_id)` | A `CONNECT` with another client of its own, then a `SUBSCRIBE` to `device/{device id}/status` and to `device/{device id}/event` |
| `MowerMQTT.subscribe_device(device_id)` | The same two `SUBSCRIBE`s on the client `connect()` made, calling `connect()` first only when there is none yet |

The two `MowerMQTT` topics are placeholders and not the form the cloud's
topics have. The [legacy README](../mower_sdk/legacy/README.md#what-is-here)
says what replaces each class.

# Changelog

Notable changes to navimow-sdk-community. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/). The import name is `mower_sdk`
throughout. From 0.2.0a5 on a version's entries go under five headings only,
in this order: Changed, Added, Deprecated, Removed, Fixed. Earlier versions
keep the headings they were released with.

## [Unreleased]

### Changed

- `NavimowMQTT`'s warning for a subscription made with no device ids now
  says that the broker grants the `+` wildcard filters and has never been
  seen to deliver a message on them, and asks for the devices in `records`.
  On an X430 in October 2026 the four filters were granted and brought
  nothing while the exact topics beside them delivered, so a facade built
  with an empty `records` is a dead feed that `subscription_results` reports
  as granted. The behaviour is unchanged: the topics are still subscribed.

### Added

- `MowerAPIError.results`: the reply's per-command result dicts, as a tuple,
  on the error that `async_send_command`, `async_send_command_receipt` and
  `send_command` raise for a command the cloud refused (an `ERROR` result
  other than `alreadyInState`). A refusal used to keep only the `errorCode`,
  as `error_code`; the result objects, which an accepted command's
  `CommandReceipt.results` keeps, were lost. The tuple is read as the receipt
  reads it: every dict entry of `data.payload.commands`, in order. It is
  empty on every other `MowerAPIError`, each raised before any result is
  read, and is not part of `str()`. The constructor takes it as a keyword
  with that default, so existing calls are unchanged.

## [0.2.0a5] - 2026-10-02

### Changed

- Package metadata. The classifiers list Python 3.13 and 3.14, which the
  checks have covered all along. A maintainer is named beside upstream's
  authors. The `dev` extra installs what the checks use (pytest with
  pytest-asyncio, nox, pre-commit, ruff and mypy) and no longer black, which
  nothing ran; the unused `[tool.black]` section is gone. Running the test
  suite now needs pytest-asyncio 1.0 or later and pytest 8.4 or later.

### Added

- **Type information.** The package ships a `py.typed` marker, so a type
  checker reads the SDK's annotations in a consumer's code. The live path
  passes mypy in strict mode; `mower_sdk.legacy` is not checked. Two
  annotations became more exact on the way: the hooks of `NavimowMQTT`
  (`on_connected`, `on_message` and the others) are annotated as returning a
  coroutine, which is what the client has always required of them, and the
  paho callbacks carry paho's types.

### Fixed

- `NavimowSDK` constructed without `loop=` while the loop set as current for
  the thread is closed raises `ValueError` saying that the current loop is
  closed. It used to say that the `loop=` given was closed, although none was.
- `NavimowMQTT.rebuild()` called from inside a running event loop other than
  the bound one raises `RuntimeError` before anything changes. It used to
  install the new paho client, tear the old one down and only then raise, from
  the final connect, leaving the new client unstarted with nothing to retry.
  The same holds for `update_credentials()` and
  `NavimowSDK.update_mqtt_credentials()` on the paths that rebuild: a refused
  update stores nothing.
- `MowerAPI`: a successful reply whose `data` is null, or not an object,
  raises `MowerTransportError` from `async_send_command`,
  `async_send_command_receipt`, `send_command`, `async_query_command_results`,
  `query_command_results` and `async_get_command_result`. The command calls
  used to let an `AttributeError` out, kept from upstream, and the result
  query an `AttributeError` for a null `data` or `payload` or, for a null
  `devices`, a `TypeError` from `async_get_command_result`; none was caught by
  `except MowerAPIError`. The cloud may still have acted on the command, as
  after a timeout. A result-query reply whose `payload` or `devices` is null
  or not of the expected type is an empty list, as one without the key is.
  The reads (`async_get_devices`, `async_get_device_statuses`,
  `async_get_mqtt_user_info`) are unchanged: empty results for a null `data`.

## [0.2.0a4] - 2026-10-01

Tagged and tested, never uploaded: no index has this version, and its
changes ship with 0.2.0a5.

### Changed

- **`async_refresh_broker_credentials` follows a broker that moved.** When the
  credential reply names a broker host, port or WebSocket path (read as
  `MqttConnectionInfo` reads them) that differs from the client's, the client
  is rebuilt on the new address, connected or not, dropping a live connection
  (`last_rebuild_reason` is "broker changed"). A value the reply does not name
  is kept; a broker it names but that cannot be read is logged and kept, and
  the credentials are applied as before. `NavimowMQTT.rebuild()`,
  `NavimowMQTT.update_credentials()` and `NavimowSDK.update_mqtt_credentials()`
  take keyword-only `broker`, `port` and `ws_path` (None keeps the current
  value; a change always rebuilds). A WebSocket path's query is no longer shown
  in log lines.
- `MowerAPI` with no token (empty or `None`) raises `MowerAuthRequiredError`
  instead of a plain `MowerAPIError`, still before any request and still with
  `status_code` 401 and `error_code` `TOKEN_EXPIRED`, so a consumer that
  branches on the class asks for sign-in rather than retrying.

### Added

- `MowerAPI.async_get_devices_raw()`, the device-list entries as the cloud
  sent them, including the fields `Device.from_dict()` does not read.
- `NavimowMQTT.subscription_results`, what the broker answered for each topic
  subscribed since the latest connect (`pending`, `granted`,
  `refused: <reason>` or `not sent: <error>`), with a warning logged for a
  refused topic and an optional `on_subscribe(topic, granted, codes)` hook. A
  refused subscription used to be invisible: its data simply never arrived.
  Both are kept across `rebuild()`.
- The payload readers the models use, public so a consumer that keeps raw
  REST or MQTT payloads reads them the same way instead of copying them:
  `RAW_STATE_TO_CANONICAL` (read-only), `canonical_state(raw)` (what
  `DeviceStateMessage.state` holds), `mower_status_from_raw(raw)` (what
  `DeviceStatus.status` holds) and `battery_from_payload(data)` (what both
  models' `battery` holds; None for anything but a dict).
- `ConnectionEvent` and `NavimowMQTT.on_connection_event(event)`: each
  connect, disconnect and connect failure with the client id it came from,
  the reason, the UTC time and the rebuild count, captured when it happened.
  The zero-argument `on_connected` and `on_disconnected` are unchanged; read
  from inside them, `last_disconnect_reason` and `client_id` may already
  belong to a client a `rebuild()` put in place.
- The payload bytes exactly as the mower sent them, beside the re-encoded
  form with `device_id` added: `NavimowMQTT.on_message` receives a re-encoded
  object payload as `mower_sdk.mqtt.ReceivedPayload`, a `bytes` subclass equal
  to what it received before, whose `original` holds the wire bytes; and
  `DeviceStateMessage`, `DeviceEventMessage`, `DeviceAttributesMessage` and
  `RejectedMessage` gain `original` (not compared, not in `to_dict()`). A
  consumer that stores messages as sent no longer has to pair `on_raw` with
  the typed callbacks, whose order is not promised.
- `SkippedLocationEntry`, one location entry that was not applied: its type,
  its time as read, the reason (`stale`, `implausible_time`, `placeholder`,
  `unparsable` or `unknown_type`) and the fields it carried, read as an applied
  entry's would be. `ParsedLocation.skipped` and `RejectedMessage.skipped` list
  them, so a consumer can tell which entry of a mixed message was skipped and
  keep a late reading marked as late. The applied entries and the record are
  unchanged.
- On `NavimowMQTT`: `last_disconnected_at` and `last_connect_failed_at`, the
  UTC time of the disconnect and the connect failure whose reasons
  `last_disconnect_reason` and `last_connect_fail_reason` hold, and
  `last_connected_monotonic`, the time of the last accepted connect on
  `time.monotonic()`, the clock `last_message_age()` uses. All three are set in
  paho's thread with the reasons and counters, whether or not a hook is set,
  and are None until the first such event.
- `parse_topic(topic)` in `mower_sdk.mqtt` and the package, the device id and
  channel of a cloud topic (`(None, None)` for any other topic); `_parse_topic`
  and `NavimowMQTT._parse_topic` remain as the same function.
- `on_message_seen(device_id, channel, received_at)`: an async hook on
  `NavimowMQTT` and a callback registration on `NavimowSDK`, called for every
  message whose topic names a device and a channel, whatever its payload, with
  the UTC time `last_message_at()` records for it. For a consumer that only
  needs to know that a message arrived and parsed the topic again in `on_raw`.
- `MqttConnectionInfo` in `mower_sdk.models` and the package, the MQTT
  credential reply read once: `broker` (a host name, from `mqttHost` or a full
  `mqttUrl`), `port` (a full `mqttUrl`'s, else `mqttHost`'s, else 443),
  `ws_path` (`mqttUrl`'s path and query, `""` without one), `username` and
  `password` (text when present). `from_dict` raises `MowerAPIError` for a
  reply that names no broker, a scheme other than `wss` or a port that is not
  a number. `MowerAPI.async_get_mqtt_connection_info()` returns one, and
  `NavimowSDK.from_connection_info(info, *, access_token, records,
  auth_headers=None, **options)` builds the facade from it: TLS over
  WebSocket, with `Authorization: Bearer <access_token>` merged into
  `auth_headers`. It refuses a reply without a WebSocket path and options that
  name what the info supplies. The README's quick example uses it.
- `mower_sdk.watchdog`, with `MqttWatchdog`, `WatchInput` and `RebuildRequest`
  also exported from the package: finds an MQTT connection that is up but no
  longer delivering, from the facade's caches and the client's message times.
  `after_poll(inputs)` asks for a rebuild when a current REST reading (taken at
  least 120 s after the last accepted MQTT state report arrived) disagrees
  with that report in a state the state channel reports, once per report;
  `check_silence(inputs)` asks when a mower with a timestamped pose that is
  shown or reported mowing or returning (never while shown mapping) has sent
  no location message for 180 s on a connected client that subscribes the
  location channel (`subscribe_location=True`). Nothing is asked
  before the first connect; `acknowledge(request)` starts a 300 s debounce
  covering both rules. No timer, no I/O: the consumer schedules the checks
  and rebuilds. The thresholds are keyword arguments and module constants.
- The dock's position on the location record. `LocationDecoder` treats every
  applied pose whose code is docked (1) or charging (2) as a sample of the
  dock, and `DeviceLocation` gains `dock_x`, `dock_y`, `dock_theta`, `dock_at`
  (the mower time of the estimate's latest pose, None for an untimed one) and
  `dock_samples` (0 without an estimate), after the existing fields and before
  `marks`, carried by `to_dict()` and `from_dict()`. The estimate is a capped
  mean (`dock_max_samples`, 200): at the cap a dock moved by less than
  `dock_move_distance_m` (1 m) is followed slowly, about 63 % of the way after
  200 more docked poses. A pose farther than that never enters the mean;
  `dock_move_samples` (3) such poses in a row, each within that distance of
  their running mean, replace the estimate.
  The three are keyword-only arguments of `LocationDecoder` and, with
  `DOCK_VEHICLE_STATES`, constants of `mower_sdk.location`.
- `target_zone(location, status)` in `mower_sdk.location` and the package: the
  first partition id of the target report, else `TargetZone.ALL` for an empty
  report while the shown status is mowing or paused (`MOW_ALL_STATES`) and
  `TargetZone.NONE` otherwise, None before any target report. An inference:
  the mower sends the same empty report for a mow-all task as when idle, and a
  charging break during a mow-all task reads `NONE`.
- README: "Threaded applications", a tested recipe for applications that are
  not asyncio (WSGI apps, scripts, CLI tools): the SDK's loop on a thread of
  its own, REST through `run_coroutine_threadsafe`, callbacks handed over from
  the loop, and which calls block and must stay off the loop.

### Fixed

- `MowerAPI.async_get_devices()` raised `AttributeError` or `TypeError` for a
  successful reply whose `data`, `payload` or `devices` was null or of another
  type, and for an entry that is not an object; it returns an empty list for
  such a reply and leaves such an entry out. An entry without an `id` (missing,
  null or empty) is left out and logged at warning level instead of becoming
  `Device(id="")`.

## [0.2.0a3] - 2026-09-30

The MQTT transport, the location channel, the models and the REST errors.
Nothing upstream published is removed; the live path behaves differently only
as listed here. `UPSTREAM.md` maps the pieces taken from the randax and
AndiHOK91 forks to their origins.

### Compatibility

An integration that does any of these must change with this release:

- overrides or wraps `NavimowMQTT._on_connect` or `_on_disconnect` with paho's
  callback API version 1 signatures (see "paho-mqtt 2.1 is required");
- classifies a failed REST call by looking for an `aiohttp.ClientError` or a
  `TimeoutError` in the exception or its cause: test for
  `MowerTransportError` instead, which also covers an HTTP 5xx and a 2xx
  reply that is not a JSON object;
- maps `MowerStatus` values through a table: `MAPPING`, `UPDATING` and
  `OFFLINE` are new, and `isMapping`, `inSoftwareUpdate` and `offline` no
  longer come out as mowing, paused and unknown;
- matches on the text of an error message (now English);
- reaches `NavimowSDK._mqtt` or reads `NavimowSDK._loop`: use `sdk.mqtt` and
  `sdk.loop`.

### Changed

- **paho-mqtt 2.1 is required.** The dependency is `paho-mqtt>=2.1,<3`, and
  `NavimowMQTT` builds its clients on paho's callback API version 2, so it no
  longer triggers paho's "Callback API version 1 is deprecated" warning (the
  0.2.0a2 notes said that warning was unchanged). `_on_connect` and
  `_on_disconnect` take the version 2 arguments `(client, userdata, flags,
  reason_code, properties=None)`. The legacy `MowerMQTT` keeps version 1 and
  paho's warning.
- **The MQTT keepalive defaults to 60 seconds** (was 2400) in `NavimowMQTT` and
  `NavimowSDK`. Idle links to the cloud die after about ten minutes without a
  FIN or DISCONNECT; a ping a minute keeps them alive and finds a dead one
  within about two minutes. `keepalive_seconds=2400` restores the old value.
- **REST errors say what kind of failure they are.** Three subclasses of
  `MowerAPIError`, so `except MowerAPIError` still catches every failed request:
  `MowerTransportError`, no usable reply (a timeout, a connection error, an
  HTTP 5xx, a status below 200 or a redirect that was not followed, or a 2xx
  whose body is not a JSON object), where a command's outcome is unknown
  rather than refused; `MowerAuthRequiredError`, HTTP 401 or 403, envelope
  code 4005, or `CODE_OAUTH_INFO_ILLEGAL` in the reply; `MowerRateLimitedError`,
  envelope code 4001, or "too frequent" or "circuit breaker" in the reply. The
  status decides for every non-2xx reply; a 2xx body is read as UTF-8 JSON
  whatever its content type. Three malformed replies that escaped as a raw
  `JSONDecodeError`, `UnicodeDecodeError` or `AttributeError` are now
  `MowerTransportError`, and a redirect's body is no longer read as an answer.
  The envelope's code is kept as `MowerAPIError.envelope_code`, outside
  `error_code` and `str()`. An HTTP error body is cut at 500 characters in the
  message. `MowerUnsupportedOperationError` gains a `message` attribute.
- **The runtime strings are English.** The eleven `ERROR_MESSAGES` values were
  Chinese; the keys are unchanged ("API request failed", "Command failed",
  "Device not found", and so on). `COMMAND_ERRORS` stays in the legacy module.
  The refusal of an MQTT command without a REST alternative no longer uses the
  blade-height wording for every such command.
- **`MowerStatus.MAPPING`, `UPDATING` and `OFFLINE`.** The raw states
  `isMapping`, `inSoftwareUpdate`, `Offline` and `offline` map to them instead
  of to mowing, paused and unknown.
- **`DeviceStatus.extra` keeps every unread key and no longer changes the
  caller's dict.** `DeviceStatus.from_dict` builds `extra` as a new dict: a
  copy of the payload's `extra`, every key no field reads, and the raw status
  keys it kept before. Before, it wrote those keys into the caller's own
  `extra` dict and dropped the rest.
- **Loop affinity.** A closed `loop=` raises `ValueError` in `NavimowMQTT` and
  `NavimowSDK`; `connect_async()` or `connect()` called from inside a running
  loop other than the bound one raises `RuntimeError`; a callback handed to a
  loop that closes at that moment is dropped (debug line) instead of raising on
  paho's thread. `NavimowSDK` no longer keeps its own `_loop`; `sdk.loop` reads
  the client's.
- **Rebuilds and reconnects.** A credential update while disconnected goes
  through `rebuild()`. Callbacks from a paho client that has been replaced are
  ignored. `connect_async()` does nothing while the current client's network
  thread runs (connected, connecting or retrying after a failure), so a
  repeated connect no longer resets paho's attempt in progress. `rebuild()`,
  `disconnect()`, a connect and a credential update run one at a time, so a
  call made while a rebuild runs on another thread waits for it. An
  empty-string username or password is now applied as a value.
- **The connection logs redact the client id, the account id and the
  WebSocket path.** The client id shows as `web_…_<suffix>`, the path as its
  first segment (`/mqtt/…`), and the username as `configured` or `not
  configured`. What is sent to the broker is unchanged.
- **Malformed payloads are reported.** A state, event or attributes payload
  that is not a JSON object, dropped silently before, is reported through
  `on_rejected` (see below).

### Added

- On `NavimowMQTT`: `on_connect_fail` (an async hook given a reason: "refused:
  ..." for a refused CONNACK, "connection failed before CONNACK" when paho's
  own connect-failure callback fires, as a bearer token refused at the
  WebSocket upgrade does); `last_connect_fail_reason`,
  `last_disconnect_reason` ("requested" or paho's reason text) and
  `last_connected_at`; the counters `connects`, `disconnects`,
  `connect_failures` and `rebuilds`, with `last_rebuild_reason`; `client_id`;
  `last_message_at(device_id, channel=None)` and
  `last_message_age(device_id, channel=None)`; `rebuild()`;
  `update_credentials(force_reconnect=True)`; `subscribe_location=`,
  `extra_topics=` (validated at construction, subscribed on every connect) and
  `on_raw(topic, payload)`, called for every message with its bytes.
- On `NavimowSDK`: `mqtt` and `loop`; `subscribe_location=` and
  `extra_topics=`; `on_raw()`, `on_location()`, `get_cached_location()`,
  `restore_location()` and `on_rejected()`; `reject_late_state=`, which drops a
  state message whose timestamp is implausible or older than the device's
  newest accepted one; `update_mqtt_credentials(force_reconnect=True)`; and
  `async_refresh_broker_credentials(api, ...)`, which fetches and applies the
  broker credentials at most once per 65 seconds and one call at a time (call
  it at startup and after a failed connect, never on a timer or a token
  refresh).
- The location channel: `mower_sdk.location` with `LocationDecoder` and
  `ParsedLocation`; `DeviceLocation`, the merged per-device record (pose, zone,
  route progress with `progress_percent` and `progress_source`, task, target,
  delay, and the high-water marks, with `to_dict()` and `from_dict()` for
  persisting it); `DeviceLocationMessage`, one decoded entry with the record as
  of that entry; `VEHICLE_STATE_TO_STATUS`; `mower_time_ms()`. The channel is
  off by default: several models never publish on it, and its payload is a
  movement trace.
- `RejectedMessage`, what `on_rejected` receives: channel, topic, device id,
  reason and reasons (`unparsable`, `implausible_time`, `unknown_type`,
  `unknown_field`, `stale`, `placeholder`), the bytes received and the
  receipt time.
- `raw` and `received_at` on `DeviceStateMessage`, `DeviceEventMessage` and
  `DeviceAttributesMessage`: `raw` is the payload as decoded, set by
  `from_dict()`; `received_at` is the UTC receipt time, set by `NavimowSDK`
  when it delivers the message. Both are None for a message built by hand (and
  `received_at` for one made by `from_dict()` directly), and both are left out
  of equality and `to_dict()`.
- `DeviceStateMessage.from_status()` and `DeviceStatus.from_state_message()`,
  the conversions between the REST status and the MQTT state message.
- `STATE_KNOWN_FIELDS` and `REST_STATUS_KNOWN_FIELDS`.
- `MowerAPI.async_get_vehicle_status_raw()`, the status entries as the cloud
  sent them.
- `MowerCommand`'s docstring says what each REST command does.
- A `UserWarning` at import when the upstream `navimow-sdk` distribution is
  listed beside `navimow-sdk-community` in the same environment: both install
  the `mower_sdk` package, and the message names both versions and says to
  uninstall both and install `navimow-sdk-community`. It fires only when this
  package's files are the ones loaded; when `navimow-sdk` was installed last,
  its files replaced these and nothing warns.

### Fixed

- A state message whose fields cannot be read (`metrics` sent as a number, a
  string or a list) raised in the message task and reached no callback; it is
  now reported to `on_rejected` as `unparsable` and neither applied nor cached.
- `MowerAPI.async_get_vehicle_status_raw()` raised `AttributeError` or
  `TypeError` for a successful reply whose `data`, `payload` or `devices` was
  null or of another type; it returns an empty list, as for a missing key.
  `async_get_device_statuses()`, which reads through it, returns an empty dict
  for such a reply, and `async_get_device_status()` raises its
  `DEVICE_NOT_FOUND` `MowerAPIError`, as for a reply without the device.
- `DeviceStateMessage.from_dict()` added `raw_state` to the caller's `metrics`
  dict; it now works on a copy and leaves the payload it was given unchanged.

## [0.2.0a2] - 2026-09-28

Two things: the legacy quarantine, and the first patches taken from other
forks, with the fixes users asked for. Nothing upstream published is deleted,
and every public name still imports from its old path. The live path
(`MowerAPI`, `NavimowMQTT`, `NavimowSDK`, the models, the errors) behaves
differently only as listed here, and each change that has a sensible way back
to upstream's behaviour names the parameter that restores it. `UPSTREAM.md`
maps every piece taken from another fork to its origin and author.

### Changed

- **REST requests time out after 20 seconds by default.** `MowerAPI` gains
  `request_timeout: float | None = 20.0`, applied to every request as
  `aiohttp.ClientTimeout(total=...)`. A request that times out raises
  `MowerAPIError`, with the `TimeoutError` as its `__cause__`, as an
  `aiohttp.ClientError` already did. Before, the caller's session decided, 300
  seconds in total for a bare aiohttp session. `request_timeout=None` passes
  no timeout and leaves the session's policy in force. The HTTP response body
  stays in the error text.
- **`battery` is `int | None`, and `capacityRemaining` comes first.** `DeviceStatus.battery`
  and `DeviceStateMessage.battery` are None when the payload carries no
  readable value (missing, unparsable, a bool or a non-finite float) instead
  of a false 0, and `to_dict` emits the None. Both readers now prefer
  `capacityRemaining`: its PERCENTAGE entry, then any entry whose `rawValue`
  parses, then the plain `battery` field. Values that changed: `True` gives
  None (was 1); a missing or unparsable value gives None (was 0); a JSON
  Infinity gives None (raised `OverflowError`); a payload carrying both keys
  takes `capacityRemaining` (`battery` won); a `capacityRemaining` list whose
  first entry does not parse is scanned further (only the first was tried).
  Out-of-range numbers still pass through. No parameter restores the old
  reader; a consumer can map None back to 0 for the missing case.
- **The MQTT command methods refuse by default.** `NavimowSDK.start_mowing`,
  `pause`, `return_to_base` and `set_blade_height` raise the new
  `MowerUnsupportedOperationError` unless the facade is constructed with
  `allow_experimental_mqtt_commands=True`. They publish to an MQTT command
  topic that the broker accepts and no mower has been seen to act on; every
  command that works goes over REST. The error names the alternative:
  `MowerAPI.async_send_command` for start, pause and dock; for blade height,
  that no supported call exists. `NavimowMQTT.publish_command` is not gated.
- **Credentials changed while connected are used by the next automatic
  reconnect.** `NavimowMQTT.update_credentials` (and
  `NavimowSDK.update_mqtt_credentials`) now sets the merged stored values on
  the live paho client with `username_pw_set` and `ws_set_options`, so a
  rotated password or bearer header takes effect at the next reconnect without
  a rebuild and without dropping the connection. Before, values changed while
  connected were only stored, and paho's automatic reconnect used the original
  ones. A partial update (password only, headers only) keeps the current
  username.
- **Construction outside a running event loop.** `NavimowMQTT` and
  `NavimowSDK` no longer call `asyncio.get_event_loop()` where it would create
  a loop: outside a running loop and with no current loop set it created an
  implicit loop on Python 3.11 to 3.13 (with asyncio's `DeprecationWarning`
  on 3.12 and 3.13) and raised `RuntimeError` on 3.14.
  The loop is the `loop=` given, else the loop running at construction, else
  the loop set as current with `asyncio.set_event_loop()` at that time (as
  before; on Python 3.11 to 3.13 it is read from the event loop policy's own
  slot, where the default policy and uvloop keep it, so a custom policy that
  keeps it elsewhere needs `loop=`), else the same two at the first
  `connect_async()` or `connect()`. A client constructed and connected with no
  running or current loop must be given `loop=` for its callbacks to be
  delivered; a callback that arrives while no loop is bound is now dropped
  with a warning and its coroutine closed, where before only a debug line and
  asyncio's "coroutine was never awaited" `RuntimeWarning` showed (a bound
  loop that has stopped keeps the debug line). paho 2.x's own warning about
  the callback API version at construction is unchanged.
- **`Device.from_dict` reads camelCase keys, and `firmware`.** `deviceModel`,
  `firmwareVersion`, `serialNumber`, `macAddress` and `isOnline` are read when,
  and only when, the snake_case key is absent, so an explicit empty snake_case
  value is kept. `firmware_version` also reads `firmware`, the key an X430's
  device list carries, before `firmwareVersion`.
- **One failing consumer callback no longer stops the others.** In
  `NavimowSDK`, a state, event or attributes callback that raises is logged
  with its traceback and the later callbacks still receive the message. Before,
  the exception escaped the scheduled task, the later callbacks were skipped,
  and asyncio reported "Task exception was never retrieved".
- **Legacy code moved to `mower_sdk.legacy`.** Seven modules moved whole:
  `client`, `cloud`, `device`, `event`, `navimow`, `state_manager` and `utils`
  are now `mower_sdk.legacy.<module>`. Three pieces were extracted from core
  modules: `MowerMQTT` (from `mower_sdk.mqtt`) is `mower_sdk.legacy.mqtt_v1`;
  `ThingParams`, `ThingStatusMessage`, `ThingPropertiesMessage` and
  `ThingEventMessage` (from `mower_sdk.models`) are
  `mower_sdk.legacy.thing_models`; `MowerAuthError` and `COMMAND_ERRORS` (from
  `mower_sdk.errors`) are `mower_sdk.legacy.errors`. Every old path still
  works: the seven vacated modules are shims that re-export the legacy module,
  and `mower_sdk`, `mower_sdk.mqtt`, `mower_sdk.models` and `mower_sdk.errors`
  serve the moved names lazily on first access. `import mower_sdk` loads no
  legacy module, and no core module imports `mower_sdk.legacy`. The full map
  is in `UPSTREAM.md`.
- **One `DeprecationWarning` per legacy module.** The first access to each
  legacy module through an old path, whether a shim import such as
  `import mower_sdk.client` or a lazy name such as `mower_sdk.MowerClient`,
  emits one `DeprecationWarning` per process, attributed to the caller's line;
  later accesses to the same legacy module, through any path, are silent. The
  message says that every name in that legacy module is deprecated. Importing
  `mower_sdk.legacy.<module>` directly never warns. A fresh
  `from mower_sdk import *` makes nine warning calls, one per legacy module
  behind the names in `__all__`. A legacy module is recorded as warned only
  when its warning completes, so with an error filter active before that,
  every deprecated access to it raises; a module whose warning has already
  completed, or was ignored by a filter, stays silent whatever filter is
  installed later, and a name already resolved is cached and never warns
  again. `DeprecationWarning` is hidden by default outside `__main__` and test
  runners; because the warning is attributed to the caller, filter by message
  or category rather than by module name.
- **`MowerAPI`'s synchronous wrappers are deprecated.** `get_devices`,
  `get_mqtt_user_info`, `get_device_status`, `send_command` and
  `query_command_results` stay, because the legacy `MowerClient` calls them,
  and each now emits a `DeprecationWarning` pointing at its `async_*`
  counterpart. They call `asyncio.run`, so they cannot be used inside a running
  event loop; that has always been so and is now documented.
- **Smaller star-import surface for `mower_sdk.mqtt` and `mower_sdk.models`.**
  Both modules now define `__all__`, listing exactly the names upstream
  published from them: the classes they define plus the aliases callers picked
  up (`Device`, `DeviceStatus`, `ERROR_MESSAGES`, `MowerMQTTError` and
  `parse_json` on `mower_sdk.mqtt`). The imported helpers a bare star import
  used to leak no longer arrive: from `mower_sdk.mqtt`, `Any`, `Awaitable`,
  `Callable`, `asyncio`, `json`, `logging`, `mqtt_client`, `urlparse` and
  `uuid`; from `mower_sdk.models`, `Any`, `Enum` and `dataclass`. Plain
  attribute access to them is unchanged. `mower_sdk.errors` also gains an
  `__all__`, which drops nothing.
- **`__module__` and logger names of legacy classes** now say
  `mower_sdk.legacy.<module>`: for example `MowerClient.__module__` is
  `mower_sdk.legacy.client`, and the generation-1 MQTT client logs under
  `mower_sdk.legacy.mqtt_v1`. Patching the globals of an old module path (for
  example `mower_sdk.client`) no longer reaches the legacy code, whose globals
  live in `mower_sdk.legacy.client`. The object reached through either path is
  the same one, and pickles that reference an old path still load through the
  shim.
- **`NavimowMQTT.subscribe_all` and `unsubscribe_all`** can be called without
  their two ignored arguments: `product_key` and `device_name` are now
  optional and default to `""`. `_on_connect` still passes both, so overrides
  written against the original two-argument signature keep working.

### Added

- `MowerAPI.async_send_command_receipt(device_id, command)` returns a
  `CommandReceipt` whose `verdict` is a `CommandVerdict`: `ACCEPTED` when the
  cloud accepted the command (it does not mean the mower acted),
  `ALREADY_IN_STATE` when the reply said the mower was already there (it wins
  over a SUCCESS in the same reply), `UNKNOWN` otherwise, an empty result list
  included. The receipt also carries the per-command `results` and the reply's
  `command_number` when it has one under a recognised key (no captured reply
  does). A refused command still raises `MowerAPIError`, as does a transport
  failure, with the cause attached; neither returns a receipt.
  `async_send_command` keeps returning the raw reply data. Both read the dict
  entries of `data.payload.commands`: a `commands` that is missing, null or not
  a list, and entries that are not dicts, give fewer results (`UNKNOWN` when
  none is left) rather than a `TypeError` or `AttributeError`. `CommandReceipt`
  is hashable, with `results` left out of the hash.
- `MowerAPI.async_get_command_result(device_id, cmd_num=None)` queries the
  command result of one device and returns its entry, or None when the reply
  has none. The reply shape of that endpoint is unconfirmed.
- `NavimowSDK.get_cached_state_age`, `get_cached_attributes_age` (seconds,
  monotonic) and `get_cached_state_received_at` (UTC `datetime`), so a consumer
  can tell a stale MQTT cache from a fresh one.
- `NavimowSDK(..., allow_experimental_mqtt_commands=True)` and
  `MowerAPI(..., request_timeout=...)`, described above.
- `CommandReceipt`, `CommandVerdict` and `MowerUnsupportedOperationError` are
  exported from the package and listed in their modules' `__all__`.
- No new synchronous wrapper: the existing ones are deprecated, and the new
  methods are async only.

### Internal

- `MowerAPI` checks the reply envelope in one place, `_unwrap`, and no longer
  defines a `__del__` that did nothing.
- `NavimowMQTT` configures its paho client in one place, `_configure_client`.
  The construction order is unchanged: `self.client` is assigned before the
  callback attributes are read, and an override of `_build_new_client` is not
  called at construction.
- The two raw-state readers in `mower_sdk.models` share one helper that keeps
  each reader's key precedence and its falsy fallbacks.
- Characterisation tests pin the REST envelope, the state and battery parsing
  and the MQTT client setup; fresh-subprocess tests pin the deprecation policy
  and core isolation.
- `tools/port_upstream.py` ports upstream commits into the moved files and
  refuses series that touch the mixed files; `tools/check_extraction.py`
  verifies that the extracted code is verbatim. Both are described in
  `UPSTREAM.md`.
- CI: ruff is blocking, with legacy code keeping only the `F` rules so it is
  never reformatted; the wheel job checks that `mower_sdk.legacy` and its ten
  modules ship; the core-isolation test runs with `-W error::DeprecationWarning`;
  the dependency-bounds job constructs the MQTT classes outside and inside a
  running loop and exercises `update_credentials` in both connection states
  against a real paho client on 1.6.1 and 2.x.
- Tests for the fork patches: the request timeout against the fake session,
  credential updates with a fake paho client, loop binding, the battery and
  discovery readers, cache ages with a fake clock, the command gate, the
  receipt verdicts and the command-number extractor, and the single-device
  result query.

## [0.2.0a1] - 2026-09-27

First pre-release of the community edition, for TestPyPI only: the fork, its
metadata and its guards. No line of executable code differs from upstream
0.1.2 (`6596aa0`) except `__version__`.

### Changed

- The distribution is `navimow-sdk-community`; the import name stays
  `mower_sdk`. Both distributions install the same package directory, so
  uninstall `navimow-sdk` before installing this one.
- License metadata corrected to `GPL-3.0-only`, matching the `LICENSE` file
  shipped since the first release; the build backend floor is setuptools 77.
- `requests` and `aiomqtt`, never imported, are no longer dependencies;
  `requirements.txt` is gone; `paho-mqtt` is allowed up to, excluding, 3.
- The version has one source, `mower_sdk.__version__`.
- Docstrings and comments are in English, with DrTree's translation credited.

### Added

- `tests/upstream_exports.json`, the inventory of upstream's public surface at
  the fork point, and the compatibility test that holds every later change to
  it.
- CI on Python 3.11 to 3.14, on both dependency bounds and from the installed
  wheel; publishing to TestPyPI on tags.
- `UPSTREAM.md`, recording the fork point, the remotes, provenance and the
  rules that keep a merge-back possible.

[Unreleased]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a5...HEAD
[0.2.0a5]: https://github.com/geordiekorper/navimow-sdk-community/compare/ab9115dd53348a2cb89446e333316cfbe91f24fb...v0.2.0a5
[0.2.0a4]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a3...ab9115dd53348a2cb89446e333316cfbe91f24fb
[0.2.0a3]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a2...v0.2.0a3
[0.2.0a2]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a1...v0.2.0a2
[0.2.0a1]: https://github.com/geordiekorper/navimow-sdk-community/releases/tag/v0.2.0a1

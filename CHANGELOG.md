# Changelog

Notable changes to navimow-sdk-community. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/). The import name is `mower_sdk`
throughout.

## [Unreleased]

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

[Unreleased]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a2...HEAD
[0.2.0a2]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a1...v0.2.0a2
[0.2.0a1]: https://github.com/geordiekorper/navimow-sdk-community/releases/tag/v0.2.0a1

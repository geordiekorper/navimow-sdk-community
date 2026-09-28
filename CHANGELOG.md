# Changelog

Notable changes to navimow-sdk-community. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/). The import name is `mower_sdk`
throughout.

## [Unreleased]

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

[Unreleased]: https://github.com/geordiekorper/navimow-sdk-community/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/geordiekorper/navimow-sdk-community/releases/tag/v0.2.0a1

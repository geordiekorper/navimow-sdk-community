# Changelog

Notable changes to navimow-sdk-community. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/). The import name is `mower_sdk`
throughout.

## [Unreleased]

Phase 1 of the fork plan: the legacy quarantine. Nothing upstream published is
deleted, and every public name still imports from its old path. Nothing on the
live path (`MowerAPI`, `NavimowMQTT`, `NavimowSDK`, the models, the errors)
behaves differently, except as listed here.

### Changed

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
  behind the names in `__all__`. Under an error filter every deprecated access
  raises. `DeprecationWarning` is hidden by default outside `__main__` and
  test runners; because the warning is attributed to the caller, filter by
  message or category rather than by module name.
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
  modules ship; the core-isolation test runs with `-W error::DeprecationWarning`.

## [0.2.0a1] - 2026-09-27

First pre-release of the community edition, for TestPyPI only. Phase 0 of the
fork plan: fork and guard. No line of executable code differs from upstream
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

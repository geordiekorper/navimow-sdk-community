# mower_sdk.legacy

Code upstream published that the SDK no longer builds on. It is kept so that
every name upstream's 0.1.2 exported still imports, and it is frozen: nothing
here is fixed, reformatted or extended. New code uses the live path instead
(`MowerAPI`, `NavimowSDK`, `NavimowMQTT`, the models and the errors); the table
below says what replaces each piece.

## What is here

Ten modules and an empty `__init__.py`. Seven were moved whole with `git mv`;
three (`mqtt_v1`, `thing_models`, `errors`) were cut out of core modules that
stay.

| Module | Names | What it is | Use instead |
|---|---|---|---|
| `client` | `MowerClient` | One client over `MowerAPI` and the generation-1 MQTT client. Its device, status and command methods only forward to `MowerAPI`. | `MowerAPI` for REST: `async_get_devices`, `async_get_device_status`, `async_send_command` with `MowerCommand.START`, `PAUSE`, `DOCK` or `RESUME`. `NavimowSDK` for the MQTT feed. |
| `mqtt_v1` | `MowerMQTT` | The generation-1 MQTT client. It subscribes to placeholder topics, its `async_connect` does nothing and its `async_subscribe_device` blocks until disconnect. It stays on paho's callback API version 1. | `NavimowSDK`, built with `NavimowSDK.from_connection_info(await api.async_get_mqtt_connection_info(), ...)`; `NavimowMQTT` is the client under it. |
| `navimow` | `Navimow`, `NavimowDeviceManager` | Account manager: builds a `NavimowMQTT` from a `MowerClient`, wraps it in a `NavimowCloud` and keeps one `NavimowCloudDevice` per mower. | `NavimowSDK.from_connection_info(..., records=devices)`. |
| `cloud` | `NavimowCloud` | Written to decode state, event and attributes messages from a `NavimowMQTT` and fan them out through `DataEvent`. It reads the channel from a topic of the form `navimow/<id>/<channel>`, which is not the form the broker's topics have, so its three message events never fire; only its connection events do. | `NavimowSDK.on_state`, `on_event`, `on_attributes`; also `on_location` and `on_rejected`, which have no legacy counterpart. For its connected and disconnected events, `sdk.mqtt.on_connection_event`. |
| `device` | `NavimowCloudDevice` | Filters a `NavimowCloud`'s messages for one mower and feeds a `StateManager`, so it receives nothing either. | Every message carries `device_id`; filter in the callback. |
| `state_manager` | `StateManager` | The last state, attributes and event seen for one mower, with a `DataEvent` for each. | `NavimowSDK.get_cached_state` and `get_cached_attributes`, with `get_cached_state_age`, `get_cached_attributes_age` and `get_cached_state_received_at` to tell a stale reading from a fresh one. The last event is not cached; keep it in the `on_event` callback. |
| `event` | `Event`, `DataEvent` | Lists of weakly referenced async subscribers. | Nothing: `NavimowSDK` takes plain callbacks. |
| `thing_models` | `ThingParams`, `ThingStatusMessage`, `ThingPropertiesMessage`, `ThingEventMessage` | Alibaba IoT "Thing" envelopes. Nothing constructs them; the cloud does not send this envelope. | Nothing. The feed carries `DeviceStateMessage`, `DeviceEventMessage` and `DeviceAttributesMessage`. |
| `errors` | `MowerAuthError`, `COMMAND_ERRORS` | An exception nothing raises and a mapping nothing reads. | `MowerAuthRequiredError` is what a refused credential raises. Nothing replaces the mapping. |
| `utils` | `setup_logger`, `parse_json`, `timestamp_to_datetime`, `datetime_to_timestamp` | Small helpers the live path does not use. | The standard library: `logging`, `json`, `datetime`. |

The README's [quick example](../../README.md#quick-example) shows the live
path end to end, and [docs/migrating.md](../../docs/migrating.md) has the
before and after of each class in code.

## How the old names still work

Nothing upstream published was removed or renamed, so code written against
navimow-sdk 0.1.2 imports as before:

- The seven old module paths (`mower_sdk/client.py`, `cloud.py`, `device.py`,
  `event.py`, `navimow.py`, `state_manager.py`, `utils.py`) are shims that
  re-export the public names of the module here with a star import.
- `mower_sdk`, `mower_sdk.mqtt`, `mower_sdk.models` and `mower_sdk.errors`
  serve the moved names on first access: `mower_sdk.MowerClient`,
  `mower_sdk.mqtt.MowerMQTT`, `mower_sdk.mqtt.parse_json`,
  `mower_sdk.models.ThingStatusMessage`, `mower_sdk.errors.MowerAuthError`
  and the rest.
- The first access to a legacy module through any of those old paths emits
  one `DeprecationWarning` per process, attributed to the caller's line and
  saying that every name in that module is deprecated. Later accesses to the
  same module are silent. The policy is in
  [`mower_sdk/_deprecation.py`](../_deprecation.py).
- Importing `mower_sdk.legacy.<module>` directly never warns.
- `import mower_sdk` loads nothing from this folder, and no core module
  imports it.
- A legacy class reports its module as `mower_sdk.legacy.<module>`, and the
  generation-1 MQTT client logs under `mower_sdk.legacy.mqtt_v1`.

Python hides `DeprecationWarning` outside `__main__` and test runners. To find
every deprecated use in a program, turn the warning into an error:

```bash
python -W error::DeprecationWarning your_program.py
```

`tests/test_legacy_shims.py`, `tests/test_core_isolation.py` and
`tests/test_public_api_compat.py` hold all of this in place.

## Why the code is frozen

The fork is meant to stay mergeable with upstream in both directions:
upstream's commits are ported into this folder, and the fork's changes are
meant to be offered back. The closer these files stay to upstream's text, the
fewer ports conflict and the smaller the diff a merge-back has to explain.
[UPSTREAM.md](../../UPSTREAM.md) has the map: where each file came from, how
upstream commits are ported, and the rules a merge-back depends on.

The files differ from upstream's at the fork point in four ways only:

- docstrings and comments were translated to English;
- two undefined names were defined (`Any` in `state_manager`, `MowerClient`
  in `navimow`);
- imports of sibling modules point at `mower_sdk.legacy.*`, while imports
  of core classes (`MowerAPI`, `NavimowMQTT`, the models) still come from
  core;
- the three extracted files have a new module docstring, the imports they
  need and, in `mqtt_v1`, a logger of its own; apart from the translation,
  the definitions themselves are identical to the originals.

Several checks keep it that way:

- The `no-protected-changes` pre-commit hook refuses a commit that edits,
  adds, deletes or moves any file in this folder.
- The commit-message rules require a `Legacy-edit: <reason>` trailer on any
  commit that does, and CI checks that for every commit of a pull request.
- ruff applies only its `F` rules here, so the code is never reformatted or
  modernised (`pyproject.toml`).
- The wheel check fails if any of the ten modules is missing from the built
  wheel.

This README is the one file in the folder that is not protected. It is this
project's own text, not upstream's, and it is changed by an ordinary commit.

## Changing something here

The change this folder expects is an upstream commit, ported as
[UPSTREAM.md](../../UPSTREAM.md#porting-upstream-commits) describes. A fix or
a feature of this project's own goes to the live path, even when the same
defect exists in a legacy class: the legacy classes stay as upstream shipped
them.

Every commit that changes a file here other than this README carries a
`Legacy-edit: <reason>` trailer, whatever its origin, and one made with
`git commit` has to skip the hook as well:

```bash
SKIP=no-protected-changes git commit
```

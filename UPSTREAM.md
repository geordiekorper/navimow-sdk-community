# Upstream and provenance

This is the community edition of [segwaynavimow/navimow-sdk](https://github.com/segwaynavimow/navimow-sdk).
This file is the map for a merge-back: where the fork left upstream, what came
from other forks and who wrote it, and what has moved. Update it in the same
commit as the change it records.

## Fork point

| | |
|---|---|
| Upstream | https://github.com/segwaynavimow/navimow-sdk |
| Fork point | `6596aa0`, the tip of upstream `main`, released to PyPI as navimow-sdk 0.1.2 on 2026-04-10 |
| Upstream releases | 0.1.0 (2026-03-12), 0.1.1 (2026-04-03), 0.1.2 (2026-04-10) |
| Upstream activity | none after 2026-04-10 |
| Distribution name | navimow-sdk-community, was navimow-sdk. The import name `mower_sdk` is unchanged |

## Remotes

| Remote | URL | Role |
|---|---|---|
| origin | https://github.com/geordiekorper/navimow-sdk-community | this fork; `main` is the community branch |
| upstream | https://github.com/segwaynavimow/navimow-sdk | fetched for comparison, never pushed to |

To see what has changed since the fork point: `git log 6596aa0..main` for the commits,
`git diff 6596aa0 main` for the tree. To compare with upstream's current tip instead:
`git fetch upstream && git diff upstream/main main`.

## Commits taken from elsewhere

Every commit whose content originates in another fork, with the origin commit
and its author. Each commit is re-implemented on this tree rather than
cherry-picked, credited with a co-author trailer, and adds its own row here in
the same commit. The randax pieces will extend the table when they are taken.

| Commit here | Origin | Author | What was taken |
|---|---|---|---|
| `da27124` | [DrTree/navimow-sdk](https://github.com/DrTree/navimow-sdk) `520b11e38846f719326964e7d35705a4b58db463`, 2026-05-27 | DrTree, credited with a co-author trailer | The English translation of docstrings and comments. Docstrings were retranslated from the Chinese because 520b11e flattened their layout and dropped words; its comment translations were reused where accurate. |
| `2737bca` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | `request_timeout=20.0` on `MowerAPI`, kept as an `aiohttp.ClientTimeout` and passed to every request, `None` leaving the session's policy in force; `TimeoutError` folded into `MowerAPIError` with the cause attached. Re-implemented on the current tree. The HTTP response body stays in the error text; bd17c30 dropped it. |
| `5e2dafd` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | In the connected branch of `NavimowMQTT.update_credentials`, `username_pw_set` and `ws_set_options` are called on the live paho client, so the next automatic reconnect uses the new values without a rebuild. Re-implemented on the current tree with the merged stored values, so a partial update keeps the current username. |
| `fcd51d1` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | `_resolve_event_loop()` (the explicit loop, else the running loop, else `None`) in place of `asyncio.get_event_loop()` in the `NavimowMQTT` and `NavimowSDK` constructors. Added here: a loop set as current with `asyncio.set_event_loop()` and not yet running is honoured without creating one, `connect_async()` binds the running or current loop when none was bound at construction, and a callback that arrives while no loop is bound is closed and logged at warning level. |
| `4724e47` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13`, 2026-09-04 | AndiHOK91, credited with a co-author trailer, for the type change; the reader itself is this fork's own | `DeviceStatus.battery` is `int \| None`, None instead of a false 0 when the payload carries no readable value. Not taken: the 0–100 bounds check of `825ee3a`; out-of-range numbers pass through. |
| `8d9d808` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | `Device.from_dict` reads `deviceModel`, `firmwareVersion`, `serialNumber`, `macAddress` and `isOnline`. Re-implemented so the fallback applies only when the snake_case key is absent (bd17c30's `or` overwrote an explicit empty value), and with `firmware`, the key an X430's device list carries, read before `firmwareVersion`. |
| `0ad181b` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13` and `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | `NavimowSDK.get_cached_state_age`, `get_cached_attributes_age` (monotonic seconds) and `get_cached_state_received_at` (UTC `datetime`), recorded when a state or attributes message is cached. Not taken from `825ee3a`: `StatusSnapshot`, `StatusSource`, `get_cached_status_snapshot()`, `invalidate_cached_status()` and `async_get_fresh_status()`; the reconciliation policy stays with the consumer. |
| `db7e0b0` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | `MowerUnsupportedOperationError` and `NavimowSDK(allow_experimental_mqtt_commands=False)`: `start_mowing`, `pause`, `return_to_base` and `set_blade_height` refuse by default, since the broker accepts the publish and no mower has been seen to act on it (upstream issue #20). Community-only: it reverses an upstream default. The four bodies collapse into one helper; `NavimowMQTT.publish_command` stays ungated. |
| `621e538` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer, for the shape; the verdict logic is this fork's own | `CommandReceipt`, `async_send_command_receipt()` and the private split of the send path. Changed on the way in: the verdict is three-way (`CommandVerdict`: accepted, already_in_state, unknown) instead of `accepted=True` for any reply that did not raise, and the command-number extractor reads scalars only under recognised keys, never a bare scalar from a list; the result list is read as its dict entries, so a null or malformed `commands` gives no results rather than an exception, and the receipt is hashable (`results` left out of the hash). Not taken: `CommandConfirmation`, `async_send_command_confirmed()` and the synchronous wrappers. |
| `a62f1ed` | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer | In `NavimowSDK._on_mqtt_message`, each state, event and attributes callback runs in its own `try`; an exception is logged with its traceback and the next callback still runs. |
| `8b0eed1` | [Armandur/navimow-sdk](https://github.com/Armandur/navimow-sdk) `b3f363da408d2faab7bcc5c7d6cad81dd682056c`, 2026-07-03 | Rasmus Pettersson Vik, credited with a co-author trailer | `MowerAPI.async_get_command_result(device_id, cmd_num=None)`, the single-device wrapper over `async_query_command_results`, with `cmdNum` sent only when given. Two defects fixed on the way in: an entry without an id matched, and the first entry was returned when none matched. Not taken: the synchronous wrapper `get_command_result`, the version bump and the bilingual docstrings (superseded by the translation). |
| feat(mqtt): paho callback API version 2; require paho-mqtt 2.1 | [randax/navimow-sdk](https://github.com/randax/navimow-sdk) `0850f2f3323acb5fe1b5a86ae93346361e220e53`, 2026-08-28 | Øyvind Randa, credited with a co-author trailer | `NavimowMQTT` builds its paho clients with `callback_api_version=CallbackAPIVersion.VERSION2`, `_on_connect` and `_on_disconnect` take the version 2 arguments, and `paho-mqtt>=2.1,<3`. Changed on the way in: a refused CONNACK is read from the reason code's `is_failure` and logged with its text and value, with no fallback for integer codes, since version 2 always passes a `ReasonCode`; the reason code is required in `_on_disconnect`. Not taken with it: the rest of 0850f2f (Python 3.9 support, the loop helpers, which come separately, and its packaging). The legacy `MowerMQTT` stays on version 1. |
| feat(mqtt,sdk): connect-failure hook, disconnect reason, client id, counters and last-message times | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer, for the last-message and connected-at bookkeeping; the hook, reasons and counters are this fork's own | The time of the last message and of the last accepted connect, from its `_last_message_at`, `last_message_age` and `_connected_at`. Changed on the way in: the message times are kept per device and per channel (`last_message_at(device_id, channel=None)` and `last_message_age(device_id, channel=None)`, None giving the newest across channels), and the connect time is a UTC `datetime` in `last_connected_at`. Not taken: `get_mqtt_health()`; its fields are properties here. |
| feat(mqtt): rebuild(), force_reconnect, a retired client's callbacks ignored, one connect at a time | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `825ee3a01ee8a570e1a9fed0cecb08d2259c32e6`, 2026-09-04 | AndiHOK91, credited with a co-author trailer, for the connect guard and the narrowed teardown; the rebuild and the retired-client guard are this fork's own | The `_loop_started` guard: `connect_async()` does nothing while paho's network thread runs, and only `disconnect()` and a rebuild clear it (a failed connect does not, since paho keeps retrying). The teardown of the old client catches `OSError`, `RuntimeError` and `ValueError`, logged at debug level, instead of any exception silently. Changed on the way in: the teardown is part of `rebuild()`, which installs the new client before disconnecting the old one. |
| fix(mqtt,sdk): loop affinity: refuse a closed or a second loop, and survive a loop closing under a callback | [randax/navimow-sdk](https://github.com/randax/navimow-sdk) `0850f2f3323acb5fe1b5a86ae93346361e220e53`, 2026-08-28, and `0820914aa3d6c65bc2424dc852430ec1a7ed3953`, 2026-08-30 | Øyvind Randa, credited with a co-author trailer | From its loop validation helpers: a closed loop is refused, a connect from another running loop is refused, and a callback handed to a loop that has closed is dropped instead of raising on paho's thread. Changed on the way in: a closed `loop=` raises `ValueError` at construction (randax raised `RuntimeError`), the drop is logged at debug level like a stopped loop's, and the facade's `loop` reads the client's binding instead of keeping its own copy. Not taken: its per-message loop lookup and its lock around shared MQTT state; this client has one paho thread and one bound loop. |
| fix(mqtt): the connection logs redact the client id, the account id and the WebSocket path | [AndiHOK91/navimow-sdk](https://github.com/AndiHOK91/navimow-sdk) `bd17c307f6744161d7a551fcabfa4252a2ec1e13`, 2026-09-04 | AndiHOK91, credited with a co-author trailer, for the "configured, not shown" username; the redaction helpers are this fork's own | From its `_configured`: the username, which is the numeric account id, is logged as `configured` or `not configured`. Community-only: the client id is logged as `web_…_<suffix>` and the WebSocket path as its first segment; what is sent to the broker is unchanged. Not taken: the rest of its log rewrite (`_device_tag` and the reworded lines). |
| feat(location): decode the location channel: DeviceLocationMessage, DeviceLocation and the per-device merge | [randax/navimow-sdk](https://github.com/randax/navimow-sdk) `84ebd8b9fb991ee4825775f4daa947fb0c21815c`, 2026-08-29, and `aab8799ef47fa459f5030ba30660e9a058669f80`, 2026-08-30 | Øyvind Randa, credited with a co-author trailer, for the message dataclass and the pose-code table; the decoder and the merged record are this fork's own | `DeviceLocationMessage`, a typed location entry with a `status` property and the entry as `raw`, and `VEHICLE_STATE_TO_STATUS`. Changed on the way in: one message per applied entry, carrying the merged `DeviceLocation` as it stood after that entry, with the entry type as an int and the task fields named as the record names them; the table gains 3 (paused) and 6 (mapping); frozen dataclasses. Not taken: `LocationFilter` and `parse_location_payload`; the decoder applies a catch-up message in time order and checks a plausibility window, which its filter did not. |
| feat(mqtt): subscribe_location, extra_topics and the on_raw hook | [randax/navimow-sdk](https://github.com/randax/navimow-sdk) `84ebd8b9fb991ee4825775f4daa947fb0c21815c`, 2026-08-29 | Øyvind Randa, credited with a co-author trailer | `NavimowMQTT(subscribe_location=False, extra_topics=None)` and `on_raw(topic, payload)`, called for every message with the bytes as received, after the topic is parsed and before the payload is decoded. Changed on the way in: each extra topic is checked at construction (a non-empty string without NUL, encodable as UTF-8, at most 65,535 bytes encoded) instead of failing later on paho's thread; the wildcard fallback subscribes the location and extra topics too. |
| feat(sdk): location messages, the cached location record, and rejected input reported | [randax/navimow-sdk](https://github.com/randax/navimow-sdk) `84ebd8b9fb991ee4825775f4daa947fb0c21815c`, 2026-08-29 | Øyvind Randa, credited with a co-author trailer, for the raw callbacks on the facade; the location wiring and `on_rejected` are this fork's own | `NavimowSDK.on_raw(callback)` and its delivery loop, `_on_mqtt_raw`, which calls each raw callback with the topic and the bytes. Changed on the way in: the MQTT client's raw hook is installed when the first raw callback is registered, not at construction, so nothing extra runs per message without one; a raising raw callback is logged and the next one still runs. Not taken: its location cache of the last filtered entry; `get_cached_location()` returns the merged record. |

What was left from the forks whose pieces are taken above, and why, so the
patches need not be re-evaluated:

- AndiHOK91 `bd17c30`: dropping the HTTP response body from `MowerAPIError`
  (the body carries codes such as `failedToSendCommands` that users need to
  see; if anything, it will be truncated, with the error taxonomy); the rewrite
  of the connection logs around `_configured()` and `_device_tag()` (what is
  logged, and at which level, is decided together with redacting the client id
  and the account id); the test suite and GitHub Actions matrix (CI already
  existed here; the test cases were re-written per commit in this repository's
  conventions rather than copied).
- AndiHOK91 `825ee3a`: the 0–100 battery bounds check (out-of-range values
  pass through unchanged, so a bad number is not hidden); `StatusSnapshot`,
  `StatusSource`, `get_cached_status_snapshot()`, `invalidate_cached_status()`
  and `async_get_fresh_status()` (the MQTT-versus-REST reconciliation policy
  belongs to the consumer, which has to account for the REST cache lag itself,
  and the state-to-status conversion they need is planned with the
  native health hooks; the cache ages and receipt time give consumers the same
  freshness facts); `CommandConfirmation` and `async_send_command_confirmed()`
  (its defaults poll REST every 2 s for 10 s, but the REST status cache lags the
  mower by one to two minutes, so the poll mostly reports failure); the
  `_loop_started` guard, `last_message_age`, `_connected_at` and
  `get_mqtt_health()` (planned with the native health hooks and `rebuild()`,
  which touch the same lines); `api=` on `NavimowSDK` (planned with the
  broker-credential refresh helper); the copy of the caller's `extra` dict in
  `DeviceStatus.from_dict` (a real defect, tracked separately); the narrowed
  `except` with a debug log in the disconnected branch of `update_credentials`
  (planned with `rebuild()`, which replaces that branch); the synchronous
  wrappers `send_command_receipt()` and `send_command_confirmed()` (the
  existing synchronous wrappers are deprecated, so no new ones are added); the
  removal of `MowerAPI.__del__` (already done here).
- AndiHOK91 `4186016` and `5ad98eb`: the SPDX header and the version bump (the
  license metadata was corrected here separately; the version has one source).
- Armandur `14eba24` and its merge `b299b7e`: the English translation of
  comments (superseded by the DrTree translation taken above). Armandur
  `b3f363d`: the synchronous wrapper `get_command_result()` and the version
  bump.

The forks reviewed on 2026-09-26, in the order their pieces are applied:

| Fork | Ahead of upstream | Last push | Plan |
|---|---:|---|---|
| DrTree | 2 | 2026-05-27 | taken, above |
| AndiHOK91 | 4 | 2026-09-04 | taken in part, above: request timeout, battery `None`, live-client credential update, event-loop resolution, cache ages, MQTT-command gate, command receipts, callback isolation, camelCase discovery keys; what was left is listed above |
| Armandur | 3 | 2026-07-03 | taken, above: the single-device command-result helper; its bilingual docstrings are superseded by the translation |
| randax | 43 | 2026-08-30 | fourth, by feature, re-implemented on the cleaned core: location channel, paho-mqtt 2 callbacks, loop affinity |
| monik3r | 2 | 2026-05-12 | not taken: debug logging and placeholder topics on the legacy client |

## Moved files

Everything outside the live path was moved into `mower_sdk.legacy` with
`git mv`, so the history of every moved line can still be traced, and a
deprecating re-export was left at the old location. The move does not by itself
keep `git merge` working; upstream changes are ported with the tool described
under "Porting upstream commits" below. `git log --follow` on a new path finds
the commit that moved or extracted it.

Every old path is a shim: it emits one `DeprecationWarning` per legacy module
per process, on the first deprecated access through any old path, and then
re-exports the legacy module with a star import. Importing
`mower_sdk.legacy.<module>` directly never warns. The policy lives in
`mower_sdk/_deprecation.py`.

| At the fork point | Now | How | Old import still works |
|---|---|---|---|
| `mower_sdk/client.py` | `mower_sdk/legacy/client.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/cloud.py` | `mower_sdk/legacy/cloud.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/device.py` | `mower_sdk/legacy/device.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/event.py` | `mower_sdk/legacy/event.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/navimow.py` | `mower_sdk/legacy/navimow.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/state_manager.py` | `mower_sdk/legacy/state_manager.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/utils.py` | `mower_sdk/legacy/utils.py` | whole file, `git mv` | yes: a shim at the old path warns once per process |
| `mower_sdk/mqtt.py` lines 49–454: `MowerMQTT` | `mower_sdk/legacy/mqtt_v1.py` | extracted | yes: `mower_sdk.mqtt.MowerMQTT` is served lazily and warns once per process |
| `mower_sdk/models.py` lines 192–271: `ThingParams`, `ThingStatusMessage`, `ThingPropertiesMessage`, `ThingEventMessage` | `mower_sdk/legacy/thing_models.py` | extracted | yes: served lazily from `mower_sdk.models`, one warning per process |
| `mower_sdk/errors.py` lines 44–58: `MowerAuthError`; lines 94–112: `COMMAND_ERRORS` | `mower_sdk/legacy/errors.py` | extracted | yes: served lazily from `mower_sdk.errors`, one warning per process |

The moved files received no edit beyond imports of sibling legacy modules
retargeted to `mower_sdk.legacy.*`; imports of core classes (`MowerAPI`,
`NavimowMQTT`, the models) still come from core. The three extractions are
cuts from files that stay, so default `git blame` on the new files starts at
the extraction commit; the line ranges above are at the fork point `6596aa0`,
and `git blame -C` or `git log -L` on those ranges is the way back. Each
extracted node is AST-identical to its original: `tools/check_extraction.py`
checks that, with docstrings and decorators included. No core module imports
`mower_sdk.legacy`: `mower_sdk.mqtt.parse_json`, which core no longer uses, is
served lazily from `mower_sdk/legacy/utils.py` like the extracted names.

## Porting upstream commits

`git merge upstream/main` does not work for the moved files. A merge compares
only the merge base and the two tips; at our tip each old path still exists as
a shim, so git treats upstream's edit and our shim as two edits of the same
file, conflicts in every shim, and nothing reaches `mower_sdk/legacy/`. (This
was measured on a clone of this repository with git 2.55.0.)

`tools/port_upstream.py <range>` ports a series commit by commit instead:

1. It scans the whole series first. If any commit touches a mixed file, one
   whose content was split between core and `legacy/` by extraction
   (`mower_sdk/mqtt.py`, `mower_sdk/models.py`, `mower_sdk/errors.py`), it
   stops before applying anything and lists those commits. Classify each hunk
   by hand, core file or extracted file, and apply it manually. The tool does
   not try to track the extracted line ranges through upstream edits.
   The series is the first-parent line of the range. A merge commit is one
   step on it, carrying its net change against its first parent; the commits
   it merged are squashed into that step and the scan lists them. Applying
   the steps in order reproduces the tree of every commit on the line
   exactly, so nothing can be lost or duplicated whatever the merge did
   (a resolution by hand, a discarded branch, two branches making the same
   change); the cost is that a merged branch's individual commits keep their
   provenance only through the merge message. A step with no change against
   its first parent (an empty commit, or a merge that kept its first parent's
   version) is skipped; a range with no such step is "nothing to port" (exit
   status 3). Start the range on the first-parent line of its end, as the
   fork point is.
2. Otherwise it builds an mbox with one entry per commit: git's own mail
   header and message (`git log --pretty=mboxrd`), a `---` separator, and the
   commit's diff (`git diff-tree -p`) with the `a/` and `b/` paths of the moved
   files rewritten through `tools/upstream_path_map.json`. The message and the
   diff come from separate git commands and are handled as bytes, split on LF
   only, so only diff header lines are ever rewritten; hunk bodies and
   messages are copied byte for byte, CRLF content included. The mbox is
   applied with `git am -3 --keep-cr --patch-format=mboxrd`, so each commit
   lands in `legacy/` with its author, date, message and bytes, and the shims
   are untouched.
3. A conflict stops `git am`. Translated docstrings are the usual cause; that
   conflict would occur without any move, and the tool does not remove it.
   Resolve it, `git add` the file and run `git am --continue`;
   `git am --abort` restores the branch.

`python tools/port_upstream.py --dry-run <range>` scans and prints the
rewritten series without applying it. Port from the last commit already taken
(the fork point `6596aa0` until then):

```bash
git fetch upstream
python tools/port_upstream.py 6596aa0..upstream/main
```

The ported commits keep upstream's authorship, so git history records the
provenance; note the last ported upstream commit here when a port is made.
The map must be extended in the same commit as any later move.

## Rules that keep a merge-back possible

- The import name stays `mower_sdk`; only the distribution name changed.
- Every public name upstream published still resolves. `tests/upstream_exports.json`,
  generated from the fork point, is the inventory and `tests/test_public_api_compat.py`
  enforces it in CI.
- Files move with `git mv`, never by copy and delete, so `git log --follow`
  and `git blame -C` show the history of moved lines (default `git blame` on
  the extracted files starts at the extraction commit). The move alone does
  not keep `git merge` working: see the section above.
- Changes that do not depend on the fork identity are written so they can be
  offered upstream as they are.

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
and its author. Later phases extend this table with the AndiHOK91, Armandur and
randax pieces.

| Commit here | Origin | Author | What was taken |
|---|---|---|---|
| `da27124` | [DrTree/navimow-sdk](https://github.com/DrTree/navimow-sdk) `520b11e38846f719326964e7d35705a4b58db463`, 2026-05-27 | DrTree, credited with a co-author trailer | The English translation of docstrings and comments. Docstrings were retranslated from the Chinese because 520b11e flattened their layout and dropped words; its comment translations were reused where accurate. |

The forks reviewed on 2026-09-26, in the order their pieces are applied:

| Fork | Ahead of upstream | Last push | Plan |
|---|---:|---|---|
| DrTree | 2 | 2026-05-27 | taken, above |
| AndiHOK91 | 4 | 2026-09-04 | second, after the legacy move: request timeouts, battery `None`, command receipts, MQTT-command gate |
| Armandur | 3 | 2026-07-03 | third: one REST helper only; its bilingual docstrings are superseded by the translation |
| randax | 43 | 2026-08-30 | fourth, by feature, re-implemented on the cleaned core: location channel, paho-mqtt 2 callbacks, loop affinity |
| monik3r | 2 | 2026-05-12 | not taken: debug logging and placeholder topics on the legacy client |

## Moved files

Phase 1 moves everything outside the live path into `mower_sdk.legacy` with
`git mv`, so history and merge-back survive, and leaves a deprecating re-export
at the old location. Commits are named by their Phase 1 label; `git log --follow`
on the new path finds each one.

| At the fork point | Now | Moved in | Old import still works |
|---|---|---|---|
| `mower_sdk/client.py` | `mower_sdk/legacy/client.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/cloud.py` | `mower_sdk/legacy/cloud.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/device.py` | `mower_sdk/legacy/device.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/event.py` | `mower_sdk/legacy/event.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/navimow.py` | `mower_sdk/legacy/navimow.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/state_manager.py` | `mower_sdk/legacy/state_manager.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |
| `mower_sdk/utils.py` | `mower_sdk/legacy/utils.py` | Phase 1 C3 (whole file) | not yet: the shim follows in C4 |

The moved files received no edit beyond imports of sibling legacy modules
retargeted to `mower_sdk.legacy.*`; imports of core classes (`MowerAPI`,
`NavimowMQTT`, the models) still come from core. One core import goes the
other way for now: `mower_sdk/mqtt.py` reads `parse_json` from
`mower_sdk.legacy.utils` until C5 moves its only caller, `MowerMQTT`, into
`legacy/` as well.

## Rules that keep a merge-back possible

- The import name stays `mower_sdk`; only the distribution name changed.
- Every public name upstream published still resolves. `tests/upstream_exports.json`,
  generated from the fork point, is the inventory and `tests/test_public_api_compat.py`
  enforces it in CI.
- Files move with `git mv`, never by copy and delete.
- Changes that do not depend on the fork identity are written so they can be
  offered upstream as they are.

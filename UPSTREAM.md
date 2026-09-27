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
`git mv`, so the history of every moved line can still be traced, and leaves a
deprecating re-export at the old location. The move does not by itself keep
`git merge` working; upstream changes are ported with the tool described under
"Porting upstream commits" below. Commits are named by their Phase 1 label and,
once known, their sha; `git log --follow` on the new path finds each one.

Every old path is a shim: it emits one `DeprecationWarning` per legacy module
per process, on the first deprecated access through any old path, and then
re-exports the legacy module with a star import. Importing
`mower_sdk.legacy.<module>` directly never warns. The policy lives in
`mower_sdk/_deprecation.py`.

| At the fork point | Now | Moved in | Old import still works |
|---|---|---|---|
| `mower_sdk/client.py` | `mower_sdk/legacy/client.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/cloud.py` | `mower_sdk/legacy/cloud.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/device.py` | `mower_sdk/legacy/device.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/event.py` | `mower_sdk/legacy/event.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/navimow.py` | `mower_sdk/legacy/navimow.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/state_manager.py` | `mower_sdk/legacy/state_manager.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/utils.py` | `mower_sdk/legacy/utils.py` | Phase 1 C3, `9ceceed` (whole file) | yes: a shim at the old path warns once per process (Phase 1 C4, `96c6696`) |
| `mower_sdk/mqtt.py` lines 49–454: `MowerMQTT` | `mower_sdk/legacy/mqtt_v1.py` | Phase 1 C5 (extraction) | yes: `mower_sdk.mqtt.MowerMQTT` is served lazily and warns once per process |
| `mower_sdk/models.py` lines 192–271: `ThingParams`, `ThingStatusMessage`, `ThingPropertiesMessage`, `ThingEventMessage` | `mower_sdk/legacy/thing_models.py` | Phase 1 C5 (extraction) | yes: served lazily from `mower_sdk.models`, one warning per process |
| `mower_sdk/errors.py` lines 44–58: `MowerAuthError`; lines 94–112: `COMMAND_ERRORS` | `mower_sdk/legacy/errors.py` | Phase 1 C5 (extraction) | yes: served lazily from `mower_sdk.errors`, one warning per process |

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
   Merge commits are inspected in the same pass, because no patch can carry a
   merge. A merge is skipped only when its tree is exactly what git's own
   clean merge of its two parents produces (`git merge-tree --write-tree`,
   git 2.38 or newer): it then only joins its parents, and the commits it
   joins are in the series and carry its changes. Any other merge is refused
   before anything is applied and has to be handled by hand: one whose parents
   conflict, so the commit resolves them itself; one whose tree differs from
   the clean merge, whether by a change of its own or by discarding a
   parent's changes (`-s ours`); an octopus merge; and every merge when git
   is too old to check. A range that holds only merge commits is "nothing to
   port" (exit status 3).
2. Otherwise it builds an mbox with one entry per commit: git's own mail
   header and message (`git log --pretty=mboxrd`), a `---` separator, and the
   commit's diff (`git diff-tree -p`) with the `a/` and `b/` paths of the moved
   files rewritten through `tools/upstream_path_map.json`. The message and the
   diff come from separate git commands, so only diff header lines are ever
   rewritten; hunk bodies and messages are copied byte for byte. The mbox is
   applied with `git am -3 --patch-format=mboxrd`, so each commit lands in
   `legacy/` with its author, date and message, and the shims are untouched.
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

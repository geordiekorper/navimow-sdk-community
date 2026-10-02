# Development

How to set up a checkout, what the checks are and what a commit has to look
like. The other documents cover the rest and are linked where they apply:
[tests/README.md](../tests/README.md) for the test suite,
[tools/README.md](../tools/README.md) for the scripts behind the checks,
[architecture.md](architecture.md) for how the code works and
[releasing.md](releasing.md) for releases.

## Setup

Python 3.11 or later; the checks run on 3.11 to 3.14.

```bash
pip install -e ".[dev]"
pre-commit install
```

`pre-commit install` sets up both hook types the configuration names, the
pre-commit hooks and the commit-message hooks. nox makes its environments with
uv when uv is installed and with virtualenv otherwise.

## Layout

| Path | What it holds |
|---|---|
| `mower_sdk/api.py` | `MowerAPI`, the REST client |
| `mower_sdk/mqtt.py` | `NavimowMQTT`, the MQTT client |
| `mower_sdk/sdk.py` | `NavimowSDK`, the facade over the MQTT client: callbacks, caches, credentials |
| `mower_sdk/models.py` | The typed models and the payload readers |
| `mower_sdk/location.py` | The location channel's decoder, the dock estimate and the target zone |
| `mower_sdk/watchdog.py` | `MqttWatchdog` |
| `mower_sdk/errors.py` | The exception classes |
| `mower_sdk/_deprecation.py` | The warning policy for legacy names |
| `mower_sdk/legacy/` and the seven shims beside it | Code moved from upstream, frozen: see its [README](../mower_sdk/legacy/README.md) |
| `tests/` | The test suite |
| `tools/` | The scripts behind the checks, and their own tests in `tools/tests/` |
| `noxfile.py` | The checks, as nox sessions |
| `.pre-commit-config.yaml`, `.gitlint` | The commit hooks and the commit-message rules |
| `.github/workflows/` | `ci.yml`, which calls the nox sessions, and `publish.yml` |

[architecture.md](architecture.md) says how the core modules work together.

## The checks

Every check CI runs is a nox session, so the same command runs it locally.

| Session | What it does | CI job |
|---|---|---|
| `tests-3.11` to `tests-3.14` | The suite from the source tree, then the core-isolation test with `DeprecationWarning` as an error | `test` |
| `tools-3.11` to `tools-3.14` | The tests of the commit checks (`tools/tests`) | `test` |
| `"bounds(oldest)"` | The suite and the MQTT construction check on Python 3.11 with the lowest allowed aiohttp and paho-mqtt | `bounds` |
| `"bounds(newest)"` | The same on Python 3.14 with the newest releases | `bounds` |
| `wheel` | Builds the wheel, checks what it ships, installs it into a fresh environment, runs the tests from another directory and checks the metadata | `wheel` |
| `lint` | ruff, with the rule set in `pyproject.toml` | `ruff` |

```bash
nox -s tests-3.14
nox -s "bounds(oldest)" "bounds(newest)"
nox -s tests-3.14 -- tests/test_location.py -k dock
nox --list
```

Arguments after `--` go to pytest. `nox` with no session runs them all and
needs all four Pythons.

CI has a fifth job, `hygiene`, which is not a nox session: it runs every
pre-commit hook over every file, and gitlint and the message leak check over
each commit message of the pull request or push. CI runs on pull requests, on
pushes to `main` and on request.

### Versions that must agree

`tools/tests/test_nox_sessions.py` fails when these drift apart, so change
them together:

- The dependency floors in `pyproject.toml` and the `oldest` pins in
  `noxfile.py`.
- The ruff and gitlint versions in `noxfile.py` and in
  `.pre-commit-config.yaml`, and the gitlint version the `hygiene` job
  installs.
- The sessions `ci.yml` calls and the sessions `noxfile.py` defines: CI runs
  every session and calls none that does not exist.

## The commit hooks

| Hook | What it refuses |
|---|---|
| `check-added-large-files`, `check-merge-conflict`, `check-toml`, `check-yaml`, `detect-private-key` | What their names say |
| `end-of-file-fixer`, `trailing-whitespace` | They fix the file and fail the commit; `mower_sdk/legacy/` is excluded |
| `gitleaks` | Secrets |
| `actionlint` | Mistakes in the workflow files |
| `ruff-check` | ruff findings; legacy code keeps only the `F` rules |
| `no-local-paths` | A path that exists only on one machine: a home directory, a per-user temporary directory. `/tmp` in an example is allowed |
| `check-leaks` | A reference to a file git does not track, in any worktree of the checkout |
| `no-protected-changes` | Any change under `mower_sdk/legacy/` other than its README, and any change to `tests/upstream_exports.json` |
| `test-style` | pytest-asyncio and `Mock` objects in `tests/`; [tests/README.md](../tests/README.md#style) gives the reason |
| `gitlint` (commit message) | A message that breaks the rules below |
| `check-message-leaks` (commit message) | A local path or the name of an untracked file in the message |

Committed text has to stand on its own. A document, comment or commit message
does not refer to a file that is not in the repository, to a path on someone's
machine, or to a plan or tracker the reader cannot see.

## Commit messages

`.gitlint` holds the format and `tools/gitlint_rules.py` the rules specific to
this repository. CI lints every commit of a pull request, so each one has to
pass, not only the last.

- The subject is a conventional commit: `type(scope): summary` with the scope
  optional and the type one of `feat`, `fix`, `docs`, `test`, `refactor`,
  `perf`, `build`, `ci`, `chore`, `style`, `revert`. At most 110 characters.
- A body is required, with lines of at most 100 characters. `docs`, `revert`
  and `chore(release)` commits may leave it out.
- `fixup!`, `squash!` and `amend!` commits are refused, and so is git's default
  `Revert "..."` subject: reword it to `revert: ...`.
- No `Refs:` line and no tracker id: the message explains the change itself.
- The subject does not end with a label in brackets, a letter and a number.
- A `feat`, `fix`, `perf` or `refactor` commit that changes `mower_sdk/` has a
  body line that starts with `Upstream-suitable.` or `Community-only.`, saying
  whether the change could be offered upstream as it is.
- `Co-authored-by:` lines sit in the trailer block at the end. An assistant's
  attribution line comes after every human co-author.
- A `Co-authored-by:` for the author of another fork needs the origin recorded
  in `UPSTREAM.md` in the same commit.
- `__version__` changes only in a `chore(release)` commit that also changes
  `CHANGELOG.md`: see [releasing.md](releasing.md).
- A change to a protected path needs a `Legacy-edit: <reason>` trailer: see
  the legacy [README](../mower_sdk/legacy/README.md#changing-something-here).

## What a change has to keep

- **Upstream's public names.** Every name upstream published still imports,
  and a new public name is registered with the compatibility tests:
  [tests/README.md](../tests/README.md#the-compatibility-tests).
- **The merge-back rules** in
  [UPSTREAM.md](../UPSTREAM.md#rules-that-keep-a-merge-back-possible).
- **Both dependency bounds.** A commit leaves the suite passing on
  `bounds(oldest)` and `bounds(newest)`.
- **The record.** A change a consumer can see gets an entry under
  `[Unreleased]` in `CHANGELOG.md`. Work taken from another fork gets its row
  in `UPSTREAM.md`. Both go in the commit that makes the change.
- **One concern per commit.** A behaviour change and the test that pins it go
  together; an unrelated tidy-up is its own commit.

## Pull requests

Branch from `main` and open the pull request against it. The five CI jobs have
to pass. Pull requests are merged with a merge commit, not squashed, so every
commit in the branch stays in history as it was reviewed.
[CONTRIBUTING.md](../CONTRIBUTING.md) says what a pull request should contain.

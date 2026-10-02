# Tools

The scripts behind the commit hooks, the nox sessions and two jobs done by
hand. None of this is part of the package: the wheel ships `mower_sdk` only.
[docs/development.md](../docs/development.md#the-commit-hooks) lists the hooks
and what each refuses; this file is about the scripts.

## What runs what

| Script | What it does | Run by |
|---|---|---|
| `check_leaks.py` | Finds names of files git does not track, in files or in a commit message, and applies the optional pattern file | The `check-leaks` and `check-message-leaks` hooks |
| `check_protected_paths.py` | Refuses a change to a protected path | The `no-protected-changes` hook |
| `gitlint_rules.py` | The commit-message rules specific to this repository | gitlint, through `.gitlint` |
| `gatelib.py` | What the three above share: reading the staged change or a commit range, the protected paths, the untracked names, the pattern file | Imported |
| `check_mqtt_construction.py` | Builds the MQTT classes outside and inside a running event loop and updates credentials in both connection states, with paho's deprecation warning as an error | The `bounds` nox sessions |
| `check_wheel.py` | `contents`: the wheel ships `mower_sdk` and `mower_sdk.legacy` and nothing else. `metadata`: the installed distribution's name, version and licence | The `wheel` nox session |
| `port_upstream.py`, `upstream_path_map.json` | Ports upstream commits into `mower_sdk/legacy/` | By hand |
| `check_extraction.py` | Compares the definitions extracted into `mower_sdk/legacy/` with their originals | By hand |
| `gen_export_inventory.py` | Generated `tests/upstream_exports.json` from the fork point | By hand, once |

Each script's docstring is its reference: arguments, exit statuses and the
reasoning behind what it does.

## How the hook scripts read a change

`gatelib.py` gives the hook scripts three modes, chosen from the environment:

- **Staged**, the default: the lines added by the staged change, as in a
  pre-commit hook.
- **Range**: the lines added between two refs, when pre-commit runs with
  `--from-ref` and `--to-ref`.
- **Content**: every line of every file, with `GATE_MODE=content`. CI's
  `hygiene` job uses this with `pre-commit run --all-files`.

`check_protected_paths.py` reads the change itself, not the files it is given,
because pre-commit never passes a deleted file to a hook.

`check_leaks.py` also reads an optional pattern file that adds
project-specific rules. It lives outside the repository. Without it only the
generic rules apply, with one exception: a session with `CLAUDECODE=1` in its
environment is refused until the file exists. The docstring of `gatelib.py`
gives its location and format.

## Jobs done by hand

**Porting upstream commits.**
[UPSTREAM.md](../UPSTREAM.md#porting-upstream-commits) describes when and how;
the tool's own tests are `tests/test_port_upstream.py`.

```bash
python tools/port_upstream.py --dry-run 6596aa0..upstream/main
```

**Checking an extraction.** When a definition is cut out of a core module into
`mower_sdk/legacy/`, `check_extraction.py` confirms that it is identical to
the original, docstrings and decorators included. Run it before committing
the extraction, or afterwards with `--base` and `--target`.

**The export inventory.** `gen_export_inventory.py` generated
`tests/upstream_exports.json`, which the
[compatibility tests](../tests/README.md#the-compatibility-tests) read.
Running it again is a deliberate change to what those tests guard, not a
routine step.

## The tests

`tools/tests` tests the commit checks themselves, each in a throwaway git
repository that ignores the developer's own git configuration.

| Module | What it pins |
|---|---|
| `test_check_leaks.py` | Local paths, untracked names, the pattern file, Markdown links |
| `test_protected_paths.py` | The protected paths, the hook's definition and the `test-style` hook's expression |
| `test_gitlint_rules.py` | `.gitlint` and the repository's rules, run by gitlint itself on a staged change and on a commit range |
| `test_ci_hygiene.py` | Which commit range CI's `hygiene` job checks, and how it runs the checks |
| `test_nox_sessions.py` | That `noxfile.py` agrees with `ci.yml`, `pyproject.toml` and the hook configuration |
| `test_publish_workflow.py` | The shape of the publish workflow: TestPyPI first, PyPI on approval |
| `test_fixture_isolation.py` | That the throwaway repositories are isolated |

```bash
nox -s tools-3.14
```

They are kept apart from `tests/` for two reasons: they need pyyaml, nox and
gitlint, which the SDK's tests do not, and `tests/` is what the `wheel`
session copies to run against the installed package, where the tools do not
exist.

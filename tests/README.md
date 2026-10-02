# Tests

The SDK's test suite. Nothing here touches the network: REST calls go to a fake
aiohttp session and the MQTT client is built on a fake paho client.

## Running

```bash
pytest
pytest tests/test_location.py
pytest tests/test_location.py -k dock
```

That runs the suite in the current environment. The nox sessions run it in
fresh ones, on each supported Python and on both dependency bounds, as CI
does: see [docs/development.md](../docs/development.md#the-checks).

## Style

A test of async code is an `async def` with `@pytest.mark.asyncio`, run by
pytest-asyncio on a fresh event loop per test. The marker is written on every
such test and not switched on by configuration: the wheel run executes this
folder from another directory, where the repository's pytest configuration is
not found, and an unmarked async test fails.

A test that needs the event loop itself in a particular state is a plain
function that drives the loop by hand with `asyncio.run` or a loop of its own:
a client or facade built with no loop running, a loop that has been closed, a
loop set as current and not running, a connect from another thread's loop,
`MowerAPI`'s synchronous wrappers, which start a loop themselves, and the
README's threaded recipe, which runs its loop on a thread of its own.

What a test replaces (an aiohttp session, paho's client, the MQTT client
under the facade) is replaced by a recording fake, not by a `Mock` object. A
fake is a small class in the test module with real methods: it records what
it was asked and answers as the test tells it. The reason is what each does
with a call nobody expected. A fake has only the methods written for it, so
code that calls one it lacks, or with arguments it does not take, fails the
test. A `Mock` accepts any call with any arguments, so the test keeps passing
when the code calls something the real object does not have. Only the `Mock`
classes (`Mock`, `MagicMock`, `AsyncMock` and their relatives, and
`create_autospec`) are ruled out, not the rest of `unittest.mock`; a bare
`patch()` makes a `MagicMock` too, so give it an explicit replacement or use
pytest's `monkeypatch`, as the suite does. The `test-style` commit hook
refuses the `Mock` classes in this folder.

Typical fakes:

- `FakeSession` and `FakeResponse` stand in for aiohttp:
  `test_api_command_result.py` has the shortest pair.
- `FakeClient` stands in for paho's client and is installed by the `fake_paho`
  fixture in `test_mqtt_client.py`.
- `FakeMQTT` stands in for `NavimowMQTT` under the facade, and `FakeClock`
  for time, in several of the `test_sdk_*.py` modules.

Several modules call themselves characterisation tests: they pin what the code
does today, case by case. When a change alters behaviour that such a test
pins, the test changes in the same commit and the commit message names the
case.

## What is where

| Area | Modules |
|---|---|
| REST (`MowerAPI`) | `test_api_envelope.py`, `test_api_command_receipt.py`, `test_api_command_result.py`, `test_api_sync_wrappers.py` |
| MQTT client (`NavimowMQTT`) | `test_mqtt_client.py` |
| Connection information | `test_connection_info.py` |
| Facade (`NavimowSDK`) | `test_sdk_cache.py`, `test_sdk_commands.py`, `test_sdk_credentials.py`, `test_sdk_location.py`, `test_sdk_state_filter.py` |
| Models and payload readers | `test_device_model.py`, `test_models_parsing.py`, `test_model_conversions.py`, `test_errors.py` |
| Location | `test_location.py`, `test_location_dock.py`, `test_target_zone.py` |
| Watchdog | `test_watchdog.py` |
| The README's threaded recipe, run as written | `test_threaded_recipe.py` |
| Compatibility with upstream | `test_public_api_compat.py`, `test_module_star_imports.py`, `test_legacy_shims.py`, `test_core_isolation.py`, `test_upstream_distribution_warning.py` |
| The upstream porting tool | `test_port_upstream.py` |

The tests of the commit checks are not here but in `tools/tests`: see
[tools/README.md](../tools/README.md#the-tests).

## The compatibility tests

`upstream_exports.json` is the inventory of every public name upstream's
package had at the fork point, generated once by
`tools/gen_export_inventory.py`. It is never edited; the commit hooks refuse a
change to it.

- `test_public_api_compat.py` checks that every module in the inventory still
  imports and every name still resolves. It imports and reads attributes only;
  it constructs nothing.
- `test_module_star_imports.py` checks what `from mower_sdk.<module> import *`
  gives. A public name this project adds goes into its module's `__all__` and
  into `COMMUNITY_ADDITIONS` in this test, in the commit that adds it.
- `test_legacy_shims.py` and `test_core_isolation.py` check the old import
  paths, the one-warning-per-module rule and that importing the package loads
  no legacy code. Each case runs in a fresh interpreter, so no other test can
  have imported a module or used up a warning first.
  [mower_sdk/legacy/README.md](../mower_sdk/legacy/README.md) describes the
  behaviour they hold in place.

## The wheel run

The `wheel` nox session copies this folder, and nothing else, to a temporary
directory and runs it against the installed wheel. A test that needs a file
from the repository therefore has to skip when the file is absent, at module
level, as `test_threaded_recipe.py` does for `README.md` and
`test_port_upstream.py` for the tool it tests. A test that only passes because
the source tree is on the path fails in that run, which is the point of it.

## Adding a test

- Put it in the module for its area, or a new `test_<area>.py`.
- No network: use a fake. Several modules have a `FakeClock` for code that
  reads the time.
- It has to pass on both dependency bounds: Python 3.11 with the lowest
  allowed aiohttp and paho-mqtt, and Python 3.14 with the newest.

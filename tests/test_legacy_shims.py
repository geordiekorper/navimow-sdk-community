"""Tests for the deprecation shims at the seven vacated module paths.

Every case runs in a fresh subprocess, so the compat suite cannot prime the
module cache or the warning registry. Each snippet installs an explicit
``always`` filter and reports the warnings it saw as JSON on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import mower_sdk

INVENTORY = json.loads(
    Path(__file__).with_name("upstream_exports.json").read_text(encoding="utf-8")
)
SHIMS = ["client", "cloud", "device", "event", "navimow", "state_manager", "utils"]


def message_for(name: str, via: str | None = None) -> str:
    return (
        f"{via or 'mower_sdk.' + name} is deprecated: all names in mower_sdk.legacy.{name} "
        "are legacy code kept only for compatibility"
    )


PRELUDE = """\
import json
import sys
import warnings

caught = []


def _record(message, category, filename, lineno, file=None, line=None):
    caught.append(
        {
            "category": category.__name__,
            "message": str(message),
            "filename": filename,
            "lineno": lineno,
        }
    )


warnings.showwarning = _record
warnings.simplefilter("always")
"""
PRELUDE_LINES = PRELUDE.count("\n")


def run_snippet(tmp_path: Path, body: str, prelude: str = PRELUDE) -> tuple[Path, dict[str, Any]]:
    """Run ``prelude + body`` in a fresh interpreter and return the JSON it prints last."""
    script = tmp_path / "snippet.py"
    script.write_text(prelude + body, encoding="utf-8")
    env = {
        **os.environ,
        # The package the test process imported, wherever it lives (source tree or wheel).
        "PYTHONPATH": str(Path(mower_sdk.__file__).resolve().parent.parent),
    }
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return script, json.loads(proc.stdout.splitlines()[-1])


@pytest.mark.parametrize("name", SHIMS)
def test_importing_a_shim_warns_once_at_the_importing_line(tmp_path: Path, name: str) -> None:
    script, caught = run_snippet(
        tmp_path,
        f"""\
import mower_sdk.{name}
import mower_sdk.{name}
import importlib
importlib.reload(sys.modules["mower_sdk.legacy.{name}"])
print(json.dumps(caught))
""",
    )
    assert caught == [
        {
            "category": "DeprecationWarning",
            "message": message_for(name),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 1,
        }
    ]


def test_from_import_through_a_shim_is_attributed_to_the_importing_line(tmp_path: Path) -> None:
    script, caught = run_snippet(
        tmp_path,
        """\
from mower_sdk.client import MowerClient
from mower_sdk.client import MowerAPI
print(json.dumps(caught))
""",
    )
    assert caught == [
        {
            "category": "DeprecationWarning",
            "message": message_for("client"),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 1,
        }
    ]


def test_direct_legacy_imports_and_the_package_import_do_not_warn(tmp_path: Path) -> None:
    _, caught = run_snippet(
        tmp_path,
        """\
import mower_sdk
from mower_sdk import MowerAPI, NavimowMQTT, NavimowSDK
import mower_sdk.legacy.client
import mower_sdk.legacy.cloud
import mower_sdk.legacy.device
import mower_sdk.legacy.event
import mower_sdk.legacy.navimow
import mower_sdk.legacy.state_manager
import mower_sdk.legacy.utils
print(json.dumps(caught))
""",
    )
    assert caught == []


def test_a_legacy_module_reached_through_another_deprecated_path_is_silent(tmp_path: Path) -> None:
    """The shim and any later lazy path share one record per legacy module."""
    _, caught = run_snippet(
        tmp_path,
        """\
import mower_sdk.client
from mower_sdk._deprecation import warn_legacy
warn_legacy("client", "mower_sdk.MowerClient")
warn_legacy("client", "mower_sdk.client")
warn_legacy("cloud", "mower_sdk.NavimowCloud")
import mower_sdk.cloud
print(json.dumps([c["message"] for c in caught]))
""",
    )
    assert caught == [message_for("client"), message_for("cloud", "mower_sdk.NavimowCloud")]


def test_the_message_covers_every_name_in_the_legacy_module(tmp_path: Path) -> None:
    _, caught = run_snippet(
        tmp_path,
        """\
import mower_sdk.state_manager
print(json.dumps(caught))
""",
    )
    (warning,) = caught
    assert warning["message"] == (
        "mower_sdk.state_manager is deprecated: all names in mower_sdk.legacy.state_manager "
        "are legacy code kept only for compatibility"
    )


def test_concurrent_first_accesses_produce_one_warning(tmp_path: Path) -> None:
    _, result = run_snippet(
        tmp_path,
        """\
import threading
from mower_sdk._deprecation import warn_legacy

count = 16
barrier = threading.Barrier(count)


def first_access() -> None:
    barrier.wait()
    warn_legacy("event", "mower_sdk.event")


threads = [threading.Thread(target=first_access) for _ in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps(caught))
""",
    )
    assert [w["message"] for w in result] == [message_for("event")]


def test_under_an_error_filter_every_deprecated_access_raises(tmp_path: Path) -> None:
    _, result = run_snippet(
        tmp_path,
        """\
warnings.simplefilter("error", DeprecationWarning)
outcomes = []
for _ in range(2):
    try:
        import mower_sdk.utils
    except DeprecationWarning as exc:
        outcomes.append(str(exc))
    else:
        outcomes.append("imported")
from mower_sdk._deprecation import warn_legacy
for _ in range(2):
    try:
        warn_legacy("utils", "mower_sdk.utils")
    except DeprecationWarning as exc:
        outcomes.append("helper: " + str(exc))
    else:
        outcomes.append("helper: silent")
cached = "mower_sdk.utils" in sys.modules
warnings.simplefilter("always")
import mower_sdk.utils
print(json.dumps({"outcomes": outcomes, "cached_after_failures": cached, "later": [c["message"] for c in caught]}))
""",
    )
    assert result["outcomes"] == [
        message_for("utils"),
        message_for("utils"),
        "helper: " + message_for("utils"),
        "helper: " + message_for("utils"),
    ]
    assert result["cached_after_failures"] is False
    assert result["later"] == [message_for("utils")]


@pytest.mark.parametrize("name", SHIMS)
def test_star_import_from_a_shim_yields_every_inventory_name(tmp_path: Path, name: str) -> None:
    module = INVENTORY["modules"][f"mower_sdk.{name}"]
    expected = sorted({*module["defines"], *module["aliases"]})
    _, result = run_snippet(
        tmp_path,
        f"""\
namespace = {{}}
exec("from mower_sdk.{name} import *", namespace)
print(json.dumps({{"names": sorted(n for n in namespace if not n.startswith("__")), "warnings": len(caught)}}))
""",
    )
    assert [n for n in expected if n not in result["names"]] == []
    assert "_warn_legacy" not in result["names"]
    assert result["warnings"] == 1


@pytest.mark.parametrize("name", SHIMS)
def test_shim_objects_are_the_legacy_objects(tmp_path: Path, name: str) -> None:
    module = INVENTORY["modules"][f"mower_sdk.{name}"]
    names = sorted({*module["defines"], *module["aliases"]})
    _, result = run_snippet(
        tmp_path,
        f"""\
import importlib
shim = importlib.import_module("mower_sdk.{name}")
legacy = importlib.import_module("mower_sdk.legacy.{name}")
same = {{n: getattr(shim, n) is getattr(legacy, n) for n in {names!r}}}
modules = {{n: getattr(shim, n).__module__ for n, kind in {module["defines"]!r}.items()}}
print(json.dumps({{"same": same, "modules": modules}}))
""",
    )
    assert result["same"] == dict.fromkeys(names, True)
    # Not promised by the compat contract, but observable and recorded in the changelog.
    assert result["modules"] == dict.fromkeys(module["defines"], f"mower_sdk.legacy.{name}")

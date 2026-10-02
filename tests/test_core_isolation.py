"""Core never loads legacy, and the package's lazy legacy names warn as specified.

Every case runs in a fresh subprocess, so nothing another test imported can
prime the module cache or the warning registry. The isolation case runs the
interpreter with ``-W error::DeprecationWarning``; the others install an
explicit ``always`` filter and report the warnings they saw as JSON.
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

CORE_MODULES = [
    "mower_sdk",
    "mower_sdk._deprecation",
    "mower_sdk.api",
    "mower_sdk.errors",
    "mower_sdk.location",
    "mower_sdk.models",
    "mower_sdk.mqtt",
    "mower_sdk.sdk",
    "mower_sdk.watchdog",
]

# The 12 legacy names the package serves lazily: name -> legacy module holding it.
LEGACY_NAMES = {
    "MowerClient": "client",
    "Navimow": "navimow",
    "MowerMQTT": "mqtt_v1",
    "NavimowCloud": "cloud",
    "NavimowCloudDevice": "device",
    "StateManager": "state_manager",
    "DataEvent": "event",
    "ThingStatusMessage": "thing_models",
    "ThingPropertiesMessage": "thing_models",
    "ThingEventMessage": "thing_models",
    "COMMAND_ERRORS": "errors",
    "MowerAuthError": "errors",
}
# The legacy modules behind the 11 legacy names in __all__ (MowerAuthError is not listed).
STAR_IMPORT_MODULES = [
    "client",
    "navimow",
    "mqtt_v1",
    "cloud",
    "device",
    "state_manager",
    "event",
    "thing_models",
    "errors",
]


def message_for(name: str, module: str) -> str:
    return (
        f"mower_sdk.{name} is deprecated: all names in mower_sdk.legacy.{module} "
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


def run_snippet(
    tmp_path: Path, body: str, *, prelude: str = PRELUDE, flags: tuple[str, ...] = ()
) -> tuple[Path, Any]:
    """Run ``prelude + body`` in a fresh interpreter and return the JSON it prints last."""
    script = tmp_path / "snippet.py"
    script.write_text(prelude + body, encoding="utf-8")
    env = {
        **os.environ,
        # The package the test process imported, wherever it lives (source tree or wheel).
        "PYTHONPATH": str(Path(mower_sdk.__file__).resolve().parent.parent),
    }
    proc = subprocess.run(
        [sys.executable, *flags, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return script, json.loads(proc.stdout.splitlines()[-1])


def test_core_imports_no_legacy_module_and_raises_no_warning(tmp_path: Path) -> None:
    _, result = run_snippet(
        tmp_path,
        """\
import json
import sys

import mower_sdk
import mower_sdk.api
import mower_sdk.errors
import mower_sdk.models
import mower_sdk.mqtt
import mower_sdk.sdk
from mower_sdk import (
    ERROR_MESSAGES,
    Device,
    DeviceAttributesMessage,
    DeviceCommandMessage,
    DeviceEventMessage,
    DeviceStateMessage,
    DeviceStatus,
    MowerAPI,
    MowerAPIError,
    MowerCommand,
    MowerError,
    MowerMQTTError,
    MowerStatus,
    NavimowMQTT,
    NavimowSDK,
)

# Use the live path by name; nothing is constructed: this checks what importing
# loads, not what the classes do.
assert MowerAPI is mower_sdk.api.MowerAPI and MowerAPI.__module__ == "mower_sdk.api"
assert NavimowMQTT is mower_sdk.mqtt.NavimowMQTT and NavimowMQTT.__module__ == "mower_sdk.mqtt"
assert NavimowSDK is mower_sdk.sdk.NavimowSDK and NavimowSDK.__module__ == "mower_sdk.sdk"
assert issubclass(MowerAPIError, Exception) and issubclass(MowerMQTTError, Exception)
assert ERROR_MESSAGES["DEVICE_NOT_FOUND"]
assert DeviceStatus.from_dict({"vehicleState": "isDocked"}).status is MowerStatus.DOCKED
assert DeviceStateMessage.from_dict({"state": "isRunning"}).state == "mowing"
assert Device.from_dict({"id": "d"}).id == "d"
assert MowerCommand.START.value == "start" and MowerError.NONE.value == "none"
assert DeviceEventMessage and DeviceAttributesMessage and DeviceCommandMessage
# dir() lists the lazy names without loading them
assert set(mower_sdk.__all__) <= set(dir(mower_sdk))

print(json.dumps({
    "legacy": sorted(m for m in sys.modules if m.startswith("mower_sdk.legacy")),
    "core": sorted(m for m in sys.modules if m.startswith("mower_sdk")),
}))
""",
        prelude="",
        flags=("-W", "error::DeprecationWarning"),
    )
    assert result["legacy"] == []
    assert result["core"] == CORE_MODULES


@pytest.mark.parametrize(("name", "module"), sorted(LEGACY_NAMES.items()))
def test_each_legacy_name_warns_once_and_is_the_legacy_object(
    tmp_path: Path, name: str, module: str
) -> None:
    script, result = run_snippet(
        tmp_path,
        f"""\
import mower_sdk
first = mower_sdk.{name}
second = mower_sdk.{name}
from mower_sdk import {name} as third
import mower_sdk.legacy.{module} as legacy
print(json.dumps({{
    "caught": caught,
    "same": first is second is third is legacy.{name},
    "cached": "{name}" in vars(mower_sdk),
}}))
""",
    )
    assert result["caught"] == [
        {
            "category": "DeprecationWarning",
            "message": message_for(name, module),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 2,
        }
    ]
    assert result["same"] is True
    assert result["cached"] is True


@pytest.mark.parametrize(
    ("statements", "first", "module"),
    [
        (["mower_sdk.MowerAuthError", "mower_sdk.COMMAND_ERRORS"], "MowerAuthError", "errors"),
        (
            [
                "mower_sdk.ThingStatusMessage",
                "mower_sdk.ThingEventMessage",
                "mower_sdk.ThingPropertiesMessage",
            ],
            "ThingStatusMessage",
            "thing_models",
        ),
        (
            ["mower_sdk.NavimowCloud", "import mower_sdk.cloud", "mower_sdk.cloud.NavimowCloud"],
            "NavimowCloud",
            "cloud",
        ),
    ],
    ids=["errors", "thing_models", "name_then_shim"],
)
def test_another_name_from_the_same_legacy_module_is_silent(
    tmp_path: Path, statements: list[str], first: str, module: str
) -> None:
    body = "import mower_sdk\n" + "".join(f"{statement}\n" for statement in statements)
    body += "print(json.dumps([c['message'] for c in caught]))\n"
    _, caught = run_snippet(tmp_path, body)
    assert caught == [message_for(first, module)]


def test_shim_import_then_top_level_name_is_silent(tmp_path: Path) -> None:
    _, caught = run_snippet(
        tmp_path,
        """\
import mower_sdk.client
import mower_sdk
mower_sdk.MowerClient
from mower_sdk import MowerClient
print(json.dumps([c["message"] for c in caught]))
""",
    )
    assert caught == [
        "mower_sdk.client is deprecated: all names in mower_sdk.legacy.client "
        "are legacy code kept only for compatibility"
    ]


def test_attribution_for_attribute_access_from_import_and_shim_import(tmp_path: Path) -> None:
    script, caught = run_snippet(
        tmp_path,
        """\
import mower_sdk
navimow = mower_sdk.Navimow
from mower_sdk import StateManager
import mower_sdk.event
print(json.dumps(caught))
""",
    )
    assert caught == [
        {
            "category": "DeprecationWarning",
            "message": message_for("Navimow", "navimow"),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 2,
        },
        {
            "category": "DeprecationWarning",
            "message": message_for("StateManager", "state_manager"),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 3,
        },
        {
            "category": "DeprecationWarning",
            "message": message_for("event", "event"),
            "filename": str(script),
            "lineno": PRELUDE_LINES + 4,
        },
    ]


def test_unknown_attribute_raises_attribute_error_without_warning(tmp_path: Path) -> None:
    _, result = run_snippet(
        tmp_path,
        """\
import mower_sdk
try:
    mower_sdk.no_such_name
except AttributeError as exc:
    outcome = str(exc)
else:
    outcome = "no error"
print(json.dumps({"outcome": outcome, "caught": caught}))
""",
    )
    assert result["outcome"] == "module 'mower_sdk' has no attribute 'no_such_name'"
    assert result["caught"] == []


def test_fresh_star_import_makes_one_warning_call_per_legacy_module(tmp_path: Path) -> None:
    _, result = run_snippet(
        tmp_path,
        """\
namespace = {}
exec("from mower_sdk import *", namespace)
import mower_sdk
print(json.dumps({
    "messages": [c["message"] for c in caught],
    "missing": [n for n in mower_sdk.__all__ if n not in namespace],
    "auth_error_arrived": "MowerAuthError" in namespace,
}))
""",
    )
    assert result["missing"] == []
    assert result["auth_error_arrived"] is False  # not in __all__, as upstream had it
    assert len(result["messages"]) == 9
    modules = [m.split("mower_sdk.legacy.")[1].split(" ")[0] for m in result["messages"]]
    assert modules == STAR_IMPORT_MODULES

"""Star imports from mower_sdk.mqtt, mower_sdk.models and mower_sdk.errors.

Before the legacy move only the package-level star import was tested. These three
modules now serve their moved names through ``__getattr__``, and names served
that way are not module globals, so each module needs an ``__all__`` for a
star import to carry them. Each ``__all__`` lists exactly the
inventory names, what upstream defined in the module plus the aliases callers
picked up, and the names the community edition has added to that module, which
``COMMUNITY_ADDITIONS`` records. The imported helpers that a bare star import
used to leak (``json``, ``asyncio``, ``dataclass`` and the like) no longer
arrive; plain attribute access to them is unchanged.
"""

from __future__ import annotations

import importlib
import json
import warnings
from pathlib import Path

import pytest

INVENTORY = json.loads(
    Path(__file__).with_name("upstream_exports.json").read_text(encoding="utf-8")
)
MODULES = ["mower_sdk.mqtt", "mower_sdk.models", "mower_sdk.errors"]

# Public names the community edition adds to a module's ``__all__`` beyond the
# inventory: module name -> the names added. Each addition is listed here in the
# commit that introduces it; ``upstream_exports.json`` itself is never edited.
COMMUNITY_ADDITIONS: dict[str, set[str]] = {
    "mower_sdk.errors": {"MowerUnsupportedOperationError"},
    "mower_sdk.models": {"CommandReceipt", "CommandVerdict"},
}


def inventory_names(module: str) -> set[str]:
    entry = INVENTORY["modules"][module]
    return {*entry["defines"], *entry["aliases"]}


def expected_all(module: str) -> set[str]:
    return inventory_names(module) | COMMUNITY_ADDITIONS.get(module, set())


def star_import(module: str) -> dict[str, object]:
    namespace: dict[str, object] = {}
    with warnings.catch_warnings():
        # The lazy legacy names warn once per process; the deprecation policy
        # itself is tested in fresh subprocesses elsewhere.
        warnings.simplefilter("ignore", DeprecationWarning)
        exec(f"from {module} import *", namespace)  # the star import is the point of this test
    return {name: value for name, value in namespace.items() if not name.startswith("__")}


@pytest.mark.parametrize("module", MODULES)
def test_every_inventory_name_arrives_with_a_star_import(module: str) -> None:
    arrived = star_import(module)
    missing = sorted(inventory_names(module) - arrived.keys())
    assert not missing, f"names missing from a star import of {module}: {missing}"


@pytest.mark.parametrize("module", MODULES)
def test_star_import_carries_the_module_attributes(module: str) -> None:
    arrived = star_import(module)
    loaded = importlib.import_module(module)
    for name, value in arrived.items():
        assert value is getattr(loaded, name), f"{module}.{name} differs from the star-imported object"


@pytest.mark.parametrize("module", MODULES)
def test_all_lists_exactly_the_inventory_names(module: str) -> None:
    declared = importlib.import_module(module).__all__
    assert sorted(declared) == sorted(expected_all(module))
    assert len(declared) == len(set(declared))
    assert set(star_import(module)) == set(declared)

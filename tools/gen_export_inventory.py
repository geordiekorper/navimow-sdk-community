#!/usr/bin/env python3
"""Generate the public-export inventory read by tests/test_public_api_compat.py.

The package directory is walked with ``ast``; nothing is imported or executed.
For every module the inventory records the public classes, functions and
constants it defines, plus its incidental aliases: names it imports from
sibling modules, which exist as attributes only because of the import (for
example ``mower_sdk.mqtt.parse_json``). Each alias carries a policy, ``keep``
for all of them at the fork point; a later phase that moves code may change a
policy in a reviewed edit. The package ``__all__`` and every name the package
``__init__`` imports at top level, listed in ``__all__`` or not, are recorded
as well. Plain ``import x`` statements and names bound inside ``if`` or
``try`` blocks are not recorded.

The committed inventory was generated once from the fork point and is not
regenerated automatically; rerunning this script is a deliberate change:

    git archive 6596aa0 mower_sdk | tar -x -C /tmp/forkpoint
    python tools/gen_export_inventory.py --package /tmp/forkpoint/mower_sdk --ref 6596aa0
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE = REPO_ROOT / "mower_sdk"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "upstream_exports.json"
GENERATOR = "tools/gen_export_inventory.py"

Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
Assignment = ast.Assign | ast.AnnAssign


def assigned_names(node: Assignment) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [sub.id for target in targets for sub in ast.walk(target) if isinstance(sub, ast.Name)]


def string_list(node: ast.AST, where: str) -> list[str]:
    """Return the string literals of the list or tuple assigned to ``__all__``."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise SystemExit(f"{where}: __all__ must be a list or tuple literal")
    return [
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def resolve_source(node: ast.ImportFrom, module_name: str, is_init: bool) -> str:
    """Absolute name of the module an ``ImportFrom`` reads from."""
    if node.level == 0:
        return node.module or ""
    anchor = module_name.split(".") if is_init else module_name.split(".")[:-1]
    anchor = anchor[: len(anchor) - (node.level - 1)]
    return ".".join(anchor + ([node.module] if node.module else []))


def collect_module(path: Path, package: str, module_name: str, is_init: bool) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defines: dict[str, str] = {}
    aliases: dict[str, dict[str, str]] = {}
    exported: list[str] | None = None
    for node in tree.body:
        if isinstance(node, Definition):
            if not node.name.startswith("_"):
                defines[node.name] = "class" if isinstance(node, ast.ClassDef) else "function"
        elif isinstance(node, Assignment):
            for name in assigned_names(node):
                if name == "__all__" and node.value is not None:
                    exported = string_list(node.value, str(path))
                elif not name.startswith("_"):
                    defines[name] = "constant"
        elif isinstance(node, ast.ImportFrom):
            source = resolve_source(node, module_name, is_init)
            if source == package or source.startswith(package + "."):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = {"source": source, "policy": "keep"}
    return {
        "defines": dict(sorted(defines.items())),
        "aliases": dict(sorted(aliases.items())),
        "all": exported,
    }


def build_inventory(package_dir: Path, ref: str) -> dict:
    package = package_dir.name
    init = collect_module(package_dir / "__init__.py", package, package, is_init=True)
    if init["all"] is None:
        raise SystemExit(f"{package_dir / '__init__.py'}: no __all__ found")
    modules: dict[str, dict] = {}
    for path in sorted(package_dir.rglob("*.py")):
        parts = list(path.relative_to(package_dir).with_suffix("").parts)
        if any(part.startswith((".", "__pycache__")) for part in parts[:-1]):
            continue
        is_init = parts[-1] == "__init__"
        if is_init:
            parts = parts[:-1]
        module_name = ".".join([package, *parts])
        if module_name == package:
            continue
        info = collect_module(path, package, module_name, is_init)
        entry = {"defines": info["defines"], "aliases": info["aliases"]}
        if info["all"] is not None:
            entry["all"] = info["all"]
        modules[module_name] = entry
    return {
        "package": package,
        "source_ref": ref,
        "generator": GENERATOR,
        "all": init["all"],
        "package_imports": {name: alias["source"] for name, alias in init["aliases"].items()},
        "modules": modules,
    }


def current_ref(package_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(package_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--package", type=Path, default=DEFAULT_PACKAGE, help="package directory to walk"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON file to write")
    parser.add_argument(
        "--ref", help="commit the inventory describes (default: git HEAD of --package)"
    )
    args = parser.parse_args(argv)

    inventory = build_inventory(args.package, args.ref or current_ref(args.package))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    modules = inventory["modules"].values()
    print(
        f"{args.output}: {len(inventory['modules'])} modules, "
        f"{sum(len(m['defines']) for m in modules)} public names, "
        f"{sum(len(m['aliases']) for m in modules)} aliases, "
        f"{len(inventory['all'])} names in __all__, "
        f"{len(inventory['package_imports'])} package imports (ref {inventory['source_ref']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

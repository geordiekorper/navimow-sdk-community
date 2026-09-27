#!/usr/bin/env python3
"""Check that the extractions into mower_sdk/legacy/ are verbatim.

Each extracted top-level node (the six classes and the COMMAND_ERRORS
assignment) is compared with the node of the same name in the core module of
a base commit, using ``ast.dump`` with docstrings and decorators included.
Line numbers and column offsets are not part of the comparison, so the nodes
may sit anywhere in their files; everything else, docstrings and decorators
included, must be identical.

Run it before committing the extraction, when HEAD is the parent-to-be:

    python tools/check_extraction.py

or afterwards, naming the extraction commit and its parent:

    python tools/check_extraction.py --base <extraction sha>~1 --target <extraction sha>

Without ``--target`` the extracted files are read from the working tree.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Extracted file -> (core module it came from, top-level names that moved).
EXTRACTIONS: dict[str, tuple[str, list[str]]] = {
    "mower_sdk/legacy/mqtt_v1.py": ("mower_sdk/mqtt.py", ["MowerMQTT"]),
    "mower_sdk/legacy/thing_models.py": (
        "mower_sdk/models.py",
        ["ThingParams", "ThingStatusMessage", "ThingPropertiesMessage", "ThingEventMessage"],
    ),
    "mower_sdk/legacy/errors.py": ("mower_sdk/errors.py", ["MowerAuthError", "COMMAND_ERRORS"]),
}


def source_at(rev: str | None, path: str) -> str:
    if rev is None:
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def top_level_node(tree: ast.Module, name: str, where: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets == [name]:
                return node
    raise SystemExit(f"{where}: no top-level definition of {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="HEAD", help="commit holding the pre-extraction core modules")
    parser.add_argument("--target", help="commit holding the extracted files (default: working tree)")
    args = parser.parse_args(argv)

    failures = 0
    for legacy_path, (core_path, names) in EXTRACTIONS.items():
        legacy_tree = ast.parse(source_at(args.target, legacy_path), filename=legacy_path)
        core_tree = ast.parse(source_at(args.base, core_path), filename=f"{args.base}:{core_path}")
        for name in names:
            extracted = ast.dump(top_level_node(legacy_tree, name, legacy_path), indent=1)
            original = ast.dump(top_level_node(core_tree, name, f"{args.base}:{core_path}"), indent=1)
            if extracted == original:
                print(f"ok    {name}: {core_path}@{args.base} == {legacy_path}")
                continue
            failures += 1
            print(f"DIFF  {name}: {core_path}@{args.base} != {legacy_path}")
            sys.stdout.writelines(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    extracted.splitlines(keepends=True),
                    fromfile=f"{args.base}:{core_path}:{name}",
                    tofile=f"{legacy_path}:{name}",
                )
            )
    if failures:
        print(f"{failures} extracted node(s) differ from the base", file=sys.stderr)
        return 1
    print("all extracted nodes are identical to the base")
    return 0


if __name__ == "__main__":
    sys.exit(main())

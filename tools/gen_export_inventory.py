#!/usr/bin/env python3
"""Generate the public-export inventory read by tests/test_public_api_compat.py.

The package directory is walked with ``ast``; nothing is imported or executed.
For every module the inventory records the public classes, functions and
constants it defines, plus its incidental aliases: names it imports from
sibling modules, which exist as attributes only because of the import (for
example ``mower_sdk.mqtt.parse_json``). Each alias carries a policy, ``keep``
for all of them at the fork point; a later change that moves code may change
a policy in a reviewed edit. The package ``__all__`` and every name the
package ``__init__`` imports at top level from the package or a module
inside it, listed in ``__all__`` or not, are recorded as well. Plain
``import x`` statements and names bound inside ``if`` or ``try`` blocks are
not recorded.

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
    """List the names an assignment statement binds.

    Args:
        node: An assignment, plain or annotated.

    Returns:
        The identifier of every name found in the statement's targets: each
        name a plain, chained or unpacking assignment binds, and also any
        name that appears in an attribute or subscript target.
    """
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [sub.id for target in targets for sub in ast.walk(target) if isinstance(sub, ast.Name)]


def string_list(node: ast.AST, where: str) -> list[str]:
    """Return the string literals of the list or tuple assigned to ``__all__``.

    Args:
        node: The value assigned to ``__all__``.
        where: The file the assignment is in, to name it in the error.

    Returns:
        The elements that are string literals, in order; an element of any
        other kind is left out.

    Raises:
        SystemExit: The value is not a list or tuple literal.
    """
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise SystemExit(f"{where}: __all__ must be a list or tuple literal")
    return [
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def resolve_source(node: ast.ImportFrom, module_name: str, is_init: bool) -> str:
    """Give the absolute name of the module an ``ImportFrom`` reads from.

    Args:
        node: The import statement.
        module_name: The dotted name of the module the statement is in.
        is_init: Whether that module is a package's ``__init__``, in which
            case a relative import starts from the module's own name and not
            from its parent's.

    Returns:
        For an absolute import, the module it names; for a relative one,
        that name resolved against module_name.
    """
    if node.level == 0:
        return node.module or ""
    anchor = module_name.split(".") if is_init else module_name.split(".")[:-1]
    anchor = anchor[: len(anchor) - (node.level - 1)]
    return ".".join(anchor + ([node.module] if node.module else []))


def collect_module(path: Path, package: str, module_name: str, is_init: bool) -> dict:
    """Record what one module defines, which package names it imports, and its ``__all__``.

    The file is parsed, not imported, and only its top-level statements are
    read: nothing inside an ``if``, a ``try`` or any other block.

    Args:
        path: The module's source file.
        package: The name of the package the inventory is of.
        module_name: The module's dotted name.
        is_init: Whether the file is a package's ``__init__``.

    Returns:
        A dict of three entries. "defines": each class, function and assigned
        name that does not start with an underscore, mapped to "class",
        "function" or "constant", sorted by name. "aliases": each name a
        ``from`` import binds from the package or a module inside it, one
        that starts with an underscore included, mapped to its "source"
        module and the "policy" "keep", sorted by name. "all": the strings
        of the module's ``__all__``, or None when it assigns none.

    Raises:
        SystemExit: ``__all__`` is assigned something other than a list or
            tuple literal.
        FileNotFoundError: The module's file does not exist.
    """
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
    """Build the inventory of a package directory.

    Every .py file under the directory is read, in sorted order, except
    those under a directory whose name starts with a full stop or with
    __pycache__. A subpackage's ``__init__`` is recorded under the
    subpackage's name; the package's own ``__init__`` gives the top-level
    entries and is not listed among the modules.

    Args:
        package_dir: The package's directory; its name is the package's name.
        ref: The commit the inventory describes, recorded as it is given.

    Returns:
        The inventory, as it is written to JSON: "package", "source_ref" and
        "generator"; "all", the package's ``__all__``; "package_imports",
        each name the package ``__init__`` imports from the package, mapped
        to the module it comes from; and "modules", each module's dotted
        name mapped to its "defines" and "aliases" (see collect_module) and,
        when the module has one, its "all".

    Raises:
        SystemExit: The package ``__init__`` assigns no ``__all__``, or an
            ``__all__`` in the package is not a list or tuple literal.
    """
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
    """Give the commit checked out where the package directory is.

    Args:
        package_dir: The directory git is run in.

    Returns:
        The abbreviated hash of HEAD, or "unknown" when git cannot be run or
        exits with an error, as it does outside a repository.
    """
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
    """Write the inventory of a package to a JSON file and print what it holds.

    The output file's directory is created when it is missing, and the file
    is replaced. The summary line (the counts and the ref) goes to standard
    error.

    Args:
        argv: The command line without the program's name; None reads
            sys.argv. --package is the directory to walk (mower_sdk in this
            repository by default), --output the file to write
            (tests/upstream_exports.json by default) and --ref the commit to
            record (by default the HEAD of the repository --package is in).

    Returns:
        The exit status: 0, the inventory was written.

    Raises:
        SystemExit: The package ``__init__`` has no ``__all__`` or an
            ``__all__`` is not a list or tuple literal, with a message and so
            status 1; or argparse ends the run, with status 2 for a command
            line that is not valid and status 0 after --help.
    """
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

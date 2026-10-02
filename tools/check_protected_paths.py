"""Refuse any change to paths protected from edits.

Code moved verbatim from upstream (mower_sdk/legacy/) and the inventory of
upstream's public names (tests/upstream_exports.json) are not changed: not
edited, added to, deleted or moved away. The check reads the change itself
(the staged diff, or the diff between two refs), because pre-commit passes
hooks only files that still exist and so never shows them a deletion.

The one path under mower_sdk/legacy/ that is not protected is its README.md:
it describes the folder and is not upstream's code.

A deliberate exception is committed with SKIP=no-protected-changes and a
"Legacy-edit: <reason>" trailer, which the gitlint rules require for any
change to these paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib

_WHAT = {"A": "added", "M": "edited", "D": "deleted or moved away", "T": "changed"}


def check() -> list[str]:
    """Find the protected paths that the change being checked touches.

    The change is the staged one, or the one between the two refs of range
    mode (see gatelib.changed_files). It is read with rename detection off,
    so a file moved away from a protected path counts as deleted and one
    moved onto a protected path as added.

    Returns:
        One finding for each changed path that gatelib.is_protected names:
        the path, what was done to it (added, edited, deleted or moved away,
        or changed for a change of type; any other status letter of git's is
        shown as it is) and what a deliberate exception needs. An empty list
        when no protected path changed.

    Raises:
        subprocess.CalledProcessError: git diff fails, as it does on a range
            ref that does not exist or outside a git repository.
    """
    return [
        f"{path}: protected from changes ({_WHAT.get(status, status)}); "
        "an exception needs SKIP=no-protected-changes and a Legacy-edit trailer"
        for status, path in gatelib.changed_files()
        if gatelib.is_protected(path)
    ]


def main() -> int:
    """Run the check and print the findings.

    It takes nothing from the command line. Each finding is printed on its
    own line on standard output.

    Returns:
        The exit status: 0 when no protected path changed, 1 when at least
        one did.

    Raises:
        subprocess.CalledProcessError: git diff fails (see check).
    """
    findings = check()
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Refuse any change to paths protected from edits.

Code moved verbatim from upstream (mower_sdk/legacy/) and the inventory of
upstream's public names (tests/upstream_exports.json) are not changed: not
edited, added to, deleted or moved away. The check reads the change itself
(the staged diff, or the diff between two refs), because pre-commit passes
hooks only files that still exist and so never shows them a deletion.

A deliberate exception is committed with SKIP=no-protected-changes and a
"Legacy-edit: <reason>" trailer, which the gitlint rules require for any
change to these paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib  # noqa: E402

_WHAT = {"A": "added", "M": "edited", "D": "deleted or moved away", "T": "changed"}


def check() -> list[str]:
    return [
        f"{path}: protected from changes ({_WHAT.get(status, status)}); "
        "an exception needs SKIP=no-protected-changes and a Legacy-edit trailer"
        for status, path in gatelib.changed_files()
        if gatelib.is_protected(path)
    ]


def main() -> int:
    findings = check()
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

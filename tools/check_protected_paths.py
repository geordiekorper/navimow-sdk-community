"""Refuse deleting or moving away code that is protected from edits.

The pre-commit hooks no-legacy-edits and no-inventory-edits refuse changes to
mower_sdk/legacy/ and tests/upstream_exports.json, but pre-commit passes only
files that still exist, so a deletion, or a move out of the protected path,
never reaches them. This check looks at the change itself.

For a deliberate exception, skip it with the other two:
  SKIP=no-legacy-edits,no-protected-deletions git commit ...
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib  # noqa: E402

def check() -> list[str]:
    return [
        f"{path}: protected from edits; deleting or moving it away is refused too"
        for status, path in gatelib.changed_files()
        if status == "D" and gatelib.is_protected(path)
    ]


def main() -> int:
    findings = check()
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Port a series of upstream commits into the files Phase 1 moved.

    python tools/port_upstream.py <range>              e.g. 6596aa0..upstream/main
    python tools/port_upstream.py --dry-run <range>    scan and print, apply nothing

A plain ``git merge upstream/main`` conflicts in every shim at a vacated path,
because a merge compares only the merge base and the two tips: at our tip the
old path still exists, so upstream's edit and our shim look like two edits of
one file, and nothing reaches ``mower_sdk/legacy/``. This tool ports a series
commit by commit instead:

1. It scans the whole series first. If any commit touches a mixed file (one
   whose content was split between core and ``legacy/`` by extraction, listed
   in ``tools/upstream_path_map.json``), it stops before applying anything and
   lists those commits for manual classification. It does not try to track the
   extracted line ranges through upstream edits.
2. Otherwise it writes the series with ``git format-patch``, rewrites the
   ``a/`` and ``b/`` paths of the moved files through the map, and applies it
   with ``git am -3``, so each commit lands in ``legacy/`` with its author, date
   and message, and the shims are untouched.
3. A conflict stops ``git am``; translated docstrings are the usual cause and
   would conflict without any move. Resolve it, ``git add`` the file and run
   ``git am --continue``; ``git am --abort`` restores the branch.

Exit status: 0 applied (or dry run), 1 ``git am`` stopped on a conflict,
2 refused because a commit touches a mixed file, 3 usage or git error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_FILE = REPO_ROOT / "tools" / "upstream_path_map.json"

# Patch lines that carry a path: the header, the pre/post-image lines, rename
# and copy headers, and the diffstat summary (informational only).
PATH_LINES = [
    re.compile(r"^(diff --git a/)(?P<a>\S+)( b/)(?P<b>\S+)$"),
    re.compile(r"^(--- a/)(?P<a>.+)$"),
    re.compile(r"^(\+\+\+ b/)(?P<a>.+)$"),
    re.compile(r"^(rename (?:from|to) )(?P<a>.+)$"),
    re.compile(r"^(copy (?:from|to) )(?P<a>.+)$"),
    re.compile(r"^( )(?P<a>\S+)(\s+\|\s+\d+.*)$"),
]


def git(*args: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=check, **kwargs
    )


def load_map() -> tuple[dict[str, str], list[str]]:
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return dict(data["moved"]), list(data["mixed"])


def commits_in(range_spec: str) -> list[str]:
    out = git("rev-list", "--reverse", range_spec).stdout.split()
    return out


def files_touched(commit: str) -> list[str]:
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit).stdout
    return [line for line in out.splitlines() if line]


def subject(commit: str) -> str:
    return git("log", "-1", "--format=%h %s", commit).stdout.strip()


def rewrite_line(line: str, moved: dict[str, str]) -> str:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    for pattern in PATH_LINES:
        match = pattern.match(body)
        if not match:
            continue
        groups = list(match.groups())
        for name in ("a", "b"):
            if name in pattern.groupindex:
                index = pattern.groupindex[name] - 1
                groups[index] = moved.get(groups[index], groups[index])
        return "".join(groups) + ending
    return line


def rewrite_series(series: str, moved: dict[str, str]) -> str:
    return "".join(rewrite_line(line, moved) for line in series.splitlines(keepends=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("range", help="commit range to port, for example 6596aa0..upstream/main")
    parser.add_argument(
        "--dry-run", action="store_true", help="scan and print the rewritten series; apply nothing"
    )
    args = parser.parse_args(argv)

    moved, mixed = load_map()
    try:
        commits = commits_in(args.range)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip(), file=sys.stderr)
        return 3
    if not commits:
        print(f"nothing to port: {args.range} is empty", file=sys.stderr)
        return 3

    # 1. Scan the whole series before touching anything.
    refused: list[tuple[str, list[str]]] = []
    for commit in commits:
        touched = files_touched(commit)
        hits = [path for path in touched if path in mixed]
        if hits:
            refused.append((commit, hits))
        moved_hits = [f"{path} -> {moved[path]}" for path in touched if path in moved]
        print(f"{subject(commit)}: " + (", ".join(moved_hits) if moved_hits else "no moved file"))
    if refused:
        print(
            f"\nrefused: {len(refused)} commit(s) touch a mixed file whose content was split "
            "between core and mower_sdk/legacy/; nothing was applied. Classify each hunk by "
            "hand (core file or extracted file) and apply it manually:",
            file=sys.stderr,
        )
        for commit, hits in refused:
            print(f"  {subject(commit)}: {', '.join(hits)}", file=sys.stderr)
        return 2

    # 2. Rewrite the series.
    series = git("format-patch", "--stdout", args.range).stdout
    rewritten = rewrite_series(series, moved)
    if args.dry_run:
        sys.stdout.write(rewritten)
        return 0

    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        print("refused: the working tree has uncommitted changes; git am needs a clean index", file=sys.stderr)
        print(dirty, file=sys.stderr)
        return 3

    # 3. Apply with the three-way fallback; git am reads the mbox from stdin.
    result = git("am", "-3", input=rewritten, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(
            "\ngit am stopped. Resolve the conflict, git add the file and run "
            "'git am --continue'; 'git am --abort' restores the branch.",
            file=sys.stderr,
        )
        return 1
    print(f"applied {len(commits)} commit(s) from {args.range}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

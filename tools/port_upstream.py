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

# Patch header lines that carry a path. Only these, and the diffstat lines in
# the message part of each patch, are rewritten; hunk bodies and commit
# messages are copied byte for byte.
PATH_LINES = [
    re.compile(r"^(diff --git a/)(?P<a>\S+)( b/)(?P<b>\S+)$"),
    re.compile(r"^(--- a/)(?P<a>.+)$"),
    re.compile(r"^(\+\+\+ b/)(?P<a>.+)$"),
    re.compile(r"^(rename (?:from|to) )(?P<a>.+)$"),
    re.compile(r"^(copy (?:from|to) )(?P<a>.+)$"),
]
DIFFSTAT_LINE = re.compile(r"^( )(?P<a>\S+)(\s+\|\s+\d+.*)$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")


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


def rewrite_path_line(line: str, patterns: list[re.Pattern[str]], moved: dict[str, str]) -> str:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    for pattern in patterns:
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
    """Rewrite the moved paths in a format-patch mbox, and nothing else.

    Each patch is a mail: headers and commit message, a "---" separator, the
    diffstat, then one diff per file. A diff has header lines (diff --git,
    index, mode, rename, copy, --- a/, +++ b/) and hunks; each hunk header
    says how many old and new lines it holds, so the hunk body is copied
    through by counting. Only diff header lines and the diffstat are rewritten;
    hunk bodies and the commit message are never touched, however much they
    look like a path.
    """
    out: list[str] = []
    state = "message"  # message | stat | header | hunk | binary
    old_left = new_left = 0
    for line in series.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if state == "hunk" and (old_left > 0 or new_left > 0):
            if body.startswith("\\"):  # "\ No newline at end of file"
                pass
            elif body.startswith("+"):
                new_left -= 1
            elif body.startswith("-"):
                old_left -= 1
            else:  # a context line (" ..." or, with trailing whitespace stripped, "")
                old_left -= 1
                new_left -= 1
            out.append(line)
            continue
        if body.startswith("From ") and state != "message":
            # The next patch in the mbox.
            state = "message"
        if body.startswith("diff --git "):
            state = "header"
            out.append(rewrite_path_line(line, PATH_LINES[:1], moved))
            continue
        if state in ("header", "hunk"):
            hunk = HUNK_HEADER.match(body)
            if hunk:
                state = "hunk"
                old_left = int(hunk.group("old")) if hunk.group("old") is not None else 1
                new_left = int(hunk.group("new")) if hunk.group("new") is not None else 1
                out.append(line)
                continue
            if body.startswith("GIT binary patch"):
                state = "binary"
                out.append(line)
                continue
            if state == "header":
                out.append(rewrite_path_line(line, PATH_LINES[1:], moved))
                continue
            # After a hunk: the trailer ("-- " and the git version) or the next mail.
            out.append(line)
            continue
        if state == "message" and body == "---":
            state = "stat"
            out.append(line)
            continue
        if state == "stat":
            out.append(rewrite_path_line(line, [DIFFSTAT_LINE], moved))
            continue
        out.append(line)
    return "".join(out)


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

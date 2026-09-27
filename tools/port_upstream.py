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
   extracted line ranges through upstream edits. Merge commits are inspected
   too: ``git format-patch`` writes no patch for a merge, so a merge whose
   tree differs from all of its parents (a conflict resolution or other change
   made in the merge itself) is refused the same way, while a merge that only
   joins its parents is skipped, because the commits it joins are in the
   series and carry its changes.
2. Otherwise it builds an mbox with one entry per commit: git's own mail
   header and message (``git log --pretty=mboxrd``), a ``---`` separator, and
   the commit's diff (``git diff-tree -p``) with the ``a/`` and ``b/`` paths of
   the moved files rewritten through the map. The message and the diff come
   from separate git commands, so the rewriter only ever sees diff text and
   the message is copied byte for byte however much it looks like a patch.
   The mbox is applied with ``git am -3 --patch-format=mboxrd``, so each
   commit lands in ``legacy/`` with its author, date and message, and the
   shims are untouched.
3. A conflict stops ``git am``; translated docstrings are the usual cause and
   would conflict without any move. Resolve it, ``git add`` the file and run
   ``git am --continue``; ``git am --abort`` restores the branch.

Exit status: 0 applied (or dry run), 1 ``git am`` stopped on a conflict,
2 refused because a commit touches a mixed file or is a merge with changes
of its own, 3 usage, git error or nothing to apply.
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

# Diff header lines that carry a path. Only these are rewritten; hunk bodies
# are copied through by counting, and commit messages never reach the rewriter.
DIFF_GIT_LINE = re.compile(r"^(diff --git a/)(?P<a>\S+)( b/)(?P<b>\S+)$")
HEADER_LINES = [
    re.compile(r"^(--- a/)(?P<a>.+)$"),
    re.compile(r"^(\+\+\+ b/)(?P<a>.+)$"),
    re.compile(r"^(rename (?:from|to) )(?P<a>.+)$"),
    re.compile(r"^(copy (?:from|to) )(?P<a>.+)$"),
]
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")


def git(*args: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=check, **kwargs
    )


def load_map() -> tuple[dict[str, str], list[str]]:
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return dict(data["moved"]), list(data["mixed"])


def commits_in(range_spec: str) -> list[tuple[str, bool]]:
    """The commits of the range, oldest first, each with whether it is a merge."""
    out = git("rev-list", "--reverse", "--parents", range_spec).stdout
    return [(fields[0], len(fields) > 2) for line in out.splitlines() if (fields := line.split())]


def files_touched(commit: str, is_merge: bool) -> list[str]:
    """Files the commit changes; for a merge, only those that differ from all its parents."""
    options = ["-c"] if is_merge else ["--root"]
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", *options, commit).stdout
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


def rewrite_diff(diff: str, moved: dict[str, str]) -> str:
    """Rewrite the moved paths in the header lines of a diff, and nothing else.

    A diff is a sequence of per-file sections, each a "diff --git" line, header
    lines (index, mode, similarity, rename, copy, --- a/, +++ b/) and hunks. A
    hunk header says how many old and new lines the hunk holds, so hunk bodies
    are copied through by counting, however much a line in them looks like a
    header. Binary patches are copied through to the next section.
    """
    out: list[str] = []
    state = "header"  # header | hunk | binary
    old_left = new_left = 0
    for line in diff.splitlines(keepends=True):
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
        if body.startswith("diff --git "):
            state = "header"
            out.append(rewrite_path_line(line, [DIFF_GIT_LINE], moved))
            continue
        if state == "binary":
            out.append(line)
            continue
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
            out.append(rewrite_path_line(line, HEADER_LINES, moved))
            continue
        out.append(line)
    return "".join(out)


def mail_for(commit: str) -> str:
    """git's own mbox entry for the commit: the From line, the headers and the message.

    The mboxrd form quotes message lines that start with "From ", so the entry
    splits correctly whatever the message holds; git am --patch-format=mboxrd
    unquotes them.
    """
    mail = git("log", "-1", "--pretty=mboxrd", commit).stdout
    return mail if mail.endswith("\n") else mail + "\n"


def diff_for(commit: str) -> str:
    """The commit's diff against its parent, renames detected, binary changes included."""
    return git("diff-tree", "--no-commit-id", "-p", "-M", "--binary", "--root", commit).stdout


def patch_for(commit: str, moved: dict[str, str]) -> str:
    """One mbox entry: the mail, a separator, and the rewritten diff."""
    return mail_for(commit) + "---\n\n" + rewrite_diff(diff_for(commit), moved)


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
    refused: list[tuple[str, str]] = []
    to_apply = 0
    for commit, is_merge in commits:
        touched = files_touched(commit, is_merge)
        if is_merge:
            if touched:
                refused.append((commit, "merge commit with changes of its own: " + ", ".join(touched)))
                print(f"{subject(commit)}: merge commit with changes of its own")
            else:
                print(f"{subject(commit)}: merge commit without changes of its own, skipped")
            continue
        to_apply += 1
        hits = [path for path in touched if path in mixed]
        if hits:
            refused.append((commit, "touches a mixed file: " + ", ".join(hits)))
        moved_hits = [f"{path} -> {moved[path]}" for path in touched if path in moved]
        print(f"{subject(commit)}: " + (", ".join(moved_hits) if moved_hits else "no moved file"))
    if refused:
        print(
            f"\nrefused: {len(refused)} commit(s) need manual classification; nothing was "
            "applied. A commit touching a mixed file has to be split by hand into the core "
            "file and the extracted file; a merge commit with changes of its own has to be "
            "applied by hand, because git format-patch writes no patch for a merge:",
            file=sys.stderr,
        )
        for commit, reason in refused:
            print(f"  {subject(commit)}: {reason}", file=sys.stderr)
        return 2
    if not to_apply:
        print(f"nothing to port: {args.range} holds only merge commits", file=sys.stderr)
        return 3

    # 2. Build the series: git's message for each commit, and its diff rewritten.
    rewritten = "".join(patch_for(commit, moved) for commit, is_merge in commits if not is_merge)
    if args.dry_run:
        sys.stdout.write(rewritten)
        return 0

    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        print("refused: the working tree has uncommitted changes; git am needs a clean index", file=sys.stderr)
        print(dirty, file=sys.stderr)
        return 3

    # 3. Apply with the three-way fallback; git am reads the mbox from stdin.
    result = git("am", "-3", "--patch-format=mboxrd", input=rewritten, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(
            "\ngit am stopped. Resolve the conflict, git add the file and run "
            "'git am --continue'; 'git am --abort' restores the branch.",
            file=sys.stderr,
        )
        return 1
    print(f"applied {to_apply} commit(s) from {args.range}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

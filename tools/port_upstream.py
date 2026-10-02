#!/usr/bin/env python3
"""Port a series of upstream commits into the files that moved to mower_sdk/legacy/.

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
   extracted line ranges through upstream edits. The series is the
   first-parent line of the range: a merge commit is one step on it, carrying
   its net change against its first parent, and the commits it merged are
   squashed into that step (the scan lists them). Applying the steps in order
   reproduces the tree of every commit on the line exactly, so nothing can be
   lost or duplicated, whatever the merge did; the cost is that a merged
   branch's individual commits keep their provenance only through the merge
   message. A step whose net change is empty (an empty commit, or a merge
   that kept its first parent's version) is skipped. The start of the range
   should lie on the first-parent line of its end, as the fork point does.
2. Otherwise it builds an mbox with one entry per commit: git's own mail
   header and message (``git log --pretty=mboxrd``), one trailer naming the
   upstream commit (``Upstream-commit: <sha>``), a ``---`` separator, and the
   commit's diff (``git diff-tree -p``) with the ``a/`` and ``b/`` paths of
   the moved files rewritten through the map. The message and the diff come
   from separate git commands, so the rewriter only ever sees diff text and
   the message is copied byte for byte however much it looks like a patch;
   the trailer goes at its end, in place of any blank lines the message ended
   with (git am drops those in any case). The trailer marks the commit as
   ported:
   the commit-message rules (``tools/gitlint_rules.py``) do not apply to a
   commit that carries it and changes nothing outside ``legacy/`` and the
   mixed files, since its message is upstream's.
   Patches are handled as bytes, split on LF only, so file content is never
   decoded or newline-translated on the way. The mbox is applied with
   ``git am -3 --keep-cr --patch-format=mboxrd``, so each commit lands in
   ``legacy/`` with its author, date, message and bytes, and the shims are
   untouched.
3. A conflict stops ``git am``; translated docstrings are the usual cause and
   would conflict without any move. Resolve it, ``git add`` the file and run
   ``git am --continue``; ``git am --abort`` restores the branch.

Exit status: 0 applied (or dry run); 1 ``git am`` stopped, as it does on a
conflict; 2 refused because a step touches a mixed file, and also argparse's
status for a command line it cannot read; 3 nothing could be applied: git
could not list the range, the range is empty or changes nothing, or tracked
files have uncommitted changes.
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
# Patches are handled as bytes throughout, so file content is never decoded
# or newline-translated on its way from git to git.
DIFF_GIT_LINE = re.compile(rb"^(diff --git a/)(?P<a>\S+)( b/)(?P<b>\S+)$")
HEADER_LINES = [
    re.compile(rb"^(--- a/)(?P<a>.+)$"),
    re.compile(rb"^(\+\+\+ b/)(?P<a>.+)$"),
    re.compile(rb"^(rename (?:from|to) )(?P<a>.+)$"),
    re.compile(rb"^(copy (?:from|to) )(?P<a>.+)$"),
]
HUNK_HEADER = re.compile(rb"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")

# The trailer that marks a ported commit; tools/gitlint_rules.py reads it.
TRAILER = b"Upstream-commit"
# A trailer line: a token of letters, digits and hyphens, a colon and a value
# (the same shape tools/gatelib.py takes for one).
TRAILER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]*: \S")


def git(*args: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run git and capture its output as text; for commit lists, names and status.

    Args:
        *args: git's arguments. The command is run as git -C REPO_ROOT, so
            it reads this repository whatever the current directory is.
        check: Whether a nonzero exit status of git is an error.
        **kwargs: Further keyword arguments for subprocess.run.

    Returns:
        The completed process, with its standard output and error as text.

    Raises:
        subprocess.CalledProcessError: check is true and git exits with a
            nonzero status.
    """
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=check, **kwargs
    )


def git_bytes(
    *args: str, check: bool = True, **kwargs: object
) -> subprocess.CompletedProcess[bytes]:
    """Run git and capture its output as bytes; for anything that carries file content.

    Args:
        *args: git's arguments. The command is run as git -C REPO_ROOT, so
            it reads this repository whatever the current directory is.
        check: Whether a nonzero exit status of git is an error.
        **kwargs: Further keyword arguments for subprocess.run, such as the
            input to send to git.

    Returns:
        The completed process, with its standard output and error as bytes.

    Raises:
        subprocess.CalledProcessError: check is true and git exits with a
            nonzero status.
    """
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, check=check, **kwargs
    )


def load_map() -> tuple[dict[str, str], list[str]]:
    """Read the path map, tools/upstream_path_map.json.

    Returns:
        Two values. The moved files: each file that moved whole into
        mower_sdk/legacy/, its upstream path mapped to its path here. The
        mixed files: the paths of the core files whose content was split
        between core and legacy.

    Raises:
        FileNotFoundError: The path map is not beside the tool.
    """
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return dict(data["moved"]), list(data["mixed"])


def encode_map(moved: dict[str, str]) -> dict[bytes, bytes]:
    """Give the map of moved files as bytes, to match against the bytes of a diff.

    Args:
        moved: The moved files, old path to new, as load_map returns them.

    Returns:
        The same map with every path encoded as UTF-8.
    """
    return {old.encode("utf-8"): new.encode("utf-8") for old, new in moved.items()}


def commits_in(range_spec: str) -> list[tuple[str, list[str]]]:
    """List the first-parent line of the range, oldest first, each commit with its parents.

    A merge is one step on that line, ported as its net change against its
    first parent; the commits it merged are not visited.

    Args:
        range_spec: The commit range, as git rev-list takes it.

    Returns:
        One pair for each commit on the line: its full hash, and the full
        hashes of its parents with the first parent first (none for a root
        commit). Empty when the range holds no commit.

    Raises:
        subprocess.CalledProcessError: git cannot list the range, an unknown
            revision for example.
    """
    out = git("rev-list", "--reverse", "--first-parent", "--parents", range_spec).stdout
    return [(fields[0], fields[1:]) for line in out.splitlines() if (fields := line.split())]


def trees_of(commit: str, parents: list[str]) -> list[str]:
    """Give the diff-tree arguments for the commit's change against its first parent.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.

    Returns:
        The first parent and the commit; for a root commit, --root and the
        commit, which makes git diff-tree show everything in it as added.
    """
    return [parents[0], commit] if parents else ["--root", commit]


def files_touched(commit: str, parents: list[str]) -> list[str]:
    """List the files the commit changes against its first parent.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.

    Returns:
        The paths as git diff-tree --name-only prints them, read through
        every directory. No rename detection is asked for, so a renamed file
        is listed under both its names. Empty when the commit changes nothing
        against its first parent.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", *trees_of(commit, parents)).stdout
    return [line for line in out.splitlines() if line]


def merged_commits(parents: list[str]) -> list[str]:
    """List the commits a merge brought in: reachable from its other parents, not from its first.

    Args:
        parents: The parents of a merge commit, the first parent first.

    Returns:
        The full hashes of those commits, in the order git rev-list prints
        them.

    Raises:
        subprocess.CalledProcessError: git cannot read the parents.
    """
    return git("rev-list", f"^{parents[0]}", *parents[1:]).stdout.split()


def subject(commit: str) -> str:
    """Give the commit's abbreviated hash and subject, to name it in a line of output.

    Args:
        commit: The commit.

    Returns:
        The abbreviated hash, a space and the subject line.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    return git("log", "-1", "--format=%h %s", commit).stdout.strip()


def split_lines(data: bytes) -> list[bytes]:
    """Split on LF only, keeping the line ends: git counts hunk lines the same way.

    bytes.splitlines would also split on a lone CR, and str.splitlines on
    vertical tabs, form feeds and the like; git treats all of those as
    ordinary content, and splitting on them would throw the hunk counting off.

    Args:
        data: The bytes to split.

    Returns:
        The lines in order, each with its LF; the last one has none when the
        data does not end with an LF. Joined, they are the data again. Empty
        for empty data.
    """
    pieces = data.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def rewrite_path_line(
    line: bytes, patterns: list[re.Pattern[bytes]], moved: dict[bytes, bytes]
) -> bytes:
    """Rewrite the moved paths in one header line of a diff.

    The first pattern that matches the line, taken without its line end,
    decides. The text of its groups named a and b is replaced when it is,
    byte for byte, a key of moved, and the line is put together again from
    all the pattern's groups.

    Args:
        line: One line of a diff, with its line end.
        patterns: The expressions to try, in order. Each captures the whole
            line in its groups, and the paths in groups named a and b.
        moved: The moved files as bytes, old path to new.

    Returns:
        The line with its moved paths rewritten and its line end as it was;
        the line unchanged when no pattern matches or it names no moved file.
    """
    body = line.rstrip(b"\r\n")
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
        return b"".join(groups) + ending
    return line


def rewrite_diff(diff: bytes, moved: dict[bytes, bytes]) -> bytes:
    """Rewrite the moved paths in the header lines of a diff, and nothing else.

    A diff is a sequence of per-file sections, each a "diff --git" line, header
    lines (index, mode, similarity, rename, copy, --- a/, +++ b/) and hunks. A
    hunk header says how many old and new lines the hunk holds, so hunk bodies
    are copied through by counting, however much a line in them looks like a
    header. Binary patches are copied through to the next section.

    Args:
        diff: The diff of one commit, as git diff-tree -p prints it.
        moved: The moved files as bytes, old path to new.

    Returns:
        The diff with each moved path in a "diff --git", "--- a/", "+++ b/",
        rename or copy line replaced by its new path, and every other byte as
        it was.
    """
    out: list[bytes] = []
    state = "header"  # header | hunk | binary
    old_left = new_left = 0
    for line in split_lines(diff):
        body = line.rstrip(b"\r\n")
        if state == "hunk" and (old_left > 0 or new_left > 0):
            if body.startswith(b"\\"):  # "\ No newline at end of file"
                pass
            elif body.startswith(b"+"):
                new_left -= 1
            elif body.startswith(b"-"):
                old_left -= 1
            else:  # a context line (" ..." or, with trailing whitespace stripped, "")
                old_left -= 1
                new_left -= 1
            out.append(line)
            continue
        if body.startswith(b"diff --git "):
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
        if body.startswith(b"GIT binary patch"):
            state = "binary"
            out.append(line)
            continue
        if state == "header":
            out.append(rewrite_path_line(line, HEADER_LINES, moved))
            continue
        out.append(line)
    return b"".join(out)


def mail_for(commit: str) -> bytes:
    """Git's own mbox entry for the commit: the From line, the headers and the message.

    The mboxrd form quotes message lines that start with "From ", so the entry
    splits correctly whatever the message holds; git am --patch-format=mboxrd
    unquotes them.

    Args:
        commit: The commit.

    Returns:
        The entry as git log --pretty=mboxrd prints it, ending with an LF
        (one is added when git's output has none).

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    mail = git_bytes("log", "-1", "--pretty=mboxrd", commit).stdout
    return mail if mail.endswith(b"\n") else mail + b"\n"


def with_trailer(mail: bytes, commit: str) -> bytes:
    """The mbox entry with one trailer naming the upstream commit at the end of its message.

    Blank lines at the end of the message are dropped, and nothing else of it
    changes. The trailer joins the message's last paragraph when every line of
    it is a trailer already (a Signed-off-by, say), and otherwise stands as a
    paragraph of its own, which is where git looks for trailers. When nothing
    follows the headers, the trailer is the whole body.

    Args:
        mail: The mbox entry of the commit, as mail_for returns it: the
            headers, a blank line and the message's body.
        commit: The upstream commit's hash, for the trailer's value.

    Returns:
        The entry with the line "Upstream-commit: <commit>" as its last line.
    """
    headers, _, body = mail.partition(b"\n\n")
    body = body.rstrip(b"\n")
    last_paragraph = body.rsplit(b"\n\n", 1)[-1].split(b"\n") if body else []
    in_trailer_block = bool(last_paragraph) and all(
        TRAILER_LINE.match(line) for line in last_paragraph
    )
    gap = b"" if not body else b"\n" if in_trailer_block else b"\n\n"
    return headers + b"\n\n" + body + gap + TRAILER + b": " + commit.encode("ascii") + b"\n"


def diff_for(commit: str, parents: list[str]) -> bytes:
    """Give the commit's diff against its first parent, renames detected, binary changes included.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.

    Returns:
        The diff as git diff-tree -p -M --binary prints it.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    return git_bytes(
        "diff-tree", "--no-commit-id", "-p", "-M", "--binary", *trees_of(commit, parents)
    ).stdout


def patch_for(commit: str, parents: list[str], moved: dict[bytes, bytes]) -> bytes:
    """Build one mbox entry: the mail with its trailer, a separator, and the rewritten diff.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.
        moved: The moved files as bytes, old path to new.

    Returns:
        The entry for git am: the commit's mail ending in the Upstream-commit
        trailer, a "---" line and a blank line, and the commit's diff with the
        moved paths rewritten.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    return (
        with_trailer(mail_for(commit), commit)
        + b"---\n\n"
        + rewrite_diff(diff_for(commit, parents), moved)
    )


def main(argv: list[str] | None = None) -> int:
    """Port the commits of a range, or with --dry-run print the series and apply nothing.

    The module's description gives the procedure. The scan prints a line for
    each step on the first-parent line of the range, and for a merge the
    commits it squashes. A refusal and its reason go to standard error. With
    --dry-run the mbox is written to standard output, and the working tree
    is not looked at; otherwise the mbox is applied with git am, whose
    output is passed on.

    Args:
        argv: The command line without the program's name; None reads
            sys.argv.

    Returns:
        The exit status.
        0: every step was applied, or the dry run printed the series.
        1: git am exited with an error, as it does when it stops on a
        conflict; standard error then says how to continue or abort.
        2: a step touches a mixed file, with or without --dry-run; nothing
        was applied.
        3: nothing could be applied. git could not list the range; the range
        holds no commit; no step on its first-parent line changes anything
        against its first parent; or, without --dry-run, tracked files have
        uncommitted changes.

    Raises:
        SystemExit: The command line is not valid: argparse exits with
            status 2, the number that otherwise means a mixed file. Also for
            --help, with status 0.
        subprocess.CalledProcessError: git fails after the range was listed:
            reading a commit's files, subject, mail or diff, or the state of
            the working tree.
    """
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
    to_apply: list[tuple[str, list[str]]] = []
    for commit, parents in commits:
        touched = files_touched(commit, parents)
        if not touched:
            print(f"{subject(commit)}: no change against its first parent, skipped")
            continue
        if len(parents) > 1:
            squashed = merged_commits(parents)
            print(
                f"{subject(commit)}: merge commit, ported as its net change against its "
                f"first parent, squashing {len(squashed)} commit(s):"
            )
            for sha in squashed:
                print(f"    {subject(sha)}")
        to_apply.append((commit, parents))
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
    if not to_apply:
        print(
            f"nothing to port: no commit on the first-parent line of {args.range} changes "
            "anything against its first parent",
            file=sys.stderr,
        )
        return 3

    # 2. Build the series: git's message for each commit, and its diff rewritten.
    moved_bytes = encode_map(moved)
    rewritten = b"".join(patch_for(commit, parents, moved_bytes) for commit, parents in to_apply)
    if args.dry_run:
        sys.stdout.flush()
        sys.stdout.buffer.write(rewritten)
        sys.stdout.buffer.flush()
        return 0

    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        print(
            "refused: the working tree has uncommitted changes; git am needs a clean index",
            file=sys.stderr,
        )
        print(dirty, file=sys.stderr)
        return 3

    # 3. Apply with the three-way fallback; git am reads the mbox from stdin. --keep-cr
    # stops git mailsplit from stripping the CR of CRLF line ends, which here can only
    # be file content, because the mbox itself is written with LF ends.
    result = git_bytes(
        "am", "-3", "--keep-cr", "--patch-format=mboxrd", input=rewritten, check=False
    )
    sys.stdout.flush()
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.flush()
    sys.stderr.buffer.write(result.stderr)
    sys.stderr.buffer.flush()
    if result.returncode != 0:
        print(
            "\ngit am stopped. Resolve the conflict, git add the file and run "
            "'git am --continue'; 'git am --abort' restores the branch.",
            file=sys.stderr,
        )
        return 1
    print(f"applied {len(to_apply)} commit(s) from {args.range}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Port a series of upstream commits into the files that moved to mower_sdk/legacy/.

    python tools/port_upstream.py <range>              e.g. 6596aa0..upstream/main
    python tools/port_upstream.py --dry-run <range>    scan and print, apply nothing
    python tools/port_upstream.py --continue           go on after resolving a conflict
    python tools/port_upstream.py --skip               drop the step that stopped the port, go on
    python tools/port_upstream.py --abort              put the branch back where the port started

A plain ``git merge upstream/main`` conflicts in every shim at a vacated path,
because a merge compares only the merge base and the two tips: at our tip the
old path still exists, so upstream's edit and our shim look like two edits of
one file, and nothing reaches ``mower_sdk/legacy/``. This tool ports a series
commit by commit instead:

1. It scans the whole series first. If any commit touches a mixed file (one
   whose content was split between core and ``legacy/`` by extraction, listed
   in ``tools/upstream_path_map.json``), it stops before applying anything and
   lists those commits for manual classification. It does not try to track
   the extracted line ranges through upstream edits. The series is the
   first-parent line of the range: a merge commit is one step on it, carrying
   its net change against its first parent, and the commits it merged are
   squashed into that step (the scan lists them). Applying the steps in order
   reproduces the tree of every commit on the line exactly, so nothing can be
   lost or duplicated, whatever the merge did; the cost is that a merged
   branch's individual commits keep their provenance only through the merge
   message. A step whose net change is empty (an empty commit, or a merge
   that kept its first parent's version) is skipped. The start of the range
   should lie on the first-parent line of its end, as the fork point does.
2. Otherwise it ports each step in turn. The commit's diff (``git diff-tree
   -p``) with the ``a/`` and ``b/`` paths of the moved files rewritten
   through the map is applied to the index and the working tree with ``git
   apply --3way``, and the result is committed with git's plumbing (``git
   write-tree``, ``git commit-tree``, ``git update-ref``): the message is the
   upstream commit's own, read from the commit object and written to the new
   one byte for byte, with one trailer naming the upstream commit
   (``Upstream-commit: <sha>``) at its end, in place of any blank lines the
   message ended with; the author and the author date are upstream's, and
   the committer is the user, as for any commit. No mail format lies between
   the two commits, so a message is never cut, reshaped or trimmed on the
   way, whatever it holds. (``git am`` reads its message from a mail and ends
   it at a line it takes for the start of the patch, a ``---`` line among
   others, joins a first paragraph of several lines into one subject, strips
   a bracketed prefix from it, reads a body that starts with a ``From:``,
   ``Subject:`` or ``Date:`` line as mail headers, and trims whitespace.) The
   commit is signed when ``commit.gpgSign`` is set, as ``git am`` signed. No
   commit hook runs: ``git commit``'s pre-commit and commit-msg hooks did not
   run under ``git am`` either, so the port is not held to this project's
   commit rules, and ``git am``'s own applypatch-msg, pre-applypatch and
   post-applypatch hooks, which this repository does not install, no longer
   run; the reference-transaction hook runs for the ref update, as for any.
   The trailer marks the commit as ported: the commit-message rules
   (``tools/gitlint_rules.py``) do not apply to a commit that carries it and
   changes nothing outside ``legacy/`` and the mixed files, since its message
   is upstream's. Patches are handled as bytes, split on LF only, so file
   content is never decoded or newline-translated on the way, and each step
   lands in ``legacy/`` with upstream's author, date, message and bytes, the
   shims untouched. A step whose change is already in the tree, because this
   repository took it earlier, is reported and makes no commit.
3. A conflict stops the port, with the step's files unmerged in the index
   and conflict markers in the working tree; translated docstrings are the
   usual cause and would conflict without any move. A step that cannot be
   applied at all stops it the same way, with its patch written to
   ``port-upstream/patch`` under the git directory for applying by hand, and
   so does a commit git refuses (a signature it cannot make, say), with the
   step applied and staged. Resolve, ``git add`` each file, and run the tool
   with ``--continue``: it commits the step with upstream's message and
   author and goes on with the rest. ``--skip`` drops the step instead and goes on, discarding every
   change to a tracked file in the index and the working tree, as ``git am
   --skip`` does. ``--abort`` puts the branch and the working tree back where
   the port started, as ``git am --abort`` does, unless commits were made
   since the port stopped. Until one of these, the tool refuses a new range.

Exit status: 0 applied (or dry run); 1 the port stopped on a step that
conflicted, could not be applied or could not be committed, or
``--continue`` found unmerged files or nothing staged, or ``--abort`` found
commits made since the stop; 2
refused because a step touches a mixed file, and also argparse's status for
a command line it cannot read; 3 nothing could be applied: git could not
list the range, the range is empty or changes nothing, tracked files have
uncommitted changes, a port is in progress, or ``--continue``, ``--skip`` or
``--abort`` found none.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
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
# The value of a commit object's author header: name, address, seconds since
# the epoch and the offset, as git writes it.
AUTHOR_LINE = re.compile(rb"^(?P<name>.*) <(?P<email>[^>]*)> (?P<date>\d+ [+-]\d{4})$")

# The directory under the git directory that holds a stopped port, and its state
# file; a per-worktree path, as git's own rebase-apply is.
STATE_DIR = "port-upstream"
STATE_FILE = "state.json"
PATCH_FILE = "patch"

Step = tuple[str, list[str]]


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


def commits_in(range_spec: str) -> list[Step]:
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


def commit_object(commit: str) -> tuple[bytes, bytes | None, bytes]:
    """Read the commit's author line, encoding and message from the commit object.

    The object is read as git stores it, so nothing is re-encoded, folded or
    trimmed on the way: what git log would show is not what is wanted here,
    the bytes are.

    Args:
        commit: The commit.

    Returns:
        Three values: the value of the author header (name, address, seconds
        since the epoch and offset, as git writes it); the value of the
        encoding header, or None when the commit has none, which means UTF-8;
        and the message, every byte after the blank line that ends the
        headers.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
        ValueError: The object has no author header, which git never writes.
    """
    raw = git_bytes("cat-file", "commit", commit).stdout
    headers, _, message = raw.partition(b"\n\n")
    author = encoding = None
    for line in headers.split(b"\n"):
        if line.startswith(b"author "):
            author = line[len(b"author ") :]
        elif line.startswith(b"encoding "):
            encoding = line[len(b"encoding ") :]
    if author is None:
        raise ValueError(f"commit {commit} has no author header")
    return author, encoding, message


def with_trailer(message: bytes, commit: str) -> bytes:
    """The message with one trailer naming the upstream commit at its end.

    Blank lines at the end of the message are dropped, and nothing else of it
    changes. The trailer joins the message's last paragraph when that is not
    the first one, the title, and every line of it is a trailer already (a
    Signed-off-by, say); otherwise it stands as a paragraph of its own. Git
    reads trailers from the last paragraph and never from the title, so a
    one-line message in the shape of a trailer ("fix: ...") gets its own
    paragraph too. For an empty message, the trailer is the whole message.

    Args:
        message: The commit's message, as commit_object returns it.
        commit: The upstream commit's hash, for the trailer's value.

    Returns:
        The message with the line "Upstream-commit: <commit>" as its last
        line, ending in an LF.
    """
    body = message.rstrip(b"\n")
    _, after_title, rest = body.partition(b"\n\n")
    last_paragraph = rest.rsplit(b"\n\n", 1)[-1].split(b"\n") if after_title else []
    in_trailer_block = bool(last_paragraph) and all(
        TRAILER_LINE.match(line) for line in last_paragraph
    )
    gap = b"" if not body else b"\n" if in_trailer_block else b"\n\n"
    return body + gap + TRAILER + b": " + commit.encode("ascii") + b"\n"


def diff_for(commit: str, parents: list[str]) -> bytes:
    """Give the commit's diff against its first parent, renames detected, binary changes included.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.

    Returns:
        The diff as git diff-tree -p -M --binary --full-index prints it; the
        full blob ids let git apply find the blobs for a three-way merge.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    return git_bytes(
        "diff-tree",
        "--no-commit-id",
        "-p",
        "-M",
        "--binary",
        "--full-index",
        *trees_of(commit, parents),
    ).stdout


def patch_for(commit: str, parents: list[str], moved: dict[bytes, bytes]) -> bytes:
    """Build the patch of one step: the commit's diff with the moved paths rewritten.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.
        moved: The moved files as bytes, old path to new.

    Returns:
        The diff git apply is given.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    return rewrite_diff(diff_for(commit, parents), moved)


def preview(commit: str, parents: list[str], moved: dict[bytes, bytes]) -> bytes:
    """Show one step as the dry run prints it: the commit to be made, then its patch.

    Args:
        commit: The commit.
        parents: Its parents, the first parent first; empty for a root commit.
        moved: The moved files as bytes, old path to new.

    Returns:
        A "commit" line with the upstream hash, an "author" line as the commit
        object has it, a blank line, the message with its trailer, a blank
        line and the patch.

    Raises:
        subprocess.CalledProcessError: git cannot read the commit.
    """
    author, _, message = commit_object(commit)
    return (
        b"commit "
        + commit.encode("ascii")
        + b"\nauthor "
        + author
        + b"\n\n"
        + with_trailer(message, commit)
        + b"\n"
        + patch_for(commit, parents, moved)
    )


def state_dir() -> Path:
    """The directory of a stopped port, under the git directory of this worktree.

    Returns:
        The path, which exists only while a port is stopped.
    """
    return Path(git("rev-parse", "--path-format=absolute", "--git-path", STATE_DIR).stdout.strip())


@dataclass
class Port:
    """A port in progress, as kept under the git directory from its start to its end.

    Attributes:
        head: Where HEAD was when the port started; --abort resets to it.
        range: The range being ported, for the messages.
        steps: The steps still to apply, oldest first.
        current: The step applied and waiting for its commit, or None.
        stopped_at: Where HEAD was when the port stopped, or None while it
            runs; --abort resets nothing when HEAD has moved since.
    """

    head: str
    range: str
    steps: list[Step]
    current: Step | None = None
    stopped_at: str | None = None

    @classmethod
    def load(cls) -> Port | None:
        """Read the port kept under the git directory.

        Returns:
            The port as save() wrote it, or None when none is kept.
        """
        path = state_dir() / STATE_FILE
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        current = data["current"]
        return cls(
            head=data["head"],
            range=data["range"],
            steps=[(sha, list(parents)) for sha, parents in data["steps"]],
            current=None if current is None else (current[0], list(current[1])),
            stopped_at=data["stopped_at"],
        )

    def save(self) -> None:
        """Write the port under the git directory, creating its directory."""
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / STATE_FILE).write_text(
            json.dumps(dataclasses.asdict(self), indent=1), encoding="utf-8"
        )


def clear_state() -> None:
    """Remove the stopped port's directory and everything in it."""
    directory = state_dir()
    if not directory.exists():
        return
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()


def head() -> str:
    """The full hash HEAD points at.

    Returns:
        The hash.
    """
    return git("rev-parse", "--verify", "HEAD").stdout.strip()


def unmerged_paths() -> list[str]:
    """The paths left unmerged in the index.

    Returns:
        The paths, one per conflicted file; empty when nothing is unmerged.
    """
    return git("diff", "--name-only", "--diff-filter=U").stdout.split()


def index_tree() -> str:
    """Write the index as a tree and give its hash.

    Returns:
        The tree's hash.

    Raises:
        subprocess.CalledProcessError: The index holds an unmerged path.
    """
    return git("write-tree").stdout.strip()


def apply_patch(patch: bytes) -> subprocess.CompletedProcess[bytes]:
    """Apply a patch to the index and the working tree, with a three-way merge as the fallback.

    Args:
        patch: The patch, as patch_for returns it.

    Returns:
        git apply's completed process: status 0 when the patch applied cleanly
        or was already in the tree, nonzero when it left conflicts or could
        not be applied at all. Upstream's whitespace is not reported.
    """
    return git_bytes("apply", "--3way", "--whitespace=nowarn", input=patch, check=False)


def signing_configured() -> bool:
    """Say whether commit.gpgSign asks for commits to be signed.

    Returns:
        True when the configuration in force for this repository sets
        commit.gpgSign to true; False when it is unset or false.
    """
    result = git("config", "--type=bool", "--get", "commit.gpgsign", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def commit_step(commit: str) -> str:
    """Commit the index as the port of an upstream commit, with its message, author and date.

    The message is the upstream commit's with the trailer added (see
    with_trailer). Its encoding header, when it has one, is carried over, and
    without one the commit is made as UTF-8, whatever i18n.commitEncoding is
    set to here, since no header means UTF-8. The author and the author date
    are upstream's; the committer and the commit date are the user's, and the
    commit is signed when commit.gpgSign is set, as for any commit. No commit
    hook runs; the reference-transaction hook does, for the ref update.

    Args:
        commit: The upstream commit the index holds the port of.

    Returns:
        The hash of the new commit, which HEAD now points at.

    Raises:
        subprocess.CalledProcessError: git could not write the tree or the
            commit, or HEAD moved under the port.
        ValueError: The upstream commit object has no author header.
    """
    author, encoding, message = commit_object(commit)
    match = AUTHOR_LINE.match(author)
    if match is None:
        raise ValueError(
            f"commit {commit} has an author header git cannot have written: {author!r}"
        )
    env = dict(os.environ)
    for name, value in match.groupdict().items():
        env[f"GIT_AUTHOR_{name.upper()}"] = value.decode("utf-8", "surrogateescape")
    config = ["-c", "i18n.commitEncoding=" + (encoding.decode("ascii") if encoding else "UTF-8")]
    sign = ["-S"] if signing_configured() else []
    parent = head()
    new = git_bytes(
        *config,
        "commit-tree",
        "-p",
        parent,
        *sign,
        index_tree(),
        input=with_trailer(message, commit),
        env=env,
    ).stdout.strip()
    git(
        "update-ref", "-m", f"port_upstream: {subject(commit)}", "HEAD", new.decode("ascii"), parent
    )
    return new.decode("ascii")


HOW_TO_GO_ON = (
    "and run the tool with --continue; --skip drops the step; --abort puts the branch back."
)


def commit_current(port: Port) -> bool:
    """Commit the port's current step, which is applied and staged, and clear it.

    When git refuses the commit (a signature it cannot make, say), the port
    is left as it is, with the step still current, applied and staged, so
    --continue tries the commit again once the cause is fixed, --skip drops
    the step and --abort puts the branch back.

    Args:
        port: The port, with current the step to commit; it is saved.

    Returns:
        True when the step was committed, with a line printed for it; False
        when git refused the commit, with git's message and how to go on on
        standard error.
    """
    assert port.current is not None
    commit, _ = port.current
    try:
        new = commit_step(commit)
    except subprocess.CalledProcessError as exc:
        error = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace")
        print(error.rstrip(), file=sys.stderr)
        print(
            f"\nthe port stopped at {subject(commit)}: git could not make the commit; the step "
            "is applied and staged. Fix the cause " + HOW_TO_GO_ON,
            file=sys.stderr,
        )
        return False
    port.current = None
    port.stopped_at = None
    port.save()
    print(f"{subject(commit)}: ported as {new[:7]}")
    return True


def port_steps(port: Port, moved: dict[bytes, bytes]) -> int:
    """Apply and commit the steps the port still holds, stopping at the first that fails.

    Each step is saved as the port's current step from its application to
    its commit, so a stop anywhere in between is resumable.

    Args:
        port: The port, with the steps still to apply, oldest first; it is
            saved as each one is applied and again as it is committed.
        moved: The moved files as bytes, old path to new.

    Returns:
        0 when every step was applied, the port forgotten and a line printed
        for each step; 1 when a step conflicted, could not be applied or
        could not be committed, the port saved with that step as current, its
        patch written beside it and standard error saying how to go on.
    """
    while port.steps:
        commit, parents = port.steps[0]
        patch = patch_for(commit, parents, moved)
        result = apply_patch(patch)
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.flush()
        port.current = port.steps.pop(0)
        port.stopped_at = head()
        port.save()
        (state_dir() / PATCH_FILE).write_bytes(patch)
        if result.returncode != 0:
            print(
                f"\nthe port stopped at {subject(commit)}: "
                + (
                    "resolve the conflicts, git add each file, "
                    if unmerged_paths()
                    else "the patch did not apply; it is at "
                    f"{state_dir() / PATCH_FILE} for applying by hand, then git add the files, "
                )
                + HOW_TO_GO_ON,
                file=sys.stderr,
            )
            return 1
        if index_tree() == git("rev-parse", "HEAD^{tree}").stdout.strip():
            print(f"{subject(commit)}: already in the tree, no commit made")
            port.current = None
            port.stopped_at = None
            port.save()
        elif not commit_current(port):
            return 1
    clear_state()
    print(f"applied {port.range}")
    return 0


def resume(skip: bool, moved: dict[bytes, bytes]) -> int:
    """Go on with a stopped port: commit the resolved step, or drop it, then port the rest.

    Args:
        skip: True drops the stopped step: the index and the working tree are
            reset to HEAD, discarding every change to a tracked file. False
            commits the index as that step.
        moved: The moved files as bytes, old path to new.

    Returns:
        What port_steps returns for the remaining steps; 1 when the step
        cannot be committed because files are still unmerged or nothing is
        staged, or git refuses the commit; 3 when no port is stopped.
    """
    port = Port.load()
    if port is None or port.current is None:
        print("nothing to continue: no port is stopped", file=sys.stderr)
        return 3
    commit, _ = port.current
    if skip:
        git("reset", "-q", "--hard", "HEAD")
        print(f"{subject(commit)}: skipped")
    else:
        if unmerged := unmerged_paths():
            print(
                "files are still unmerged; resolve each and git add it, or --skip the step: "
                + ", ".join(unmerged),
                file=sys.stderr,
            )
            return 1
        if index_tree() == git("rev-parse", "HEAD^{tree}").stdout.strip():
            print(
                f"nothing is staged for {subject(commit)}: git add the resolved files, or "
                "--skip the step if its change is already in the tree",
                file=sys.stderr,
            )
            return 1
        if not commit_current(port):
            return 1
        return port_steps(port, moved)
    port.current = None
    port.stopped_at = None
    port.save()
    return port_steps(port, moved)


def abort() -> int:
    """Put the branch and the working tree back where the port started, and forget the port.

    Returns:
        0 when HEAD was reset to where the port started; 1 when commits were
        made since the port stopped, in which case nothing is reset and the
        port is only forgotten; 3 when no port is stopped.
    """
    port = Port.load()
    if port is None:
        print("nothing to abort: no port is stopped", file=sys.stderr)
        return 3
    if port.stopped_at != head():
        clear_state()
        print(
            "HEAD has moved since the port stopped; the port is forgotten and nothing is reset",
            file=sys.stderr,
        )
        return 1
    git("reset", "-q", "--hard", port.head)
    clear_state()
    print(f"the port of {port.range} is aborted; HEAD is back at {port.head[:7]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Port the commits of a range, print the series with --dry-run, or go on with a stopped port.

    The module's description gives the procedure. The scan prints a line for
    each step on the first-parent line of the range, and for a merge the
    commits it squashes. A refusal and its reason go to standard error. With
    --dry-run each step is printed as preview shows it, and the working tree
    is not looked at; otherwise the steps are applied and committed one by
    one, a line per step. --continue, --skip and --abort act on a stopped port
    and take no range.

    Args:
        argv: The command line without the program's name; None reads
            sys.argv.

    Returns:
        The exit status.
        0: every step was applied, the dry run printed the series, or the
        port was aborted.
        1: the port stopped on a step that conflicted, could not be applied
        or could not be committed; standard error then says how to go on.
        Also --continue with unmerged files or nothing staged, and --abort
        after HEAD moved.
        2: a step touches a mixed file, with or without --dry-run; nothing
        was applied.
        3: nothing could be applied. git could not list the range; the range
        holds no commit; no step on its first-parent line changes anything
        against its first parent; or, without --dry-run, tracked files have
        uncommitted changes or a port is already stopped. Also --continue,
        --skip or --abort with no stopped port.

    Raises:
        SystemExit: The command line is not valid: argparse exits with
            status 2, the number that otherwise means a mixed file. Also for
            --help, with status 0.
        subprocess.CalledProcessError: git fails after the range was listed:
            reading a commit's files, subject or diff, the state of the
            working tree, or writing a commit.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "range", nargs="?", help="commit range to port, for example 6596aa0..upstream/main"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="scan and print the rewritten series; apply nothing"
    )
    stopped = parser.add_mutually_exclusive_group()
    stopped.add_argument(
        "--continue",
        dest="go_on",
        action="store_true",
        help="commit the resolved step of a stopped port and go on with the rest",
    )
    stopped.add_argument(
        "--skip", action="store_true", help="drop the stopped step and go on with the rest"
    )
    stopped.add_argument(
        "--abort", action="store_true", help="put the branch back where the stopped port started"
    )
    args = parser.parse_args(argv)
    if args.go_on or args.skip or args.abort:
        if args.range is not None or args.dry_run:
            parser.error("--continue, --skip and --abort take no range and no --dry-run")
    elif args.range is None:
        parser.error("a range is required")

    moved, mixed = load_map()
    if args.abort:
        return abort()
    if args.go_on or args.skip:
        return resume(args.skip, encode_map(moved))

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
    to_apply: list[Step] = []
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

    # 2. With --dry-run, print each step as it would be committed and applied.
    moved_bytes = encode_map(moved)
    if args.dry_run:
        sys.stdout.flush()
        for commit, parents in to_apply:
            sys.stdout.buffer.write(preview(commit, parents, moved_bytes))
        sys.stdout.buffer.flush()
        return 0

    if Port.load() is not None:
        print(
            "refused: a port is stopped; run the tool with --continue, --skip or --abort first",
            file=sys.stderr,
        )
        return 3
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        print(
            "refused: the working tree has uncommitted changes; the port needs a clean index",
            file=sys.stderr,
        )
        print(dirty, file=sys.stderr)
        return 3

    # 3. Apply and commit the steps one by one; a step that does not apply stops the port.
    port = Port(head=head(), range=args.range, steps=list(to_apply))
    port.save()
    return port_steps(port, moved_bytes)


if __name__ == "__main__":
    sys.exit(main())

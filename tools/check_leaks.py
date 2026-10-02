"""Reject references to files git does not track, and project-specific terms.

Committed text must stand on its own: it may not name a file that exists
only in someone's checkout (an ignored or untracked document in any
worktree), or, when the optional pattern file is present, anything its rules
forbid. A Markdown link to an ignored document is caught by the name it
contains, like any other mention. Local paths in files are the pygrep hook
no-local-paths in .pre-commit-config.yaml; in a commit message they are
checked here, with the same expression, on the lines git commits (comment
lines and a `git commit -v` diff are not part of the message).

Usage:
  check_leaks.py FILE...           the files pre-commit passes (see gatelib for modes)
  check_leaks.py --message FILE    a commit message file
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib


def _local_pattern_hits(
    patterns: list[gatelib.LocalPattern], text: str, scope: str, is_trailer: bool
) -> list[str]:
    """What the rules of the pattern file find in one line.

    A rule is applied when its scope is the given scope or "both". On a
    trailer line a rule that skips trailers is not applied. A rule reports
    its first match in the text and no further one.

    Args:
        patterns: The rules of the pattern file.
        text: The line to search.
        scope: What the line belongs to: "files" or "message".
        is_trailer: Whether the line is in the trailer block of a message.

    Returns:
        One "<rule's name>: <matched text>" for each rule that matches, in
        the order of the rules; an empty list when none does.
    """
    hits = []
    for pattern in patterns:
        if pattern.scope not in (scope, "both"):
            continue
        if is_trailer and pattern.skip_trailers:
            continue
        match = pattern.regex.search(text)
        if match:
            hits.append(f"{pattern.name}: {match.group(0)}")
    return hits


def check_texts(items: list[tuple[str, int, str, bool]], scope: str) -> list[str]:
    """Check (path, line number, text, is_trailer) items; return the findings.

    Each line is searched for three things, reported in this order: a local
    path, with the expression of the no-local-paths hook, only when the scope
    is "message" (in files that hook finds them); the name of a file git does
    not track, in any worktree; and what the rules of the optional pattern
    file forbid (see _local_pattern_hits). Every local path and every
    untracked name in a line is a finding of its own.

    The pattern file is read before anything else, so a Claude session
    without it is refused even when there is nothing to check. With no items
    the worktrees are not listed and the hook configuration is not read.

    Args:
        items: The lines to check, each as (path, line number, text, whether
            the line is a trailer of a message). The path and the number only
            label the finding.
        scope: What the lines belong to: "files" or "message".

    Returns:
        One "<path>:<line number>: <what was found>" for each hit, in the
        order of the items; an empty list when nothing was found.

    Raises:
        SystemExit: The pattern file is missing in a Claude session or holds
            a line that cannot be read (see gatelib.load_local_rules); or the
            scope is "message" and the configuration defines no
            no-local-paths hook.
        ModuleNotFoundError: The scope is "message" and pyyaml, which reads
            the configuration, is not installed.
        FileNotFoundError: The scope is "message" and the repository has no
            pre-commit configuration.
        subprocess.CalledProcessError: A git command fails, as it does when
            the current directory is not inside a git working tree.
    """
    patterns = gatelib.load_local_rules()
    if not items:
        return []  # nothing to read: skip listing every worktree
    untracked = gatelib.untracked_name_regex()
    local_paths = gatelib.local_path_pattern() if scope == "message" else None
    findings = []
    for path, number, text, is_trailer in items:
        hits = []
        if local_paths is not None:
            hits += [f"local path: {m.group(0)}" for m in local_paths.finditer(text)]
        if untracked is not None:
            hits += [
                f"names a file git does not track: {m.group(0)}" for m in untracked.finditer(text)
            ]
        hits += _local_pattern_hits(patterns, text, scope, is_trailer)
        findings += [f"{path}:{number}: {hit}" for hit in hits]
    return findings


def check_files(files: list[str]) -> list[str]:
    """Check the lines of files that the current mode selects.

    The lines are the ones gatelib.lines_to_check gives: those added by the
    staged change or between the two refs of range mode, or every line with
    GATE_MODE=content. They are checked with the scope "files", and none
    counts as a trailer.

    Args:
        files: The paths to check, as pre-commit passes them.

    Returns:
        The findings (see check_texts); an empty list when nothing was found
        or there was no line to check.

    Raises:
        SystemExit: The pattern file is missing in a Claude session or holds
            a line that cannot be read (see gatelib.load_local_rules).
        subprocess.CalledProcessError: A git command fails, as it does on a
            range ref that does not exist.
    """
    items = [
        (path, number, text, False)
        for path, lines in gatelib.lines_to_check(files).items()
        for number, text in lines
    ]
    return check_texts(items, "files")


def check_message(path: str, text: str) -> list[str]:
    """Check a commit message, on the lines git commits.

    Comment lines and a `git commit -v` diff are not part of the message and
    are left out (see gatelib.message_lines). The lines kept are checked with
    the scope "message"; those of the trailer block (see
    gatelib.trailer_start) count as trailers, which a rule of the pattern file
    flagged notrailers does not read.

    Args:
        path: The message file's path. It labels the findings and is not
            read.
        text: The content of the message file.

    Returns:
        The findings (see check_texts), with the line numbers of the text as
        given; an empty list when nothing was found.

    Raises:
        SystemExit: The pattern file is missing in a Claude session or holds
            a line that cannot be read (see gatelib.load_local_rules); or the
            configuration defines no no-local-paths hook.
        ModuleNotFoundError: pyyaml, which reads the configuration, is not
            installed.
        FileNotFoundError: The repository has no pre-commit configuration.
        subprocess.CalledProcessError: A git command fails, as it does when
            the current directory is not inside a git working tree.
    """
    lines = gatelib.message_lines(text)
    start = gatelib.trailer_start([line for _, line in lines])
    items = [(path, number, line, index >= start) for index, (number, line) in enumerate(lines)]
    return check_texts(items, "message")


def main(argv: list[str] | None = None) -> int:
    """Run the check from the command line and print the findings.

    With --message FILE that file is read as UTF-8 and checked as a commit
    message, and file arguments are not read. Without it the files given are
    checked. Each finding is printed on its own line on standard output.

    Args:
        argv: The command line's arguments, without the program's name; None
            reads them from sys.argv.

    Returns:
        The exit status: 0 when nothing was found, 1 when there is at least
        one finding.

    Raises:
        SystemExit: argparse ends the run (status 2 for arguments it does not
            accept, 0 after --help); or a check refuses to run (see
            check_texts).
        subprocess.CalledProcessError: A git command fails (see check_files
            and check_message).
        FileNotFoundError: The file given with --message does not exist, or
            the pre-commit configuration the message check reads is missing.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message", metavar="FILE", help="check a commit message file")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if args.message:
        findings = check_message(args.message, Path(args.message).read_text(encoding="utf-8"))
    else:
        findings = check_files(args.files)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

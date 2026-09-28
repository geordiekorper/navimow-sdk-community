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

import gatelib  # noqa: E402

def _local_pattern_hits(
    patterns: list[gatelib.LocalPattern], text: str, scope: str, is_trailer: bool
) -> list[str]:
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
    """Check (path, line number, text, is_trailer) items; return the findings."""
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
            hits += [f"names a file git does not track: {m.group(0)}" for m in untracked.finditer(text)]
        hits += _local_pattern_hits(patterns, text, scope, is_trailer)
        findings += [f"{path}:{number}: {hit}" for hit in hits]
    return findings


def check_files(files: list[str]) -> list[str]:
    items = [
        (path, number, text, False)
        for path, lines in gatelib.lines_to_check(files).items()
        for number, text in lines
    ]
    return check_texts(items, "files")


def check_message(path: str, text: str) -> list[str]:
    lines = gatelib.message_lines(text)
    start = gatelib.trailer_start([line for _, line in lines])
    items = [(path, number, line, index >= start) for index, (number, line) in enumerate(lines)]
    return check_texts(items, "message")


def main(argv: list[str] | None = None) -> int:
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

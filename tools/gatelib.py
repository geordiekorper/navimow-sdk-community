"""Shared helpers for the commit checks in tools/check_*.py.

The checks run in three modes, chosen from the environment:

* staged (default): the added lines of ``git diff --cached``, as in a
  pre-commit hook;
* range: the added lines of ``git diff FROM...TO`` when pre-commit exports
  ``PRE_COMMIT_FROM_REF`` and ``PRE_COMMIT_TO_REF`` (``pre-commit run
  --from-ref A --to-ref B``), as in CI;
* content: every line of each file when ``GATE_MODE=content``.

An optional pattern file adds project-specific rules. It lives outside the
repository, at ``$COMMIT_GATE_PATTERNS`` or
``<git common dir>/commit-gate/patterns.txt``; without it the checks apply the
generic rules only. Each non-comment line is tab-separated, either
``<scope>\\t<name>\\t<flags>\\t<regex>`` with scope ``files``, ``message`` or
``both`` and flags ``-`` or ``notrailers`` (not applied to trailer lines of a
message), or ``coauthor\\t<exact Co-authored-by value>``.
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCISSORS = "# ------------------------ >8 ------------------------"


def git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    ).stdout


def git_bytes(*args: str, cwd: str | Path | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel").strip())


def common_dir() -> Path:
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())


def tracked_files() -> set[str]:
    """Paths in the index (tracked or staged), unquoted: read NUL-delimited."""
    return {p for p in git("ls-files", "-z").split("\0") if p}


def is_claude() -> bool:
    return os.environ.get("CLAUDECODE") == "1"


def range_refs() -> tuple[str, str] | None:
    source, target = os.environ.get("PRE_COMMIT_FROM_REF"), os.environ.get("PRE_COMMIT_TO_REF")
    return (source, target) if source and target else None


# --- the optional pattern file -----------------------------------------------


@dataclass(frozen=True)
class LocalPattern:
    scope: str
    name: str
    skip_trailers: bool
    regex: re.Pattern[str]


@dataclass
class LocalRules:
    patterns: list[LocalPattern]
    coauthors: list[str]
    source: Path | None


def patterns_file() -> Path:
    override = os.environ.get("COMMIT_GATE_PATTERNS")
    return Path(override) if override else common_dir() / "commit-gate" / "patterns.txt"


def load_local_rules() -> LocalRules:
    """Read the pattern file; a Claude session without it is refused."""
    path = patterns_file()
    if not path.is_file():
        if is_claude():
            raise SystemExit(
                f"commit checks: the local pattern file {path} is missing; "
                "Claude commits need it (run `commit-gate provision`)."
            )
        return LocalRules([], [], None)
    patterns, coauthors = [], []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split("\t")
        if fields[0] == "coauthor" and len(fields) == 2:
            coauthors.append(fields[1])
        elif fields[0] in ("files", "message", "both") and len(fields) == 4:
            patterns.append(
                LocalPattern(fields[0], fields[1], fields[2] == "notrailers", re.compile(fields[3]))
            )
        else:
            raise SystemExit(f"{path}:{number}: malformed pattern line")
    return LocalRules(patterns, coauthors, path)


# --- names of files git does not track ---------------------------------------

_NOISE_PARTS = {
    "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", "build", "dist", "htmlcov", ".git", ".idea", ".vscode",
    # The issue tracker's and its database's directories, spelt in pieces so
    # the names are not in the text this checker reads.
    "." + "beads", "." + "dolt",
}
_NOISE_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".DS_Store", ".coverage", ".db")
_WALK_LIMIT = 5000


# Nested checkouts: every file under them belongs to another worktree, which
# is read on its own. Other local files under .claude/ (an agent definition,
# settings) are ordinary untracked documents and are checked.
_NESTED_WORKTREES = ".claude/" + "worktrees"


def _noise(relative: str) -> bool:
    relative = relative.rstrip("/")
    if relative == _NESTED_WORKTREES or relative.startswith(_NESTED_WORKTREES + "/"):
        return True
    parts = relative.split("/")
    return any(part in _NOISE_PARTS or part.endswith(_NOISE_SUFFIXES) for part in parts)


def _worktrees() -> list[Path]:
    paths = []
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            if path.is_dir():
                paths.append(path)
    return paths


def _untracked_in(worktree: Path) -> set[str]:
    names: set[str] = set()
    listings = [
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
    ]
    for args in listings:
        for entry in git(*args, cwd=worktree, check=False).split("\0"):
            if not entry or _noise(entry):
                continue
            if not entry.endswith("/"):
                names.add(entry)
                continue
            names.add(entry.rstrip("/"))  # the directory itself, then what is in it
            count = 0
            for dirpath, dirnames, filenames in os.walk(worktree / entry):
                dirnames[:] = [
                    d for d in dirnames
                    if not _noise(os.path.relpath(os.path.join(dirpath, d), worktree))
                ]
                for filename in filenames:
                    relative = os.path.relpath(os.path.join(dirpath, filename), worktree)
                    if not _noise(relative):
                        names.add(relative)
                        count += 1
                if count > _WALK_LIMIT:
                    break
    return names


def _excluded_names() -> set[str]:
    exclude = common_dir() / "info" / "exclude"
    names = set()
    if exclude.is_file():
        for line in exclude.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not any(c in line for c in "*?[!"):
                names.add(line.strip("/"))
    return names


def _word_like(name: str) -> bool:
    """A bare name without an extension that reads as an ordinary word ("notes").

    A name with an extension ("a.md", "x.txt") or a long one is specific
    enough to report, however short.
    """
    if "/" in name:
        return False
    stem = name.strip(".")
    has_extension = "." in stem and not stem.endswith(".")
    return not (has_extension or len(name) >= 12)


def untracked_name_regex() -> re.Pattern[str] | None:
    """One expression matching a name of any ignored or untracked file.

    It reads every worktree, because an ignored document may exist only in
    one of them. Names that are tracked (or staged) here are left out, and so
    are short names without an extension, which read as ordinary words. Names
    listed in info/exclude are kept whatever their shape: the owner put them
    there on purpose.
    """
    tracked = tracked_files()
    tracked_basenames = {Path(p).name for p in tracked}
    excluded = _excluded_names()
    candidates: set[str] = set(excluded)
    for worktree in _worktrees():
        for relative in _untracked_in(worktree):
            for name in (relative, Path(relative).name):
                if not _word_like(name):
                    candidates.add(name)
    candidates = {
        c for c in candidates
        if c and c not in tracked and c not in tracked_basenames and len(c) >= 3
    }
    if not candidates:
        return None
    alternation = "|".join(re.escape(c) for c in sorted(candidates, key=len, reverse=True))
    # A name ends where the text stops being a file name: sentence punctuation
    # may follow ("see draft.md."), a further suffix may not ("draft.md.j2").
    return re.compile(r"(?<![\w.-])(?:" + alternation + r")(?![\w-]|\.[\w-])")


# --- which lines to check ----------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_path(raw: str) -> str | None:
    """The path of a ``+++`` header, with git's C-style quoting undone."""
    if raw.endswith("\t"):
        raw = raw[:-1]  # git ends the header with a tab when the name holds a space
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    return raw[2:] if raw.startswith("b/") else raw


def _parse_added(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Added lines per file of a unified diff.

    Hunk lengths are followed, so an added line that happens to start with
    "++ " is content, not a file header.
    """
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    number = old_left = new_left = 0
    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                if path is not None:
                    added.setdefault(path, []).append((number, line[1:]))
                number += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif not line.startswith("\\"):  # "\ No newline at end of file" counts for neither side
                number += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("+++ "):
            path = _diff_path(line[4:])
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1) or 1)
            number = int(hunk.group(2))
            new_left = int(hunk.group(3) or 1)
    return added


def lines_to_check(files: list[str]) -> dict[str, list[tuple[int, str]]]:
    """The lines of ``files`` to inspect in the current mode."""
    if not files:
        return {}
    if os.environ.get("GATE_MODE") == "content":
        result = {}
        for name in files:
            try:
                text = Path(name).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            result[name] = list(enumerate(text.splitlines(), 1))
        return result
    refs = range_refs()
    base = ["diff", f"{refs[0]}...{refs[1]}"] if refs else ["diff", "--cached"]
    diff = git_bytes(
        *base, "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "--", *files
    )
    return _parse_added(diff.decode("utf-8", "replace"))


def changed_files() -> list[tuple[str, str]]:
    """(status, path) of every changed file in the current mode."""
    refs = range_refs()
    base = ["diff", f"{refs[0]}...{refs[1]}"] if refs else ["diff", "--cached"]
    out = git(*base, "--name-status", "--no-renames", "-z")
    fields = [f for f in out.split("\0") if f]
    return list(zip(fields[0::2], fields[1::2], strict=True))


def message_lines(text: str) -> list[tuple[int, str]]:
    """Every line of a commit message file above git's scissors line."""
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith(SCISSORS):
            break
        lines.append((number, line))
    return lines


def trailer_start(lines: list[str]) -> int:
    """Index of the first line of the trailer block (``len(lines)`` if none).

    The trailer block is the last paragraph when every line in it is a
    ``Key: value`` trailer.
    """
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and lines[start - 1].strip():
        start -= 1
    block = lines[start:end]
    if start and block and all(re.match(r"^[A-Za-z][A-Za-z0-9-]*: \S", line) for line in block):
        return start
    return len(lines)


def trailer_block(lines: list[str]) -> list[str]:
    """The trailer lines at the end of a message (see ``trailer_start``)."""
    return [line for line in lines[trailer_start(lines):] if line.strip()]

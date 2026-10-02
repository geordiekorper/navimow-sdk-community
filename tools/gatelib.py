r"""Shared helpers for the commit checks in tools/check_*.py.

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
generic rules only. Each non-comment line is tab-separated:
``<scope>\t<name>\t<flags>\t<regex>`` with scope ``files``, ``message`` or
``both`` and flags ``-`` or ``notrailers`` (not applied to trailer lines of a
message).
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCISSORS = "------------------------ >8 ------------------------"


def git(
    *args: str, cwd: str | Path | None = None, check: bool = True, env: dict[str, str] | None = None
) -> str:
    """Run git and give its standard output as text.

    Both of git's output streams are captured, so nothing reaches the
    terminal. Standard error is not returned; when the call fails with check
    true it is on the exception.

    Args:
        *args: The git subcommand and its arguments, without the leading
            "git".
        cwd: The directory git runs in; None is the current directory.
        check: Whether a non-zero exit status is an error. With False a
            failing git gives whatever it wrote to standard output, usually
            the empty string.
        env: The whole environment for git; None passes on the calling
            process's environment.

    Returns:
        Git's standard output, decoded with the locale's encoding, its
        trailing newline included.

    Raises:
        subprocess.CalledProcessError: check is true and git exits with a
            non-zero status.
    """
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True, env=env
    ).stdout


def foreign_env() -> dict[str, str]:
    """The environment for git in another worktree, without this repository's GIT_ variables.

    Inside a hook git exports GIT_INDEX_FILE (relative to this worktree, or
    its index.lock during `commit -a`) and GIT_DIR; passed on, they make git
    in another worktree read this worktree's index, or fail.

    Returns:
        A copy of the calling process's environment without the variables
        whose names start with GIT_. The one exception is GIT_EXEC_PATH,
        which is kept.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k == "GIT_EXEC_PATH"}


def git_bytes(*args: str, cwd: str | Path | None = None) -> bytes:
    """Run git and give its standard output as bytes, not decoded.

    For output that holds the content of files, which the caller decodes
    itself (see lines_to_check). Git gets the calling process's environment.

    Args:
        *args: The git subcommand and its arguments, without the leading
            "git".
        cwd: The directory git runs in; None is the current directory.

    Returns:
        Git's standard output as it was written.

    Raises:
        subprocess.CalledProcessError: Git exits with a non-zero status.
    """
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def repo_root() -> Path:
    """The top directory of the working tree that the current directory is in.

    Asked of git with `git rev-parse --show-toplevel`. In a linked worktree
    it is that worktree's top directory, not the main checkout's.

    Returns:
        The path git prints, which is absolute.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git working tree.
    """
    return Path(git("rev-parse", "--show-toplevel").strip())


def common_dir() -> Path:
    """The git directory that every worktree of the repository shares.

    Asked of git with `git rev-parse --path-format=absolute --git-common-dir`.
    In a linked worktree it is the main checkout's git directory, not the
    worktree's own.

    Returns:
        The absolute path of the common directory.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git repository.
    """
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())


def tracked_files() -> set[str]:
    """Paths in the index (tracked or staged), unquoted.

    Read with `git ls-files -z` in the current directory. The output is
    NUL-delimited, so git does not put a name with unusual characters in
    quotes, as it does otherwise. Git gets the calling environment, so inside
    a hook the index is the one being committed.

    Returns:
        The paths as git lists them, relative to the current directory. An
        empty set when the index holds none.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git repository.
    """
    return {p for p in git("ls-files", "-z").split("\0") if p}


# Code moved verbatim from upstream, and the inventory of upstream's public
# names: never changed except with a Legacy-edit trailer. The folder's README
# is this repository's own text, not upstream's, and is the one path under
# the folder that is not protected: that exact path, nothing else.
LEGACY_DIR = "mower_sdk/legacy/"
LEGACY_README = LEGACY_DIR + "README.md"
INVENTORY = "tests/upstream_exports.json"


def is_protected(path: str) -> bool:
    """Say whether a path is protected from changes.

    Protected are every path under LEGACY_DIR other than LEGACY_README, and
    INVENTORY itself. The path is compared as text: it is not normalised, so
    it has to be spelt as git prints it.

    Args:
        path: A path relative to the repository's top directory, with forward
            slashes.

    Returns:
        True for a protected path, False for any other.
    """
    return (path.startswith(LEGACY_DIR) and path != LEGACY_README) or path == INVENTORY


def is_claude() -> bool:
    """Say whether the check runs in a Claude Code session.

    Returns:
        True when the environment variable CLAUDECODE is exactly "1"; False
        when it is unset or has any other value.
    """
    return os.environ.get("CLAUDECODE") == "1"


def range_refs() -> tuple[str, str] | None:
    """The two refs of range mode, read from the environment.

    pre-commit exports PRE_COMMIT_FROM_REF and PRE_COMMIT_TO_REF when it is
    run with --from-ref and --to-ref.

    Returns:
        (the from ref, the to ref) when both variables are set and not empty;
        None when either is unset or empty.
    """
    source, target = os.environ.get("PRE_COMMIT_FROM_REF"), os.environ.get("PRE_COMMIT_TO_REF")
    return (source, target) if source and target else None


# --- the optional pattern file -----------------------------------------------


@dataclass(frozen=True)
class LocalPattern:
    """One rule of the optional pattern file.

    Attributes:
        scope: Where the rule applies: "files", "message" or "both".
        name: The rule's name.
        skip_trailers: True when the rule is not applied to the trailer lines
            of a message (the flags field of its line is "notrailers").
        regex: The rule's expression, compiled.
    """

    scope: str
    name: str
    skip_trailers: bool
    regex: re.Pattern[str]


def patterns_file() -> Path:
    """The location of the optional pattern file, which need not exist.

    Returns:
        The path in the environment variable COMMIT_GATE_PATTERNS, as given,
        when that is set and not empty; otherwise commit-gate/patterns.txt
        under the git common directory (see common_dir).

    Raises:
        subprocess.CalledProcessError: COMMIT_GATE_PATTERNS is unset or empty
            and the current directory is not inside a git repository.
    """
    override = os.environ.get("COMMIT_GATE_PATTERNS")
    return Path(override) if override else common_dir() / "commit-gate" / "patterns.txt"


def load_local_rules() -> list[LocalPattern]:
    """Read the pattern file; a Claude session without it is refused.

    The file is the one patterns_file names, read as UTF-8. A blank line and a
    line that starts with "#" are skipped. Every other line has to be four
    tab-separated fields: a scope ("files", "message" or "both"), the rule's
    name, the flags and the expression. The flags field "notrailers" sets the
    rule's skip_trailers; any other value leaves it False.

    Returns:
        The file's rules, in the file's order. An empty list when the file
        holds no rule, or when there is no file at the path and this is not a
        Claude session (see is_claude).

    Raises:
        SystemExit: There is no file at the path and this is a Claude
            session; or a line's first field is "coauthor"; or a line is
            malformed (another first field, or not exactly four fields). The
            message names the file and, for a line, its number.
        re.error: An expression in the file does not compile.
        subprocess.CalledProcessError: The location has to be asked of git
            and the current directory is not inside a git repository (see
            patterns_file).
    """
    path = patterns_file()
    if not path.is_file():
        if is_claude():
            raise SystemExit(
                f"commit checks: the local pattern file {path} is missing; "
                "Claude commits need it (run `commit-gate provision`)."
            )
        return []
    patterns = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split("\t")
        if fields[0] == "coauthor":
            raise SystemExit(f"{path}:{number}: coauthor lines are no longer read; remove them")
        if fields[0] in ("files", "message", "both") and len(fields) == 4:
            patterns.append(
                LocalPattern(fields[0], fields[1], fields[2] == "notrailers", re.compile(fields[3]))
            )
        else:
            raise SystemExit(f"{path}:{number}: malformed pattern line")
    return patterns


# --- names of files git does not track ---------------------------------------

_NOISE_PARTS = {
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "build",
    "dist",
    "htmlcov",
    ".git",
    ".idea",
    ".vscode",
    ".nox",
    ".tox",
    # The issue tracker's and its database's directories, spelt in pieces so
    # the names are not in the text this checker reads.
    "." + "beads",
    "." + "dolt",
}
_NOISE_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".DS_Store", ".coverage", ".db")
_WALK_LIMIT = 5000


# Nested checkouts: every file under them belongs to another worktree, which
# is read on its own. Other local files under .claude/ (an agent definition,
# settings) are ordinary untracked documents and are checked.
_NESTED_WORKTREES = ".claude/" + "worktrees"


def _noise(relative: str) -> bool:
    """Say whether the scan for untracked names leaves a path out.

    Left out are the directory of nested checkouts (_NESTED_WORKTREES) with
    everything under it, and any path with a component that is in
    _NOISE_PARTS or that ends with one of _NOISE_SUFFIXES.

    Args:
        relative: A path relative to its worktree's top directory, with
            forward slashes; trailing slashes are ignored.

    Returns:
        True when the path is left out, False when it is read.
    """
    relative = relative.rstrip("/")
    if relative == _NESTED_WORKTREES or relative.startswith(_NESTED_WORKTREES + "/"):
        return True
    parts = relative.split("/")
    return any(part in _NOISE_PARTS or part.endswith(_NOISE_SUFFIXES) for part in parts)


def _worktrees() -> list[Path]:
    """The directories of the repository's worktrees.

    Read with `git worktree list --porcelain`. A worktree whose directory
    does not exist is left out.

    Returns:
        The path of each worktree, the current one included, in the order
        git lists them.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git repository.
    """
    paths = []
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            if path.is_dir():
                paths.append(path)
    return paths


def _untracked_in(worktree: Path, own: bool) -> set[str]:
    """Untracked and ignored names in a worktree.

    Two listings are read: `git ls-files --others --exclude-standard` for the
    untracked files, and the same with --ignored for the ignored ones. Both
    have --directory, so a directory that is untracked or ignored as a whole
    is listed once, as "dir/". Such a directory gives its own name and is
    then walked for the files under it; the walk of one listed directory
    stops after the directory in which its count of files passes _WALK_LIMIT.
    A path that _noise leaves out is skipped, in the listings and in the walk.
    A git that exits with an error is not an error here: its listing is then
    whatever it printed, usually nothing.

    The current worktree keeps the calling environment, so that inside a hook
    its index is the one being committed; any other worktree is read with its
    own index.

    Args:
        worktree: The worktree's top directory.
        own: True for the worktree the check runs in, where git gets the
            calling environment; False for any other, where git gets
            foreign_env().

    Returns:
        The names, as paths relative to the worktree: each listed file, each
        listed directory without its trailing slash, and each file found
        under such a directory. An empty set when there are none.
    """
    env = None if own else foreign_env()
    names: set[str] = set()
    listings = [
        # Whole untracked or ignored directories are listed as "dir/" and walked
        # below, with the noise filter and the walk limit applied.
        ["ls-files", "--others", "--exclude-standard", "--directory", "-z"],
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
    ]
    for args in listings:
        for entry in git(*args, cwd=worktree, check=False, env=env).split("\0"):
            if not entry or _noise(entry):
                continue
            if not entry.endswith("/"):
                names.add(entry)
                continue
            names.add(entry.rstrip("/"))  # the directory itself, then what is in it
            count = 0
            for dirpath, dirnames, filenames in os.walk(worktree / entry):
                dirnames[:] = [
                    d
                    for d in dirnames
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
    """The plain names listed in the repository's info/exclude file.

    The file is info/exclude under the git common directory, read as UTF-8.
    A line counts when, with the whitespace around it removed, it is not
    empty, does not start with "#" and holds none of the pattern characters
    "*", "?", "[" and "!".

    Returns:
        Each such line without its leading and trailing slashes. An empty set
        when the file does not exist or lists no plain name.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git repository.
    """
    exclude = common_dir() / "info" / "exclude"
    names = set()
    if exclude.is_file():
        for line in exclude.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not any(c in line for c in "*?[!"):
                names.add(line.strip("/"))
    return names


def _word_like(name: str) -> bool:
    """Say whether a name is a bare one that reads as an ordinary word ("notes").

    A name with an extension ("a.md", "x.txt") or a long one is specific
    enough to report, however short. A name has an extension when a dot
    stands inside it: leading and trailing dots do not count.

    Args:
        name: A file's name, or its path relative to a worktree.

    Returns:
        True when the name holds no slash, has no extension and is shorter
        than 12 characters; False otherwise.
    """
    if "/" in name:
        return False
    has_extension = "." in name.strip(".")
    return not (has_extension or len(name) >= 12)


def untracked_name_regex() -> re.Pattern[str] | None:
    """One expression matching a name of any ignored or untracked file.

    It reads every worktree, because an ignored document may exist only in
    one of them. Each untracked or ignored path gives two names: the path
    relative to its worktree, and its last component. Names that are tracked
    (or staged) here are left out, and so are short names without an
    extension, which read as ordinary words (see _word_like). Names listed in
    info/exclude are kept even when they read as words: the owner put them
    there on purpose. Whatever its source, info/exclude included, a name is
    also left out when it is the last component of a tracked path or is
    shorter than three characters.

    A name is matched only where it stands as a whole name. It may not follow
    a word character, a dot or a hyphen, nor be followed by a word character
    or a hyphen, or by a dot and then one of those; sentence punctuation may
    follow it. Where several names could match, the longest is tried first.

    Returns:
        The compiled expression, or None when there is no name to match.

    Raises:
        subprocess.CalledProcessError: The current directory is not inside a
            git working tree.
    """
    tracked = tracked_files()
    tracked_basenames = {Path(p).name for p in tracked}
    excluded = _excluded_names()
    candidates: set[str] = set(excluded)
    current = repo_root().resolve()
    for worktree in _worktrees():
        for relative in _untracked_in(worktree, own=worktree.resolve() == current):
            for name in (relative, Path(relative).name):
                if not _word_like(name):
                    candidates.add(name)
    candidates = {
        c
        for c in candidates
        if c and c not in tracked and c not in tracked_basenames and len(c) >= 3
    }
    if not candidates:
        return None
    alternation = "|".join(re.escape(c) for c in sorted(candidates, key=len, reverse=True))
    # A name ends where the text stops being a file name: sentence punctuation
    # may follow ("see draft.md."), a further suffix may not ("draft.md.j2").
    return re.compile(r"(?<![\w.-])(?:" + alternation + r")(?![\w-]|\.[\w-])")


# --- the local-path pattern ------------------------------------------------


def hook(hook_id: str, config: Path | None = None) -> dict:
    """A hook's definition in .pre-commit-config.yaml (this repository's by default).

    The file is read as UTF-8 and parsed with pyyaml, which is imported here
    and not with the module.

    Args:
        hook_id: The hook's id.
        config: The configuration file to read; None reads
            .pre-commit-config.yaml in the top directory of the current
            working tree (see repo_root).

    Returns:
        The first hook definition with that id, among all the file's
        repositories, as the mapping pyyaml loads.

    Raises:
        SystemExit: No hook in the file has that id.
        ModuleNotFoundError: pyyaml is not installed.
        FileNotFoundError: The configuration file does not exist.
        subprocess.CalledProcessError: config is None and the current
            directory is not inside a git working tree.
    """
    import yaml  # only callers that read the configuration need it

    path = config or repo_root() / ".pre-commit-config.yaml"
    for repository in yaml.safe_load(path.read_text(encoding="utf-8"))["repos"]:
        for definition in repository["hooks"]:
            if definition["id"] == hook_id:
                return definition
    raise SystemExit(f"{hook_id} is not defined in {path}")


def local_path_pattern() -> re.Pattern[str]:
    """The no-local-paths pygrep hook's expression.

    One definition serves the file hook (pygrep) and the message check, which
    has to read the message as git commits it and so cannot be a pygrep hook.
    The expression is the entry of the hook with the id no-local-paths in the
    .pre-commit-config.yaml of the current working tree.

    Returns:
        The expression, compiled without flags.

    Raises:
        SystemExit: The configuration defines no hook with that id.
        ModuleNotFoundError: pyyaml is not installed.
        FileNotFoundError: The repository has no pre-commit configuration.
        subprocess.CalledProcessError: The current directory is not inside a
            git working tree.
    """
    return re.compile(hook("no-local-paths")["entry"])


# --- which lines to check ----------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_path(raw: str) -> str | None:
    """The path of a ``+++`` header, with git's C-style quoting undone.

    One trailing tab is removed first: git ends the header with a tab when
    the name holds a space. A name that git put in double quotes has its
    escapes decoded and is read as UTF-8, with the replacement character for
    bytes that are not.

    Args:
        raw: What follows "+++ " on the header line.

    Returns:
        The path, without the "b/" prefix when it has one; None when the
        header names /dev/null, as it does for a deleted file.
    """
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
    "++ " is content, not a file header. A hunk header that leaves a length
    out means one line. The line that marks a missing newline at the end of a
    file counts for neither side. The text is split at newlines only.

    Args:
        diff: The text of a unified diff.

    Returns:
        For each file with added lines, its path (see _diff_path) and a list
        of (line number in the new file, the line without its leading "+").
        A file without added lines is not in the mapping, and neither are
        lines under a header that names /dev/null.
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


def _diff_args() -> list[str]:
    """The git diff of the current mode: between the refs, or the staged change.

    Returns:
        The first arguments for git: ["diff", "FROM...TO"] when range_refs
        gives the two refs, which is what TO changed since its merge base
        with FROM; otherwise ["diff", "--cached"], the staged change.
    """
    refs = range_refs()
    return ["diff", f"{refs[0]}...{refs[1]}"] if refs else ["diff", "--cached"]


def lines_to_check(files: list[str]) -> dict[str, list[tuple[int, str]]]:
    """The lines of ``files`` to inspect in the current mode.

    With GATE_MODE=content in the environment, every line of each file, read
    as UTF-8 from the path as given; a file that is missing, is a directory
    or is not UTF-8 is left out. With any other value, or none, the lines the
    change adds to the files: the diff between the range refs, or the staged
    change (see _diff_args), taken without context lines. Git's output is
    read as UTF-8, with the replacement character for bytes that are not.

    Args:
        files: The paths to inspect, relative to the current directory.

    Returns:
        For each file, a list of (line number, line), numbered from 1 as in
        the file. In content mode the key is the path as given and an empty
        file has an empty list; otherwise the key is the path as the diff
        names it and a file without added lines is not in the mapping. An
        empty mapping when files is empty: nothing is then read.

    Raises:
        subprocess.CalledProcessError: Not in content mode, and git diff
            fails, as it does on a ref that does not exist or outside a
            git repository.
    """
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
    diff = git_bytes(
        *_diff_args(),
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--",
        *files,
    )
    return _parse_added(diff.decode("utf-8", "replace"))


def changed_files() -> list[tuple[str, str]]:
    """(status, path) of every changed file in the current mode.

    Read with `git diff --name-status --no-renames -z` of the range or of the
    staged change (see _diff_args); GATE_MODE is not consulted. Rename
    detection is off, so a moved file is its old path deleted and its new
    path added.

    Returns:
        One (status letter, path) for each changed file, as git gives them.
        Among the letters are A for a file added, M for one modified, D for
        one deleted and T for a change of type. An empty list when nothing
        changed.

    Raises:
        subprocess.CalledProcessError: git diff fails, as it does on a ref
            that does not exist or outside a git repository.
    """
    out = git(*_diff_args(), "--name-status", "--no-renames", "-z")
    fields = [f for f in out.split("\0") if f]
    return list(zip(fields[0::2], fields[1::2], strict=True))


def comment_char() -> str:
    """Git's comment character for messages (core.commentChar; "auto" reads as "#").

    Asked of git with `git config --get core.commentChar`, in the current
    directory.

    Returns:
        The configured value; "#" when git prints none, which includes git
        failing, or when the value is "auto".
    """
    configured = git("config", "--get", "core.commentChar", check=False).strip()
    return configured if configured and configured != "auto" else "#"


def message_lines(text: str) -> list[tuple[int, str]]:
    """The lines git commits from a message file, with their line numbers.

    Comment lines and everything from the scissors line on (the diff that
    `git commit -v` appends) are dropped, as git's default cleanup does for
    a commit made in the editor: git's own template lists untracked files in
    comment lines, which are not part of the message. A comment line is one
    that starts with comment_char(); the scissors line is one that starts
    with that character, a space and SCISSORS. Nothing else of git's cleanup
    is done: blank lines and trailing whitespace stay.

    Args:
        text: The content of the message file.

    Returns:
        (line number, line) for each line kept, numbered from 1 in the text
        as given, so the numbers skip the dropped lines. An empty list when
        no line is kept.
    """
    comment = comment_char()
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith(f"{comment} {SCISSORS}"):
            break
        if line.startswith(comment):
            continue
        lines.append((number, line))
    return lines


_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")
_CHERRY_PICKED = re.compile(r"^\(cherry picked from commit [0-9a-f]{7,64}\)$")


def trailer_start(lines: list[str]) -> int:
    """Index of the first line of the trailer block (``len(lines)`` if none).

    The trailer block is the last paragraph when every line in it is a
    ``Key: value`` trailer, or the line `git cherry-pick -x` appends there.
    Blank lines after the last paragraph are passed over. A key is a letter
    followed by letters, digits and hyphens, and the value follows a colon
    and one space. A last paragraph that begins at the first line is not a
    trailer block: something has to stand before it.

    Args:
        lines: The lines of a message, or of its body, without their line
            ends.

    Returns:
        The index in lines of the trailer block's first line; len(lines)
        when there is no trailer block.
    """
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and lines[start - 1].strip():
        start -= 1
    block = lines[start:end]
    if (
        start
        and block
        and all(_TRAILER.match(line) or _CHERRY_PICKED.match(line) for line in block)
    ):
        return start
    return len(lines)


def trailer_block(lines: list[str]) -> list[str]:
    """The trailer lines at the end of a message (see ``trailer_start``).

    Args:
        lines: The lines of a message, or of its body, without their line
            ends.

    Returns:
        The lines of the trailer block, without the blank lines after it; an
        empty list when there is no trailer block.
    """
    return [line for line in lines[trailer_start(lines) :] if line.strip()]

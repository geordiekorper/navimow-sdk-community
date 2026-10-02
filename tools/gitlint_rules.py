"""The repository's own commit-message rules, as gitlint user rules.

gitlint's built-in and contrib rules cover the subject and body format
(see .gitlint); these cover what is specific to this repository.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from gitlint.rules import CommitRule, ConfigurationRule, RuleViolation

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib

_TYPE = re.compile(r"^(\w+)(?:\([^)]*\))?!?: ")
_KIND = re.compile(r"^(?:Upstream-suitable\.|Community-only\.|Fork-only[.:])(?:\s|$)")
_CO_AUTHOR = re.compile(r"^Co-authored-by: ", re.IGNORECASE)
_ASSISTANT = re.compile(r"^Co-Authored-By: Claude\b", re.IGNORECASE)
_PORTED = re.compile(r"^Upstream-commit: [0-9a-f]{40}$")
_LABEL_SUFFIX = re.compile(
    r"\s\((?:[A-Z]{1,2}\d{1,2}(?:[-.]\d+)?|P\d+-C\d+|[A-Z]\d+(?:, ?[A-Z]\d+)+)\)$"
)


def _trailers(commit) -> list[str]:
    """Give the trailer lines of the commit's message.

    Args:
        commit: gitlint's commit; its message body (the lines after the
            subject) is read.

    Returns:
        The non-blank lines of the trailer block, as gatelib.trailer_block
        finds it; empty when the message has none.
    """
    return gatelib.trailer_block(commit.message.body)


def _changed_paths(commit) -> list[str]:
    """List every path the change touches: the staged change, or the commit's own.

    Read from git with rename detection off, so a move is the old path deleted
    and the new one added. gitlint's own changed_files splits git's
    "dir/{old => new}/file" rename notation on whitespace and so mangles
    moved paths. The list is read once per commit, for every rule: it is kept
    on the commit object, as its _gate_changed_paths attribute.

    Args:
        commit: gitlint's commit. With a sha (a commit already made, as
            when a range is linted) its own change is read, with git
            diff-tree; without one (the commit being made) the staged change,
            with git diff --cached. git runs in
            commit.context.repository_path.

    Returns:
        The paths as git names them, relative to the top of the repository;
        empty when the change touches none.

    Raises:
        subprocess.CalledProcessError: git exits with an error.
    """
    paths = getattr(commit, "_gate_changed_paths", None)
    if paths is None:
        args = (
            ["diff-tree", "--no-commit-id", "-r", "--root", commit.sha]
            if commit.sha
            else ["diff", "--cached"]
        )
        out = gatelib.git(
            *args, "--no-renames", "--name-only", "-z", cwd=commit.context.repository_path
        )
        paths = [path for path in out.split("\0") if path]
        commit._gate_changed_paths = paths
    return paths


def _diff(commit, path: str) -> str:
    """Give the change to one path: the staged one, or the commit's own when linting a range.

    Args:
        commit: gitlint's commit. With a sha its own change is read, with
            git show; without one the staged change, with git diff --cached.
            git runs in commit.context.repository_path.
        path: The path whose change is wanted, relative to the top of the
            repository.

    Returns:
        The diff git prints for the path, empty when the change does not touch
        it. A git that exits with an error is not reported: what it printed
        is returned.
    """
    args = ["show", "--format=", commit.sha] if commit.sha else ["diff", "--cached"]
    return gatelib.git(*args, "--", path, cwd=commit.context.repository_path, check=False)


def _is_ported(commit) -> bool:
    """Say whether the commit carries the trailer of a ported commit.

    tools/port_upstream.py gives each commit it ports the trailer
    "Upstream-commit: <sha>". Only that exact line counts: the sha is the
    full 40 hexadecimal digits, in lower case.

    Args:
        commit: gitlint's commit; the trailer block of its message body is
            read.

    Returns:
        True when a trailer line is an Upstream-commit trailer of that shape.
    """
    return any(_PORTED.match(line) for line in _trailers(commit))


def _outside_a_port(commit) -> list[str]:
    """List the paths the commit changes that a port of upstream's code cannot change.

    A port changes the code moved from upstream, which is the protected part
    of mower_sdk/legacy/, and at most the files whose content was split
    between the live path and legacy (the "mixed" files of
    tools/upstream_path_map.json), where upstream's hunks are applied by hand.

    Args:
        commit: gitlint's commit; sha and context.repository_path are read
            for the paths it changes (see _changed_paths).

    Returns:
        The changed paths that are neither a protected path under
        mower_sdk/legacy/ nor a mixed file; empty when the commit stays
        within what a port can change, and when it changes nothing.

    Raises:
        subprocess.CalledProcessError: git exits with an error while the
            changed paths are read.
        FileNotFoundError: The path map is not beside this file.
    """
    path_map = json.loads(
        (Path(__file__).resolve().parent / "upstream_path_map.json").read_text(encoding="utf-8")
    )
    mixed = set(path_map["mixed"])
    return [
        path
        for path in _changed_paths(commit)
        if not (path.startswith(gatelib.LEGACY_DIR) and gatelib.is_protected(path))
        and path not in mixed
    ]


class PortedCommit(ConfigurationRule):
    """A commit ported from upstream keeps upstream's message, so no rule applies to it.

    tools/port_upstream.py marks each commit it ports with an
    "Upstream-commit: <sha>" trailer and otherwise leaves upstream's message
    as it is: its subject is not a conventional commit, and it has no
    Legacy-edit trailer. The trailer exempts a commit only when the commit
    changes something and nothing outside what a port can change (see
    _outside_a_port), so it cannot be added to another commit to skip the
    rules; PortedCommitScope reports that use.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "ported-commit"
    id = "UC8"

    def apply(self, config, commit):
        """Switch every rule off for a commit that is a port of upstream's code.

        Nothing is changed unless the commit carries the Upstream-commit
        trailer, changes at least one path and changes none outside what a
        port can change.

        Args:
            config: gitlint's configuration for the commit being linted.
                Nothing is read from it; its ignore option is set to "all".
            commit: gitlint's commit: the message body is read for the
                trailer, and sha and context.repository_path for the paths it
                changes.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        if _is_ported(commit) and _changed_paths(commit) and not _outside_a_port(commit):
            config.ignore = "all"


class PortedCommitScope(CommitRule):
    """The Upstream-commit trailer is only for a commit that changes upstream's code.

    A commit that carries the trailer and changes a path outside what a port
    can change (see _outside_a_port), or changes nothing at all, is reported.
    PortedCommit does not exempt such a commit, so the other rules apply to
    it too.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "ported-commit-scope"
    id = "UC9"

    def validate(self, commit):
        """Report an Upstream-commit trailer on a commit that is not a port.

        Args:
            commit: gitlint's commit: the message body is read for the
                trailer, and sha and context.repository_path for the paths it
                changes.

        Returns:
            One violation, on line 1: it names the first path the commit
            changes outside what a port can change, or says that the commit
            changes nothing. Empty when the commit has no Upstream-commit
            trailer, and when it is a port.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        if not _is_ported(commit):
            return []
        outside = _outside_a_port(commit)
        if outside:
            return [
                RuleViolation(
                    self.id,
                    "an Upstream-commit trailer is for a commit that changes only code moved from "
                    "upstream (mower_sdk/legacy/ and the mixed files), "
                    f"and this one changes {outside[0]}",
                    None,
                    1,
                )
            ]
        if not _changed_paths(commit):
            return [
                RuleViolation(
                    self.id, "an Upstream-commit trailer on a commit that changes nothing", None, 1
                )
            ]
        return []


class NoTrackerTrailer(CommitRule):
    """No Refs: trailer: the message carries no tracker ids.

    The message explains the change itself. The rule looks for the line that
    would carry an id and not for ids: a body line that starts with "Refs:"
    or "Ref:", in any letter case, in the trailer block or above it.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "no-tracker-trailer"
    id = "UC1"

    def validate(self, commit):
        """Report every Refs: line of the message body.

        Args:
            commit: gitlint's commit; its message body (the lines after the
                subject) is read.

        Returns:
            One violation for each body line that starts with "Refs:" or
            "Ref:", with the line and its number in the message (the subject
            is line 1); empty when there is none.
        """
        return [
            RuleViolation(
                self.id, "commit messages carry no tracker ids (no Refs: trailer)", line, number
            )
            for number, line in enumerate(commit.message.body, 2)
            if re.match(r"^Refs?:", line, re.IGNORECASE)
        ]


class NoPlanningLabel(CommitRule):
    """The subject does not end with a parenthesised label (a letter and a number).

    Three shapes are found, each in brackets at the very end of the subject
    and after whitespace: one or two capital letters and one or two digits,
    with or without a hyphen or full stop and a further number; "P", a number,
    "-C" and a number; and two or more pairs of a capital letter and a number,
    separated by commas.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "no-planning-label"
    id = "UC2"

    def validate(self, commit):
        """Report a subject that ends with a parenthesised label.

        Args:
            commit: gitlint's commit; its message title (the subject) is read.

        Returns:
            One violation, with the subject and line 1, when the subject ends
            with such a label; empty when it does not.
        """
        if _LABEL_SUFFIX.search(commit.message.title):
            return [
                RuleViolation(
                    self.id, "subject ends with a parenthesised label", commit.message.title, 1
                )
            ]
        return []


class KindLine(CommitRule):
    """A code change says whether it suits upstream.

    A commit whose subject has the type feat, fix, perf or refactor and which
    changes a path under mower_sdk/ has a body line that starts with its kind
    sentence: "Upstream-suitable.", "Community-only.", or "Fork-only." (also
    read as "Fork-only:"), followed by whitespace or the end of the line. The
    sentence says whether the change could be offered upstream as it is.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "kind-line"
    id = "UC3"

    def validate(self, commit):
        """Report a code change to mower_sdk/ whose message has no kind sentence.

        Args:
            commit: gitlint's commit: the message title is read for the
                commit's type, the message body for the kind sentence, and
                sha and context.repository_path for the paths it changes.

        Returns:
            One violation, on line 1, when the kind sentence is missing. Empty
            when it is there, when the subject is not a conventional one of
            type feat, fix, perf or refactor, and when the commit changes
            nothing under mower_sdk/.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        kind = _TYPE.match(commit.message.title)
        if not kind or kind.group(1) not in ("feat", "fix", "perf", "refactor"):
            return []
        if not any(path.startswith("mower_sdk/") for path in _changed_paths(commit)):
            return []
        if any(_KIND.match(line) for line in commit.message.body):
            return []
        return [
            RuleViolation(
                self.id,
                "a change to mower_sdk/ needs its kind sentence: "
                "'Upstream-suitable.' or 'Community-only.'",
                None,
                1,
            )
        ]


class TrailerOrder(CommitRule):
    """Co-author lines are trailers, and no co-author follows the assistant attribution.

    Other trailers may follow it, such as the Signed-off-by that `git commit -s`
    appends. The assistant attribution is a "Co-Authored-By: Claude" line; both
    it and the Co-authored-by key are read in any letter case.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "trailer-order"
    id = "UC4"

    def validate(self, commit):
        """Report co-author lines outside the trailer block or after the assistant attribution.

        Args:
            commit: gitlint's commit; its message body (the lines after the
                subject) is read.

        Returns:
            One violation for each Co-authored-by line above the trailer
            block (every such line when the message has no trailer block),
            with the line and its number in the message, and one more,
            without a line or a number, when a Co-authored-by trailer follows
            the last assistant attribution; empty when the rule holds.
        """
        body = commit.message.body
        start = gatelib.trailer_start(body)
        violations = [
            RuleViolation(
                self.id, "co-author lines belong in the trailer block at the end", line, number
            )
            for number, line in enumerate(body[:start], 2)
            if _CO_AUTHOR.match(line)
        ]
        trailers = [line for line in body[start:] if line.strip()]
        assistant = [i for i, line in enumerate(trailers) if _ASSISTANT.match(line)]
        if assistant and any(_CO_AUTHOR.match(line) for line in trailers[assistant[-1] + 1 :]):
            violations.append(
                RuleViolation(self.id, "the assistant attribution must be the last co-author")
            )
        return violations


class VersionInRelease(CommitRule):
    """__version__ changes only in a release commit that also updates the changelog.

    A change of __version__ is an added line of mower_sdk/__init__.py that
    starts with "__version__". A release commit is one whose subject starts
    with "chore(release)"; it also has to change CHANGELOG.md.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "version-in-release"
    id = "UC5"

    def validate(self, commit):
        """Report a change of __version__ outside a release commit.

        Args:
            commit: gitlint's commit: sha and context.repository_path are
                read for the paths it changes and for its change to
                mower_sdk/__init__.py, and the message title for the kind of
                commit.

        Returns:
            One violation, on line 1, when the commit adds a __version__ line
            and is not a chore(release) commit that also changes CHANGELOG.md;
            empty otherwise.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        changed = _changed_paths(commit)
        if "mower_sdk/__init__.py" not in changed:
            return []
        if not any(
            line.startswith("+__version__")
            for line in _diff(commit, "mower_sdk/__init__.py").splitlines()
        ):
            return []
        if commit.message.title.startswith("chore(release)") and "CHANGELOG.md" in changed:
            return []
        return [
            RuleViolation(
                self.id,
                "__version__ changes only in a chore(release) commit "
                "that also updates CHANGELOG.md",
                None,
                1,
            )
        ]


class ForkAuthorProvenance(CommitRule):
    """Crediting a fork author means recording the origin in docs/UPSTREAM.md.

    Every Co-authored-by trailer other than the assistant attribution counts
    as crediting a fork author. The commit then has to change
    docs/UPSTREAM.md; what it writes there is not read.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "fork-author-provenance"
    id = "UC6"

    def validate(self, commit):
        """Report a credited co-author in a commit that does not change docs/UPSTREAM.md.

        Args:
            commit: gitlint's commit: the message body is read for the
                Co-authored-by trailers and, when it credits someone, sha and
                context.repository_path for the paths it changes.

        Returns:
            One violation, with the first crediting trailer and no line
            number, when the commit credits a co-author and does not change
            docs/UPSTREAM.md; empty otherwise.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        credited = [
            line
            for line in _trailers(commit)
            if _CO_AUTHOR.match(line) and not _ASSISTANT.match(line)
        ]
        if credited and "docs/UPSTREAM.md" not in _changed_paths(commit):
            return [
                RuleViolation(
                    self.id,
                    "a fork author is credited but docs/UPSTREAM.md does not record the origin",
                    credited[0],
                )
            ]
        return []


_LEGACY_EDIT = re.compile(r"^Legacy-edit:(.*)$")


class LegacyEditTrailer(CommitRule):
    """A change to a protected path says why, in a Legacy-edit trailer.

    Code moved verbatim from upstream (mower_sdk/legacy/) and the inventory of
    upstream's public names (tests/upstream_exports.json) are not changed;
    the hook no-protected-changes refuses it. A deliberate exception skips
    that hook and carries "Legacy-edit: <reason>", which this rule requires,
    here and in CI. The folder's README.md is not protected, so a commit that
    changes only it carries no trailer.

    Attributes:
        name: The rule's name in gitlint's configuration.
        id: The rule's id, which gitlint prints with each violation.
    """

    name = "legacy-edit-trailer"
    id = "UC7"

    def validate(self, commit):
        """Report a protected path changed without a Legacy-edit trailer, or the trailer misused.

        Args:
            commit: gitlint's commit: the message body is read for the
                Legacy-edit trailers, and sha and context.repository_path for
                the paths it changes.

        Returns:
            One violation: when a protected path is changed and no Legacy-edit
            trailer is there (it names the first such path, on line 1); when
            the trailer is there and no protected path is changed; or when a
            Legacy-edit trailer has an empty reason. Empty when the rule
            holds.

        Raises:
            subprocess.CalledProcessError: git exits with an error while the
                changed paths are read.
        """
        reasons = [
            m.group(1).strip() for line in _trailers(commit) if (m := _LEGACY_EDIT.match(line))
        ]
        touched = [path for path in _changed_paths(commit) if gatelib.is_protected(path)]
        if touched and not reasons:
            return [
                RuleViolation(
                    self.id,
                    f"a change to a protected path ({touched[0]}) needs "
                    "a 'Legacy-edit: <reason>' trailer",
                    None,
                    1,
                )
            ]
        if reasons and not touched:
            return [
                RuleViolation(
                    self.id, "a Legacy-edit trailer on a commit that changes no protected path"
                )
            ]
        if any(not reason for reason in reasons):
            return [RuleViolation(self.id, "the Legacy-edit trailer needs a reason")]
        return []

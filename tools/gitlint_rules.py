"""The repository's own commit-message rules, as gitlint user rules.

gitlint's built-in and contrib rules cover the subject and body format
(see .gitlint); these cover what is specific to this repository.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from gitlint.rules import CommitRule, RuleViolation

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gatelib  # noqa: E402  (the trailer-block rule is shared with the leak check)

_TYPE = re.compile(r"^(\w+)(?:\([^)]*\))?!?: ")
_KIND = re.compile(r"^(?:Upstream-suitable\.|Community-only\.|Fork-only[.:])(?:\s|$)")
_CO_AUTHOR = re.compile(r"^Co-authored-by: ", re.IGNORECASE)
_ASSISTANT = re.compile(r"^Co-Authored-By: Claude\b", re.IGNORECASE)
_LABEL_SUFFIX = re.compile(r"\s\((?:[A-Z]{1,2}\d{1,2}(?:[-.]\d+)?|P\d+-C\d+|[A-Z]\d+(?:, ?[A-Z]\d+)+)\)$")


def _trailers(commit) -> list[str]:
    return gatelib.trailer_block(commit.message.body)


def _changed_paths(commit) -> list[str]:
    """Every path the change touches: the staged change, or the commit's own.

    Read from git with rename detection off, so a move is the old path deleted
    and the new one added. gitlint's own changed_files splits git's
    "dir/{old => new}/file" rename notation on whitespace and so mangles
    moved paths.
    """
    paths = getattr(commit, "_gate_changed_paths", None)  # read once per commit, for every rule
    if paths is None:
        args = (["diff-tree", "--no-commit-id", "-r", "--root", commit.sha] if commit.sha
                else ["diff", "--cached"])
        out = gatelib.git(*args, "--no-renames", "--name-only", "-z", cwd=commit.context.repository_path)
        paths = [path for path in out.split("\0") if path]
        commit._gate_changed_paths = paths
    return paths


def _diff(commit, path: str) -> str:
    """The change to ``path``: the staged one, or the commit's own when linting a range."""
    args = ["show", "--format=", commit.sha] if commit.sha else ["diff", "--cached"]
    return gatelib.git(*args, "--", path, cwd=commit.context.repository_path, check=False)


class NoTrackerTrailer(CommitRule):
    """No Refs: trailer: the message carries no tracker ids."""

    name = "no-tracker-trailer"
    id = "UC1"

    def validate(self, commit):
        return [
            RuleViolation(self.id, "commit messages carry no tracker ids (no Refs: trailer)", line, number)
            for number, line in enumerate(commit.message.body, 2)
            if re.match(r"^Refs?:", line, re.IGNORECASE)
        ]


class NoPlanningLabel(CommitRule):
    """The subject does not end with a parenthesised label (a letter and a number)."""

    name = "no-planning-label"
    id = "UC2"

    def validate(self, commit):
        if _LABEL_SUFFIX.search(commit.message.title):
            return [RuleViolation(self.id, "subject ends with a parenthesised label", commit.message.title, 1)]
        return []


class KindLine(CommitRule):
    """A code change says whether it suits upstream."""

    name = "kind-line"
    id = "UC3"

    def validate(self, commit):
        kind = _TYPE.match(commit.message.title)
        if not kind or kind.group(1) not in ("feat", "fix", "perf", "refactor"):
            return []
        if not any(path.startswith("mower_sdk/") for path in _changed_paths(commit)):
            return []
        if any(_KIND.match(line) for line in commit.message.body):
            return []
        return [RuleViolation(
            self.id, "a change to mower_sdk/ needs its kind sentence: 'Upstream-suitable.' or 'Community-only.'",
            None, 1,
        )]


class TrailerOrder(CommitRule):
    """Co-author lines are trailers, and no co-author follows the assistant attribution.

    Other trailers may follow it, such as the Signed-off-by that `git commit -s`
    appends.
    """

    name = "trailer-order"
    id = "UC4"

    def validate(self, commit):
        body = commit.message.body
        start = gatelib.trailer_start(body)
        violations = [
            RuleViolation(self.id, "co-author lines belong in the trailer block at the end", line, number)
            for number, line in enumerate(body[:start], 2)
            if _CO_AUTHOR.match(line)
        ]
        trailers = [line for line in body[start:] if line.strip()]
        assistant = [i for i, line in enumerate(trailers) if _ASSISTANT.match(line)]
        if assistant and any(_CO_AUTHOR.match(line) for line in trailers[assistant[-1] + 1:]):
            violations.append(RuleViolation(self.id, "the assistant attribution must be the last co-author"))
        return violations


class VersionInRelease(CommitRule):
    """__version__ changes only in a release commit that also updates the changelog."""

    name = "version-in-release"
    id = "UC5"

    def validate(self, commit):
        changed = _changed_paths(commit)
        if "mower_sdk/__init__.py" not in changed:
            return []
        if not any(line.startswith("+__version__") for line in _diff(commit, "mower_sdk/__init__.py").splitlines()):
            return []
        if commit.message.title.startswith("chore(release)") and "CHANGELOG.md" in changed:
            return []
        return [RuleViolation(
            self.id, "__version__ changes only in a chore(release) commit that also updates CHANGELOG.md", None, 1,
        )]


class ForkAuthorProvenance(CommitRule):
    """Crediting a fork author means recording the origin in UPSTREAM.md."""

    name = "fork-author-provenance"
    id = "UC6"

    def validate(self, commit):
        credited = [line for line in _trailers(commit) if _CO_AUTHOR.match(line) and not _ASSISTANT.match(line)]
        if credited and "UPSTREAM.md" not in _changed_paths(commit):
            return [RuleViolation(
                self.id, "a fork author is credited but UPSTREAM.md does not record the origin", credited[0],
            )]
        return []


_LEGACY_EDIT = re.compile(r"^Legacy-edit:(.*)$")


class LegacyEditTrailer(CommitRule):
    """A change to a protected path says why, in a Legacy-edit trailer.

    Code moved verbatim from upstream (mower_sdk/legacy/) and the inventory of
    upstream's public names (tests/upstream_exports.json) are not changed;
    the hooks no-legacy-edits, no-inventory-edits and no-protected-deletions
    refuse it. A deliberate exception skips those hooks and carries
    "Legacy-edit: <reason>", which this rule requires, here and in CI.
    """

    name = "legacy-edit-trailer"
    id = "UC7"

    def validate(self, commit):
        reasons = [m.group(1).strip() for line in _trailers(commit) if (m := _LEGACY_EDIT.match(line))]
        touched = [path for path in _changed_paths(commit) if gatelib.is_protected(path)]
        if touched and not reasons:
            return [RuleViolation(
                self.id, f"a change to a protected path ({touched[0]}) needs a 'Legacy-edit: <reason>' trailer",
                None, 1,
            )]
        if reasons and not touched:
            return [RuleViolation(self.id, "a Legacy-edit trailer on a commit that changes no protected path")]
        if any(not reason for reason in reasons):
            return [RuleViolation(self.id, "the Legacy-edit trailer needs a reason")]
        return []

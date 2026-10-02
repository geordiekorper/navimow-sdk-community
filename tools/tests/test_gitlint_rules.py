"""The commit-message rules: .gitlint and tools/gitlint_rules.py, run by gitlint itself.

Each test stages a change in a throwaway repository and lints a message
against it as the commit-msg hook does (gitlint --staged --msg-filename), or
lints a commit range as CI does (gitlint --commits).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, run, stage

pytest.importorskip("gitlint")

GITLINT = [
    sys.executable,
    "-m",
    "gitlint.cli",
    "--config",
    str(ROOT / ".gitlint"),
    "--extra-path",
    str(ROOT / "tools" / "gitlint_rules.py"),
]

GOOD = """feat(sdk): report the cache age of state messages

Why the change is made and what a caller sees now.

Upstream-suitable. 12 tests pass on 3.11 (oldest bounds) and 3.14
(newest); ruff is clean.

Co-authored-by: Fork Author <fork@example.com>
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
"""


# A violation line: "<line number or ->: <rule id> <message>".
_VIOLATION = re.compile(r"^(?:\d+|-): (\S+) ")


def rule_ids(output: str) -> list[str]:
    return [m.group(1) for line in output.splitlines() if (m := _VIOLATION.match(line))]


def run_gitlint(repo: Path, *args: str) -> str:
    """Run gitlint and return its report, failing on anything but violations.

    gitlint exits 0 when nothing is broken and with the number of violations
    otherwise; a configuration or git error has its own exit code and output,
    which must not pass for "no violations".
    """
    proc = subprocess.run([*GITLINT, *args], cwd=repo, capture_output=True, text=True, check=False)
    unexpected = [
        line
        for line in proc.stderr.splitlines()
        if line.strip() and not _VIOLATION.match(line) and not line.startswith("Commit ")
    ]
    assert not unexpected and not proc.stdout.strip(), proc.stdout + proc.stderr
    assert proc.returncode == min(len(rule_ids(proc.stderr)), 250), (proc.returncode, proc.stderr)
    return proc.stderr


def gitlint(repo: Path, *args: str) -> list[str]:
    """The ids of the rules gitlint reports (see run_gitlint)."""
    return rule_ids(run_gitlint(repo, *args))


def lint(repo: Path, message: str) -> list[str]:
    """The ids of the rules the message breaks, against the staged change."""
    path = repo / "MSG"
    path.write_text(message, encoding="utf-8")
    return sorted(set(gitlint(repo, "--staged", "--msg-filename", str(path))))


@pytest.fixture
def code(repo: Path) -> Path:
    """A staged change to the package and its provenance record."""
    stage(repo, "mower_sdk/sdk.py", "X = 1\n")
    stage(repo, "docs/UPSTREAM.md", "# Provenance\n")
    return repo


def test_a_conventional_message_passes(code: Path) -> None:
    assert lint(code, GOOD) == []


@pytest.mark.parametrize(
    ("subject", "rule"),
    [
        ("update things", "CT1"),
        ("feature(sdk): x", "CT1"),
        ("fixup! feat(sdk): report the cache age", "CT1"),
        ("squash! feat(sdk): report the cache age", "CT1"),
        ("amend! feat(sdk): report the cache age", "CT1"),
        ("feat(sdk): report the age.", "T3"),
        ("feat(sdk): keepalive defaults to 60 s (" + "D1)", "UC2"),
        ("feat(sdk): " + "x" * 62, "T1"),  # 73 characters
    ],
)
def test_subject_rules(code: Path, subject: str, rule: str) -> None:
    assert rule in lint(code, GOOD.replace(GOOD.splitlines()[0], subject, 1))


def test_a_subject_of_72_characters_passes(code: Path) -> None:
    assert lint(code, GOOD.replace(GOOD.splitlines()[0], "feat(sdk): " + "x" * 61, 1)) == []


def test_body_length_limits(code: Path) -> None:
    assert "B1" in lint(code, GOOD.replace("Why the change", "x" * 101 + "\nWhy the change"))
    assert "B5" in lint(code, "feat(sdk): x\n\nShort.\n")


def test_a_tracker_trailer_is_refused(code: Path) -> None:
    message = GOOD.replace("Co-authored-by: Fork", "Refs: TRK-1\nCo-authored-by: Fork")
    assert "UC1" in lint(code, message)


def test_a_body_is_required_except_for_reverts_and_releases(repo: Path) -> None:
    stage(repo, "README.md", "# Test\n\nMore.\n")
    assert "B6" in lint(repo, "fix(api): x\n")
    assert "B6" in lint(repo, "docs: x\n")
    assert "B6" in lint(repo, "docs(readme): x\n")
    assert "B5" in lint(repo, "docs: x\n\nShort.\n")
    assert lint(repo, "docs: x\n\nWhy the change is made, and what it does.\n") == []
    assert lint(repo, "revert: fix(api): x\n") == []
    assert lint(repo, "chore(release): bump version to 1.0\n") == []


def test_a_package_change_needs_its_kind_sentence(code: Path, repo: Path) -> None:
    assert "UC3" in lint(code, GOOD.replace("Upstream-suitable. ", ""))
    prose = GOOD.replace(
        "Upstream-suitable. 12 tests", "Community-only behaviour changed; 12 tests"
    )
    assert "UC3" in lint(code, prose)
    run("git", "reset", "-q", "mower_sdk/sdk.py", cwd=repo)
    assert "UC3" not in lint(code, GOOD.replace("Upstream-suitable. ", ""))  # no package change


@pytest.mark.parametrize(
    "line",
    [
        "Co-authored-by: Fork Author <fork@example.com>",
        "Co-Authored-By: Claude Opus 5.5 <x@example.com>",
    ],
)
def test_a_co_author_line_in_the_body_is_refused(code: Path, line: str) -> None:
    message = GOOD.replace("Why the change", f"{line}\nWhy the change")
    assert "UC4" in lint(code, message)


def test_the_assistant_attribution_is_the_last_trailer(code: Path) -> None:
    lines = GOOD.rstrip("\n").splitlines()
    swapped = "\n".join([*lines[:-2], lines[-1], lines[-2]]) + "\n"
    assert "UC4" in lint(code, swapped)


def test_a_sign_off_after_the_assistant_attribution_passes(code: Path) -> None:
    # git commit -s appends Signed-off-by after the existing trailers.
    assert lint(code, GOOD + "Signed-off-by: Test <test@example.com>\n") == []


def test_a_cherry_pick_line_keeps_the_trailer_block(code: Path, repo: Path) -> None:
    # git cherry-pick -x appends this line to the last paragraph.
    picked = GOOD + "(cherry picked from commit 0123456789abcdef0123456789abcdef01234567)\n"
    assert lint(code, picked) == []
    run("git", "reset", "-q", "docs/UPSTREAM.md", cwd=repo)
    assert lint(code, picked) == ["UC6"]  # the fork credit is still read as a trailer


def test_the_version_changes_only_in_a_release_commit(repo: Path) -> None:
    stage(repo, "mower_sdk/__init__.py", '__version__ = "9.9"\n')
    assert "UC5" in lint(repo, "build(sdk): x\n\nWhy.\n")
    assert "UC5" in lint(repo, "chore(release): bump version to 9.9\n")  # no changelog
    stage(repo, "CHANGELOG.md", "# Changelog\n")
    assert lint(repo, "chore(release): bump version to 9.9\n") == []


def test_crediting_a_fork_author_needs_the_provenance_record(code: Path, repo: Path) -> None:
    run("git", "reset", "-q", "docs/UPSTREAM.md", cwd=repo)
    assert "UC6" in lint(code, GOOD)
    # The record is docs/UPSTREAM.md: a file of that name anywhere else does not count.
    stage(repo, "UPSTREAM.md", "# Provenance\n")
    assert "UC6" in lint(code, GOOD)


def test_the_range_form_judges_each_commit_by_its_own_diff(repo: Path) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    stage(repo, "mower_sdk/sdk.py", "X = 1\n")
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "feat(sdk): x\n\nWhy the change is made, and what it does.",
        cwd=repo,
    )
    # A fix without a kind sentence passes only because this commit touches no
    # mower_sdk/ path: a changed-file list shared across the range would fail it.
    stage(repo, "tests/test_x.py", "def test_x():\n    pass\n")
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "fix(tests): x\n\nWhy the change is made, and what it does.",
        cwd=repo,
    )
    assert gitlint(repo, "--commits", f"{base}..HEAD") == ["UC3"]


def test_the_range_form_reads_each_commits_own_version_change(repo: Path) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    stage(repo, "mower_sdk/__init__.py", '__version__ = "1.0"\n')
    stage(repo, "CHANGELOG.md", "# Changelog\n")
    run("git", "commit", "-q", "-m", "chore(release): bump version to 1.0", cwd=repo)
    stage(repo, "mower_sdk/__init__.py", '__version__ = "1.0"\nX = 1\n')  # no version change
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "build(sdk): x\n\nWhy the change is made, and what it does.",
        cwd=repo,
    )
    stage(repo, "mower_sdk/__init__.py", '__version__ = "1.1"\nX = 1\n')
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "build(sdk): y\n\nWhy the change is made, and what it does.",
        cwd=repo,
    )
    last = run("git", "rev-parse", "--short=10", "HEAD", cwd=repo).strip()
    report = run_gitlint(repo, "--commits", f"{base}..HEAD")
    assert rule_ids(report) == ["UC5"]
    assert f"Commit {last}" in report


def per_commit(output: str) -> dict[str, list[str]]:
    """Rule ids per commit from a --commits run ("Commit <sha>:" headers)."""
    result: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("Commit "):
            current = line.split()[1].rstrip(":")
            result[current] = []
        elif (match := _VIOLATION.match(line)) and current:
            result[current].append(match.group(1))
    return result


def test_the_range_form_applies_every_rule_to_each_commit(repo: Path) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    body = "Why the change is made, and what it does."
    commits = {}

    def commit(path: str, text: str, message: str) -> None:
        stage(repo, path, text)
        run("git", "commit", "-q", "-m", message, cwd=repo)
        commits[message.split("\n", 1)[0]] = run(
            "git", "rev-parse", "--short=10", "HEAD", cwd=repo
        ).strip()

    commit("a.txt", "a\n", f"docs(a): tracker\n\n{body}\n\nRefs: TRK-1")
    commit("b.txt", "b\n", "docs(b): labelled (" + "Q5)\n\n" + body)
    commit(
        "c.txt",
        "c\n",
        f"docs(c): order\n\n{body}\n\nCo-Authored-By: Claude X <x@example.com>\n"
        "Co-authored-by: Fork Author <fork@example.com>",
    )
    commit(
        "d.txt",
        "d\n",
        f"docs(d): credit\n\n{body}\n\nCo-authored-by: Fork Author <fork@example.com>",
    )
    commit("docs/UPSTREAM.md", "# Provenance\n", f"docs(e): provenance elsewhere\n\n{body}")
    report = run_gitlint(repo, "--commits", f"{base}..HEAD")
    found = per_commit(report)
    # A fork credit in one commit is not satisfied by docs/UPSTREAM.md in another.
    assert found == {
        commits["docs(a): tracker"]: ["UC1"],
        commits["docs(b): labelled (" + "Q5)"]: ["UC2"],
        commits["docs(c): order"]: ["UC4", "UC6"],
        commits["docs(d): credit"]: ["UC6"],
    }


LEGACY = "\nLegacy-edit: upstream's fix for a crash, taken as is\n"


@pytest.mark.parametrize(
    ("change", "trailer", "expected"),
    [
        ("edit", False, ["UC7"]),
        ("edit", True, []),
        ("delete-inventory", False, ["UC7"]),
        ("delete-inventory", True, []),
        ("none", True, ["UC7"]),
    ],
)
def test_a_protected_change_needs_a_legacy_edit_trailer(
    repo: Path, change: str, trailer: bool, expected: list[str]
) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    stage(repo, "tests/upstream_exports.json", "{}\n")
    run("git", "commit", "-q", "-m", "chore: layout", cwd=repo)
    if change == "edit":
        stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    elif change == "delete-inventory":
        run("git", "rm", "-q", "tests/upstream_exports.json", cwd=repo)
    else:
        stage(repo, "README.md", "# Test\n\nMore.\n")
    message = "chore(legacy): x\n\nWhy the change is made, and what it does.\n" + (
        LEGACY if trailer else ""
    )
    assert lint(repo, message) == expected


@pytest.mark.parametrize(
    ("with_module", "trailer", "expected"),
    [
        (False, False, []),  # the README alone is an ordinary change
        (False, True, ["UC7"]),  # and a trailer on it claims a protected change that is not there
        (True, False, ["UC7"]),  # the README does not carry a module edit
        (True, True, []),
    ],
)
def test_the_legacy_readme_needs_no_trailer(
    repo: Path, with_module: bool, trailer: bool, expected: list[str]
) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    run("git", "commit", "-q", "-m", "chore: layout", cwd=repo)
    stage(repo, "mower_sdk/legacy/README.md", "# Legacy\n")
    if with_module:
        stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    message = "chore(legacy): x\n\nWhy the change is made, and what it does.\n" + (
        LEGACY if trailer else ""
    )
    assert lint(repo, message) == expected
    run("git", "commit", "-q", "-m", message, cwd=repo)
    # The same commit in range mode, as CI lints it.
    assert gitlint(repo, "--commits", "HEAD~1..HEAD") == expected


@pytest.mark.parametrize("trailer", [False, True])
def test_moving_code_out_of_legacy_needs_the_trailer(repo: Path, trailer: bool) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    run("git", "commit", "-q", "-m", "chore: layout", cwd=repo)
    (repo / "mower_sdk" / "core").mkdir()
    run("git", "mv", "mower_sdk/legacy/client.py", "mower_sdk/core/client.py", cwd=repo)
    message = "chore(legacy): x\n\nWhy the change is made, and what it does.\n" + (
        LEGACY if trailer else ""
    )
    assert lint(repo, message) == ([] if trailer else ["UC7"])
    run("git", "commit", "-q", "-m", message, cwd=repo)
    # The same commit in range mode (one commit: gitlint prints no header).
    assert gitlint(repo, "--commits", "HEAD~1..HEAD") == ([] if trailer else ["UC7"])


def test_a_legacy_edit_trailer_needs_a_reason(repo: Path) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    assert "UC7" in lint(
        repo, "chore(legacy): x\n\nWhy the change is made, and what it does.\n\nLegacy-edit: \n"
    )


def test_the_range_form_checks_each_commit_for_its_trailer(repo: Path) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    body = "Why the change is made, and what it does."
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    run("git", "commit", "-q", "-m", f"chore(legacy): sanctioned\n\n{body}\n{LEGACY}", cwd=repo)
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    run("git", "commit", "-q", "-m", f"chore(legacy): unsanctioned\n\n{body}", cwd=repo)
    unsanctioned = run("git", "rev-parse", "--short=10", "HEAD", cwd=repo).strip()
    report = run_gitlint(repo, "--commits", f"{base}..HEAD")
    assert per_commit(report) == {unsanctioned: ["UC7"]}


def test_a_revert_commit_is_linted(repo: Path) -> None:
    stage(repo, "README.md", "# Test\n\nMore.\n")
    assert "CT1" in lint(repo, 'Revert "feat(sdk): x"\n\nThis reverts commit 0123456789abcdef.\n')
    assert lint(repo, "revert: feat(sdk): x\n\nThis reverts commit 0123456789abcdef.\n") == []
    assert lint(repo, "revert: feat(sdk): x\n") == []  # no body needed


# ---- commits ported from upstream ----------------------------------------------------------------

UPSTREAM_SHA = "0123456789abcdef0123456789abcdef01234567"
# Upstream's message: no rule would pass it.
PORTED = f"update the client.\n\nUpstream-commit: {UPSTREAM_SHA}\n"


@pytest.mark.parametrize(
    "path",
    [
        "mower_sdk/legacy/client.py",
        "mower_sdk/mqtt.py",
        "mower_sdk/models.py",
        "mower_sdk/errors.py",
    ],
    ids=["legacy", "mixed-mqtt", "mixed-models", "mixed-errors"],
)
def test_a_ported_commit_is_exempt_from_every_rule(repo: Path, path: str) -> None:
    stage(repo, path, "OLD = 2\n")
    assert {"CT1", "T3"} <= set(
        lint(repo, "update the client.\n")
    )  # the same message without the trailer
    assert lint(repo, PORTED) == []


def test_a_ported_commit_keeps_its_exemption_when_its_subject_matches_another_ignore_rule(
    repo: Path,
) -> None:
    # chore(release) subjects are exempt from the body rules only; the port's exemption is
    # not narrowed to that.
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    assert lint(repo, f"chore(release): upstream's 1.0.\n\nUpstream-commit: {UPSTREAM_SHA}\n") == []


@pytest.mark.parametrize(
    "path",
    ["mower_sdk/sdk.py", "mower_sdk/legacy/README.md", "tests/upstream_exports.json", "README.md"],
    ids=["live-path", "legacy-readme", "inventory", "document"],
)
def test_the_trailer_exempts_nothing_on_a_commit_that_changes_another_path(
    repo: Path, path: str
) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    stage(repo, path, "X = 1\n")
    found = lint(repo, PORTED)
    assert {"UC9", "CT1", "T3"} <= set(found)


def test_the_trailer_exempts_nothing_on_a_commit_that_changes_nothing(repo: Path) -> None:
    assert {"UC9", "CT1"} <= set(lint(repo, PORTED))


@pytest.mark.parametrize(
    "message",
    [
        "update the client.\n\nUpstream-commit: 0123456\n",
        f"update the client.\n\nUpstream-commit: {UPSTREAM_SHA}\n\nMore text after it.\n",
        f"update the client.\n\nupstream-commit: {UPSTREAM_SHA}\n",
    ],
    ids=["short-sha", "not-in-the-trailer-block", "another-spelling"],
)
def test_only_the_trailer_as_the_tool_writes_it_exempts(repo: Path, message: str) -> None:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 2\n")
    assert "CT1" in lint(repo, message)


def test_the_exemption_does_not_reach_the_other_commits_of_a_range(repo: Path) -> None:
    """A ported commit between two that break the rules: only those two are reported."""
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    broken = []
    for content, message in (
        ("OLD = 1\n", "update the client."),
        ("OLD = 2\n", PORTED),
        ("OLD = 3\n", "update it again."),
    ):
        stage(repo, "mower_sdk/legacy/client.py", content)
        run("git", "commit", "-q", "-m", message, cwd=repo)
        if message != PORTED:
            broken.append(run("git", "rev-parse", "--short=10", "HEAD", cwd=repo).strip())
    found = per_commit(run_gitlint(repo, "--commits", f"{base}..HEAD"))
    assert sorted(found) == sorted(broken)  # the one before the ported commit and the one after it
    assert all({"CT1", "UC7"} <= set(rules) for rules in found.values())


def test_a_series_ported_by_the_tool_passes_in_range_mode(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream's commits, ported with tools/port_upstream.py, pass as CI lints them."""
    import port_upstream

    stage(repo, "mower_sdk/client.py", "class MowerClient:\n    pass\n")
    run("git", "commit", "-q", "-m", "chore: upstream's layout", cwd=repo)
    run("git", "tag", "base", cwd=repo)

    upstream = ["-c", "user.name=Upstream", "-c", "user.email=upstream@example.invalid"]
    run("git", "checkout", "-q", "-b", "upstream", cwd=repo)
    stage(repo, "mower_sdk/client.py", "class MowerClient:\n    token = None\n")
    run("git", *upstream, "commit", "-q", "-m", "update the client.", cwd=repo)
    stage(repo, "mower_sdk/client.py", "class MowerClient:\n    token = None\n    updates = 0\n")
    signed = "Count Token Updates\n\nx\n\nSigned-off-by: Upstream <upstream@example.invalid>"
    run("git", *upstream, "commit", "-q", "-m", signed, cwd=repo)

    run("git", "checkout", "-q", "-b", "fork", "base", cwd=repo)
    (repo / "mower_sdk" / "legacy").mkdir()
    run("git", "mv", "mower_sdk/client.py", "mower_sdk/legacy/client.py", cwd=repo)
    stage(repo, "mower_sdk/client.py", "from mower_sdk.legacy.client import *\n")
    run("git", "commit", "-q", "-m", "refactor: move client.py to legacy/", cwd=repo)
    moved = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    monkeypatch.setattr(port_upstream, "REPO_ROOT", repo)

    # Without the trailer the ported commits break the rules: upstream's subjects and no
    # Legacy-edit trailer.
    with monkeypatch.context() as unmarked:
        unmarked.setattr(port_upstream, "with_trailer", lambda mail, _commit: mail)
        assert port_upstream.main(["base..upstream"]) == 0
    found = per_commit(run_gitlint(repo, "--commits", f"{moved}..HEAD"))
    assert len(found) == 2 and all({"CT1", "UC7"} <= set(rules) for rules in found.values())
    run("git", "reset", "-q", "--hard", moved, cwd=repo)

    assert port_upstream.main(["base..upstream"]) == 0
    assert (
        run("git", "diff", "--name-only", moved, "HEAD", cwd=repo) == "mower_sdk/legacy/client.py\n"
    )
    subjects = run("git", "log", "--format=%an: %s", f"{moved}..HEAD", cwd=repo).splitlines()
    assert subjects == ["Upstream: Count Token Updates", "Upstream: update the client."]
    trailers = run(
        "git",
        "log",
        "--format=%(trailers:key=Upstream-commit,valueonly)",
        f"{moved}..HEAD",
        cwd=repo,
    ).split()
    assert trailers == run("git", "rev-list", "base..upstream", cwd=repo).split()
    assert gitlint(repo, "--commits", f"{moved}..HEAD") == []

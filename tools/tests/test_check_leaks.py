"""check_leaks: local paths, untracked names, local patterns, Markdown links.

Every leak in these tests is assembled from pieces, because the checks also
run over this file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import check_leaks
import pytest
from conftest import run, stage

EXCLUDED_DIR = "scratch" + "_docs"


def test_a_clean_change_passes(repo: Path) -> None:
    stage(repo, "src/module.py", "VALUE = 2\n")
    assert check_leaks.check_files(["src/module.py"]) == []


def test_an_ignored_document_is_reported_by_name_and_by_basename(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(f"{EXCLUDED_DIR}/\n", encoding="utf-8")
    (repo / EXCLUDED_DIR).mkdir()
    (repo / EXCLUDED_DIR / "release-plan.html").write_text("x", encoding="utf-8")
    stage(repo, "README.md", "# Test\n\nSee the release-plan.html for details.\n")
    stage(repo, "src/module.py", f"# kept in {EXCLUDED_DIR}\n")
    findings = check_leaks.check_files(["README.md", "src/module.py"])
    assert any("release-plan.html" in f for f in findings)
    assert any(EXCLUDED_DIR in f for f in findings)


@pytest.mark.parametrize("name", ["a.md", "x.txt"])
def test_a_short_untracked_name_with_an_extension_is_reported(repo: Path, name: str) -> None:
    (repo / name).write_text("x", encoding="utf-8")
    stage(repo, "README.md", f"# Test\n\nSee {name} for details.\n")
    assert [f.split(": ")[-1] for f in check_leaks.check_files(["README.md"])] == [name]


def test_an_untracked_name_inside_a_longer_tracked_name_is_not_reported(repo: Path) -> None:
    stage(repo, "templates/draft.md.j2", "x\n")
    run("git", "commit", "-q", "-m", "chore: template", cwd=repo)
    (repo / "draft.md").write_text("x", encoding="utf-8")
    stage(repo, "README.md", "# Test\n\nSee draft.md.j2 and, separately, draft.md.\n")
    findings = check_leaks.check_files(["README.md"])
    assert [f.split(": ")[-1] for f in findings] == ["draft.md"]


def test_a_short_untracked_name_that_reads_as_a_word_is_ignored(repo: Path) -> None:
    (repo / "notes").write_text("x", encoding="utf-8")
    stage(repo, "README.md", "# Test\n\nRelease notes are available.\n")
    assert check_leaks.check_files(["README.md"]) == []


def test_an_untracked_file_is_reported_until_it_is_staged(repo: Path) -> None:
    (repo / "draft-notes.txt").write_text("x", encoding="utf-8")
    stage(repo, "src/module.py", "# from draft-notes.txt\n")
    assert check_leaks.check_files(["src/module.py"])
    run("git", "add", "draft-notes.txt", cwd=repo)
    assert check_leaks.check_files(["src/module.py"]) == []


def test_local_patterns_apply_by_scope(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "patterns.txt").write_text(
        "# comment\nboth\tproject-name\t-\t(?i)\\bsecret-project\\b\n"
        "message\ttracker-id\t-\t\\bTRK-\\d+\\b\n",
        encoding="utf-8",
    )
    stage(repo, "src/module.py", "# Secret-Project and TRK-12\n")
    findings = check_leaks.check_files(["src/module.py"])
    assert len(findings) == 1
    assert "project-name" in findings[0]


@pytest.mark.usefixtures("repo")
def test_message_trailers_skip_notrailers_patterns(tmp_path: Path) -> None:
    (tmp_path / "patterns.txt").write_text(
        "message\tauthor-name\tnotrailers\tAlice Example\n", encoding="utf-8"
    )
    trailer = "fix: x\n\nBody.\n\nCo-authored-by: Alice Example <a@example.com>\n"
    assert check_leaks.check_message("MSG", trailer) == []
    body = "fix: x\n\nThanks to Alice Example.\n"
    assert len(check_leaks.check_message("MSG", body)) == 1
    # The same line in the body is not exempt because it also ends the message.
    repeated = (
        "fix: x\n\nCo-authored-by: Alice Example <a@example.com>\nmore body\n\n"
        "Co-authored-by: Alice Example <a@example.com>\n"
    )
    findings = check_leaks.check_message("MSG", repeated)
    assert [f.split(":")[1] for f in findings] == ["3"]


def test_a_claude_session_without_the_pattern_file_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    stage(repo, "src/module.py", "VALUE = 3\n")
    with pytest.raises(SystemExit, match="pattern file"):
        check_leaks.check_files(["src/module.py"])


def test_a_link_to_an_ignored_document_is_caught_by_its_name(repo: Path) -> None:
    (repo / ".git" / "info" / "exclude").write_text(f"{EXCLUDED_DIR}/\n", encoding="utf-8")
    (repo / EXCLUDED_DIR).mkdir()
    (repo / EXCLUDED_DIR / "release-plan.html").write_text("x", encoding="utf-8")
    stage(repo, "docs/guide.md", f"See [the document](../{EXCLUDED_DIR}/release-plan.html).\n")
    findings = check_leaks.check_files(["docs/guide.md"])
    assert any("release-plan.html" in f for f in findings)


def _untracked(repo: Path, name: str = "private-notes.txt") -> str:
    (repo / name).write_text("x", encoding="utf-8")
    return name


def test_only_added_lines_are_checked_in_staged_mode(repo: Path) -> None:
    name = _untracked(repo)
    stage(repo, "src/module.py", f"# see {name}\n")
    assert check_leaks.check_files(["src/module.py"]) == [
        f"src/module.py:1: names a file git does not track: {name}"
    ]
    run("git", "commit", "-q", "-m", "chore: x", cwd=repo)
    stage(repo, "src/module.py", f"# see {name}\nOTHER = 1\n")
    assert check_leaks.check_files(["src/module.py"]) == []


def test_content_mode_reads_whole_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _untracked(repo)
    stage(repo, "src/module.py", f"# see {name}\n")
    run("git", "commit", "-q", "-m", "chore: x", cwd=repo)
    monkeypatch.setenv("GATE_MODE", "content")
    assert len(check_leaks.check_files(["src/module.py"])) == 1


def test_range_mode_reads_the_diff_between_refs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    name = _untracked(repo)
    stage(repo, "src/module.py", f"# see {name}\n")
    run("git", "commit", "-q", "-m", "chore: x", cwd=repo)
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", base)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "HEAD")
    assert len(check_leaks.check_files(["src/module.py"])) == 1


def test_an_added_line_that_looks_like_a_diff_header_is_checked(repo: Path) -> None:
    name = _untracked(repo)
    stage(repo, "README.md", f"# Test\n++ {name}\n")
    assert [f.split(": ")[0] for f in check_leaks.check_files(["README.md"])] == ["README.md:2"]


@pytest.mark.parametrize(
    "path", ["docs/my guide.md", "docs/tab\tname.md", "docs/quote\"name.md", "docs/café.md"]
)
def test_files_with_awkward_names_are_checked_under_their_own_name(repo: Path, path: str) -> None:
    name = _untracked(repo)
    stage(repo, path, f"see {name}\n")
    assert check_leaks.check_files([path]) == [f"{path}:1: names a file git does not track: {name}"]


CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"


@pytest.mark.parametrize(
    ("text", "local"),
    [
        ("/" + "Users/alice/x", True),
        ("/" + "home/bob/x", True),
        ("/" + "home/runner/work/x", False),
        ("/" + "private/tmp/a", True),
        ("/" + "var/folders/xy/T/a", True),
        ("C:\\" + "Users\\Alice\\x", True),
        ("c:\\" + "users\\alice", True),
        ("~/" + "Projects/x", True),
        (".claude/" + "worktrees/x", True),
        ("/tmp/extract", False),
        ("~/.config/app", False),
        ("https://example.com/home/docs", False),
        ("/opt/app/home/config", False),
        ("file:///" + "Users/alice/x", True),
    ],
)
def test_the_local_path_hook_pattern(text: str, local: bool) -> None:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    (hook,) = [h for r in config["repos"] for h in r["hooks"] if h["id"] == "no-local-paths"]
    assert hook["language"] == "pygrep"
    assert "stages" not in hook  # files only; the message check reads the same expression
    assert bool(re.search(hook["entry"], text)) is local


def test_an_ignored_document_in_another_worktree_is_reported(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other-worktree"
    run("git", "worktree", "add", "-q", "--detach", str(other), cwd=repo)
    (repo / ".git" / "info" / "exclude").write_text("drafts/\n", encoding="utf-8")
    (other / "drafts").mkdir()
    (other / "drafts" / "release-checklist.md").write_text("x", encoding="utf-8")
    stage(repo, "README.md", "# Test\n\nSee release-checklist.md.\n")
    findings = check_leaks.check_files(["README.md"])
    assert findings == ["README.md:3: names a file git does not track: release-checklist.md"]


def test_local_files_under_dot_claude_are_checked_but_nested_worktrees_are_not(repo: Path) -> None:
    (repo / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "review-agent.md").write_text("x", encoding="utf-8")
    (repo / ".claude" / "worktrees" / "wt" / "docs").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "wt" / "docs" / "nested-only.md").write_text("x", encoding="utf-8")
    stage(repo, "README.md", "# Test\n\nSee review-agent.md and nested-only.md.\n")
    findings = check_leaks.check_files(["README.md"])
    assert [f.split(": ")[-1] for f in findings] == ["review-agent.md"]


EDITOR_TEMPLATE = """
# Please enter the commit message for your changes. Lines starting
# with '#' will be ignored, and an empty message aborts the commit.
#
# Untracked files:
#\t{untracked}
#
"""


def test_git_comment_lines_are_not_part_of_the_message(repo: Path) -> None:
    name = _untracked(repo)
    home = "/" + "Users/alice/notes"
    message = "docs: x\n\nBody.\n" + EDITOR_TEMPLATE.format(untracked=name) + f"# {home}\n"
    assert check_leaks.check_message("MSG", message) == []


@pytest.mark.usefixtures("repo")
def test_a_verbose_diff_below_the_scissors_is_not_part_of_the_message() -> None:
    home = "/" + "Users/alice/notes"
    message = (
        "docs: x\n\nBody.\n# ------------------------ >8 ------------------------\n"
        f"diff --git a/x b/x\n+PATH = '{home}'\n"
    )
    assert check_leaks.check_message("MSG", message) == []


def test_the_message_is_checked_for_local_paths_and_untracked_names(repo: Path) -> None:
    name = _untracked(repo)
    home = "/" + "Users/alice/notes"
    findings = check_leaks.check_message("MSG", f"docs: x\n\nSee {home} and {name}.\n")
    assert [f.split(": ", 1)[1] for f in findings] == [
        "local path: /" + "Users/alice", f"names a file git does not track: {name}",
    ]


def test_another_comment_character_is_honoured(repo: Path) -> None:
    run("git", "config", "core.commentChar", ";", cwd=repo)
    name = _untracked(repo)
    message = f"docs: x\n\n# a line git keeps: {name}\n; a comment: {name}\n"
    assert [f.split(":")[1] for f in check_leaks.check_message("MSG", message)] == ["3"]


@pytest.mark.usefixtures("repo")
def test_trailers_followed_by_git_comments_are_still_trailers(tmp_path: Path) -> None:
    (tmp_path / "patterns.txt").write_text(
        "message\tauthor-name\tnotrailers\tAlice Example\n", encoding="utf-8"
    )
    message = (
        "fix: x\n\nBody.\n\nCo-authored-by: Alice Example <a@example.com>\n"
        + EDITOR_TEMPLATE.format(untracked="nothing")
    )
    assert check_leaks.check_message("MSG", message) == []


TOOLS = Path(__file__).resolve().parents[1]


@pytest.fixture
def hooked(repo: Path, tmp_path: Path) -> Path:
    """The repository with a pre-commit hook running check_leaks, and a second
    worktree on another branch holding an untracked file and a file tracked
    only on that branch."""
    other = tmp_path / "other"
    run("git", "worktree", "add", "-q", "-b", "feature", str(other), cwd=repo)
    (other / "feature-only.md").write_text("x", encoding="utf-8")
    run("git", "add", "feature-only.md", cwd=other)
    run("git", "commit", "-q", "-m", "docs: feature", cwd=other)
    (other / "genuinely-untracked.txt").write_text("x", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{TOOLS / "check_leaks.py"}" $(git diff --cached --name-only)\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return repo


def _commit(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "commit", "-q", *args], cwd=repo, capture_output=True, text=True, check=False)


def test_inside_a_commit_hook_another_worktrees_untracked_file_is_reported(hooked: Path) -> None:
    stage(hooked, "README.md", "# Test\n\nSee genuinely-untracked.txt.\n")
    proc = _commit(hooked, "-m", "docs: x")
    assert proc.returncode != 0
    assert "names a file git does not track: genuinely-untracked.txt" in proc.stdout + proc.stderr


@pytest.mark.parametrize("all_tracked", [False, True], ids=["commit", "commit -a"])
def test_inside_a_commit_hook_another_branchs_tracked_file_is_not_reported(
    hooked: Path, all_tracked: bool
) -> None:
    (hooked / "README.md").write_text("# Test\n\nSee feature-only.md.\n", encoding="utf-8")
    if all_tracked:
        proc = _commit(hooked, "-a", "-m", "docs: x")  # the hook sees this worktree's index.lock
    else:
        run("git", "add", "README.md", cwd=hooked)
        proc = _commit(hooked, "-m", "docs: x")  # the hook sees a relative GIT_INDEX_FILE
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_nothing_to_check_lists_no_worktree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(check_leaks.gatelib, "untracked_name_regex", lambda: calls.append("scan"))
    stage(repo, "src/module.py", "VALUE = 1\n")  # staged, but no added line
    assert check_leaks.check_files(["src/module.py"]) == []
    assert check_leaks.check_files([]) == []
    assert calls == []

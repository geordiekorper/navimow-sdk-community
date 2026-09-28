"""The CI hygiene job: which range it checks, and how it runs the checks.

The base-selection step is taken from .github/workflows/ci.yml and run as
written in a throwaway repository, with the event values GitHub would pass.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import run, stage

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
JOB = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["hygiene"]
STEPS = {step.get("name"): step for step in JOB["steps"]}


def base_for(repo: Path, tmp_path: Path, base_ref: str, before: str) -> str:
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding="utf-8")
    env = {**os.environ, "BASE_REF": base_ref, "BEFORE": before, "GITHUB_ENV": str(github_env)}
    subprocess.run(["bash", "-e", "-c", STEPS["Find the base of the range"]["run"]],
                   cwd=repo, env=env, check=True, capture_output=True)
    (line,) = github_env.read_text(encoding="utf-8").splitlines()
    assert line.startswith("BASE=")
    return line[len("BASE="):]


@pytest.fixture
def pushed(repo: Path) -> tuple[Path, str]:
    """A repository with an origin/main and one commit on top of it."""
    run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    before = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    stage(repo, "README.md", "# Test\n\nMore.\n")
    run("git", "commit", "-q", "-m", "docs: more", cwd=repo)
    return repo, before


def test_a_pull_request_is_checked_from_its_base_branch(pushed: tuple[Path, str], tmp_path: Path) -> None:
    repo, before = pushed
    assert base_for(repo, tmp_path, "main", before) == "origin/main"


def test_a_push_is_checked_from_the_previous_tip(pushed: tuple[Path, str], tmp_path: Path) -> None:
    repo, before = pushed
    assert base_for(repo, tmp_path, "", before) == before


@pytest.mark.parametrize("before", ["", "0" * 40, "1234567890abcdef1234567890abcdef12345678"])
def test_without_a_known_base_only_the_whole_tree_is_checked(
    pushed: tuple[Path, str], tmp_path: Path, before: str
) -> None:
    repo, _ = pushed
    assert base_for(repo, tmp_path, "", before) == ""


def test_the_steps_run_every_check_in_the_right_mode() -> None:
    assert JOB["steps"][0]["with"] == {"fetch-depth": 0}
    whole = STEPS["Every hook over every file"]
    assert whole["env"] == {"GATE_MODE": "content", "SKIP": "no-legacy-edits,no-inventory-edits"}
    assert "--all-files" in whole["run"]
    ranged = [
        STEPS["The hooks over the range's changes"],
        STEPS["The range's commit messages"],
        STEPS["Local paths and untracked names in the range's commit messages"],
    ]
    assert all(step["if"] == "env.BASE != ''" for step in ranged)
    assert '--from-ref "$BASE" --to-ref HEAD' in ranged[0]["run"]
    assert 'gitlint --commits "$BASE..HEAD"' in ranged[1]["run"]
    assert "pre-commit run check-message-leaks" in ranged[2]["run"]
    assert "--hook-stage commit-msg" in ranged[2]["run"]


def test_the_base_step_takes_its_values_from_the_event() -> None:
    assert STEPS["Find the base of the range"]["env"] == {
        "BASE_REF": "${{ github.base_ref }}",
        "BEFORE": "${{ github.event.before }}",
    }


# A stand-in for pre-commit: records the hook and the message's subject for
# each call, and fails for a message containing BAD, as a hook would for a
# real problem.
FAKE_PRE_COMMIT = (
    "#!/bin/sh\n"
    'hook="$2"; message=""\n'
    'while [ $# -gt 0 ]; do [ "$1" = "--commit-msg-filename" ] && message="$2"; shift; done\n'
    'printf "%s|%s\\n" "$(sed -n 1p "$message")" "$hook" >> "$CALLS"\n'
    'grep -q BAD "$message" && exit 1\n'
    "exit 0\n"
)


def test_the_message_step_checks_every_non_merge_commit_and_fails_on_one(repo: Path, tmp_path: Path) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    body = "Why the change is made, and what it does."
    stage(repo, "a.txt", "a\n")
    run("git", "commit", "-q", "-m", f"docs: good\n\n{body}", cwd=repo)
    run("git", "switch", "-q", "-c", "side", cwd=repo)
    stage(repo, "b.txt", "b\n")
    run("git", "commit", "-q", "-m", f"docs: BAD message\n\n{body}", cwd=repo)
    bad = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    run("git", "switch", "-q", "main", cwd=repo)
    run("git", "merge", "-q", "--no-ff", "-m", "Merge side", "side", cwd=repo)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "pre-commit"
    fake.write_text(FAKE_PRE_COMMIT, encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "BASE": base, "RUNNER_TEMP": str(tmp_path), "CALLS": str(calls),
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    step = STEPS["Local paths and untracked names in the range's commit messages"]
    proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 1
    assert f"in {bad}" in proc.stdout
    recorded = sorted(calls.read_text(encoding="utf-8").splitlines())
    assert recorded == sorted(["docs: good|check-message-leaks", "docs: BAD message|check-message-leaks"])

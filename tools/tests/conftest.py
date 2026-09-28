"""Fixtures for the tests of the commit checks: a throwaway git repository."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository with one commit, as the current directory.

    The checks read the environment, so a Claude session, a pre-commit range
    and a pattern file from the caller are all cleared.
    """
    # A GIT_DIR inherited from a hook or an alias would point every git command
    # below at the caller's repository instead of this one.
    for name in [n for n in os.environ if n.startswith("GIT_") and n != "GIT_EXEC_PATH"]:
        monkeypatch.delenv(name)
    root = tmp_path / "repo"
    root.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=root)
    run("git", "config", "user.email", "test@example.com", cwd=root)
    run("git", "config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run("git", "add", ".", cwd=root)
    run("git", "commit", "-q", "-m", "chore: start", cwd=root)
    monkeypatch.chdir(root)
    for name in ("CLAUDECODE", "PRE_COMMIT_FROM_REF", "PRE_COMMIT_TO_REF", "GATE_MODE",
                 "GATE_ALLOW_LEGACY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMIT_GATE_PATTERNS", str(tmp_path / "patterns.txt"))
    return root


def stage(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run("git", "add", relative, cwd=root)

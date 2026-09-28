"""Fixtures for the tests of the commit checks: a throwaway git repository."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gatelib  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".pre-commit-config.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def hook(hook_id: str) -> dict:
    """A hook's definition in this repository's .pre-commit-config.yaml."""
    return gatelib.hook(hook_id, CONFIG)


def _isolated_env(global_config: Path) -> dict[str, str]:
    """The caller's environment without its git state or git configuration.

    A GIT_DIR inherited from a hook or an alias would point git at the
    caller's repository; the developer's own configuration (commit signing, a
    global hooks path, a commit template, another comment character) must
    not reach the throwaway repositories.
    """
    return {**gatelib.foreign_env(), "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The starting repository, built once and copied for each test."""
    base = tmp_path_factory.mktemp("template")
    global_config = base / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    env = _isolated_env(global_config)
    root = base / "repo"
    root.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=root, env=env)
    run("git", "config", "user.email", "test@example.com", cwd=root, env=env)
    run("git", "config", "user.name", "Test", cwd=root, env=env)
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    # The message check reads the local-path expression from the configuration.
    shutil.copy(CONFIG, root / ".pre-commit-config.yaml")
    run("git", "add", ".", cwd=root, env=env)
    run("git", "commit", "-q", "-m", "chore: start", cwd=root, env=env)
    return root


@pytest.fixture
def repo(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository with one commit (and this repository's pre-commit
    configuration), as the current directory.

    The checks read the environment, so the caller's git state and git
    configuration, a Claude session, a pre-commit range and a pattern file
    are all cleared.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    isolated = _isolated_env(global_config)
    for name in [n for n in os.environ if n not in isolated]:
        monkeypatch.delenv(name)
    for name, value in isolated.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    shutil.copytree(template, root, symlinks=True)
    monkeypatch.chdir(root)
    for name in ("CLAUDECODE", "PRE_COMMIT_FROM_REF", "PRE_COMMIT_TO_REF", "GATE_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMIT_GATE_PATTERNS", str(tmp_path / "patterns.txt"))
    return root


def stage(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run("git", "add", relative, cwd=root)

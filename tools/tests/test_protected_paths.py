"""Protected paths: the deletion check, and the hooks that guard edits."""

from __future__ import annotations

import re
from pathlib import Path

import check_protected_paths
import pytest
from conftest import run, stage

CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"


@pytest.fixture
def sdk(repo: Path) -> Path:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    stage(repo, "tests/upstream_exports.json", "{}\n")
    run("git", "commit", "-q", "-m", "chore: layout", cwd=repo)
    return repo


def test_edits_and_additions_are_left_to_the_other_hooks(sdk: Path) -> None:
    stage(sdk, "mower_sdk/legacy/client.py", "OLD = 2\n")
    stage(sdk, "mower_sdk/legacy/new.py", "NEW = 1\n")
    assert check_protected_paths.check() == []


def test_deleting_legacy_code_or_the_inventory_is_refused(sdk: Path) -> None:
    run("git", "rm", "-q", "mower_sdk/legacy/client.py", "tests/upstream_exports.json", cwd=sdk)
    assert [f.split(":")[0] for f in check_protected_paths.check()] == [
        "mower_sdk/legacy/client.py", "tests/upstream_exports.json",
    ]


def test_only_the_inventory_itself_is_protected(sdk: Path) -> None:
    stage(sdk, "tests/upstream_exports.json.bak", "{}\n")
    run("git", "commit", "-q", "-m", "chore: backup", cwd=sdk)
    run("git", "rm", "-q", "tests/upstream_exports.json.bak", cwd=sdk)
    assert check_protected_paths.check() == []


def test_moving_code_out_of_legacy_is_refused(sdk: Path) -> None:
    (sdk / "src").mkdir(exist_ok=True)
    run("git", "mv", "mower_sdk/legacy/client.py", "src/client.py", cwd=sdk)
    assert [f.split(":")[0] for f in check_protected_paths.check()] == ["mower_sdk/legacy/client.py"]


def test_range_mode_checks_the_commits_between_refs(sdk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = run("git", "rev-parse", "HEAD", cwd=sdk).strip()
    run("git", "rm", "-q", "mower_sdk/legacy/client.py", cwd=sdk)
    run("git", "commit", "-q", "-m", "chore: x", cwd=sdk)
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", base)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", "HEAD")
    assert len(check_protected_paths.check()) == 1


def _hook(hook_id: str) -> dict:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    (hook,) = [h for r in config["repos"] for h in r["hooks"] if h["id"] == hook_id]
    return hook


@pytest.mark.parametrize(
    ("hook_id", "path", "guarded"),
    [
        ("no-legacy-edits", "mower_sdk/legacy/client.py", True),
        ("no-legacy-edits", "mower_sdk/mqtt.py", False),
        ("no-inventory-edits", "tests/upstream_exports.json", True),
        ("no-inventory-edits", "tests/test_upstream_exports.py", False),
    ],
)
def test_the_edit_guards_cover_exactly_the_protected_paths(hook_id: str, path: str, guarded: bool) -> None:
    hook = _hook(hook_id)
    assert hook["language"] == "fail"
    assert bool(re.search(hook["files"], path)) is guarded


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        ("import pytest_" + "asyncio", True),
        ("@pytest.mark." + "asyncio", True),
        ("from unittest import " + "mock", True),
        ("import unittest." + "mock", True),
        ("asyncio.run(main())", False),
        ("import unittest", False),
    ],
)
def test_the_test_style_hook(line: str, refused: bool) -> None:
    hook = _hook("test-style")
    assert hook["language"] == "pygrep"
    assert re.search(hook["files"], "tests/test_x.py")
    assert bool(re.search(hook["entry"], line)) is refused

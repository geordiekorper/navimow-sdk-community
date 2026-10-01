"""Protected paths: the no-protected-changes check and its hook."""

from __future__ import annotations

import re
from pathlib import Path

import check_protected_paths
import gatelib
import pytest
from conftest import hook, run, stage


@pytest.fixture
def sdk(repo: Path) -> Path:
    stage(repo, "mower_sdk/legacy/client.py", "OLD = 1\n")
    stage(repo, "tests/upstream_exports.json", "{}\n")
    run("git", "commit", "-q", "-m", "chore: layout", cwd=repo)
    return repo


def test_an_ordinary_change_passes(sdk: Path) -> None:
    stage(sdk, "src/module.py", "VALUE = 2\n")
    assert check_protected_paths.check() == []


def test_editing_or_adding_legacy_code_or_editing_the_inventory_is_refused(sdk: Path) -> None:
    stage(sdk, "mower_sdk/legacy/client.py", "OLD = 2\n")
    stage(sdk, "mower_sdk/legacy/new.py", "NEW = 1\n")
    stage(sdk, "tests/upstream_exports.json", '{"a": 1}\n')
    findings = check_protected_paths.check()
    assert sorted(f.split(":")[0] for f in findings) == [
        "mower_sdk/legacy/client.py", "mower_sdk/legacy/new.py", "tests/upstream_exports.json",
    ]
    assert all("SKIP=no-protected-changes" in f for f in findings)


def test_deleting_legacy_code_or_the_inventory_is_refused(sdk: Path) -> None:
    run("git", "rm", "-q", "mower_sdk/legacy/client.py", "tests/upstream_exports.json", cwd=sdk)
    assert [f.split(":")[0] for f in check_protected_paths.check()] == [
        "mower_sdk/legacy/client.py", "tests/upstream_exports.json",
    ]


def test_the_legacy_readme_is_added_edited_and_deleted_freely(sdk: Path) -> None:
    stage(sdk, "mower_sdk/legacy/README.md", "# Legacy\n")
    assert check_protected_paths.check() == []
    run("git", "commit", "-q", "-m", "docs: readme", cwd=sdk)
    stage(sdk, "mower_sdk/legacy/README.md", "# Legacy\n\nMore.\n")
    assert check_protected_paths.check() == []
    run("git", "rm", "-q", "-f", "mower_sdk/legacy/README.md", cwd=sdk)
    assert check_protected_paths.check() == []


def test_the_readme_does_not_carry_a_change_to_legacy_code(sdk: Path) -> None:
    stage(sdk, "mower_sdk/legacy/README.md", "# Legacy\n")
    stage(sdk, "mower_sdk/legacy/client.py", "OLD = 2\n")
    assert [f.split(":")[0] for f in check_protected_paths.check()] == ["mower_sdk/legacy/client.py"]


def test_moving_legacy_code_onto_the_readme_is_refused(sdk: Path) -> None:
    run("git", "mv", "mower_sdk/legacy/client.py", "mower_sdk/legacy/README.md", cwd=sdk)
    assert [f.split(":")[0] for f in check_protected_paths.check()] == ["mower_sdk/legacy/client.py"]


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


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("mower_sdk/legacy/client.py", True),
        ("mower_sdk/legacy/__init__.py", True),
        # The folder's own README, and nothing that only resembles it.
        ("mower_sdk/legacy/README.md", False),
        ("mower_sdk/legacy/readme.md", True),
        ("mower_sdk/legacy/README.py", True),
        ("mower_sdk/legacy/README.md.py", True),
        ("mower_sdk/legacy/NOTES.md", True),
        ("mower_sdk/legacy/sub/README.md", True),
        ("mower_sdk/mqtt.py", False),
        ("tests/upstream_exports.json", True),
        ("tests/test_upstream_exports.py", False),
    ],
)
def test_the_protected_paths(path: str, protected: bool) -> None:
    assert gatelib.is_protected(path) is protected


def test_the_hook_reads_the_change_on_every_commit() -> None:
    pytest.importorskip("yaml")
    definition = hook("no-protected-changes")
    assert definition["entry"] == "python tools/check_protected_paths.py"
    assert definition["pass_filenames"] is False  # deletions are never passed as files
    assert definition["always_run"] is True


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
    pytest.importorskip("yaml")
    definition = hook("test-style")
    assert definition["language"] == "pygrep"
    assert re.search(definition["files"], "tests/test_x.py")
    assert bool(re.search(definition["entry"], line)) is refused

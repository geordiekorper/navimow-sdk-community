"""The throwaway repositories ignore the developer's own git configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_a_hostile_global_config_does_not_reach_the_tests(tmp_path: Path) -> None:
    # Signing that cannot succeed, and a global hook that refuses every commit:
    # either would fail the fixture's own commits if the configuration leaked.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    refuse = hooks / "pre-commit"
    refuse.write_text("#!/bin/sh\necho refused by a global hook >&2\nexit 1\n", encoding="utf-8")
    refuse.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    hostile = home / ".gitconfig"  # where a developer's own configuration lives
    hostile.write_text(
        f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n[core]\n\thooksPath = {hooks}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME")}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(HERE / "test_check_leaks.py::test_only_added_lines_are_checked_in_staged_mode"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

"""noxfile.py agrees with CI, pyproject.toml and the pre-commit configuration.

CI calls the nox sessions by name, so a session renamed in one place and not
the other would make a CI job fail or, worse, run nothing; and the pins that
define the oldest dependency bound, like the tool versions, are written in
two places that must move together.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import CONFIG, ROOT, WORKFLOW

nox = pytest.importorskip("nox")
yaml = pytest.importorskip("yaml")



@functools.cache
def _noxfile():
    """The noxfile's module, for its constants; loaded once (nox registers sessions on load)."""
    spec = importlib.util.spec_from_file_location("noxfile_under_test", ROOT / "noxfile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def _session_names(noxfile: Path = ROOT / "noxfile.py") -> frozenset[str]:
    """The sessions nox itself discovers in a noxfile (nox --list --json)."""
    proc = subprocess.run([sys.executable, "-m", "nox", "--list", "--json", "-f", str(noxfile)],
                          capture_output=True, text=True, check=True, cwd=ROOT)
    return frozenset(session["session"] for session in json.loads(proc.stdout))


def _ci_session_calls() -> list[str]:
    """The sessions ci.yml runs, with each job's matrix values filled in."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    calls = []
    for job in jobs.values():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        rows = matrix.get("include") or [{"python-version": v} for v in matrix.get("python-version", [])] or [{}]
        for step in job["steps"]:
            for quoted, bare in re.findall(r'nox -s (?:"([^"]+)"|(\S+))', step.get("run", "")):
                session = quoted or bare
                for row in rows:
                    calls.append(re.sub(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", lambda m, r=row: str(r[m.group(1)]), session))
    return calls


def test_every_session_ci_calls_exists() -> None:
    calls = _ci_session_calls()
    assert calls, "ci.yml calls no nox session"
    assert set(calls) <= _session_names(), set(calls) - _session_names()


def test_ci_covers_every_session() -> None:
    assert _session_names() <= set(_ci_session_calls())


def test_the_session_names_come_from_nox_itself(tmp_path: Path) -> None:
    # A renamed session function changes what nox lists, and so what the two
    # tests above compare against.
    renamed = tmp_path / "noxfile.py"
    renamed.write_text((ROOT / "noxfile.py").read_text(encoding="utf-8").replace("def lint(", "def style("),
                       encoding="utf-8")
    names = _session_names(renamed)
    assert "lint" not in names and "style" in names
    assert not set(_ci_session_calls()) <= names


def test_the_oldest_bound_is_pyprojects_floor() -> None:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    floors = {}
    for requirement in dependencies:
        name, floor = re.match(r"^([\w-]+)\s*>=\s*([\w.]+)", requirement).groups()
        floors[name] = floor
    _, pins = _noxfile().BOUNDS["oldest"]
    assert dict(pin.split("==") for pin in pins) == floors


def test_tool_versions_match_the_pre_commit_hooks() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    revs = {repo["repo"].rsplit("/", 1)[-1]: repo.get("rev", "") for repo in config["repos"]}
    noxfile = _noxfile()
    assert "ruff==" + revs["ruff-pre-commit"].lstrip("v") == noxfile.RUFF
    assert "gitlint==" + revs["gitlint"].lstrip("v") == noxfile.GITLINT
    # The hygiene job installs gitlint itself; the same version.
    install = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["hygiene"]["steps"]
    pins = [word for step in install for word in step.get("run", "").split() if word.startswith("gitlint==")]
    assert pins == [noxfile.GITLINT]


def test_the_nox_jobs_cache_pip_downloads_per_noxfile() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    nox_jobs = [job for job in jobs.values()
                if any("nox -s" in step.get("run", "") for step in job["steps"])]
    assert nox_jobs
    for job in nox_jobs:
        (setup,) = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/setup-python")]
        assert setup["with"]["cache"] == "pip"
        assert setup["with"]["cache-dependency-path"] == "noxfile.py"

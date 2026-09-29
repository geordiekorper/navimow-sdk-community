"""The Publish workflow: a tag goes to TestPyPI, then to PyPI on approval."""

from __future__ import annotations

import pytest
from conftest import ROOT

yaml = pytest.importorskip("yaml")

WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
DOWNLOAD = {"uses": "actions/download-artifact@v8", "with": {"name": "dist", "path": "dist/"}}


def test_only_a_version_tag_starts_a_release() -> None:
    # PyYAML reads the bare key "on" as the boolean true.
    assert WORKFLOW[True] == {"push": {"tags": ["v*"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_testpypi_uploads_the_build_unattended() -> None:
    job = JOBS["testpypi"]
    assert job["needs"] == "build"
    assert job["environment"]["name"] == "testpypi"
    assert job["permissions"] == {"id-token": "write"}
    assert job["steps"][0] == DOWNLOAD
    assert job["steps"][1]["with"] == {"repository-url": "https://test.pypi.org/legacy/"}


def test_pypi_waits_for_testpypi_and_the_approval_of_its_environment() -> None:
    job = JOBS["pypi"]
    # A failed TestPyPI upload stops the run before PyPI.
    assert job["needs"] == ["build", "testpypi"]
    # The pypi environment's required-reviewer rule is what holds the job.
    assert job["environment"] == {"name": "pypi", "url": "https://pypi.org/project/navimow-sdk-community/"}
    assert job["permissions"] == {"id-token": "write"}
    # The same artifact TestPyPI received, and the default index: PyPI.
    assert job["steps"] == [DOWNLOAD, {"uses": "pypa/gh-action-pypi-publish@release/v1"}]


def test_the_build_job_checks_the_tag_and_the_long_description() -> None:
    runs = "\n".join(step.get("run", "") for step in JOBS["build"]["steps"])
    assert "does not match mower_sdk.__version__" in runs
    assert "twine check --strict dist/*" in runs
    assert set(JOBS) == {"build", "testpypi", "pypi"}

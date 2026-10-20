"""The Publish workflow: a tag is tested, goes to TestPyPI, then to PyPI on approval."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

yaml = pytest.importorskip("yaml")

WORKFLOW = yaml.safe_load(
    (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
)
JOBS = WORKFLOW["jobs"]
DOWNLOAD = {"uses": "actions/download-artifact@v8", "with": {"name": "dist", "path": "dist/"}}
A_CONSUMER = "steps.consumer.outputs.named == 'true'"


def run_step(
    step: dict, tmp_path: Path, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a step's script as GitHub would: bash -e, the workspace at tmp_path.

    The environment is the caller's, with GITHUB_WORKSPACE, GITHUB_OUTPUT and
    GITHUB_STEP_SUMMARY set and env on top. The script's exit status is the
    returned process's returncode; the files GITHUB_OUTPUT and
    GITHUB_STEP_SUMMARY are tmp_path / "github_output" and
    tmp_path / "github_step_summary".

    Args:
        step: The step; its "run" script is what runs.
        tmp_path: The workspace, and the working directory unless cwd is given.
        env: Variables set for the script.
        cwd: The step's working directory, when it has one.
    """
    output, summary = tmp_path / "github_output", tmp_path / "github_step_summary"
    output.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")
    github = {
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=cwd or tmp_path,
        env={**os.environ, **github, **env},
        capture_output=True,
        text=True,
    )


def written(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text(encoding="utf-8")


def test_only_a_version_tag_starts_a_release() -> None:
    # PyYAML reads the bare key "on" as the boolean true.
    assert WORKFLOW[True] == {"push": {"tags": ["v*"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_compat_runs_the_named_consumers_tests_against_the_wheel() -> None:
    job = JOBS["compat"]
    assert job["needs"] == "build"
    # The consumer is named by repository variables, not in the workflow.
    assert job["env"] == {
        "CONSUMER_REPOSITORY": "${{ vars.CONSUMER_REPOSITORY }}",
        "CONSUMER_REF": "${{ vars.CONSUMER_REF }}",
    }
    gate, download, checkout, run = job["steps"]
    assert gate["id"] == "consumer"
    assert download == {**DOWNLOAD, "if": A_CONSUMER}
    assert checkout["if"] == A_CONSUMER
    # The consumer's code runs here: no credential is left in its checkout.
    assert checkout["with"] == {
        "repository": "${{ env.CONSUMER_REPOSITORY }}",
        "ref": "${{ env.CONSUMER_REF }}",
        "path": "consumer",
        "persist-credentials": False,
    }
    assert run["if"] == A_CONSUMER
    assert run["working-directory"] == "consumer"
    assert 'scripts/test-against-sdk.sh --wheel "$1"' in run["run"]


def test_compat_names_the_consumer_or_says_that_none_is_named(tmp_path: Path) -> None:
    gate = JOBS["compat"]["steps"][0]
    both = {"CONSUMER_REPOSITORY": "owner/consumer", "CONSUMER_REF": "main"}
    proc = run_step(gate, tmp_path, both)
    assert proc.returncode == 0
    assert written(tmp_path, "github_output") == "named=true\n"
    assert written(tmp_path, "github_step_summary") == ""
    assert "consumer: owner/consumer at main" in proc.stdout
    # GitHub gives an unset variable as the empty string.
    for missing in ("CONSUMER_REPOSITORY", "CONSUMER_REF"):
        proc = run_step(gate, tmp_path, {**both, missing: ""})
        assert proc.returncode == 0
        assert written(tmp_path, "github_output") == "named=false\n"
        assert written(tmp_path, "github_step_summary").startswith("No consumer named")
        assert "::notice::No consumer named" in proc.stdout


# A stand-in for a consumer's test entry point: records its arguments and
# exits with the status the test chose.
FAKE_ENTRY_POINT = '#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORDED"\nexit "$STATUS"\n'


def consumer_checkout(tmp_path: Path) -> Path:
    consumer = tmp_path / "consumer"
    script = consumer / "scripts" / "test-against-sdk.sh"
    script.parent.mkdir(parents=True)
    script.write_text(FAKE_ENTRY_POINT, encoding="utf-8")
    script.chmod(0o755)
    return consumer


@pytest.mark.parametrize("status", [0, 3])
def test_compat_runs_the_entry_point_in_the_consumers_directory_with_the_wheels_path(
    tmp_path: Path, status: int
) -> None:
    run = JOBS["compat"]["steps"][3]
    consumer = consumer_checkout(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "navimow_sdk_community-0.2.0a9-py3-none-any.whl").write_bytes(b"")
    (tmp_path / "dist" / "navimow_sdk_community-0.2.0a9.tar.gz").write_bytes(b"")
    recorded = tmp_path / "recorded"
    env = {"RECORDED": str(recorded), "STATUS": str(status)}
    proc = run_step(run, tmp_path, env, cwd=consumer)
    # The script's exit status is the verdict.
    assert proc.returncode == status
    wheel = tmp_path / "dist" / "navimow_sdk_community-0.2.0a9-py3-none-any.whl"
    assert recorded.read_text(encoding="utf-8") == f"--wheel\n{wheel}\n"


@pytest.mark.parametrize("wheels", [0, 2])
def test_compat_refuses_to_run_without_exactly_one_wheel(tmp_path: Path, wheels: int) -> None:
    run = JOBS["compat"]["steps"][3]
    consumer = consumer_checkout(tmp_path)
    (tmp_path / "dist").mkdir()
    for n in range(wheels):
        (tmp_path / "dist" / f"navimow_sdk_community-0.2.0a{n}-py3-none-any.whl").write_bytes(b"")
    recorded = tmp_path / "recorded"
    proc = run_step(run, tmp_path, {"RECORDED": str(recorded), "STATUS": "0"}, cwd=consumer)
    assert proc.returncode == 1
    assert "expected one wheel in dist/" in proc.stdout
    assert not recorded.exists()


def test_testpypi_uploads_the_build_unattended() -> None:
    job = JOBS["testpypi"]
    # A failed consumer test stops the run before TestPyPI.
    assert job["needs"] == ["build", "compat"]
    assert job["environment"]["name"] == "testpypi"
    assert job["permissions"] == {"id-token": "write"}
    assert job["steps"][0] == DOWNLOAD
    assert job["steps"][1]["with"] == {"repository-url": "https://test.pypi.org/legacy/"}


def test_pypi_waits_for_testpypi_and_the_approval_of_its_environment() -> None:
    job = JOBS["pypi"]
    # A failed TestPyPI upload stops the run before PyPI.
    assert job["needs"] == ["build", "testpypi"]
    # The pypi environment's required-reviewer rule is what holds the job.
    assert job["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/project/navimow-sdk-community/",
    }
    assert job["permissions"] == {"id-token": "write"}
    # The same artifact TestPyPI received, and the default index: PyPI.
    assert job["steps"] == [DOWNLOAD, {"uses": "pypa/gh-action-pypi-publish@release/v1"}]


def test_the_build_job_checks_the_tag_and_the_long_description() -> None:
    runs = "\n".join(step.get("run", "") for step in JOBS["build"]["steps"])
    assert "does not match mower_sdk.__version__" in runs
    assert "twine check --strict dist/*" in runs
    assert set(JOBS) == {"build", "compat", "testpypi", "pypi"}

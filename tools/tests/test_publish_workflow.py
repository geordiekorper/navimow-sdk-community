"""The Publish workflow: a tag goes to TestPyPI, is tested from there, then to PyPI on approval.

The consumers workflow it calls, and the check that runs that one by hand,
are pinned here too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, run

yaml = pytest.importorskip("yaml")

WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


WORKFLOW = workflow("publish.yml")
JOBS = WORKFLOW["jobs"]
CONSUMERS = workflow("consumers.yml")
CHECK = workflow("consumers-check.yml")
CONSUMERS_WORKFLOW = "./.github/workflows/consumers.yml"
DOWNLOAD = {"uses": "actions/download-artifact@v8", "with": {"name": "dist", "path": "dist/"}}
VERSION = "0.2.0a9"


def run_step(
    step: dict,
    tmp_path: Path,
    env: dict[str, str],
    cwd: Path | None = None,
    workspace: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a step's script as GitHub would: bash -e, in a workspace under tmp_path.

    The environment is the caller's, with GITHUB_WORKSPACE, RUNNER_TEMP
    (tmp_path / "runner_temp", made here), GITHUB_OUTPUT and
    GITHUB_STEP_SUMMARY set and env on top. The script's exit status is the
    returned process's returncode; the files GITHUB_OUTPUT and
    GITHUB_STEP_SUMMARY are tmp_path / "github_output" and
    tmp_path / "github_step_summary".

    Args:
        step: The step; its "run" script is what runs.
        tmp_path: Where the files go, and the workspace unless one is given.
        env: Variables set for the script.
        cwd: The step's working directory, when it is not the workspace.
        workspace: The checkout the step runs in, when it is not tmp_path.
    """
    workspace = workspace or tmp_path
    output, summary = tmp_path / "github_output", tmp_path / "github_step_summary"
    output.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")
    (tmp_path / "runner_temp").mkdir(exist_ok=True)
    github = {
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(tmp_path / "runner_temp"),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=cwd or workspace,
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


BUILD_STEPS = {step.get("name"): step for step in JOBS["build"]["steps"]}
TAG_CHECK = BUILD_STEPS["Check that the tag matches mower_sdk.__version__"]


def test_the_build_job_checks_the_tag_and_the_long_description() -> None:
    runs = "\n".join(step.get("run", "") for step in JOBS["build"]["steps"])
    assert "does not match mower_sdk.__version__" in runs
    assert "twine check --strict dist/*" in runs
    assert set(JOBS) == {"build", "testpypi", "testpypi-verify", "consumers", "pypi"}


def test_the_tag_check_hands_the_version_on_to_the_consumers(tmp_path: Path) -> None:
    # A with: value is an expression, not a shell, so the version the
    # consumers job passes on is a job output of build.
    assert JOBS["build"]["outputs"] == {"version": "${{ steps.version.outputs.version }}"}
    assert TAG_CHECK["id"] == "version"
    (tmp_path / "mower_sdk").mkdir()
    (tmp_path / "mower_sdk" / "__init__.py").write_text(
        f'__version__ = "{VERSION}"\n', encoding="utf-8"
    )
    proc = run_step(TAG_CHECK, tmp_path, {"GITHUB_REF_NAME": f"v{VERSION}"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"releasing {VERSION}\n"
    assert written(tmp_path, "github_output") == f"version={VERSION}\n"
    proc = run_step(TAG_CHECK, tmp_path, {"GITHUB_REF_NAME": "v0.2.0a8"})
    assert proc.returncode == 1
    assert f"tag v0.2.0a8 does not match mower_sdk.__version__ {VERSION}" in proc.stderr
    assert written(tmp_path, "github_output") == ""


def test_testpypi_uploads_the_build_unattended() -> None:
    job = JOBS["testpypi"]
    # The build is all TestPyPI waits for: the consumers test the release from there.
    assert job["needs"] == ["build"]
    assert job["environment"]["name"] == "testpypi"
    assert job["permissions"] == {"id-token": "write"}
    assert job["steps"][0] == DOWNLOAD
    assert job["steps"][1]["with"] == {"repository-url": "https://test.pypi.org/legacy/"}


WHEEL = f"navimow_sdk_community-{VERSION}-py3-none-any.whl"
VERIFY_STEPS = {step.get("name"): step for step in JOBS["testpypi-verify"]["steps"]}
INSTALL = VERIFY_STEPS["Install the wheel TestPyPI serves into a fresh environment"]
TEST = VERIFY_STEPS["Run the tag's tests against the installed package from another directory"]

# A stand-in for python. "-m venv DIR" makes an environment whose python is
# this script again. Every other call is recorded with its directory. A
# "pip download" fails FAILS times, then writes the wheel to --dest: a copy
# of the built one, or other bytes when OTHER is 1.
FAKE_PYTHON = (
    "#!/bin/sh\n"
    'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
    '  mkdir -p "$3/bin" && cp "$0" "$3/bin/python"; exit 0\n'
    "fi\n"
    'printf "%s|%s\\n" "$PWD" "$*" >> "$CALLS"\n'
    'case "$*" in *"pip download"*)\n'
    '  count=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 )); echo "$count" > "$COUNT"\n'
    '  [ "$count" -le "$FAILS" ] && exit 1\n'
    '  while [ $# -gt 0 ]; do [ "$1" = --dest ] && dest="$2"; shift; done\n'
    '  mkdir -p "$dest"\n'
    '  if [ "$OTHER" = 1 ]; then echo other > "$dest/' + WHEEL + '"\n'
    '  else cp "$GITHUB_WORKSPACE"/dist/*.whl "$dest"/; fi ;;\n'
    "esac\n"
    "exit 0\n"
)
FAKE_SLEEP = '#!/bin/sh\necho "$1" >> "$SLEPT"\n'


def verify_fakes(tmp_path: Path, fails: int) -> dict[str, str]:
    """The fakes on the PATH, the tag and the record files for the testpypi-verify steps."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("python", FAKE_PYTHON), ("sleep", FAKE_SLEEP)):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REF_NAME": f"v{VERSION}",
        "CALLS": str(tmp_path / "calls"),
        "COUNT": str(tmp_path / "count"),
        "SLEPT": str(tmp_path / "slept"),
        "FAILS": str(fails),
    }


def built(workspace: Path) -> None:
    (workspace / "dist").mkdir()
    (workspace / "dist" / WHEEL).write_bytes(b"the wheel")
    (workspace / "dist" / f"navimow_sdk_community-{VERSION}.tar.gz").write_bytes(b"the sdist")


def calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_testpypi_verify_checks_the_index_installs_from_it_and_runs_the_tags_tests() -> None:
    job = JOBS["testpypi-verify"]
    assert job["needs"] == ["build", "testpypi"]
    # The oldest and the newest supported Python, as the bounds job of CI.
    assert job["strategy"] == {"fail-fast": False, "matrix": {"python-version": ["3.11", "3.14"]}}
    checkout, python, download, check, install, test = job["steps"]
    assert checkout == {"uses": "actions/checkout@v7"}
    assert python["with"] == {"python-version": "${{ matrix.python-version }}"}
    assert download == DOWNLOAD
    assert check["run"] == (
        'python tools/check_index.py --index https://test.pypi.org "${GITHUB_REF_NAME#v}" dist/'
    )
    assert install is INSTALL
    assert test is TEST


def test_the_install_step_asks_again_while_the_index_lags_then_installs_what_it_served(
    tmp_path: Path,
) -> None:
    built(tmp_path)
    proc = run_step(INSTALL, tmp_path, verify_fakes(tmp_path, fails=2))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    served = tmp_path / "runner_temp" / "served" / WHEEL
    recorded = calls(tmp_path)
    downloads = [c for c in recorded if "pip download" in c]
    assert len(downloads) == 3
    for call in downloads:
        assert "--no-deps" in call
        assert "--index-url https://test.pypi.org/simple/" in call
        assert call.endswith(f"--dest {served.parent} navimow-sdk-community=={VERSION}")
    assert (tmp_path / "slept").read_text(encoding="utf-8").splitlines() == ["20", "20"]
    assert recorded[-1] == f"{tmp_path}|-m pip install {served} pytest pytest-asyncio"


def test_the_install_step_gives_up_after_six_asks(tmp_path: Path) -> None:
    built(tmp_path)
    proc = run_step(INSTALL, tmp_path, verify_fakes(tmp_path, fails=6))
    assert proc.returncode == 1
    assert f"did not serve navimow-sdk-community {VERSION} in 6 attempts" in proc.stdout
    assert len([c for c in calls(tmp_path) if "pip download" in c]) == 6
    assert not any("pip install" in c for c in calls(tmp_path))
    assert (tmp_path / "slept").read_text(encoding="utf-8").splitlines() == ["20"] * 5


def test_the_install_step_refuses_a_served_wheel_that_differs_from_the_built_one(
    tmp_path: Path,
) -> None:
    built(tmp_path)
    proc = run_step(INSTALL, tmp_path, {**verify_fakes(tmp_path, fails=0), "OTHER": "1"})
    assert proc.returncode != 0
    assert "differ" in proc.stdout + proc.stderr
    assert not any("pip install" in c for c in calls(tmp_path))


def test_the_test_step_runs_the_tracked_tests_from_another_directory(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")
    (repo / "tests" / "scratch.py").write_text("# not tracked\n", encoding="utf-8")
    run("git", "add", "tests/test_x.py", cwd=repo)
    run("git", "commit", "-q", "-m", "test: one", cwd=repo)
    env = verify_fakes(tmp_path, fails=0)
    venv = tmp_path / "runner_temp" / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(FAKE_PYTHON, encoding="utf-8")
    (venv / "python").chmod(0o755)
    proc = run_step(TEST, tmp_path, env, workspace=repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    checkout = tmp_path / "runner_temp" / "checkout"
    assert (checkout / "tests" / "test_x.py").is_file()
    assert not (checkout / "tests" / "scratch.py").exists()
    assert calls(tmp_path) == [
        f'{checkout}|-c import mower_sdk; print("importing", mower_sdk.__file__)',
        f"{checkout}|-m pytest tests",
        f"{checkout}|{repo}/tools/check_wheel.py metadata",
    ]


def test_the_consumers_job_calls_the_reusable_workflow_with_the_tags_version() -> None:
    # Once testpypi-verify has seen TestPyPI serve the version. The consumers
    # come from the repository variable, never from the workflow.
    assert JOBS["consumers"] == {
        "needs": ["build", "testpypi", "testpypi-verify"],
        "uses": CONSUMERS_WORKFLOW,
        "with": {
            "version": "${{ needs.build.outputs.version }}",
            "consumers": "${{ vars.CONSUMERS }}",
        },
    }


def test_the_consumers_workflow_is_called_with_a_version_and_the_entries() -> None:
    assert set(CONSUMERS[True]) == {"workflow_call"}
    inputs = CONSUMERS[True]["workflow_call"]["inputs"]
    assert {name: spec["type"] for name, spec in inputs.items()} == {
        "version": "string",
        "consumers": "string",
    }
    assert CONSUMERS["permissions"] == {"contents": "read"}
    assert set(CONSUMERS["jobs"]) == {"consumers", "compat"}


PARSE = CONSUMERS["jobs"]["consumers"]["steps"][0]
COMPAT = CONSUMERS["jobs"]["compat"]
RUN = COMPAT["steps"][2]


def test_compat_runs_each_consumers_tests_against_the_release_testpypi_serves() -> None:
    assert CONSUMERS["jobs"]["consumers"]["outputs"] == {
        "matrix": "${{ steps.parse.outputs.matrix }}"
    }
    assert PARSE["id"] == "parse"
    assert PARSE["env"] == {"CONSUMERS": "${{ inputs.consumers }}"}
    assert COMPAT["needs"] == ["consumers"]
    # One leg per entry; a failing consumer does not stop the others.
    assert COMPAT["strategy"] == {
        "fail-fast": False,
        "matrix": "${{ fromJSON(needs.consumers.outputs.matrix) }}",
    }
    assert COMPAT["timeout-minutes"] == 30
    checkout, uv, run_ = COMPAT["steps"]
    # The consumer's code runs here: no credential is left in its checkout.
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"] == {
        "repository": "${{ matrix.repository }}",
        "ref": "${{ matrix.ref }}",
        "path": "consumer",
        "persist-credentials": False,
    }
    # The consumer's script makes its environment with uv, so uv is there first.
    # Its cache is off: the action's post step fails a run that cached nothing.
    assert uv["uses"].startswith("astral-sh/setup-uv@v")
    assert uv["with"] == {"enable-cache": False}
    assert run_ is RUN
    assert RUN["working-directory"] == "consumer"
    assert RUN["env"] == {"VERSION": "${{ inputs.version }}"}


def parse(tmp_path: Path, consumers: str) -> subprocess.CompletedProcess[str]:
    return run_step(PARSE, tmp_path, {"CONSUMERS": consumers})


def matrix(*entries: tuple[str, str]) -> str:
    """The parse step's output line for these (repository, ref) entries."""
    include = [{"repository": repository, "ref": ref} for repository, ref in entries]
    return "matrix=" + json.dumps({"include": include}) + "\n"


# GitHub gives an unset variable as the empty string.
@pytest.mark.parametrize("consumers", ["", " \n\t "])
def test_no_consumer_configured_fails_the_run(tmp_path: Path, consumers: str) -> None:
    proc = parse(tmp_path, consumers)
    assert proc.returncode == 1
    assert proc.stdout == (
        "::error::no consumer is configured: set the repository variable CONSUMERS\n"
    )
    assert written(tmp_path, "github_output") == ""


def test_an_entry_is_a_repository_at_main_unless_it_names_a_ref(tmp_path: Path) -> None:
    proc = parse(tmp_path, "owner/name")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert written(tmp_path, "github_output") == matrix(("owner/name", "main"))
    # Any whitespace separates entries; @ref is the branch, tag or commit to check out.
    proc = parse(tmp_path, " owner/name@release\n\tother-owner/other.name \n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert written(tmp_path, "github_output") == matrix(
        ("owner/name", "release"), ("other-owner/other.name", "main")
    )


@pytest.mark.parametrize(
    "entry", ["name", "owner/name@", "owner/name/extra", "owner/name@v1 owner"]
)
def test_an_entry_that_is_not_a_repository_at_a_ref_fails_the_run(
    tmp_path: Path, entry: str
) -> None:
    proc = parse(tmp_path, entry)
    assert proc.returncode == 1
    assert proc.stdout.startswith("::error::")
    assert "is not a consumer entry: owner/name or owner/name@ref" in proc.stdout
    assert written(tmp_path, "github_output") == ""


# A stand-in for a consumer's test entry point: records its directory, how
# many arguments it got and the arguments, and exits with the status the
# test chose.
FAKE_ENTRY_POINT = '#!/bin/sh\nprintf "%s\\n" "$PWD" "$#" "$@" > "$RECORDED"\nexit "$STATUS"\n'


def consumer_checkout(tmp_path: Path, script: str | None = FAKE_ENTRY_POINT) -> Path:
    """A consumer's checkout with its entry point, or without one when script is None."""
    consumer = tmp_path / "consumer"
    (consumer / "scripts").mkdir(parents=True)
    if script is not None:
        path = consumer / "scripts" / "test-against-sdk.sh"
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return consumer


def recorded(tmp_path: Path) -> tuple[Path, list[str]]:
    """The directory the stand-in ran in, and its argument count followed by the arguments."""
    first, *rest = (tmp_path / "recorded").read_text(encoding="utf-8").splitlines()
    return Path(first).resolve(), rest


@pytest.mark.parametrize("status", [0, 3])
def test_compat_runs_the_entry_point_in_the_consumers_directory_with_the_version(
    tmp_path: Path, status: int
) -> None:
    consumer = consumer_checkout(tmp_path)
    env = {"RECORDED": str(tmp_path / "recorded"), "STATUS": str(status), "VERSION": VERSION}
    proc = run_step(RUN, tmp_path, env, cwd=consumer)
    # The script's exit status is the verdict.
    assert proc.returncode == status
    # The version is the only argument.
    assert recorded(tmp_path) == (consumer.resolve(), ["1", VERSION])


def test_compat_passes_no_argument_when_no_version_is_given(tmp_path: Path) -> None:
    # The check run may leave the version to the script.
    consumer = consumer_checkout(tmp_path)
    env = {"RECORDED": str(tmp_path / "recorded"), "STATUS": "0", "VERSION": ""}
    proc = run_step(RUN, tmp_path, env, cwd=consumer)
    assert proc.returncode == 0
    assert recorded(tmp_path) == (consumer.resolve(), ["0"])


@pytest.mark.parametrize("executable", [None, False], ids=["absent", "not executable"])
def test_compat_fails_when_the_consumer_has_no_entry_point(
    tmp_path: Path, executable: bool | None
) -> None:
    consumer = consumer_checkout(tmp_path, None if executable is None else FAKE_ENTRY_POINT)
    if executable is False:
        (consumer / "scripts" / "test-against-sdk.sh").chmod(0o644)
    env = {"RECORDED": str(tmp_path / "recorded"), "STATUS": "0", "VERSION": VERSION}
    proc = run_step(RUN, tmp_path, env, cwd=consumer)
    assert proc.returncode == 1
    assert proc.stdout == (
        "::error::the consumer has no scripts/test-against-sdk.sh at the configured ref; "
        "the release cannot be checked\n"
    )
    assert not (tmp_path / "recorded").exists()


def test_consumers_check_runs_the_same_jobs_by_hand_without_a_release() -> None:
    assert set(CHECK[True]) == {"workflow_dispatch"}
    inputs = CHECK[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"version", "consumers"}
    # No version leaves the choice to the consumer's script.
    assert inputs["version"]["type"] == "string"
    assert inputs["version"]["required"] is False
    assert inputs["version"]["default"] == ""
    assert inputs["consumers"]["type"] == "string"
    assert inputs["consumers"]["required"] is True
    assert CHECK["permissions"] == {"contents": "read"}
    assert CHECK["jobs"] == {
        "consumers": {
            "uses": CONSUMERS_WORKFLOW,
            "with": {"version": "${{ inputs.version }}", "consumers": "${{ inputs.consumers }}"},
        }
    }


def test_pypi_waits_for_the_checks_and_the_approval_of_its_environment() -> None:
    job = JOBS["pypi"]
    # A failed upload to TestPyPI, a release that fails from there, or a
    # consumer whose tests fail against it, stops the run before PyPI.
    assert job["needs"] == ["build", "testpypi", "testpypi-verify", "consumers"]
    # The pypi environment's required-reviewer rule is what holds the job.
    assert job["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/project/navimow-sdk-community/",
    }
    # The run's token reads the commit statuses for the soak gate.
    assert job["permissions"] == {"id-token": "write", "statuses": "read"}
    gate, *upload = job["steps"]
    assert gate["env"] == {"GH_TOKEN": "${{ github.token }}"}
    # The same artifact TestPyPI received, and the default index: PyPI.
    assert upload == [DOWNLOAD, {"uses": "pypa/gh-action-pypi-publish@release/v1"}]


# A stand-in for gh: records its arguments and answers "gh api ... --jq
# EXPR" with EXPR applied by jq to the JSON in RESPONSE.
FAKE_GH = (
    "#!/bin/sh\n"
    'printf "%s\\n" "$@" > "$RECORDED"\n'
    'while [ $# -gt 0 ]; do [ "$1" = --jq ] && expr="$2"; shift; done\n'
    'jq -r "$expr" "$RESPONSE"\n'
)
GATE = JOBS["pypi"]["steps"][0]
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq applies the expression")


def statuses(*states: tuple[str, str]) -> str:
    """The combined status of a commit, as the API gives it: one entry per context."""
    return json.dumps({"statuses": [{"context": c, "state": s} for c, s in states]})


def run_gate(tmp_path: Path, response: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    (tmp_path / "response.json").write_text(response, encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF_NAME": f"v{VERSION}",
        "RECORDED": str(tmp_path / "recorded"),
        "RESPONSE": str(tmp_path / "response.json"),
    }
    return run_step(GATE, tmp_path, env)


@needs_jq
def test_a_passed_soak_lets_the_upload_go_on(tmp_path: Path) -> None:
    response = statuses(("navimow-alpha/smoke", "success"), ("navimow-alpha/soak", "success"))
    proc = run_gate(tmp_path, response)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"navimow-alpha/soak on v{VERSION} is success\n"
    # The tag's name resolves to its commit on the API's side.
    recorded = (tmp_path / "recorded").read_text(encoding="utf-8").splitlines()
    assert recorded[:2] == ["api", f"repos/owner/repo/commits/v{VERSION}/status"]


@needs_jq
@pytest.mark.parametrize(
    ("response", "state"),
    [
        (statuses(("navimow-alpha/soak", "pending")), "pending"),
        (statuses(("navimow-alpha/soak", "failure")), "failure"),
        (statuses(("navimow-alpha/smoke", "success")), "absent"),
        (statuses(), "absent"),
    ],
)
def test_anything_but_a_passed_soak_refuses_the_upload(
    tmp_path: Path, response: str, state: str
) -> None:
    proc = run_gate(tmp_path, response)
    assert proc.returncode == 1
    assert proc.stdout.startswith(
        f"::error::navimow-alpha/soak on v{VERSION} is {state}, not success"
    )

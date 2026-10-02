"""The checks CI runs, as nox sessions: CI calls them, and so can anyone.

  nox -s tests-3.11 ... tests-3.14      the suite from the source tree and the
                                        core-isolation test with DeprecationWarning
                                        as an error, on each supported Python
  nox -s "bounds(oldest)" "bounds(newest)"
                                        the lowest and the highest allowed aiohttp
                                        and paho-mqtt: the suite and the MQTT
                                        construction check
  nox -s wheel                          build; check what the wheel ships; install
                                        it into a fresh environment and test it from
                                        another directory; check its metadata
  nox -s lint                           ruff: the rules, then the formatting (both
                                        configured in pyproject.toml)
  nox -s types                          mypy in strict mode over the package
                                        (configured in pyproject.toml)
  nox -s tools-3.11 ... tools-3.14      the commit checks' own tests (tools/tests)

Run everything with `nox`; the environments are made with uv when it is
installed, with virtualenv otherwise.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import nox

nox.needs_version = ">=2025.5.1"
nox.options.default_venv_backend = "uv|virtualenv"
nox.options.error_on_missing_interpreters = True

ROOT = Path(__file__).resolve().parent
PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
# The dependency bounds pyproject.toml allows; "newest" takes the latest release.
BOUNDS = {
    "oldest": ("3.11", ["aiohttp==3.9.0", "paho-mqtt==2.1.0"]),
    "newest": ("3.14", ["aiohttp", "paho-mqtt"]),
}
# What the suite needs beside the package: async tests are pytest-asyncio tests.
TEST_DEPS = ["pytest", "pytest-asyncio"]
RUFF = "ruff==0.16.9"  # the version the ruff hook in .pre-commit-config.yaml uses
MYPY = "mypy==2.4.0"
GITLINT = "gitlint==0.19.1"  # the version the gitlint hook uses


def _remove_build_output() -> None:
    """Installing the package builds it in the tree (build/); leave the tree as it was."""
    shutil.rmtree(ROOT / "build", ignore_errors=True)


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    session.install("-e", ".", *TEST_DEPS)
    session.run("python", "-m", "pytest", *session.posargs)
    session.run(
        "python", "-W", "error::DeprecationWarning", "-m", "pytest", "tests/test_core_isolation.py"
    )


@nox.session
@nox.parametrize(
    ("python", "pins"),
    [nox.param(python, pins, id=name) for name, (python, pins) in BOUNDS.items()],
)
def bounds(session: nox.Session, pins: list[str]) -> None:
    session.install("--upgrade", ".", *pins, *TEST_DEPS)
    _remove_build_output()
    session.run(
        "python",
        "-c",
        "import aiohttp, paho.mqtt; "
        "print('aiohttp==' + aiohttp.__version__); print('paho-mqtt==' + paho.mqtt.__version__)",
    )
    session.run("python", "-m", "pytest", *session.posargs)
    session.run("python", "tools/check_mqtt_construction.py")


@nox.session(python="3.14")
def wheel(session: nox.Session) -> None:
    tmp = Path(session.create_tmp()).resolve()
    dist, venv = tmp / "dist", tmp / "venv"
    for path in (dist, venv):
        shutil.rmtree(path, ignore_errors=True)
    session.install("build")
    session.run("python", "-m", "build", "--outdir", str(dist))
    _remove_build_output()
    session.run("python", "tools/check_wheel.py", "contents", str(dist))
    # A fresh environment with only the wheel, and the tests run from another
    # directory, so a wheel that omits a package fails here.
    session.run("python", "-m", "venv", str(venv))
    python = str(venv / "bin" / "python")
    (wheel_file,) = dist.glob("*.whl")
    session.run(python, "-m", "pip", "install", "-q", str(wheel_file), *TEST_DEPS, external=True)
    # Outside the source tree, so neither the package source nor the
    # repository's pytest configuration is found from there.
    checkout = Path(tempfile.mkdtemp(prefix="navimow-wheel-tests-"))
    try:
        # Without compiled files, which record where they were compiled.
        shutil.copytree(
            ROOT / "tests", checkout / "tests", ignore=shutil.ignore_patterns("__pycache__")
        )
        session.chdir(checkout)
        session.run(
            python, "-c", "import mower_sdk; print('importing', mower_sdk.__file__)", external=True
        )
        session.run(python, "-m", "pytest", "tests", external=True)
        session.run(python, str(ROOT / "tools" / "check_wheel.py"), "metadata", external=True)
    finally:
        session.chdir(ROOT)
        shutil.rmtree(checkout, ignore_errors=True)


@nox.session(python="3.14")
def lint(session: nox.Session) -> None:
    session.install(RUFF)
    session.run("ruff", "check", *(session.posargs or ["."]))
    # The formatter's check takes no arguments: it reads the whole tree.
    session.run("ruff", "format", "--check", ".")


@nox.session(python="3.14")
def types(session: nox.Session) -> None:
    # The package is installed for its dependencies, which carry their own
    # annotations; mypy reads the package itself from the source tree.
    session.install(".", MYPY)
    session.run("mypy")


@nox.session(python=PYTHONS)
def tools(session: nox.Session) -> None:
    # The plugin is installed here too: pyproject.toml sets its options, and
    # pytest warns about options no installed plugin knows.
    session.install(*TEST_DEPS, "pyyaml", "nox", GITLINT, RUFF, MYPY)
    session.run("python", "-m", "pytest", "tools/tests", "-q", *session.posargs)

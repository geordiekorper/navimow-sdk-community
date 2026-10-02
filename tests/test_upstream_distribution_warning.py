"""The import-time warning when the upstream navimow-sdk distribution is listed beside this one."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

import mower_sdk
from mower_sdk import _warn_if_upstream_installed


def lookup(versions: dict[str, str]) -> tuple[Callable[[str], str], list[str]]:
    """A recording stand-in for importlib.metadata.version answering from ``versions``."""
    asked: list[str] = []

    def version_of(name: str) -> str:
        asked.append(name)
        try:
            return versions[name]
        except KeyError:
            raise importlib.metadata.PackageNotFoundError(name) from None

    return version_of, asked


def test_both_distributions_listed_give_one_warning_naming_both_versions() -> None:
    version_of, asked = lookup({"navimow-sdk": "0.1.2", "navimow-sdk-community": "0.2.0a9"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_if_upstream_installed(version_of)
    assert asked == ["navimow-sdk", "navimow-sdk-community"]
    (warning,) = caught
    assert warning.category is UserWarning
    text = str(warning.message)
    assert text.startswith("navimow-sdk 0.1.2 is installed beside navimow-sdk-community 0.2.0a9. ")
    assert f"(this import is version {mower_sdk.__version__})" in text
    assert text.endswith("Uninstall both, then install navimow-sdk-community.")


@pytest.mark.parametrize(
    "versions",
    [
        {},
        {"navimow-sdk-community": "0.2.0a9"},
        {"navimow-sdk": "0.1.2"},
    ],
    ids=["neither", "community only", "upstream only"],
)
def test_a_missing_distribution_gives_no_warning(versions: dict[str, str]) -> None:
    version_of, _ = lookup(versions)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _warn_if_upstream_installed(version_of)


def test_the_test_environment_lists_only_this_distribution() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _warn_if_upstream_installed()


def test_importing_the_package_warns_from_its_init(tmp_path: Path) -> None:
    # Metadata for both distributions, first on the path, so the lookup finds
    # these whatever else is installed.
    for dist_info, name, version in [
        ("navimow_sdk-0.1.2.dist-info", "navimow-sdk", "0.1.2"),
        ("navimow_sdk_community-0.2.0a9.dist-info", "navimow-sdk-community", "0.2.0a9"),
    ]:
        (tmp_path / dist_info).mkdir()
        (tmp_path / dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
    script = (
        "import json, warnings\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always')\n"
        "    import mower_sdk\n"
        "print(json.dumps([[w.category.__name__, str(w.message), w.filename] for w in caught]))\n"
    )
    package_parent = Path(mower_sdk.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(package_parent)])}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    ((category, message, filename),) = json.loads(proc.stdout.splitlines()[-1])
    assert category == "UserWarning"
    assert message.startswith(
        "navimow-sdk 0.1.2 is installed beside navimow-sdk-community 0.2.0a9. "
    )
    assert Path(filename).resolve() == Path(mower_sdk.__file__).resolve()

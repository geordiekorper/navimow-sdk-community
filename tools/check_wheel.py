"""Check the built wheel: what it ships, and the installed distribution's metadata.

Run by the wheel session of noxfile.py:
  check_wheel.py contents DIST_DIR   the wheel ships mower_sdk with its py.typed marker
                                     and mower_sdk.legacy, and nothing else
  check_wheel.py metadata            run with the fresh environment's Python, after
                                     installing the wheel: name, version, license
"""

from __future__ import annotations

import glob
import sys
import zipfile
from importlib.metadata import distribution


def contents(dist_dir: str) -> None:
    """Check what the wheel in a directory ships, and print the names of its files.

    Every file must be under mower_sdk/ or in a .dist-info directory, and
    the wheel must hold mower_sdk/__init__.py, mower_sdk/py.typed,
    mower_sdk/legacy/__init__.py and these modules of mower_sdk/legacy/:
    client, cloud, device, errors, event, mqtt_v1, navimow, state_manager,
    thing_models and utils.

    Args:
        dist_dir: The directory the build wrote to; it holds one wheel.

    Raises:
        SystemExit: The wheel ships a file from elsewhere, or lacks a
            required file; the message names the files, and the exit status
            is 1.
        ValueError: The directory does not hold exactly one .whl file.
    """
    (wheel,) = glob.glob(f"{dist_dir}/*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    print("\n".join(names))
    stray = [n for n in names if not (n.startswith("mower_sdk/") or ".dist-info/" in n)]
    if stray:
        sys.exit(f"unexpected files in {wheel}: {stray}")
    required = ["mower_sdk/__init__.py", "mower_sdk/py.typed", "mower_sdk/legacy/__init__.py"] + [
        f"mower_sdk/legacy/{module}.py"
        for module in (
            "client",
            "cloud",
            "device",
            "errors",
            "event",
            "mqtt_v1",
            "navimow",
            "state_manager",
            "thing_models",
            "utils",
        )
    ]
    missing = [n for n in required if n not in names]
    if missing:
        sys.exit(f"{wheel} does not contain {missing}")


def metadata() -> None:
    """Check the metadata of the installed distribution, and print a line of it.

    Run with the Python of the environment the wheel was installed into. The
    distribution navimow-sdk-community must carry that name, the version
    that mower_sdk.__version__ gives and the licence expression GPL-3.0-only,
    and must package a LICENSE file in its dist-info licenses directory that
    is on disk and names the GNU General Public License in its first 200
    characters. The line printed gives the name, the version and the licence
    expression, whether or not the checks hold.

    Raises:
        SystemExit: A check fails; the message lists every failure, and the
            exit status is 1.
        importlib.metadata.PackageNotFoundError: The distribution is not
            installed.
    """
    import mower_sdk

    dist = distribution("navimow-sdk-community")
    problems = []
    if dist.metadata["Name"] != "navimow-sdk-community":
        problems.append(f"Name is {dist.metadata['Name']!r}")
    if dist.version != mower_sdk.__version__:
        problems.append(
            f"version {dist.version!r} != mower_sdk.__version__ {mower_sdk.__version__!r}"
        )
    if dist.metadata["License-Expression"] != "GPL-3.0-only":
        problems.append(f"License-Expression is {dist.metadata['License-Expression']!r}")
    licenses = [
        path
        for path in dist.files or []
        if path.name == "LICENSE"
        and path.parent.name == "licenses"
        and path.parent.parent.name.endswith(".dist-info")
    ]
    if not licenses:
        problems.append("dist-info/licenses/LICENSE is not packaged")
    elif not licenses[0].locate().is_file():
        problems.append(f"{licenses[0]} is recorded but missing")
    elif "GNU GENERAL PUBLIC LICENSE" not in licenses[0].read_text()[:200]:
        problems.append(f"{licenses[0]} is not the GPL text")
    print(f"{dist.metadata['Name']} {dist.version}, {dist.metadata['License-Expression']}")
    if problems:
        sys.exit("wheel metadata: " + "; ".join(problems))


if __name__ == "__main__":
    if sys.argv[1:2] == ["contents"] and len(sys.argv) == 3:
        contents(sys.argv[2])
    elif sys.argv[1:] == ["metadata"]:
        metadata()
    else:
        sys.exit(__doc__)

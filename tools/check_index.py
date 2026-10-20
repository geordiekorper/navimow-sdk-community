"""Check that a package index serves a version's files as they were built.

Run by the testpypi-verify job of .github/workflows/publish.yml:
  check_index.py --index https://test.pypi.org VERSION DIST_DIR

The index's JSON API, <index>/pypi/navimow-sdk-community/<version>/json,
lists the files the index serves for the version, each with its sha256.
Every file in DIST_DIR (the sdist and the wheel the build made) must be
listed under its own name with the digest of the local file, and the index
must list no other file. An index that does not answer for the version yet
(the files have just been uploaded) is asked again: --attempts times in
all, --delay seconds apart.

The exit status is 0 when every file matches, 1 with the problems on
stderr, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = "navimow-sdk-community"


def served_digests(index: str, version: str, attempts: int, delay: float) -> dict[str, str]:
    """Read what the index serves for the version: each file's name and sha256.

    Args:
        index: The index's base URL, such as https://test.pypi.org.
        version: The version to ask for.
        attempts: How many times to ask before giving up. An error while
            connecting, an HTTP error status, a timeout and a failure while
            reading the body are each one failed attempt, printed with its
            number and cause.
        delay: Seconds between two attempts.

    Returns:
        The sha256 of every file the index lists for the version, by the
        file's name.

    Raises:
        SystemExit: The index gave no record in the attempts allowed; the
            exit status is 1.
    """
    url = f"{index}/pypi/{PROJECT}/{version}/json"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                record = json.load(response)
        except (OSError, http.client.HTTPException) as error:
            print(f"attempt {attempt} of {attempts}: {url}: {error}")
            if attempt < attempts:
                time.sleep(delay)
            continue
        return {file["filename"]: file["digests"]["sha256"] for file in record["urls"]}
    sys.exit(f"{index} does not serve {PROJECT} {version} after {attempts} attempts")


def compare(served: dict[str, str], dist: Path) -> list[str]:
    """Compare what the index serves with the files in a directory.

    Each file that matches is printed with its digest.

    Args:
        served: The sha256 of each served file, by name, as served_digests
            gives it.
        dist: The directory holding the built files.

    Returns:
        One line for each problem: a built file the index does not serve or
        serves with another digest, a served file that was not built, or a
        directory with no file in it. Empty when every file matches.
    """
    built = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(dist.iterdir())
        if path.is_file()
    }
    problems = [] if built else [f"{dist} holds no file"]
    for name, digest in built.items():
        if name not in served:
            problems.append(f"{name} is not served")
        elif served[name] != digest:
            problems.append(f"{name} is served with sha256 {served[name]}, built {digest}")
        else:
            print(f"{name} {digest} served as built")
    problems.extend(f"{name} is served but was not built" for name in served if name not in built)
    return problems


def main(argv: list[str] | None = None) -> None:
    """Run the check from the command line.

    Args:
        argv: The arguments; the process's own when None.

    Raises:
        SystemExit: A problem was found (status 1, the problems on stderr),
            the index gave no record (status 1), or the arguments are wrong
            (status 2).
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--index", required=True, help="the index's base URL")
    parser.add_argument("--attempts", type=int, default=10, help="asks before giving up")
    parser.add_argument("--delay", type=float, default=15, help="seconds between asks")
    parser.add_argument("version")
    parser.add_argument("dist", type=Path, help="the directory the build wrote to")
    args = parser.parse_args(argv)
    problems = compare(
        served_digests(args.index, args.version, args.attempts, args.delay), args.dist
    )
    if problems:
        sys.exit("index check: " + "; ".join(problems))


if __name__ == "__main__":
    main()

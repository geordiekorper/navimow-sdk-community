"""check_index: what an index serves for a version, against the built files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import check_index
import pytest

VERSION = "0.2.0a9"
WHEEL = f"navimow_sdk_community-{VERSION}-py3-none-any.whl"
SDIST = f"navimow_sdk_community-{VERSION}.tar.gz"
BUILT = {WHEEL: b"the wheel", SDIST: b"the sdist"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def built(tmp_path: Path, files: dict[str, bytes] = BUILT) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name, data in files.items():
        (dist / name).write_bytes(data)
    return dist


def record_path(tmp_path: Path) -> Path:
    return tmp_path / "index" / "pypi" / check_index.PROJECT / VERSION / "json"


def serve(tmp_path: Path, files: dict[str, str]) -> str:
    """Write the index's record of VERSION under tmp_path; give the index's base URL."""
    record = record_path(tmp_path)
    record.parent.mkdir(parents=True, exist_ok=True)
    urls = [{"filename": name, "digests": {"sha256": digest}} for name, digest in files.items()]
    record.write_text(json.dumps({"urls": urls}), encoding="utf-8")
    return (tmp_path / "index").as_uri()


def check(index: str, dist: Path, attempts: int = 1) -> None:
    args = ["--index", index, "--attempts", str(attempts), "--delay", "0", VERSION, str(dist)]
    check_index.main(args)


def test_the_built_files_served_as_built_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = built(tmp_path)
    index = serve(tmp_path, {name: sha256(data) for name, data in BUILT.items()})
    check(index, dist)
    out = capsys.readouterr().out
    assert f"{WHEEL} {sha256(BUILT[WHEEL])} served as built" in out
    assert f"{SDIST} {sha256(BUILT[SDIST])} served as built" in out


def test_a_file_served_with_another_digest_fails(tmp_path: Path) -> None:
    dist = built(tmp_path)
    index = serve(tmp_path, {WHEEL: sha256(b"other bytes"), SDIST: sha256(BUILT[SDIST])})
    with pytest.raises(SystemExit) as exc:
        check(index, dist)
    assert str(exc.value) == (
        f"index check: {WHEEL} is served with sha256 {sha256(b'other bytes')}, "
        f"built {sha256(BUILT[WHEEL])}"
    )


def test_a_built_file_the_index_does_not_serve_fails(tmp_path: Path) -> None:
    dist = built(tmp_path)
    index = serve(tmp_path, {WHEEL: sha256(BUILT[WHEEL])})
    with pytest.raises(SystemExit, match=f"^index check: {SDIST} is not served$"):
        check(index, dist)


def test_a_served_file_that_was_not_built_fails(tmp_path: Path) -> None:
    dist = built(tmp_path)
    served = {name: sha256(data) for name, data in BUILT.items()}
    served["navimow_sdk_community-0.2.0a9-py3-none-win_amd64.whl"] = sha256(b"x")
    with pytest.raises(SystemExit, match="win_amd64.whl is served but was not built$"):
        check(serve(tmp_path, served), dist)


def test_an_empty_build_directory_fails(tmp_path: Path) -> None:
    dist = built(tmp_path, {})
    with pytest.raises(SystemExit, match="holds no file$"):
        check(serve(tmp_path, {}), dist)


def test_an_index_without_the_version_is_asked_again_then_given_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = built(tmp_path)
    index = (tmp_path / "index").as_uri()
    with pytest.raises(SystemExit) as exc:
        check(index, dist, attempts=3)
    assert (
        str(exc.value) == f"{index} does not serve {check_index.PROJECT} {VERSION} after 3 attempts"
    )
    out = capsys.readouterr().out
    assert [line[:16] for line in out.splitlines()] == [
        "attempt 1 of 3: ",
        "attempt 2 of 3: ",
        "attempt 3 of 3: ",
    ]


class _Stalled:
    """A response whose body cannot be read: the connection timed out after the headers."""

    def __enter__(self) -> _Stalled:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, _size: int = -1) -> bytes:
        raise TimeoutError("timed out")


def test_a_failure_while_reading_the_record_is_one_attempt_and_the_next_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = built(tmp_path)
    index = serve(tmp_path, {name: sha256(data) for name, data in BUILT.items()})
    real_urlopen = check_index.urllib.request.urlopen
    opened: list[str] = []

    def stall_once(url: str, timeout: float) -> object:
        opened.append(url)
        return _Stalled() if len(opened) == 1 else real_urlopen(url, timeout=timeout)

    monkeypatch.setattr(check_index.urllib.request, "urlopen", stall_once)
    check(index, dist, attempts=2)
    assert len(opened) == 2
    assert "attempt 1 of 2: " in capsys.readouterr().out


def test_a_version_the_index_serves_after_the_first_ask_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = built(tmp_path)
    index = (tmp_path / "index").as_uri()
    slept: list[float] = []

    def appear(seconds: float) -> None:
        slept.append(seconds)
        serve(tmp_path, {name: sha256(data) for name, data in BUILT.items()})

    monkeypatch.setattr(check_index.time, "sleep", appear)
    check_index.main(["--index", index, "--attempts", "2", "--delay", "4.5", VERSION, str(dist)])
    assert slept == [4.5]
